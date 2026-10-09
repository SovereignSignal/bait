"""Step "onchain" (right after activity): bring the on-chain indexer's tables into the ones the web app and score read.

Reads v4_pools, pool_daily_onchain, indexer_state (written by `python -m bait.indexer`). No-op when they are empty.
1. Hooks: every v4_pools hook missing from `hooks` becomes an unlisted hook row (name None, like snapshot keyless), busiest
   first, at most ONCHAIN_MAX_NEW_HOOKS (500) per night so the group step's Claude long tail stays bounded; code_hash from
   keyless.code_hashes where a public RPC exists for the chain (KEYLESS != 0), else None.
2. Pools: every v4_pools row whose hook is in `hooks` is upserted into `pools` (any chain; the web filters unknown chains),
   incrementally (per-chain `onchain_watermarks` on `block_number`, plus every pool of a newly known hook); a row is
   rewritten only when a value changed. A Postgres size line (pg_database_size vs DISK_LIMIT_GB) is logged every night.
   token0/token1 are only set on insert, so GeckoTerminal/subgraph symbols survive; created_at = Initialize block time.
3. Usage (chains whose indexer_state is backfilled and updated within ONCHAIN_MAX_STALE_HOURS): for each pool with
   pool_daily_onchain rows in day-14..day, pool_snapshots becomes a cumulative volume / swap series from the on-chain daily
   numbers (zero-seeded the day before, like snapshot keyless), replacing the pool's earlier snapshots so one scale is
   used; TVL keeps the latest GeckoTerminal/subgraph reserve (the indexer does not compute TVL). pool_stats(day) gets
   vol_24h / traders_24h from the last complete UTC day. On-chain is exact, so it overrides keyless values for those pools.
   While a chain's on-chain history is shorter than the 14-day window, pools keyless already snapshots keep their
   keyless series (only pool_stats is overridden). Volume the chain cannot measure (custom-accounting hooks, pool-days
   mostly unpriced) never overrides keyless numbers; pool_stats.vol_source says which source won.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time as dtime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from ..db import (as_utc, hooks, indexer_state, onchain_watermarks, pool_daily_onchain, pool_snapshots, pool_stats, pools,
                  upsert, v4_pools)
from ..indexer.chains import CHAINS
from ..rules import custom_accounting, flags_from_address
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
           "pools_usage": 0, "series_replaced": 0, "stats": 0, "chains_usage": 0, "chains_stale": 0, "vol_unknown": 0, "errors": 0,
           "pools_written": 0, "db_gb": None, "disk_warn": 0}
    _disk_check(conn, out)
    where = [v4_pools.c.chain_id.in_(ctx.chains)] if ctx.chains else []
    out["v4_pools"] = conn.execute(sa.select(sa.func.count()).select_from(v4_pools).where(*where)).scalar() or 0
    if not out["v4_pools"]:
        log.info("onchain: indexer tables empty, nothing to do")
        return out
    known = _add_hooks(conn, ctx, where, out)
    _upsert_pools(conn, ctx, known, out)
    _usage(conn, ctx, out)
    log.info("onchain v4_pools=%d hooks_added=%d deferred=%d pools=%d pools_written=%d usage_pools=%d series=%d stats=%d",
             out["v4_pools"], out["hooks_added"], out["hooks_deferred"], out["pools"], out["pools_written"], out["pools_usage"],
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
    new = [{"address": r.hook_address, "chain_id": r.chain_id, "name": None, "description": "", "flags": flags_from_address(r.hook_address), "properties": {}, "hooklisted": False,
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
    """v4_pools -> pools for known hooks, incrementally: 1) per chain, rows above the block watermark (Initialize history
    only grows upward per chain); 2) every pool of a known hook with no pools row yet (hooks that became known tonight,
    incl. ones the cap deferred while their pools passed the watermark). Unchanged rows are never rewritten."""
    marks = {r.chain_id: r.pools_block for r in conn.execute(sa.select(onchain_watermarks))}
    chain_ids = {c.chain_id for c in CHAINS} | set(marks) | {r[0] for r in conn.execute(sa.select(indexer_state.c.chain_id))}
    for cid in sorted(chain_ids):
        if ctx.chains and cid not in ctx.chains:
            continue
        # strictly above the watermark block: a block's Initialize logs are committed in one indexer page, never split
        floor = -1 if marks.get(cid) is None else marks[cid]
        last = top = None
        while True:
            q = sa.select(v4_pools).where(v4_pools.c.chain_id == cid, v4_pools.c.block_number > floor)
            if last is not None:
                q = q.where(sa.tuple_(v4_pools.c.block_number, v4_pools.c.pool_id) > sa.tuple_(sa.literal(last[0]), sa.literal(last[1])))
            batch = conn.execute(q.order_by(v4_pools.c.block_number, v4_pools.c.pool_id).limit(2000)).all()
            if not batch:
                break
            last = top = (batch[-1].block_number, batch[-1].pool_id)
            _copy_pools(conn, [r for r in batch if (r.hook_address, r.chain_id) in known], out)
        if top is not None:
            upsert(conn, onchain_watermarks, [{"chain_id": cid, "pools_block": top[0], "updated_at": ctx.now}], keys=["chain_id"])
    orphans = conn.execute(sa.select(hooks.c.chain_id, hooks.c.address).where(
        ~sa.exists().where(pools.c.chain_id == hooks.c.chain_id, pools.c.hook_address == hooks.c.address),
        sa.exists().where(v4_pools.c.chain_id == hooks.c.chain_id, v4_pools.c.hook_address == hooks.c.address))).all()
    for h in orphans:
        if ctx.chains and h.chain_id not in ctx.chains:
            continue
        rows = conn.execute(sa.select(v4_pools).where(v4_pools.c.chain_id == h.chain_id, v4_pools.c.hook_address == h.address)).all()
        for i in range(0, len(rows), 2000):
            _copy_pools(conn, rows[i:i + 2000], out)


def _copy_pools(conn, batch, out: dict) -> None:
    ins = postgresql.insert if conn.dialect.name == "postgresql" else sqlite.insert
    rows = [{"pool_id": r.pool_id, "chain_id": r.chain_id, "hook_address": r.hook_address,
             "token0": {"id": r.currency0, "symbol": None}, "token1": {"id": r.currency1, "symbol": None},
             "fee": r.fee, "tick_spacing": r.tick_spacing, "created_at": r.block_time} for r in batch]
    for i in range(0, len(rows), CHUNK):
        stmt = ins(pools).values(rows[i:i + CHUNK])
        ex = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["pool_id", "chain_id"],
            set_={"hook_address": ex.hook_address, "fee": ex.fee, "tick_spacing": ex.tick_spacing,
                  "created_at": sa.func.coalesce(ex.created_at, pools.c.created_at)},
            where=sa.or_(pools.c.hook_address.is_distinct_from(ex.hook_address), pools.c.fee.is_distinct_from(ex.fee),
                         pools.c.tick_spacing.is_distinct_from(ex.tick_spacing),
                         sa.and_(ex.created_at.is_not(None), pools.c.created_at.is_distinct_from(ex.created_at))))
        # RETURNING, not rowcount: a multi-row VALUES insert reports rowcount -1 on psycopg
        out["pools_written"] += len(conn.execute(stmt.returning(pools.c.pool_id)).all())
    out["pools"] += len(rows)


def _disk_check(conn, out: dict) -> None:
    """Nightly size line. pg_database_size covers this database only (no WAL): an estimate of the volume's use."""
    if conn.dialect.name != "postgresql":
        return
    size = conn.execute(sa.text("SELECT pg_database_size(current_database())")).scalar_one()
    try:
        limit = float(os.environ.get("DISK_LIMIT_GB", "") or 50)
    except ValueError:
        limit = 50.0
    gb = size / 1024 ** 3
    pct = 100 * gb / limit
    out["db_gb"] = round(gb, 2)
    if pct >= 60:
        out["disk_warn"] = 1
        log.warning("onchain disk: database %.1f GB is %.0f%% of the %.0f GB volume (DISK_LIMIT_GB)", gb, pct, limit)
    else:
        log.info("onchain disk: database %.1f GB, %.0f%% of %.0f GB", gb, pct, limit)


def ref_day(ctx: RunContext) -> date:
    """The day pool_stats reports: the last complete UTC day (today is still running when the nightly job fires)."""
    return ctx.day - timedelta(days=1) if ctx.day >= ctx.now.astimezone(timezone.utc).date() else ctx.day


def ready_chains(conn, ctx: RunContext, out: dict | None = None) -> list[int]:
    """Chains whose on-chain usage may replace keyless numbers: backfilled, written within ONCHAIN_MAX_STALE_HOURS, and the
    swap cursor's block time past the end of the reference day. updated_at alone is not enough: Initialize pages keep
    bumping it while swap queries fail every loop."""
    stale_h = _env_int("ONCHAIN_MAX_STALE_HOURS", 6)
    ref_end = datetime.combine(ref_day(ctx) + timedelta(days=1), dtime.min, tzinfo=timezone.utc)
    ready = []
    for st in conn.execute(sa.select(indexer_state).order_by(indexer_state.c.chain_id)):
        if (ctx.chains and st.chain_id not in ctx.chains) or not st.backfilled:
            continue
        fresh = st.updated_at is not None and as_utc(st.updated_at) >= ctx.now - timedelta(hours=stale_h)
        swaps_done = st.swap_block_time is not None and as_utc(st.swap_block_time) >= ref_end
        if fresh and swaps_done:
            ready.append(st.chain_id)
        elif out is not None:
            out["chains_stale"] += 1
            log.warning("onchain chain_id=%s not ready (updated_at=%s swap_block_time=%s, need >= %s); keyless usage kept",
                        st.chain_id, st.updated_at, st.swap_block_time, ref_end)
    return ready


def _usage(conn, ctx: RunContext, out: dict) -> None:
    ready = ready_chains(conn, ctx, out)
    window_start = ctx.day - timedelta(days=WINDOW_DAYS)
    ref = ref_day(ctx)
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


def _volume_unknown(hook: str | None, days) -> bool:
    """On-chain USD volume cannot stand in for keyless numbers: a custom-accounting hook, or most swaps had no priced side.
    days: iterable of (swaps, wallets, volume, unpriced)."""
    if hook and custom_accounting(hook):
        return True
    days = list(days)
    swaps, unpriced = sum(d[0] for d in days), sum(d[3] for d in days)
    return swaps > 0 and unpriced * 2 > swaps


def _usage_chunk(conn, ctx: RunContext, cid: int, chunk: list[str], start: date, full: bool, ref: date, coverage: date,
                 extras_stats, out: dict) -> None:
    daily: dict[str, dict[date, tuple[int, int, float, int]]] = {}
    for r in conn.execute(sa.select(pool_daily_onchain).where(
            pool_daily_onchain.c.chain_id == cid, pool_daily_onchain.c.pool_id.in_(chunk),
            pool_daily_onchain.c.day >= start, pool_daily_onchain.c.day <= ctx.day)):
        daily.setdefault(r.pool_id, {})[r.day] = (r.swaps or 0, r.wallets or 0, r.volume_usd or 0.0, r.unpriced_swaps or 0)
    hook_of = dict(conn.execute(sa.select(pools.c.pool_id, pools.c.hook_address).where(
        pools.c.chain_id == cid, pools.c.pool_id.in_(chunk))).all())
    tvl: dict[str, float] = {}
    for r in conn.execute(sa.select(pool_snapshots.c.pool_id, pool_snapshots.c.tvl_usd).where(
            pool_snapshots.c.chain_id == cid, pool_snapshots.c.pool_id.in_(chunk), pool_snapshots.c.day <= ctx.day,
            pool_snapshots.c.tvl_usd.is_not(None)).order_by(pool_snapshots.c.day)):
        tvl[r.pool_id] = r.tvl_usd                         # ordered by day: the last one wins
    has_window = {r[0] for r in conn.execute(sa.select(pool_snapshots.c.pool_id).distinct().where(
        pool_snapshots.c.chain_id == cid, pool_snapshots.c.pool_id.in_(chunk),
        pool_snapshots.c.day >= ctx.day - timedelta(days=WINDOW_DAYS + 1), pool_snapshots.c.day <= ctx.day))}
    unknown = {pid: _volume_unknown(hook_of.get(pid), daily.get(pid, {}).values()) for pid in chunk}
    # unknown volume never replaces a keyless series; without one, the series carries swap counts and NULL volume
    replace = [pid for pid in chunk if (full or pid not in has_window) and not (unknown[pid] and pid in has_window)]
    snap_rows = []
    for pid in replace:
        cum_v, cum_n = 0.0, 0
        snap_rows.append({"pool_id": pid, "chain_id": cid, "day": start - timedelta(days=1), "tvl_usd": tvl.get(pid),
                          "volume_usd_cum": None if unknown[pid] else 0.0, "tx_count_cum": 0})
        d = start
        while d <= ctx.day:
            n, _, v, _ = daily.get(pid, {}).get(d, (0, 0, 0.0, 0))
            cum_v += v
            cum_n += n
            snap_rows.append({"pool_id": pid, "chain_id": cid, "day": d, "tvl_usd": tvl.get(pid),
                              "volume_usd_cum": None if unknown[pid] else cum_v, "tx_count_cum": cum_n})
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
    stat_rows, held = [], []
    for pid in chunk:
        n, w, v, u = daily.get(pid, {}).get(ref, (0, 0, 0.0, 0))
        hook = hook_of.get(pid)
        if _volume_unknown(hook, [(n, w, v, u)]):
            held.append({"pool_id": pid, "chain_id": cid, "day": ctx.day, "vol_24h": None, "traders_24h": w,
                         "vol_source": "hook" if hook and custom_accounting(hook) else "unpriced"})
            if extras_stats is not None:
                extras_stats[(pid, cid)] = ((extras_stats.get((pid, cid)) or (0.0, 0))[0], w)
        else:
            stat_rows.append({"pool_id": pid, "chain_id": cid, "day": ctx.day, "vol_24h": v, "traders_24h": w, "vol_source": "onchain"})
            if extras_stats is not None:
                extras_stats[(pid, cid)] = (v, w)
    keys = ["pool_id", "chain_id", "day"]
    for i in range(0, len(stat_rows), CHUNK):
        upsert(conn, pool_stats, stat_rows[i:i + CHUNK], keys=keys, update=["vol_24h", "traders_24h", "vol_source"])
    for i in range(0, len(held), CHUNK):  # keyless vol_24h survives; a new row gets NULL (unknown), never $0
        upsert(conn, pool_stats, held[i:i + CHUNK], keys=keys, update=["traders_24h", "vol_source"])
    out["stats"] += len(stat_rows) + len(held)
    out["vol_unknown"] += len(held)
