"""Package 9: one-off data repair. `python -m bait repair <command>`; see pipeline/README.md "Repair".

backfill-days  GeckoTerminal daily candles (free, paced) -> pool_snapshots.volume_usd_day for closed days, where a row has
               no per-day value (written before 0012) or only an estimate. One call per pool, busiest first; resumable.
rescore        score.run for each stored run day in the range, oldest first, after a one-time backup of that day's rows.
prose          Claude only, paid: what_line for projects whose Parallel facts are stored but whose line was never written,
               or whose line the old code cut mid-word at 200 chars; why_blurb for the signals targets whose blurb predates
               the package 8 window text. Every call passes the spend Meter; --max-usd caps this run on top of it.
verify         Free: Bait's 24h / 7d / prev-7d against GeckoTerminal candle sums for a few projects, as a markdown table.
all            The plan (--dry-run), or backfill-days -> rescore -> verify, then prose only within --prose-budget-usd.

Every command that changes or checks data takes the run lock (exit 3 while the nightly runs) and writes one run_receipts
row per sub-step (day = today, notes = "repair <name>"). Dry runs read only and write nothing.
"""
from __future__ import annotations

import argparse
import math
import os
import random
from datetime import date, datetime, time as dtime, timedelta, timezone

import sqlalchemy as sa

from . import db
from . import keyless as K
from .clients.claude import prose_estimate, what_line_prompt, why_blurb_prompt
from .costs import BudgetExceeded, Meter
from .db import (api_spend, as_utc, hooks, pool_daily_onchain, pool_snapshots, pool_stats, pools, project_daily, projects,
                 run_receipts, upsert)
from .steps import RunContext, enrich, log, score, signals
from .steps.snapshot import keyless_http

PRIOR_DAYS = 14            # a run day's windows reach 14 closed days back (7d and prev 7d)
OHLCV_LIMIT = 30           # daily candles per GeckoTerminal call, one call per pool
MAX_SINCE = OHLCV_LIMIT - PRIOR_DAYS - 1   # 15: the oldest run day whose windows 30 candles still cover
# Blurbs written before package 8 merged used the old window text ("swaps in 7 days" that were 24h).
P8_MERGED_AT = datetime(2026, 10, 9, 15, 29, 25, tzinfo=timezone.utc)
NET_BY_CHAIN = {cid: net for net, (cid, *_rest) in K.CHAINS.items()}
SLUG_BY_CHAIN = {cid: slug for _net, (cid, slug, *_rest) in K.CHAINS.items()}
EXIT_PRECONDITION = 5

_D = timedelta(days=1)
_COLS = [c.name for c in project_daily.c]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------------------------------------
# backfill-days
# ------------------------------------------------------------------------------------------------
def backfill_window(today: date, since: int) -> tuple[date, date]:
    """Closed days a re-score of the run days today-since .. today reads: (today - since - 14) .. (today - 1).
    `today` itself is the running day and is never written."""
    return today - timedelta(days=since + PRIOR_DAYS), today - _D


def replaceable(r) -> bool:
    """A row a candle may fill: written before 0012 with a known, non-zero cumulative (a zero cumulative is a seed: nothing
    traded up to that day, so the day adds $0 either way; a NULL cumulative is volume the onchain step marked unknown), or
    an estimate on a closed day (GeckoTerminal's rolling 24h, or a running-day candle that was never replaced)."""
    return r.estimated is True or (r.volume_usd_day is None and r.volume_usd_cum is not None and r.volume_usd_cum > 0)


def backfill_targets(conn, today: date, since: int) -> tuple[list[tuple[str, int, float]], dict]:
    """Pools of scored projects with a replaceable row in the window, as (pool_id, chain_id, 7-day volume), busiest first
    (7-day volume = the last 7 nightly pool_stats.vol_24h). Pools on chains GeckoTerminal does not list are counted, not
    returned."""
    start, end = backfill_window(today, since)
    c = pool_snapshots.c
    cond = sa.or_(c.estimated.is_(True), sa.and_(c.volume_usd_day.is_(None), c.volume_usd_cum.is_not(None), c.volume_usd_cum > 0))
    need: dict[tuple[str, int], int] = {}
    for r in conn.execute(sa.select(c.pool_id, c.chain_id, sa.func.count().label("n"))
                          .where(c.day >= start, c.day <= end, cond).group_by(c.pool_id, c.chain_id)):
        need[(r.pool_id, r.chain_id)] = r.n
    scored = {(r.pool_id, r.chain_id) for r in conn.execute(
        sa.select(pools.c.pool_id, pools.c.chain_id)
        .select_from(pools.join(hooks, (hooks.c.address == pools.c.hook_address) & (hooks.c.chain_id == pools.c.chain_id)))
        .where(hooks.c.project_id.is_not(None)))}
    vol7 = {(r.pool_id, r.chain_id): r.v or 0.0 for r in conn.execute(
        sa.select(pool_stats.c.pool_id, pool_stats.c.chain_id, sa.func.sum(pool_stats.c.vol_24h).label("v"))
        .where(pool_stats.c.day > today - 7 * _D, pool_stats.c.day <= today)
        .group_by(pool_stats.c.pool_id, pool_stats.c.chain_id))}
    info = {"rows": 0, "no_network": {}, "by_chain": {}}
    out = []
    for key, n in need.items():
        if key not in scored:
            continue
        cid = key[1]
        if cid not in NET_BY_CHAIN:
            info["no_network"][str(cid)] = info["no_network"].get(str(cid), 0) + 1
            continue
        info["rows"] += n
        slug = SLUG_BY_CHAIN[cid]
        info["by_chain"][slug] = info["by_chain"].get(slug, 0) + 1
        out.append((key[0], cid, vol7.get(key, 0.0)))
    out.sort(key=lambda t: (-t[2], t[1], t[0]))
    return out, info


def fill_pool(conn, pool_id: str, chain_id: int, candles: list[tuple[int, float]], start: date, end: date) -> tuple[int, int]:
    """Write one pool's candles to its replaceable rows in start..end (volume_usd_day, estimated False); a day without a
    candle had no trades ($0). A day with volume and no row gets one, carrying TVL and the legacy cumulatives from the
    nearest earlier row (TVL from the nearest later one when there is none), so it never changes a pool's known/unknown
    state. tx_count_day is left as it is: GeckoTerminal candles carry no trade counts. Returns (updated, inserted)."""
    vols: dict[date, float] = {}
    for ts, v in candles:
        vols[datetime.fromtimestamp(ts, tz=timezone.utc).date()] = v
    complete = len(candles) < OHLCV_LIMIT       # fewer than asked: the response is the pool's whole trading history
    first = min(vols)
    c = pool_snapshots.c
    rows = {r.day: r for r in conn.execute(sa.select(pool_snapshots).where(c.pool_id == pool_id, c.chain_id == chain_id))}
    updates, inserts = [], []
    d = start
    while d <= end:
        if complete or d >= first:
            v = vols.get(d, 0.0)
            r = rows.get(d)
            if r is not None:
                if replaceable(r):
                    updates.append({"p": pool_id, "c": chain_id, "d": d, "v": v})
            elif v > 0:
                earlier = [x for x in rows if x < d]
                later = [x for x in rows if x > d]
                base = rows[max(earlier)] if earlier else None
                tvl = base.tvl_usd if base is not None else (rows[min(later)].tvl_usd if later else None)
                inserts.append({"pool_id": pool_id, "chain_id": chain_id, "day": d, "tvl_usd": tvl,
                                "volume_usd_cum": base.volume_usd_cum if base is not None else 0.0,
                                "tx_count_cum": base.tx_count_cum if base is not None else None,
                                "volume_usd_day": v, "tx_count_day": None, "estimated": False})
        d += _D
    if updates:
        conn.execute(sa.update(pool_snapshots)
                     .where(c.pool_id == sa.bindparam("p"), c.chain_id == sa.bindparam("c"), c.day == sa.bindparam("d"))
                     .values(volume_usd_day=sa.bindparam("v"), estimated=False), updates)
    if inserts:
        upsert(conn, pool_snapshots, inserts, keys=["pool_id", "chain_id", "day"], update=[])
    return len(updates), len(inserts)


def backfill(engine, ctx: RunContext, since: int, max_pools: int | None = None, deadline: datetime | None = None,
             clock=_utcnow) -> dict:
    """Pull candles for every target pool (or the busiest max_pools), one committed transaction per pool, so a stop at any
    point keeps what is done and a rerun skips it. Stops between pools at `deadline`."""
    today = ctx.day
    start, end = backfill_window(today, since)
    with engine.connect() as conn:
        targets, info = backfill_targets(conn, today, since)
    take = targets if max_pools is None else targets[:max_pools]
    out = {"window": f"{start}..{end}", "pools_needing": len(targets), "no_network": sum(info["no_network"].values()),
           "done": 0, "failed": 0, "no_candles": 0, "beyond_cap": len(targets) - len(take), "left_at_deadline": 0,
           "rows_updated": 0, "rows_inserted": 0, "calls": 0}
    http = keyless_http(ctx)
    log.info("repair backfill-days: %d pools need candles for %s..%s; pulling %d", len(targets), start, end, len(take))
    for i, (pid, cid, _v) in enumerate(take):
        if deadline is not None and clock() >= deadline:
            out["left_at_deadline"] = len(take) - i
            log.warning("repair backfill-days: stop time %s reached, %d pools left; rerun to resume", deadline, len(take) - i)
            break
        candles = K.fetch_daily(http, NET_BY_CHAIN[cid], pid, limit=OHLCV_LIMIT)
        out["calls"] += 1
        if candles is None:
            out["failed"] += 1
            continue
        if not candles:
            out["no_candles"] += 1          # GeckoTerminal has no volume for it: leave the rows alone
            continue
        with engine.begin() as conn:
            u, n = fill_pool(conn, pid, cid, candles, start, end)
        out["done"] += 1
        out["rows_updated"] += u
        out["rows_inserted"] += n
        if (i + 1) % 50 == 0:
            log.info("repair backfill-days: %d/%d pools, %d rows updated, %d inserted, %d failed",
                     i + 1, len(take), out["rows_updated"], out["rows_inserted"], out["failed"])
    return out


def backfill_plan(conn, today: date, since: int, max_pools: int | None, spacing: float) -> tuple[list[str], dict]:
    start, end = backfill_window(today, since)
    targets, info = backfill_targets(conn, today, since)
    calls = len(targets) if max_pools is None else min(max_pools, len(targets))
    minutes = calls * spacing / 60
    chains = ", ".join(f"{k} {v}" for k, v in sorted(info["by_chain"].items(), key=lambda kv: -kv[1])) or "none"
    lines = [f"backfill-days: closed days {start}..{end} (the running day {today} is never written)",
             f"  pools needing candles: {len(targets)} ({info['rows']} rows); by chain: {chains}"]
    if info["no_network"]:
        lines.append(f"  skipped, chain not on GeckoTerminal: {sum(info['no_network'].values())} pools "
                     f"(chain ids {', '.join(sorted(info['no_network']))})")
    left = len(targets) - calls
    lines.append(f"  GeckoTerminal calls: {calls}{f' (--pools {max_pools}; {left} left for a rerun)' if left else ''}, "
                 f"{spacing:g} s apart: about {minutes:.0f} min, free")
    if not p8_rows_present(conn, today):
        lines.append("  WARNING: no per-day snapshot rows from the last day. Run this after a package 8 nightly; before "
                     "it, every on-chain pool still looks unfilled and the call count above is inflated.")
    return lines, {"calls": calls, "minutes": minutes}


def p8_rows_present(conn, today: date) -> bool:
    """A package 8 nightly ran within the last day: it writes estimated on every row it touches."""
    return conn.execute(sa.select(sa.func.count()).select_from(pool_snapshots).where(
        pool_snapshots.c.estimated.is_not(None), pool_snapshots.c.day >= today - _D)).scalar_one() > 0
