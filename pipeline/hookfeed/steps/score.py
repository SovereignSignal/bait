"""Step 5: project_daily rollup from pool_snapshots for ctx.day.

Window math per pool: value(day) - baseline(day-7). baseline = latest snapshot on/before day-7; if none and
the pool was created after day-7 -> 0; if none and the pool predates the window (cold start) -> the earliest
snapshot we have, so a partial window never inflates vol_7d with all-time volume.
"""
from __future__ import annotations

from datetime import date, timedelta

import sqlalchemy as sa

from ..db import as_utc, hooks, pool_snapshots, pools, project_daily, projects, upsert
from ..rules import is_new, is_spam, score_trending
from . import RunContext, log


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    day = ctx.day
    since = day - timedelta(days=14)
    hook_to_project = {(r.chain_id, r.address): r.project_id for r in conn.execute(
        sa.select(hooks.c.chain_id, hooks.c.address, hooks.c.project_id).where(hooks.c.project_id.is_not(None)))}
    pool_rows = conn.execute(sa.select(pools.c.pool_id, pools.c.chain_id, pools.c.hook_address, pools.c.created_at)).all()
    pool_project = {}
    for p in pool_rows:
        pid = hook_to_project.get((p.chain_id, p.hook_address))
        if pid:
            pool_project[(p.pool_id, p.chain_id)] = (pid, as_utc(p.created_at).date() if p.created_at else None)
    snaps = conn.execute(sa.select(pool_snapshots).where(pool_snapshots.c.day <= day, pool_snapshots.c.day >= since)
                         .order_by(pool_snapshots.c.pool_id, pool_snapshots.c.chain_id, pool_snapshots.c.day)).all()
    # oldest snapshot per pool overall (for cold start) may be before `since`; fetch earliest days separately
    earliest = {(r.pool_id, r.chain_id): r for r in conn.execute(
        sa.select(pool_snapshots).where(sa.tuple_(pool_snapshots.c.pool_id, pool_snapshots.c.chain_id, pool_snapshots.c.day).in_(
            sa.select(pool_snapshots.c.pool_id, pool_snapshots.c.chain_id, sa.func.min(pool_snapshots.c.day))
            .group_by(pool_snapshots.c.pool_id, pool_snapshots.c.chain_id))))} if snaps else {}

    by_pool: dict[tuple, list] = {}
    for s in snaps:
        by_pool.setdefault((s.pool_id, s.chain_id), []).append(s)

    agg: dict[int, dict] = {}
    for key, series in by_pool.items():
        if key not in pool_project:
            continue
        pid, created = pool_project[key]
        a = agg.setdefault(pid, {"tvl": 0.0, "vol7": 0.0, "vol7p": 0.0, "tx7": 0, "pools": 0, "max_tvl": 0.0, "stats": []})
        cur = _at(series, day)
        if cur is None:
            continue
        b7 = _baseline(series, day - 7 * _D, created, earliest.get(key))
        b14 = _baseline(series, day - 14 * _D, created, earliest.get(key))
        a["pools"] += 1
        a["tvl"] += cur.tvl_usd or 0.0
        a["max_tvl"] = max(a["max_tvl"], cur.tvl_usd or 0.0)
        a["vol7"] += max((cur.volume_usd_cum or 0.0) - (b7.volume_usd_cum or 0.0), 0.0)
        a["vol7p"] += max((b7.volume_usd_cum or 0.0) - (b14.volume_usd_cum or 0.0), 0.0)
        a["tx7"] += max((cur.tx_count_cum or 0) - (b7.tx_count_cum or 0), 0)
        a["stats"].append((cur.tvl_usd or 0.0, cur.tx_count_cum or 0))

    first_seen = {r.id: as_utc(r.first_seen_at) for r in conn.execute(sa.select(projects.c.id, projects.c.first_seen_at))}
    rows = []
    for pid, fs in first_seen.items():
        a = agg.get(pid, {"tvl": 0.0, "vol7": 0.0, "vol7p": 0.0, "tx7": 0, "pools": 0, "max_tvl": 0.0, "stats": []})
        rows.append({
            "project_id": pid, "day": day, "tvl_usd": a["tvl"], "vol_7d": a["vol7"], "vol_7d_prev": a["vol7p"],
            "swaps_7d": a["tx7"], "pools_n": a["pools"],
            "score_trending": score_trending(a["vol7"], a["vol7p"]),
            "is_new": is_new(fs, day, a["max_tvl"], a["tx7"]),
            "is_spam": is_spam(a["stats"]),
        })
    for i in range(0, len(rows), 500):
        upsert(conn, project_daily, rows[i:i + 500], keys=["project_id", "day"])
    trending = sum(1 for r in rows if r["score_trending"] > 0)
    return {"projects": len(rows), "with_pools": len(agg), "new": sum(r["is_new"] for r in rows),
            "trending": trending, "spam": sum(r["is_spam"] for r in rows)}


_D = timedelta(days=1)


class _Zero:
    tvl_usd = 0.0
    volume_usd_cum = 0.0
    tx_count_cum = 0


def _at(series, day: date):
    """Latest snapshot on/before day."""
    best = None
    for s in series:
        if s.day <= day:
            best = s
    return best


def _baseline(series, cutoff: date, created: date | None, earliest):
    b = _at(series, cutoff)
    if b is not None:
        return b
    if created is not None and created > cutoff:
        return _Zero
    return earliest or _Zero
