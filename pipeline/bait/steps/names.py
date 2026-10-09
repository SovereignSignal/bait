"""Name unlisted hooks from verified source (runs at the end of the group step; free APIs only).

1. Lookup: hooks with no hooklist name, in an "Unlisted hook" project, not checked in the last RECHECK_DAYS, busiest
   first, at most NAMES_MAX per run. Stores hooks.contract_name (raw, even when generic) + hooks.name_checked_at.
2. Rename: an "Unlisted hook" project whose hooks carry a useful contract name takes that name. Projects are keyed
   per chain ("contract:<chain>:<norm>") so the same contract name on one chain becomes one project (a launchpad's
   per-token hooks), while two teams that both wrote "LimitOrderHook" on different chains stay apart. A project
   emptied by a merge is deleted with its rows. Curated (hooklist) projects are never merged into: a name clash gets
   the chain appended.
"""
from __future__ import annotations

import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import sqlalchemy as sa

from ..clients.contract_names import useful_name
from ..config import CHAIN_BY_ID
from ..indexer.chains import CHAIN_BY_ID as INDEXED_BY_ID
from ..db import hooks, project_daily, projects, receipts, signals
from ..rules import normalize_name, slugify
from . import RunContext, log

UNLISTED = "Unlisted hook%"
RECHECK_DAYS = 14


def names_max() -> int:
    try:
        return max(0, int(os.environ.get("NAMES_MAX", "") or 400))
    except ValueError:
        return 400


def run(conn: sa.Connection, ctx: RunContext, out: dict) -> None:
    out.setdefault("names_checked", 0)
    out.setdefault("names_found", 0)
    out.setdefault("projects_renamed", 0)
    out.setdefault("projects_merged", 0)
    client = ctx.client("names")
    if client is not None:
        _lookup(conn, ctx, client, out)
    _rename(conn, out)


def _lookup(conn, ctx: RunContext, client, out: dict) -> None:
    cap = names_max()
    if cap == 0:
        return
    latest = conn.execute(sa.select(sa.func.max(project_daily.c.day))).scalar_one()
    vol = sa.select(project_daily.c.project_id, project_daily.c.vol_7d).where(project_daily.c.day == latest).subquery()
    stale = ctx.now - timedelta(days=RECHECK_DAYS)
    q = (sa.select(hooks.c.chain_id, hooks.c.address)
         .join(projects, projects.c.id == hooks.c.project_id)
         .outerjoin(vol, vol.c.project_id == hooks.c.project_id)
         .where(projects.c.name.like(UNLISTED), sa.or_(hooks.c.name.is_(None), hooks.c.name == ""),
                hooks.c.contract_name.is_(None),
                sa.or_(hooks.c.name_checked_at.is_(None), hooks.c.name_checked_at < stale))
         .order_by(sa.func.coalesce(vol.c.vol_7d, 0).desc(), hooks.c.chain_id, hooks.c.address)
         .limit(cap))
    todo = conn.execute(q).all()
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=6) as ex:     # network only; all writes stay on this thread
        found = list(ex.map(lambda h: client.lookup(h.chain_id, h.address), todo))
    for h, res in zip(todo, found):
        vals = {"name_checked_at": ctx.now}
        if res:
            vals.update(contract_name=res[0], verified_source=True)
            out["names_found"] += 1
        conn.execute(sa.update(hooks).values(**vals).where(hooks.c.address == h.address, hooks.c.chain_id == h.chain_id))
    out["names_checked"] += len(todo)
    log.info("names: checked %d unlisted hooks, %d have verified source names", len(todo), out["names_found"])


def _rename(conn, out: dict) -> None:
    unlisted = conn.execute(sa.select(projects.c.id).where(projects.c.name.like(UNLISTED))).scalars().all()
    if not unlisted:
        return
    rows = conn.execute(sa.select(hooks.c.project_id, hooks.c.chain_id, hooks.c.contract_name)
                        .where(hooks.c.project_id.in_(unlisted), hooks.c.contract_name.is_not(None))).all()
    by_project: dict[int, list] = {}
    for r in rows:
        if useful_name(r.contract_name):
            by_project.setdefault(r.project_id, []).append(r)
    if not by_project:
        return
    by_key = {r.norm_name: r.id for r in conn.execute(
        sa.select(projects.c.norm_name, projects.c.id).where(projects.c.norm_name.like("contract:%")))}
    taken_names = {n.lower() for n in conn.execute(sa.select(projects.c.name)).scalars()}
    curated_norms = {r for r in conn.execute(sa.select(projects.c.norm_name).where(
        projects.c.norm_name.is_not(None), sa.not_(projects.c.norm_name.like("contract:%")),
        sa.not_(projects.c.norm_name.like("code:%")))).scalars()}

    for pid in sorted(by_project):
        hs = by_project[pid]
        (cname, chain_id), _ = Counter((h.contract_name.strip(), h.chain_id) for h in hs).most_common(1)[0]
        norm = normalize_name(cname) or cname.lower()
        key = f"contract:{chain_id}:{norm}"
        target = by_key.get(key)
        if target is not None and target != pid:
            conn.execute(sa.update(hooks).values(project_id=target).where(hooks.c.project_id == pid))
            _delete_project(conn, pid)
            out["projects_merged"] += 1
            continue
        display = cname
        if display.lower() in taken_names or norm in curated_norms:
            display = f"{cname} ({chain_label(chain_id)})"
        conn.execute(sa.update(projects).values(name=display, norm_name=key, slug=_unique_slug(conn, display, pid))
                     .where(projects.c.id == pid))
        by_key[key] = pid
        taken_names.add(display.lower())
        out["projects_renamed"] += 1
    if out["projects_renamed"] or out["projects_merged"]:
        log.info("names: renamed %d unlisted projects, merged %d into a same-named project",
                 out["projects_renamed"], out["projects_merged"])


LABELS = {"bnb": "BNB", "megaeth": "MegaETH", "zora": "Zora", "wc": "World Chain", "worldchain": "World Chain"}


def chain_label(chain_id: int) -> str:
    ch = CHAIN_BY_ID.get(chain_id) or INDEXED_BY_ID.get(chain_id)
    if not ch:
        return str(chain_id)
    return LABELS.get(ch.slug, ch.slug.replace("-", " ").title())


def _unique_slug(conn, name: str, pid: int) -> str:
    base = slugify(name)
    taken = {r for r in conn.execute(sa.select(projects.c.slug).where(projects.c.slug.like(base + "%"), projects.c.id != pid)).scalars()}
    slug, i = base, 2
    while slug in taken:
        slug, i = f"{base}-{i}", i + 1
    return slug


def _delete_project(conn, pid: int) -> None:
    for t in (project_daily, signals, receipts):
        conn.execute(sa.delete(t).where(t.c.project_id == pid))
    conn.execute(sa.delete(projects).where(projects.c.id == pid))
