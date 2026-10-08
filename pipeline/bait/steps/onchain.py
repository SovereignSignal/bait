"""Step "onchain" (right after activity): bring the on-chain indexer's tables into the ones the web app and score read.

Reads v4_pools, pool_daily_onchain, indexer_state (written by `python -m bait.indexer`). No-op when they are empty.
1. Hooks: every v4_pools hook missing from `hooks` becomes an unlisted hook row (name None, like snapshot keyless), busiest
   first, at most ONCHAIN_MAX_NEW_HOOKS (500) per night so the group step's Claude long tail stays bounded; code_hash from
   keyless.code_hashes where a public RPC exists for the chain (KEYLESS != 0), else None.
2. Pools: every v4_pools row whose hook is in `hooks` is upserted into `pools` (any chain; the web filters unknown chains).
   token0/token1 are only set on insert, so GeckoTerminal/subgraph symbols survive; created_at = Initialize block time.
3. Usage (chains whose indexer_state is backfilled and updated within ONCHAIN_MAX_STALE_HOURS): for each pool with
   pool_daily_onchain rows in day-14..day, pool_snapshots becomes a cumulative volume / swap series from the on-chain daily
   numbers (zero-seeded the day before, like snapshot keyless), replacing the pool's earlier snapshots so one scale is
   used; TVL keeps the latest GeckoTerminal/subgraph reserve (the indexer does not compute TVL). pool_stats(day) gets
   vol_24h / traders_24h from the last complete UTC day. On-chain is exact, so it overrides keyless values for those pools.
   While a chain's on-chain history is shorter than the 14-day window, pools keyless already snapshots keep their
   keyless series (only pool_stats is overridden).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from ..db import (as_utc, hooks, indexer_state, pool_daily_onchain, pool_snapshots, pool_stats, pools, upsert,
                  v4_pools)
from . import RunContext, log

WINDOW_DAYS = 14
CHUNK = 500


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"v4_pools": 0, "hooks_added": 0, "hooks_deferred": 0, "code_hashes": 0, "pools": 0,
           "pools_usage": 0, "series_replaced": 0, "stats": 0, "chains_usage": 0, "chains_stale": 0, "errors": 0}
    where = [v4_pools.c.chain_id.in_(ctx.chains)] if ctx.chains else []
    out["v4_pools"] = conn.execute(sa.select(sa.func.count()).select_from(v4_pools).where(*where)).scalar() or 0
    if not out["v4_pools"]:
        log.info("onchain: indexer tables empty, nothing to do")
        return out
    known = _add_hooks(conn, ctx, where, out)
    _upsert_pools(conn, ctx, known, out)
    _usage(conn, ctx, out)
    log.info("onchain v4_pools=%d hooks_added=%d deferred=%d pools=%d usage_pools=%d series=%d stats=%d",
             out["v4_pools"], out["hooks_added"], out["hooks_deferred"], out["pools"], out["pools_usage"],
             out["series_replaced"], out["stats"])
    return out


def _add_hooks(conn, ctx: RunContext, where: list, out: dict) -> set[tuple[str, int]]:
    known = {(r.address, r.chain_id) for r in conn.execute(sa.select(hooks.c.address, hooks.c.chain_id))}
    since = ctx.day - timedelta(days=WINDOW_DAYS)
    swaps = (sa.select(pool_daily_onchain.c.chain_id, pool_daily_onchain.c.pool_id, sa.func.sum(pool_daily_onchain.c.swaps).label("n"))
             .where(pool_daily_onchain.c.day >= since).group_by(pool_daily_onchain.c.chain_id, pool_daily_onchain.c.pool_id).subquery())
    rows = conn.execute(
        sa.select(v4_pools.c.chain_id, v4_pools.c.hook_address,
                  sa.func.coalesce(sa.func.sum(swaps.c.n), 0).label("swaps"), sa.func.max(v4_pools.c.block_number).label("newest"))
        .select_from(v4_pools.outerjoin(swaps, (swaps.c.chain_id == v4_pools.c.chain_id) & (swaps.c.pool_id == v4_pools.c.pool_id)))
        .where(*where).group_by(v4_pools.c.chain_id, v4_pools.c.hook_address)).all()
    missing = [r for r in rows if (r.hook_address, r.chain_id) not in known]
    if not missing:
        return known
    missing.sort(key=lambda r: (-(r.swaps or 0), -(r.newest or 0)))
    cap = _env_int("ONCHAIN_MAX_NEW_HOOKS", 500)
    take, out["hooks_deferred"] = missing[:cap], max(len(missing) - cap, 0)
    codes = _code_hashes(ctx, take, out)
    new = [{"address": r.hook_address, "chain_id": r.chain_id, "name": None, "description": "", "flags": {}, "properties": {},
            "verified_source": None, "audit_url": None, "deployer": None, "first_seen_at": ctx.now,
            "code_hash": codes.get((r.chain_id, r.hook_address))} for r in take]
    for i in range(0, len(new), CHUNK):
        upsert(conn, hooks, new[i:i + CHUNK], keys=["address", "chain_id"], update=[])
    out["hooks_added"] = len(new)
    if out["hooks_deferred"]:
        log.info("onchain: %d more unlisted hooks deferred to later nights (ONCHAIN_MAX_NEW_HOOKS=%d)", out["hooks_deferred"], cap)
    known.update((r.hook_address, r.chain_id) for r in take)
    return known


def _code_hashes(ctx: RunContext, rows, out: dict) -> dict[tuple[int, str], str]:
    if os.environ.get("KEYLESS", "1") == "0":
        return {}
    from .. import keyless as K
    from .snapshot import keyless_http
    net_by_chain = {cid: net for net, (cid, *_rest) in K.CHAINS.items()}
    by_net: dict[str, set[str]] = {}
    for r in rows:
        net = net_by_chain.get(r.chain_id)
        if net:
            by_net.setdefault(net, set()).add(r.hook_address)
    if not by_net:
        return {}
    try:
        got = K.code_hashes(keyless_http(ctx), by_net)
    except Exception as e:
        out["errors"] += 1
        log.warning("onchain code_hash lookup failed: %s", e)
        return {}
    res = {(K.CHAINS[net][0], addr): h for (net, addr), h in got.items()}
    out["code_hashes"] = len(res)
    return res


def _upsert_pools(conn, ctx: RunContext, known: set[tuple[str, int]], out: dict) -> None:
    ins = postgresql.insert if conn.dialect.name == "postgresql" else sqlite.insert
    last = (-1, "")
    while True:
        q = (sa.select(v4_pools).where(sa.tuple_(v4_pools.c.chain_id, v4_pools.c.pool_id) > sa.tuple_(sa.literal(last[0]), sa.literal(last[1])))
             .order_by(v4_pools.c.chain_id, v4_pools.c.pool_id).limit(2000))
        if ctx.chains:
            q = q.where(v4_pools.c.chain_id.in_(ctx.chains))
        batch = conn.execute(q).all()
        if not batch:
            break
        last = (batch[-1].chain_id, batch[-1].pool_id)
        rows = [{"pool_id": r.pool_id, "chain_id": r.chain_id, "hook_address": r.hook_address,
                 "token0": {"id": r.currency0, "symbol": None}, "token1": {"id": r.currency1, "symbol": None},
                 "fee": r.fee, "tick_spacing": r.tick_spacing, "created_at": r.block_time}
                for r in batch if (r.hook_address, r.chain_id) in known]
        for i in range(0, len(rows), CHUNK):
            stmt = ins(pools).values(rows[i:i + CHUNK])
            stmt = stmt.on_conflict_do_update(index_elements=["pool_id", "chain_id"], set_={
                "hook_address": stmt.excluded.hook_address, "fee": stmt.excluded.fee, "tick_spacing": stmt.excluded.tick_spacing,
                "created_at": sa.func.coalesce(stmt.excluded.created_at, pools.c.created_at)})
            conn.execute(stmt)
        out["pools"] += len(rows)


def _usage(conn, ctx: RunContext, out: dict) -> None:
    stale_h = _env_int("ONCHAIN_MAX_STALE_HOURS", 6)
    ready = []
    for st in conn.execute(sa.select(indexer_state)):
        if ctx.chains and st.chain_id not in ctx.chains:
            continue
        fresh = st.updated_at is not None and as_utc(st.updated_at) >= ctx.now - timedelta(hours=stale_h)
        if st.backfilled and fresh:
            ready.append(st.chain_id)
        elif st.backfilled:
            out["chains_stale"] += 1
            log.warning("onchain chain_id=%s indexer stale (updated_at=%s); keyless usage kept", st.chain_id, st.updated_at)
    window_start = ctx.day - timedelta(days=WINDOW_DAYS)
    # The day pool_stats reports: the last complete UTC day (today is still running when the nightly job fires).
    ref = ctx.day - timedelta(days=1) if ctx.day >= ctx.now.astimezone(timezone.utc).date() else ctx.day
    extras_stats = ctx.extras.get("keyless_pool_stats")
    for cid in ready:
        coverage = conn.execute(sa.select(sa.func.min(pool_daily_onchain.c.day)).where(pool_daily_onchain.c.chain_id == cid)).scalar()
        if coverage is None:
            continue
        out["chains_usage"] += 1
        full = coverage <= window_start
        start = max(coverage, window_start)
        pool_ids = [r[0] for r in conn.execute(
            sa.select(pool_daily_onchain.c.pool_id).distinct()
            .join(pools, (pools.c.pool_id == pool_daily_onchain.c.pool_id) & (pools.c.chain_id == pool_daily_onchain.c.chain_id))
            .where(pool_daily_onchain.c.chain_id == cid, pool_daily_onchain.c.day >= start, pool_daily_onchain.c.day <= ctx.day)
            .order_by(pool_daily_onchain.c.pool_id))]
        for i in range(0, len(pool_ids), CHUNK):
            _usage_chunk(conn, ctx, cid, pool_ids[i:i + CHUNK], start, full, ref, coverage, extras_stats, out)


def _usage_chunk(conn, ctx: RunContext, cid: int, chunk: list[str], start: date, full: bool, ref: date, coverage: date,
                 extras_stats, out: dict) -> None:
    daily: dict[str, dict[date, tuple[int, int, float]]] = {}
    for r in conn.execute(sa.select(pool_daily_onchain).where(
            pool_daily_onchain.c.chain_id == cid, pool_daily_onchain.c.pool_id.in_(chunk),
            pool_daily_onchain.c.day >= start, pool_daily_onchain.c.day <= ctx.day)):
        daily.setdefault(r.pool_id, {})[r.day] = (r.swaps or 0, r.wallets or 0, r.volume_usd or 0.0)
    tvl: dict[str, float] = {}
    for r in conn.execute(sa.select(pool_snapshots.c.pool_id, pool_snapshots.c.tvl_usd).where(
            pool_snapshots.c.chain_id == cid, pool_snapshots.c.pool_id.in_(chunk), pool_snapshots.c.day <= ctx.day,
            pool_snapshots.c.tvl_usd.is_not(None)).order_by(pool_snapshots.c.day)):
        tvl[r.pool_id] = r.tvl_usd                         # ordered by day: the last one wins
    has_window = {r[0] for r in conn.execute(sa.select(pool_snapshots.c.pool_id).distinct().where(
        pool_snapshots.c.chain_id == cid, pool_snapshots.c.pool_id.in_(chunk),
        pool_snapshots.c.day >= ctx.day - timedelta(days=WINDOW_DAYS + 1), pool_snapshots.c.day <= ctx.day))}
    replace = [pid for pid in chunk if full or pid not in has_window]
    snap_rows = []
    for pid in replace:
        cum_v, cum_n = 0.0, 0
        snap_rows.append({"pool_id": pid, "chain_id": cid, "day": start - timedelta(days=1), "tvl_usd": tvl.get(pid),
                          "volume_usd_cum": 0.0, "tx_count_cum": 0})
        d = start
        while d <= ctx.day:
            n, _, v = daily.get(pid, {}).get(d, (0, 0, 0.0))
            cum_v += v
            cum_n += n
            snap_rows.append({"pool_id": pid, "chain_id": cid, "day": d, "tvl_usd": tvl.get(pid),
                              "volume_usd_cum": cum_v, "tx_count_cum": cum_n})
            d += timedelta(days=1)
    if replace:
        conn.execute(sa.delete(pool_snapshots).where(pool_snapshots.c.chain_id == cid, pool_snapshots.c.pool_id.in_(replace),
                                                     pool_snapshots.c.day <= ctx.day))
        for i in range(0, len(snap_rows), CHUNK):
            upsert(conn, pool_snapshots, snap_rows[i:i + CHUNK], keys=["pool_id", "chain_id", "day"])
    out["series_replaced"] += len(replace)
    out["pools_usage"] += len(chunk)
    if ref < coverage:
        return  # no complete on-chain day yet
    stat_rows = []
    for pid in chunk:
        _, w, v = daily.get(pid, {}).get(ref, (0, 0, 0.0))
        stat_rows.append({"pool_id": pid, "chain_id": cid, "day": ctx.day, "vol_24h": v, "traders_24h": w})
        if extras_stats is not None:
            extras_stats[(pid, cid)] = (v, w)
    for i in range(0, len(stat_rows), CHUNK):
        upsert(conn, pool_stats, stat_rows[i:i + CHUNK], keys=["pool_id", "chain_id", "day"], update=["vol_24h", "traders_24h"])
    out["stats"] += len(stat_rows)
