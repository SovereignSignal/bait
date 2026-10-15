"""Step "onchain" (right after activity): bring the on-chain indexer's tables into the ones the web app and score read.

Reads v4_pools, pool_daily_onchain, indexer_state (written by `python -m bait.indexer`). No-op when they are empty.
1. Hooks: every v4_pools hook missing from `hooks` becomes an unlisted hook row (name None, like snapshot keyless), busiest
   first, at most ONCHAIN_MAX_NEW_HOOKS (500) per night so the group step's Claude long tail stays bounded; code_hash from
   keyless.code_hashes where a public RPC exists for the chain (KEYLESS != 0), else None.
2. Pools: every v4_pools row whose hook is in `hooks` is upserted into `pools` (any chain; the web filters unknown chains),
   incrementally (per-chain `onchain_watermarks` on `block_number` as the fast path, plus a nightly pool-level anti-join
   for known hooks' pools with no pools row); a row is rewritten only when a value changed.
   hooks.pools_total = every pool initialized through the hook (v4_pools rows, or hook_pool_counts.pools_total once the
   indexer capped it), written only when it changed; score reports pools_n as at least that.
   A capped hook whose pools past both caps swapped gets a pools row for its aggregate pool 'hook:<address>' (the
   indexer folds those swaps there per day), so step 3 treats it like a pool and pool_stats.vol_source says 'hook_total'. A Postgres size line (pg_database_size vs DISK_LIMIT_GB) is logged every night.
   token0/token1 are only set on insert, so GeckoTerminal/subgraph symbols survive; created_at = Initialize block time.
3. Usage (chains whose indexer_state is backfilled and updated within ONCHAIN_MAX_STALE_HOURS, with no swap before the
   reference day's end still owed to pool_lookup_queue): for each pool with
   pool_daily_onchain rows in day-14..day, pool_snapshots gets one row per day from `start` (volume_usd_day, tx_count_day;
   the legacy cumulative columns zero-seeded the day before), replacing the pool's rows from `start` on; older rows and the
   seed day's daily values are kept so a past day can be re-scored. TVL keeps the latest GeckoTerminal/subgraph reserve (the indexer does not compute TVL). pool_stats(day) gets
   vol_24h / traders_24h / wallets_24h (distinct) from the last complete UTC day, window_end = its end. On-chain is exact, so it overrides keyless values for those pools.
   While a chain's on-chain history is shorter than the 14-day window, pools keyless already snapshots keep their
   keyless series (only pool_stats is overridden). Volume the chain cannot measure (custom-accounting hooks, pool-days
   mostly unpriced) never overrides keyless numbers; pool_stats.vol_source says which source won ('hook_total' for a hook's aggregate pool). The rule is per day:
   from the first unknown day on, the series carries NULL volume, or the keyless series is kept when that day is on or
   before the reference day, and pool_stats keeps keyless volume exactly when the series' reference day is unknown.
   Pools whose keyless series is kept still get exact tx_count_day from the indexer.
4. A bounded candle refresh repairs estimated closed days on those held-back pools when exact positive swaps are known.
   Only explicit GeckoTerminal UTC/USD candles replace estimates; custom-accounting protection is never bypassed with
   PoolManager USD. ONCHAIN_CANDLE_REFRESH_CAP (default 10 pools per run; 0 disables) uses the shared paced keyless client.
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime, time as dtime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from ..db import (HOOK_POOL_PREFIX, as_utc, hook_pool_counts, hook_pool_id, hooks, indexer_state, onchain_watermarks, pool_daily_onchain,
                  pool_lookup_queue, pool_snapshots, pool_stats, pools, upsert, v4_pools)
from ..indexer.chains import CHAINS
from ..rules import custom_accounting, flags_from_address
from . import RunContext, log

WINDOW_DAYS = 14
CHUNK = 500
SNAP_KEYS = ["pool_id", "chain_id", "day"]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"v4_pools": 0, "hooks_added": 0, "hooks_deferred": 0, "code_hashes": 0, "pools": 0,
           "pools_usage": 0, "series_replaced": 0, "stats": 0, "chains_usage": 0, "chains_stale": 0, "vol_unknown": 0, "errors": 0,
           "pools_written": 0, "db_gb": None, "disk_warn": 0, "hook_totals": 0, "hook_pools": 0,
           "candle_refresh_candidates": 0, "candle_refresh_attempts": 0, "candle_refresh_rows": 0,
           "candle_refresh_misses": 0, "candle_refresh_deferred": 0}
    _disk_check(conn, out)
    where = [v4_pools.c.chain_id.in_(ctx.chains)] if ctx.chains else []
    out["v4_pools"] = conn.execute(sa.select(sa.func.count()).select_from(v4_pools).where(*where)).scalar() or 0
    if not out["v4_pools"]:
        log.info("onchain: indexer tables empty, nothing to do")
        return out
    known = _add_hooks(conn, ctx, where, out)
    _upsert_pools(conn, ctx, known, out)
    _pool_totals(conn, ctx, out)
    _hook_pools(conn, ctx, known, out)
    ready = _usage(conn, ctx, out)
    _refresh_held_candles(conn, ctx, ready, out)
    log.info("onchain v4_pools=%d hooks_added=%d deferred=%d pools=%d pools_written=%d hook_totals=%d usage_pools=%d series=%d "
             "stats=%d", out["v4_pools"], out["hooks_added"], out["hooks_deferred"], out["pools"], out["pools_written"],
             out["hook_totals"], out["pools_usage"], out["series_replaced"], out["stats"])
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
    """v4_pools -> pools for known hooks: 1) fast path, per chain, rows above the block watermark (new Initialize logs,
    and value changes of recent rows); 2) every night, a pool-level anti-join for rows of known hooks that have no pools
    row (the watermark alone misses rows a backfill inserts below it). Unchanged rows are never rewritten."""
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
    # Pool-level catch-up, every night: v4_pools rows of a known hook that have no pools row. Covers rows a backfill
    # inserted below the watermark (RPC head-following before a token was set, the sync_rpc gap path, a chain still
    # backfilling) and hooks that became known after their pools passed the watermark. Keyset-paginated on the v4_pools
    # primary key, so each batch is an index range scan with a per-row probe of the pools primary key.
    hook_known = sa.exists().where(hooks.c.chain_id == v4_pools.c.chain_id, hooks.c.address == v4_pools.c.hook_address)
    not_copied = ~sa.exists().where(pools.c.chain_id == v4_pools.c.chain_id, pools.c.pool_id == v4_pools.c.pool_id)
    last = None
    while True:
        q = sa.select(v4_pools).where(hook_known, not_copied)
        if ctx.chains:
            q = q.where(v4_pools.c.chain_id.in_(ctx.chains))
        if last is not None:
            q = q.where(sa.tuple_(v4_pools.c.chain_id, v4_pools.c.pool_id) > sa.tuple_(sa.literal(last[0]), sa.literal(last[1])))
        batch = conn.execute(q.order_by(v4_pools.c.chain_id, v4_pools.c.pool_id).limit(2000)).all()
        if not batch:
            break
        last = (batch[-1].chain_id, batch[-1].pool_id)
        _copy_pools(conn, batch, out)


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


def _pool_totals(conn, ctx: RunContext, out: dict) -> None:
    """hooks.pools_total = every pool initialized through the hook: its v4_pools rows, or hook_pool_counts.pools_total
    once the indexer capped it (that counts the pools it never stored). Only changed values are written."""
    where = [v4_pools.c.chain_id.in_(ctx.chains)] if ctx.chains else []
    totals = {(r.chain_id, r.hook_address): r.n for r in conn.execute(
        sa.select(v4_pools.c.chain_id, v4_pools.c.hook_address, sa.func.count().label("n")).where(*where)
        .group_by(v4_pools.c.chain_id, v4_pools.c.hook_address))}
    cwhere = [hook_pool_counts.c.chain_id.in_(ctx.chains)] if ctx.chains else []
    for r in conn.execute(sa.select(hook_pool_counts).where(*cwhere)):
        k = (r.chain_id, r.hook_address)
        totals[k] = max(totals.get(k, 0), r.pools_total or 0)
    have = {(r.chain_id, r.address): r.pools_total for r in conn.execute(sa.select(hooks.c.chain_id, hooks.c.address, hooks.c.pools_total))}
    changed = [{"c": c, "a": a, "n": n} for (c, a), n in totals.items() if (c, a) in have and have[(c, a)] != n]
    for i in range(0, len(changed), CHUNK):
        conn.execute(sa.update(hooks).where(hooks.c.chain_id == sa.bindparam("c"), hooks.c.address == sa.bindparam("a"))
                     .values(pools_total=sa.bindparam("n")), changed[i:i + CHUNK])
    out["hook_totals"] = len(changed)


def _hook_pools(conn, ctx: RunContext, known: set[tuple[str, int]], out: dict) -> None:
    """A pools row for each known hook's aggregate pool (db.hook_pool_id): the swaps of its pools past both indexer caps
    are in pool_daily_onchain under that id, so step 3 gives it a series and pool_stats (vol_source 'hook_total') and its
    project's windows include them. Inserted once; token columns stay empty."""
    where = [hook_pool_counts.c.swaps_total > 0]
    if ctx.chains:
        where.append(hook_pool_counts.c.chain_id.in_(ctx.chains))
    want = [(r.chain_id, r.hook_address) for r in conn.execute(sa.select(hook_pool_counts.c.chain_id, hook_pool_counts.c.hook_address)
                                                                 .where(*where)) if (r.hook_address, r.chain_id) in known]
    for cid, h in want:
        pid = hook_pool_id(h)
        if conn.execute(sa.select(pools.c.pool_id).where(pools.c.pool_id == pid, pools.c.chain_id == cid)).first() is None:
            conn.execute(sa.insert(pools).values(pool_id=pid, chain_id=cid, hook_address=h))
            out["hook_pools"] += 1


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


def owed_before(conn, chain_id: int, t: datetime) -> int:
    """Pool ids whose swaps before t the indexer left out and has not settled yet (pool_lookup_queue): until they are, a
    day before t is not final for every pool (a launchpad's pools past the cap are the ones that wait)."""
    q = pool_lookup_queue.c
    return conn.execute(sa.select(sa.func.count()).select_from(pool_lookup_queue).where(
        q.chain_id == chain_id, sa.or_(q.from_time.is_(None), q.from_time < t))).scalar() or 0


def ready_chains(conn, ctx: RunContext, out: dict | None = None) -> list[int]:
    """Chains whose on-chain usage may replace keyless numbers: backfilled, written within ONCHAIN_MAX_STALE_HOURS, the
    swap cursor's block time past the end of the reference day, and no swap before that end still owed to the lookup
    queue. updated_at alone is not enough: Initialize pages keep bumping it while swap queries fail every loop."""
    stale_h = _env_int("ONCHAIN_MAX_STALE_HOURS", 6)
    ref_end = datetime.combine(ref_day(ctx) + timedelta(days=1), dtime.min, tzinfo=timezone.utc)
    ready = []
    for st in conn.execute(sa.select(indexer_state).order_by(indexer_state.c.chain_id)):
        if (ctx.chains and st.chain_id not in ctx.chains) or not st.backfilled:
            continue
        fresh = st.updated_at is not None and as_utc(st.updated_at) >= ctx.now - timedelta(hours=stale_h)
        swaps_done = st.swap_block_time is not None and as_utc(st.swap_block_time) >= ref_end
        owed = owed_before(conn, st.chain_id, ref_end) if fresh and swaps_done else 0
        if fresh and swaps_done and not owed:
            ready.append(st.chain_id)
        elif out is not None:
            out["chains_stale"] += 1
            log.warning("onchain chain_id=%s not ready (updated_at=%s swap_block_time=%s, need >= %s; %d pool ids owe swaps from "
                        "before it); keyless usage kept", st.chain_id, st.updated_at, st.swap_block_time, ref_end, owed)
    return ready


def _usage(conn, ctx: RunContext, out: dict) -> list[int]:
    ready = ready_chains(conn, ctx, out)
    window_start = ctx.day - timedelta(days=WINDOW_DAYS)
    ref = ref_day(ctx)
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
            _usage_chunk(conn, ctx, cid, pool_ids[i:i + CHUNK], start, full, ref, coverage, out)
    return ready


def _refresh_held_candles(conn, ctx: RunContext, ready: list[int], out: dict) -> None:
    """Repair only explicit estimated days with positive exact swaps on a ready chain and held-back on-chain USD.

    Oldest affected day first, then most known swaps, then chain/pool: a deterministic bounded queue independent of
    names or USD agreement. The cap counts pool fetch attempts (the shared HTTP client's existing retries still apply).
    Missing candles, HTTP failures and invalid values leave the estimate and its flag intact. No missing date implies
    zero. Daily USD is replaced, never added; legacy cumulative columns are preserved like repair.fill_pool.
    """
    from .. import keyless as K
    from .snapshot import keyless_http

    cap = max(0, _env_int("ONCHAIN_CANDLE_REFRESH_CAP", 10))
    if not ready or cap == 0 or os.environ.get("KEYLESS", "1") == "0":
        return
    ref, start = ref_day(ctx), ctx.day - timedelta(days=WINDOW_DAYS)
    net_by_chain = {v[0]: net for net, v in K.CHAINS.items()}
    s, raw, st = pool_snapshots.c, pool_daily_onchain.c, pool_stats.c
    q = (sa.select(s.pool_id, s.chain_id, s.day, raw.swaps)
         .select_from(pool_snapshots.join(pool_daily_onchain,
                      (s.pool_id == raw.pool_id) & (s.chain_id == raw.chain_id) & (s.day == raw.day))
                      .join(pool_stats, (s.pool_id == st.pool_id) & (s.chain_id == st.chain_id) & (st.day == ctx.day)))
         .where(s.chain_id.in_(ready), s.day >= start, s.day <= ref, s.estimated.is_(True),
                s.volume_usd_cum.is_not(None), s.volume_usd_day.is_not(None), raw.swaps > 0,
                st.vol_source.in_(["hook", "unpriced", "keyless_candle", "closed_day"])))
    wanted: dict[tuple[str, int], dict] = {}
    for r in conn.execute(q):
        if r.chain_id not in net_by_chain or r.pool_id.startswith(HOOK_POOL_PREFIX):
            continue
        wanted.setdefault((r.pool_id, r.chain_id), {})[r.day] = r.swaps
    ordered = sorted(wanted, key=lambda k: (min(wanted[k]), -sum(wanted[k].values()), k[1], k[0]))
    out["candle_refresh_candidates"] = len(ordered)
    out["candle_refresh_deferred"] = max(0, len(ordered) - cap)
    if not ordered:
        return
    http = keyless_http(ctx)
    for pid, cid in ordered[:cap]:
        out["candle_refresh_attempts"] += 1
        try:
            payload = http.gt(f"/networks/{net_by_chain[cid]}/pools/{pid}/ohlcv/day?aggregate=1&limit={WINDOW_DAYS + 1}&currency=usd")
            candles = (((payload or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
            # The general keyless parser treats missing volume as zero; repair must require an explicit valid amount.
            values = {}
            for row in candles:
                if not isinstance(row, list) or len(row) < 6 or row[5] is None or isinstance(row[5], bool):
                    continue
                try:
                    ts, v = int(row[0]), float(row[5])
                    if float(row[0]) == ts and ts % 86400 == 0 and math.isfinite(v) and v >= 0:
                        values[datetime.fromtimestamp(ts, tz=timezone.utc).date()] = v
                except (TypeError, ValueError, OverflowError):
                    continue
        except Exception as exc:
            log.warning("onchain candle refresh chain=%s pool=%s failed: %s", cid, pid, exc)
            values = {}
        updated = 0
        for day in sorted(wanted[(pid, cid)]):
            if day not in values:
                continue
            conn.execute(sa.update(pool_snapshots).where(s.pool_id == pid, s.chain_id == cid, s.day == day)
                         .values(volume_usd_day=values[day], estimated=False))
            if day == ref:
                conn.execute(sa.update(pool_stats).where(st.pool_id == pid, st.chain_id == cid, st.day == ctx.day)
                             .values(vol_24h=values[day], vol_source="keyless_candle"))
            updated += 1
        out["candle_refresh_rows"] += updated
        out["candle_refresh_misses"] += int(updated == 0)
        log.info("onchain candle refresh chain=%s pool=%s source=geckoterminal_utc_usd updated_days=%d missing_days=%d",
                 cid, pid, updated, len(wanted[(pid, cid)]) - updated)


def _unknown_from(hook: str | None, days: dict, start: date) -> date | None:
    """First day whose on-chain USD volume cannot stand in for keyless numbers: `start` for a custom-accounting hook, else
    the first day with more than half its swaps unpriced. days: {day: (swaps, wallets, volume, unpriced)}. A cumulative
    series cannot skip a day, so volume stays unknown from that day on."""
    if hook and custom_accounting(hook):
        return start
    bad = [d for d, (n, _, _, u) in days.items() if n > 0 and u * 2 > n]
    return min(bad) if bad else None


def _usage_chunk(conn, ctx: RunContext, cid: int, chunk: list[str], start: date, full: bool, ref: date, coverage: date,
                 out: dict) -> None:
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
    # Per day: from the first unknown day on, the series carries NULL volume (swap counts kept). If that day is on or before
    # the reference day, a keyless series is kept instead and pool_stats keeps keyless volume, so the two always agree.
    bad_from = {pid: _unknown_from(hook_of.get(pid), daily.get(pid, {}), start) for pid in chunk}
    held_back = {pid for pid, b in bad_from.items() if b is not None and b <= ref}
    replace = [pid for pid in chunk if (full or pid not in has_window) and not (pid in held_back and pid in has_window)]
    snap_rows, seeds = [], []
    for pid in replace:
        cum_v, cum_n = 0.0, 0
        seeds.append({"pool_id": pid, "chain_id": cid, "day": start - timedelta(days=1), "tvl_usd": tvl.get(pid),
                      "volume_usd_cum": 0.0, "tx_count_cum": 0})
        d = start
        while d <= ctx.day:
            n, _, v, _ = daily.get(pid, {}).get(d, (0, 0, 0.0, 0))
            cum_v += v
            cum_n += n
            unknown = bad_from[pid] is not None and d >= bad_from[pid]
            snap_rows.append({"pool_id": pid, "chain_id": cid, "day": d, "tvl_usd": tvl.get(pid),
                              "volume_usd_cum": None if unknown else cum_v, "tx_count_cum": cum_n,
                              "volume_usd_day": None if unknown else v, "tx_count_day": n,
                              "estimated": d >= ctx.day})   # the running day is partial
            d += timedelta(days=1)
    if replace:
        # Rewrites the series from `start` on. The seed day only resets the legacy cumulative (its daily values stay) and
        # older days are kept, so `score --day <past day>` still finds their daily values.
        conn.execute(sa.delete(pool_snapshots).where(pool_snapshots.c.chain_id == cid, pool_snapshots.c.pool_id.in_(replace),
                                                     pool_snapshots.c.day >= start, pool_snapshots.c.day <= ctx.day))
        for i in range(0, len(seeds), CHUNK):
            upsert(conn, pool_snapshots, seeds[i:i + CHUNK], keys=SNAP_KEYS, update=["tvl_usd", "volume_usd_cum", "tx_count_cum"])
        for i in range(0, len(snap_rows), CHUNK):
            upsert(conn, pool_snapshots, snap_rows[i:i + CHUNK], keys=SNAP_KEYS)
    replaced = set(replace)
    _swap_counts(conn, cid, [pid for pid in chunk if pid not in replaced], daily, start, ref)
    out["series_replaced"] += len(replace)
    out["pools_usage"] += len(chunk)
    if ref < coverage:
        return  # no complete on-chain day yet
    window_end = datetime.combine(ref + timedelta(days=1), dtime.min, tzinfo=timezone.utc)   # the closed day `ref` ends here
    # A same-day rerun must preserve the provenance of a candle refreshed after the previous usage copy.
    candle_stats = set(conn.execute(sa.select(pool_stats.c.pool_id).where(
        pool_stats.c.chain_id == cid, pool_stats.c.pool_id.in_(chunk), pool_stats.c.day == ctx.day,
        pool_stats.c.vol_source == "keyless_candle", pool_stats.c.window_end == window_end)).scalars())
    closed_volume = dict(conn.execute(sa.select(pool_snapshots.c.pool_id, pool_snapshots.c.volume_usd_day).where(
        pool_snapshots.c.chain_id == cid, pool_snapshots.c.pool_id.in_(chunk), pool_snapshots.c.day == ref,
        pool_snapshots.c.estimated.is_(False), pool_snapshots.c.volume_usd_cum.is_not(None),
        pool_snapshots.c.volume_usd_day.is_not(None))).all())
    stat_rows, held = [], []
    for pid in chunk:
        _, w, v, _ = daily.get(pid, {}).get(ref, (0, 0, 0.0, 0))
        hook = hook_of.get(pid)
        if pid in held_back and pid in closed_volume:
            # A full same-day pipeline rerun resets pool_stats to rolling keyless data. Restore the accepted closed-day
            # snapshot here even when no fetch is needed, so wash rules never judge rolling USD under a UTC-day boundary.
            stat_rows.append({"pool_id": pid, "chain_id": cid, "day": ctx.day, "vol_24h": closed_volume[pid],
                              "traders_24h": w, "wallets_24h": w, "window_end": window_end,
                              "vol_source": "keyless_candle" if pid in candle_stats else "closed_day"})
        elif pid in held_back:
            held.append({"pool_id": pid, "chain_id": cid, "day": ctx.day, "vol_24h": None, "traders_24h": w,
                         "wallets_24h": w, "window_end": window_end,
                         "vol_source": "keyless_candle" if pid in candle_stats else "hook" if hook and custom_accounting(hook) else "unpriced"})
        else:
            stat_rows.append({"pool_id": pid, "chain_id": cid, "day": ctx.day, "vol_24h": v, "traders_24h": w,
                              "wallets_24h": w, "window_end": window_end,
                              "vol_source": "hook_total" if pid.startswith(HOOK_POOL_PREFIX) else "onchain"})
    keys = ["pool_id", "chain_id", "day"]
    for i in range(0, len(stat_rows), CHUNK):
        upsert(conn, pool_stats, stat_rows[i:i + CHUNK], keys=keys, update=["vol_24h", "traders_24h", "wallets_24h", "window_end", "vol_source"])
    for i in range(0, len(held), CHUNK):  # keyless vol_24h survives; a new row gets NULL (unknown), never $0
        upsert(conn, pool_stats, held[i:i + CHUNK], keys=keys, update=["traders_24h", "wallets_24h", "window_end", "vol_source"])
    out["stats"] += len(stat_rows) + len(held)
    out["vol_unknown"] += len(held)


def _swap_counts(conn, cid: int, pids: list[str], daily: dict, start: date, ref: date) -> None:
    """Pools whose keyless series is kept (history shorter than the window, or volume held back) still get exact swap
    counts: on a ready chain the indexer sees every swap, so a day without a row is 0. Only existing rows are updated."""
    if not pids or ref < start:
        return
    params, d = [], start
    while d <= ref:
        params += [{"p": pid, "d": d, "n": daily.get(pid, {}).get(d, (0, 0, 0.0, 0))[0]} for pid in pids]
        d += timedelta(days=1)
    conn.execute(sa.update(pool_snapshots)
                 .where(pool_snapshots.c.chain_id == cid, pool_snapshots.c.pool_id == sa.bindparam("p"),
                        pool_snapshots.c.day == sa.bindparam("d"))
                 .values(tx_count_day=sa.bindparam("n")), params)
