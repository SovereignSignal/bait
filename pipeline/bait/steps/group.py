"""Step 4 (group): hooks -> projects. Rule 1 normalized name, rule 2 code_hash, rule 3 Claude for the long tail.
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
        # Unassigned hooks that share bytecode with each other (one launchpad, many clones) become one project.
        fam: dict[str, list] = {}
        for h in still:
            if h.code_hash:
                fam.setdefault(h.code_hash, []).append(h)
        still = [h for h in still if not h.code_hash or len(fam[h.code_hash]) == 1]
        for members in fam.values():
            if len(members) < 2:
                continue
            first = sorted(m.address for m in members)[0]
            pid = _create_project(conn, ctx, f"Unlisted hook family {first[:6]}…", None, None)
            for m in members:
                _assign(conn, m, pid)
            out["created"] += 1
            out["by_code"] += len(members)
        unassigned = still

    # Rule 3: Claude classification for the long tail (nameless hooks or names that matched nothing).
    if unassigned:
        claude = ctx.client("claude")
        if claude is None:
            # No key: every unnamed hook becomes its own project keyed by address so the feed still renders.
            for h in unassigned:
                pid = _create_project(conn, ctx, h.name or f"Unlisted hook {h.address[:6]}…{h.address[-4:]}", None, h.description, category=None)
                _assign(conn, h, pid)
                out["created"] += 1
            unassigned = []
        else:
            from ..costs import BudgetExceeded
            names = {r.id: r.name for r in conn.execute(sa.select(projects.c.id, projects.c.name))}
            # A hook with no name and no description is only an address: Claude would be guessing, so it is never
            # paid for. Those get an address-named project (bytecode families were already merged above for free).
            bare = [h for h in unassigned if not (h.name or "").strip() and not (h.description or "").strip()]
            for h in bare:
                pid = _create_project(conn, ctx, f"Unlisted hook {h.address[:6]}…{h.address[-4:]}", None, None, category=None)
                _assign(conn, h, pid)
                out["created"] += 1
                out["unlisted_named"] = out.get("unlisted_named", 0) + 1
            unassigned = [h for h in unassigned if h not in bare]
            cap = claude_cap()
            if len(unassigned) > cap:
                log.info("group: %d hooks need Claude, classifying %d this run (GROUP_CLAUDE_MAX); the rest wait", len(unassigned), cap)
            for h in unassigned[:cap]:
                try:
                    res = claude.classify_hook(h.name or "", h.description or "", h.address,
                                               shortlist(h.name or "", h.description or "", list(set(names.values()))))
                except BudgetExceeded:
                    out["budget_stop"] = 1
                    break
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
    if category is None:
        from ..keyless import categorize
        category = categorize(f"{description or ''} {name}", {}, {})
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


# --- Claude classification cost controls -------------------------------------------------------------
SHORTLIST_N = 25            # candidate project names sent per call (was every name, up to 400)
PROMPT_CHARS_MAX = 2500     # worst-case prompt size with the shortlist, used by the cost preview


def claude_cap() -> int:
    """Max Claude classifications per run (GROUP_CLAUDE_MAX, default 100); the rest wait for later runs."""
    import os
    try:
        return max(0, int(os.environ.get("GROUP_CLAUDE_MAX", "") or 100))
    except ValueError:
        return 100


def shortlist(name: str, description: str, known: list[str], n: int = SHORTLIST_N) -> list[str]:
    """The n known project names closest to this hook's name/description; empty when nothing is close.
    Unnamed hooks with no description have nothing to match on, so they get an empty list (Claude names them)."""
    import difflib
    probe = f"{name} {description}".strip().lower()
    if not probe or not known:
        return []
    scored = []
    for k in known:
        kl = k.lower()
        r = difflib.SequenceMatcher(None, probe[:120], kl).ratio()
        if kl and (kl in probe or probe.split()[0] in kl):
            r += 1.0
        scored.append((r, k))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [k for r, k in scored[:n] if r >= 0.35]


def claude_candidates_count(conn) -> int:
    """Upper bound on hooks that would reach Claude now: unassigned, unnamed, but with a description to work from.
    (Named hooks are grouped by name for free; hooks with neither name nor description are never sent.)"""
    return conn.execute(sa.select(sa.func.count()).select_from(hooks).where(
        hooks.c.project_id.is_(None), sa.or_(hooks.c.name.is_(None), hooks.c.name == ""),
        hooks.c.description.is_not(None), hooks.c.description != "")).scalar_one()
