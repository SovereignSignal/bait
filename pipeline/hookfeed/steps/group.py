"""Step 3: hooks -> projects. Rule 1 normalized name, rule 2 code_hash, rule 3 Claude for the long tail.
is_factory when a project has > 20 hooks."""
from __future__ import annotations

import sqlalchemy as sa

from ..db import hooks, projects, utcnow
from ..rules import normalize_name, slugify
from . import RunContext, log

FACTORY_MIN_HOOKS = 21


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"by_name": 0, "by_code": 0, "by_claude": 0, "created": 0, "unassigned": 0, "code_fetched": 0, "factories": 0}
    unassigned = _unassigned(conn)

    # Rule 1: normalized name -> existing project, or new project per distinct name.
    by_norm = _projects_by_norm(conn)
    for h in unassigned:
        norm = normalize_name(h.name)
        if not norm:
            continue
        pid = by_norm.get(norm)
        if pid is None:
            pid = _create_project(conn, ctx, h.name, norm, h.description)
            by_norm[norm] = pid
            out["created"] += 1
        _assign(conn, h, pid)
        out["by_name"] += 1

    # Rule 2: code_hash. Fetch missing hashes (capped), then join unassigned hooks to a project sharing the hash.
    unassigned = _unassigned(conn)
    if unassigned:
        out["code_fetched"] = _fetch_code_hashes(conn, ctx, unassigned)
        unassigned = _unassigned(conn)
        hash_to_pid = dict(conn.execute(
            sa.select(hooks.c.code_hash, sa.func.min(hooks.c.project_id))
            .where(hooks.c.project_id.is_not(None), hooks.c.code_hash.is_not(None)).group_by(hooks.c.code_hash)).all())
        still = []
        for h in unassigned:
            pid = hash_to_pid.get(h.code_hash) if h.code_hash else None
            if pid:
                _assign(conn, h, pid)
                out["by_code"] += 1
            else:
                still.append(h)
        unassigned = still

    # Rule 3: Claude classification for the long tail (nameless hooks or names that matched nothing).
    if unassigned:
        claude = ctx.client("claude")
        if claude is None:
            # No key: every unnamed hook becomes its own project keyed by address so the feed still renders.
            for h in unassigned:
                pid = _create_project(conn, ctx, h.name or f"hook {h.address[:10]}", None, h.description, category=None)
                _assign(conn, h, pid)
                out["created"] += 1
            unassigned = []
        else:
            names = {r.id: r.name for r in conn.execute(sa.select(projects.c.id, projects.c.name))}
            for h in unassigned:
                try:
                    res = claude.classify_hook(h.name or "", h.description or "", h.address, sorted(set(names.values())))
                except Exception as e:
                    log.warning("group claude classify failed for %s: %s", h.address, e)
                    continue
                pname = (res.get("project") or h.name or f"hook {h.address[:10]}").strip()
                match = next((pid for pid, n in names.items() if n.lower() == pname.lower()), None)
                if match is None:
                    match = _create_project(conn, ctx, pname, normalize_name(pname) or None, h.description, category=res.get("category"))
                    names[match] = pname
                    out["created"] += 1
                _assign(conn, h, match)
                out["by_claude"] += 1
            unassigned = _unassigned(conn)
    out["unassigned"] = len(unassigned)

    # Factories: > 20 hooks.
    counts = conn.execute(sa.select(hooks.c.project_id, sa.func.count()).where(hooks.c.project_id.is_not(None)).group_by(hooks.c.project_id)).all()
    factory_ids = [pid for pid, n in counts if n >= FACTORY_MIN_HOOKS]
    conn.execute(sa.update(projects).values(is_factory=False).where(projects.c.is_factory.is_(True)))
    if factory_ids:
        conn.execute(sa.update(projects).values(is_factory=True).where(projects.c.id.in_(factory_ids)))
    out["factories"] = len(factory_ids)
    return out


def _unassigned(conn):
    return conn.execute(sa.select(hooks).where(hooks.c.project_id.is_(None)).order_by(hooks.c.chain_id, hooks.c.address)).all()


def _projects_by_norm(conn) -> dict[str, int]:
    return {r.norm_name: r.id for r in conn.execute(sa.select(projects.c.norm_name, projects.c.id).where(projects.c.norm_name.is_not(None)))}


def _assign(conn, h, pid: int) -> None:
    conn.execute(sa.update(hooks).values(project_id=pid).where(hooks.c.address == h.address, hooks.c.chain_id == h.chain_id))


def _create_project(conn, ctx, name: str, norm: str | None, description: str | None, category: str | None = None) -> int:
    base = slugify(name)
    slug, i = base, 2
    taken = {r[0] for r in conn.execute(sa.select(projects.c.slug).where(projects.c.slug.like(base + "%")))}
    while slug in taken:
        slug, i = f"{base}-{i}", i + 1
    what = (description or "").strip().split(". ")[0][:160] or None  # placeholder until step 4 runs Claude
    r = conn.execute(sa.insert(projects).values(
        slug=slug, name=name, norm_name=norm, category=category, what_line=what, first_seen_at=ctx.now))
    return r.inserted_primary_key[0]


def _fetch_code_hashes(conn, ctx, unassigned) -> int:
    """Fill code_hash for hooks missing one, unassigned hooks first, then assigned ones (both sides of a
    code_hash join need a hash). Capped by ctx.max_code_fetch per run; the rest catch up on later nights."""
    bs = ctx.client("blockscout")
    if bs is None:
        return 0
    pending = [h for h in unassigned if not h.code_hash]
    pending += conn.execute(sa.select(hooks).where(hooks.c.project_id.is_not(None), hooks.c.code_hash.is_(None))
                            .order_by(hooks.c.first_seen_at.desc())).all()
    n = 0
    for h in pending[: ctx.max_code_fetch]:
        info = bs.fetch_code(h.chain_id, h.address)
        n += 1
        if info and info.code_hash:
            vals = {"code_hash": info.code_hash}
            if info.verified:
                vals["verified_source"] = True
            conn.execute(sa.update(hooks).values(**vals).where(hooks.c.address == h.address, hooks.c.chain_id == h.chain_id))
    return n
