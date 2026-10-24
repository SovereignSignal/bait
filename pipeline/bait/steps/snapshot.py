"""Step 2: per chain with a confirmed subgraph, paginate hooked pools -> pools + pool_snapshots(day - 1); keyless for the rest."""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa

from ..config import CHAINS
from ..db import hooks, pool_snapshots, pool_stats, pools, savepoint, upsert
from ..rules import flags_from_address, is_self_priced_supply, symbols_swapped
from . import RunContext, log

SERIES_DAYS = 15          # candles per pool (fetch_ohlcv limit=15): the 14 closed days score reads plus the running day
SNAP_KEYS = ["pool_id", "chain_id", "day"]
BASE_LOOKBACK_DAYS = 30   # how far back a legacy cumulative is continued from


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    g = ctx.client("graph")
    out = {"chains": 0, "skipped": 0, "pools": 0, "errors": 0, "keyless_chains": 0, "keyless_pools": 0}
    wanted = [ch for ch in CHAINS if not ctx.chains or ch.chain_id in ctx.chains]
    covered: set[int] = set()
    for ch in wanted:
        if g is None or not ch.subgraph_id:
            continue
        try:
            with savepoint(conn):
                n = _snapshot_chain(conn, ctx, g, ch)
            out["chains"] += 1
            out["pools"] += n
            covered.add(ch.chain_id)
            log.info("snapshot chain=%s pools=%d", ch.slug, n)
        except Exception as e:  # one bad subgraph must not kill the night
            out["errors"] += 1
            log.warning("snapshot chain=%s failed: %s", ch.slug, e)
    out["symbols_fixed"] = _fix_swapped_symbols(conn)
    rest = [ch.chain_id for ch in wanted if ch.chain_id not in covered]
    if rest and os.environ.get("KEYLESS", "1") != "0":
        try:
            with savepoint(conn):
                _keyless(conn, ctx, rest, out)
        except Exception as e:
            out["errors"] += 1
            log.warning("snapshot keyless failed: %s", e)
    else:
        out["skipped"] += len(rest)
    return out


def _keyless(conn, ctx: RunContext, chain_ids: list[int], out: dict) -> None:
    """Chains with no subgraph (or no GRAPH_API_KEY): GeckoTerminal + on-chain hook resolution. See keyless.py.

    Every value goes to the UTC day it belongs to (score.py: windows end the day before the run). Candles give each day's
    volume. A pool past the OHLCV cap only has GeckoTerminal's rolling 24h volume: it is written as yesterday's, marked
    estimated, and no other day is touched. Rolling buys + sells become yesterday's swap count (an estimate too)."""
    from .. import keyless as K
    nets = [net for net, (cid, *_rest) in K.CHAINS.items() if cid in chain_ids]
    log.info("snapshot keyless chains=%s", ",".join(K.CHAINS[n][1] for n in nets))
    http = keyless_http(ctx)
    # `or default`: a copied .env.example leaves these set but empty, which means the default, not a crash
    found = K.collect(http, int(os.environ.get("KEYLESS_PAGES", "") or 4), int(os.environ.get("KEYLESS_NEW_PAGES", "") or 2),
                      nets=nets)
    K.resolve_hooks(http, found)
    K.resolve_via_logs(http, found)
    hooked = [p for p in found.values() if p.get("key") and int(p["key"]["hooks"], 16) != 0]
    K.fetch_ohlcv(http, hooked, int(os.environ.get("KEYLESS_OHLCV_CAP", "") or 70))

    end = ctx.day - timedelta(days=1)
    known = {(r.address, r.chain_id) for r in conn.execute(sa.select(hooks.c.address, hooks.c.chain_id))}
    new_hooks, pool_rows, snap_rows, stat_rows, capped, swaps = {}, [], [], [], [], {}
    self_priced = 0
    for p in hooked:
        a, k = p["attrs"], p["key"]
        cid = K.CHAINS[p["net"]][0]
        pid, hook = a["address"].lower(), k["hooks"].lower()
        if (hook, cid) not in known and (hook, cid) not in new_hooks:
            new_hooks[(hook, cid)] = {"address": hook, "chain_id": cid, "name": None, "net": p["net"],
                                      "description": "", "flags": flags_from_address(hook), "properties": {}, "hooklisted": False,
                                      "verified_source": None,
                                      "audit_url": None, "deployer": None, "first_seen_at": ctx.now}
        sym0, sym1 = pool_symbols(a.get("name", ""), p.get("tokens"), k["currency0"], k["currency1"])
        pool_rows.append({"pool_id": pid, "chain_id": cid, "hook_address": hook,
                          "token0": {"id": k["currency0"], "symbol": sym0}, "token1": {"id": k["currency1"], "symbol": sym1},
                          "fee": k["fee"], "tick_spacing": k["tickSpacing"],
                          "created_at": _ts(a.get("pool_created_at"))})
        tvl = _float(a.get("reserve_in_usd"))   # missing reserve: unknown TVL, never a known $0
        tx = a.get("transactions", {}).get("h24", {})
        counts = {f"{n}_24h": _int(tx.get(n)) for n in ("buys", "sells", "buyers", "sellers")}
        vol24 = float(a["volume_usd"]["h24"] or 0)
        if is_self_priced_supply(tvl, _float(a.get("fdv_usd")), vol24):
            tvl = None                          # a launch's own supply at its own price, idle: not liquidity (rules.py)
            self_priced += 1
        # vol_source=None (keyless): a same-day rerun must not leave the onchain step's label on a keyless number.
        # traders_24h = buyers + sellers: an upper bound of distinct wallets (one that bought and sold counts twice).
        stat_rows.append({"pool_id": pid, "chain_id": cid, "day": ctx.day, "vol_24h": vol24, "vol_24h_rolling": vol24,
                          "traders_24h": (counts["buyers_24h"] or 0) + (counts["sellers_24h"] or 0), "wallets_24h": None,
                          "window_end": ctx.now, "vol_source": None, **counts})
        swaps[(pid, cid)] = (counts["buys_24h"] or 0) + (counts["sells_24h"] or 0)
        if p.get("daily"):
            snap_rows += _candle_rows(pid, cid, p["daily"], tvl, ctx.day)
        else:
            capped.append((pid, cid, tvl, vol24))
    if new_hooks:
        by_net: dict[str, set[str]] = {}
        for h in new_hooks.values():
            by_net.setdefault(h["net"], set()).add(h["address"])
        codes = K.code_hashes(http, by_net)
        rows = []
        for h in new_hooks.values():
            net = h.pop("net")
            rows.append({**h, "code_hash": codes.get((net, h["address"]))})
        upsert(conn, hooks, rows, keys=["address", "chain_id"], update=[])
    for i in range(0, len(pool_rows), 500):
        upsert(conn, pools, pool_rows[i:i + 500], keys=["pool_id", "chain_id"])
    vol_cols = ["tvl_usd", "volume_usd_cum", "volume_usd_day", "estimated"]
    for i in range(0, len(snap_rows), 500):
        upsert(conn, pool_snapshots, snap_rows[i:i + 500], keys=SNAP_KEYS, update=vol_cols)
    before = _before(conn, list(swaps), end)
    est = _carry_forward(capped, end, before)
    for i in range(0, len(est), 500):
        upsert(conn, pool_snapshots, est[i:i + 500], keys=SNAP_KEYS, update=vol_cols)
    _rolling_swaps(conn, swaps, end, before)
    for i in range(0, len(stat_rows), 500):
        upsert(conn, pool_stats, stat_rows[i:i + 500], keys=SNAP_KEYS)
    out["keyless_chains"] = len(nets)
    out["keyless_pools"] = len(pool_rows)
    out["estimated_pools"] = len(est)
    out["tvl_self_priced"] = self_priced
    out["pools"] += len(pool_rows)
    log.info("snapshot keyless hooked_pools=%d unlisted_hooks=%d estimated=%d gt_calls=%d", len(pool_rows), len(new_hooks),
             len(est), http.gt_calls)


def _candle_rows(pid: str, cid: int, candles, tvl: float | None, day: date) -> list[dict]:
    """Daily candles (UTC days) -> one row per day from the first candle (at most SERIES_DAYS back) through `day`, the
    running day: that day's candle volume, $0 on a day with no candle (no trades). The running day's row is partial, so it
    is estimated (a capped night replaces it). Days before the first candle are never written. volume_usd_cum (legacy) is
    the running sum from the first row."""
    vols = {datetime.fromtimestamp(t, tz=timezone.utc).date(): float(v) for t, v in candles}
    d = max(min(vols), day - timedelta(days=SERIES_DAYS - 1))
    rows, cum = [], 0.0
    while d <= day:
        v = vols.get(d, 0.0)
        cum += v
        rows.append({"pool_id": pid, "chain_id": cid, "day": d, "tvl_usd": tvl, "volume_usd_cum": cum, "volume_usd_day": v,
                     "estimated": d >= day})   # the running day's candle is partial: replaceable tomorrow
        d += timedelta(days=1)
    return rows


def _before(conn, keys: list[tuple[str, int]], end: date) -> dict[tuple[str, int], tuple]:
    """Per pool: (last volume_usd_cum before `end`, last tx_count_cum before `end`, whether `end` holds a candle row).
    estimated NULL (a row from before 0012) is not a candle row: it is replaced."""
    want, out = set(keys), {}
    pids = sorted({p for p, _ in keys})
    for i in range(0, len(pids), 500):
        q = (sa.select(pool_snapshots.c.pool_id, pool_snapshots.c.chain_id, pool_snapshots.c.day, pool_snapshots.c.volume_usd_cum,
                       pool_snapshots.c.tx_count_cum, pool_snapshots.c.estimated)
             .where(pool_snapshots.c.pool_id.in_(pids[i:i + 500]), pool_snapshots.c.day <= end,
                    pool_snapshots.c.day >= end - timedelta(days=BASE_LOOKBACK_DAYS))
             .order_by(pool_snapshots.c.day))
        for r in conn.execute(q):
            k = (r.pool_id, r.chain_id)
            if k not in want:
                continue
            vol, txs, candle = out.get(k, (None, None, False))
            if r.day < end:
                out[k] = (r.volume_usd_cum if r.volume_usd_cum is not None else vol,
                          r.tx_count_cum if r.tx_count_cum is not None else txs, candle)
            else:
                out[k] = (vol, txs, r.estimated is False)
    return out


def _carry_forward(capped: list[tuple[str, int, float | None, float]], end: date, before: dict) -> list[dict]:
    """Rows for pools past the OHLCV cap: GeckoTerminal's rolling 24h volume (it ends at run time, about 6 h into today) as
    `end`'s volume, estimated. Only `end` is written, so a pool that leaves the cap never zeroes its history, and a candle
    row already there wins. volume_usd_cum (legacy) continues from the last known value."""
    rows = []
    for pid, cid, tvl, vol24 in capped:
        base, _, candle = before.get((pid, cid), (None, None, False))
        if not candle:
            rows.append({"pool_id": pid, "chain_id": cid, "day": end, "tvl_usd": tvl, "volume_usd_cum": (base or 0.0) + vol24,
                         "volume_usd_day": vol24, "estimated": True})
    return rows


def _rolling_swaps(conn, swaps: dict[tuple[str, int], int], end: date, before: dict) -> None:
    """GeckoTerminal's rolling 24h buys + sells -> `end`'s tx_count_day (an estimate, like the rolling volume; the onchain
    step replaces it with exact counts on ready chains). tx_count_cum (legacy; is_spam's lifetime count) continues from the
    last known value. Only existing `end` rows are touched."""
    params = [{"p": pid, "c": cid, "n": n, "cum": (before.get((pid, cid), (None, None, False))[1] or 0) + n}
              for (pid, cid), n in swaps.items()]
    if params:
        conn.execute(sa.update(pool_snapshots)
                     .where(pool_snapshots.c.pool_id == sa.bindparam("p"), pool_snapshots.c.chain_id == sa.bindparam("c"),
                            pool_snapshots.c.day == end)
                     .values(tx_count_day=sa.bindparam("n"), tx_count_cum=sa.bindparam("cum")), params)


def keyless_http(ctx: RunContext):
    """One GeckoTerminal client per run (shared by snapshot and activity) so call spacing stays in one place; keyed by
    COINGECKO_API_KEY when it is set (keyless.http_from_env).
    Tests inject a fake via ctx.extras["keyless_http"]."""
    from .. import keyless as K
    http = ctx.extras.get("keyless_http")
    if http is None:
        http = K.http_from_env(cache_dir=os.environ.get("KEYLESS_CACHE_DIR") or None)
        ctx.extras["keyless_http"] = http
    return http


def _fix_swapped_symbols(conn) -> int:
    """Put back pool labels written in GeckoTerminal's base/quote order on earlier nights: where a side Bait knows by
    address (native coin, wrapped native, major stablecoins) carries the other side's symbol, swap the two. Pools
    GeckoTerminal lists tonight are rewritten from token ids anyway; this covers the ones it no longer lists."""
    fixes = []
    for r in conn.execute(sa.select(pools.c.pool_id, pools.c.chain_id, pools.c.token0, pools.c.token1)
                          .where(pools.c.token0.is_not(None), pools.c.token1.is_not(None))):
        if isinstance(r.token0, dict) and isinstance(r.token1, dict) and symbols_swapped(r.chain_id, r.token0, r.token1):
            fixes.append({"k_pool": r.pool_id, "k_chain": r.chain_id,
                          "t0": {**r.token0, "symbol": r.token1.get("symbol")}, "t1": {**r.token1, "symbol": r.token0.get("symbol")}})
    if fixes:
        conn.execute(pools.update().where(pools.c.pool_id == sa.bindparam("k_pool"), pools.c.chain_id == sa.bindparam("k_chain"))
                     .values(token0=sa.bindparam("t0"), token1=sa.bindparam("t1")), fixes)
        log.info("snapshot: %d pool labels put back on the right token", len(fixes))
    return len(fixes)


def _name_symbols(name: str) -> tuple[str | None, str | None]:
    """GeckoTerminal's "BASE / QUOTE 0.3%" -> (base, quote); a symbol may contain spaces, only the fee tier is cut."""
    if " / " not in name:
        return None, None
    a, b = name.split(" / ", 1)
    b = re.sub(r"\s+\d+(\.\d+)?%$", "", b.strip())
    return a.strip() or None, b.strip() or None


def pool_symbols(name: str, tokens, currency0: str, currency1: str) -> tuple[str | None, str | None]:
    """Symbols for currency0 and currency1, matched through GeckoTerminal's base and quote token addresses. Native ETH
    (currency 0x0) has its own GeckoTerminal id, so a side that matches neither takes the other symbol when exactly one
    side matched. Without token ids the order is unknown: no symbols (the web resolves them on-chain), never a guess.
    2026-10-16 audit: assigning base/quote by position mislabelled about 10% of pools (USDC as USDG, ETH as the launch
    token) and the unlisted names built from them."""
    base, quote = _name_symbols(name)
    b_addr, q_addr = tokens or (None, None)
    if not (b_addr or q_addr):
        return None, None
    by_addr = {x: s for x, s in ((b_addr, base), (q_addr, quote)) if x}
    s0, s1 = by_addr.get(currency0.lower()), by_addr.get(currency1.lower())
    if s0 is None and s1 is not None:
        s0 = quote if s1 == base else base
    elif s1 is None and s0 is not None:
        s1 = quote if s0 == base else base
    return s0, s1


def _ts(iso: str | None):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None


def _snapshot_chain(conn, ctx, g, ch) -> int:
    try:
        meta = g.meta(ch.subgraph_id)
        if meta.get("hasIndexingErrors"):
            log.warning("snapshot chain=%s subgraph hasIndexingErrors=true block=%s", ch.slug, meta.get("block", {}).get("number"))
    except Exception as e:
        log.warning("snapshot chain=%s _meta unavailable: %s", ch.slug, e)
    # The cumulative is read at run time (06:00 UTC): it is written as the closed day `end`, about 6 h late (estimated).
    end = ctx.day - timedelta(days=1)
    prev = {r.pool_id: r for r in conn.execute(
        sa.select(pool_snapshots.c.pool_id, pool_snapshots.c.volume_usd_cum, pool_snapshots.c.tx_count_cum)
        .where(pool_snapshots.c.chain_id == ch.chain_id, pool_snapshots.c.day == end - timedelta(days=1)))}
    pool_rows, snap_rows = [], []
    for p in g.hooked_pools(ch.subgraph_id):
        pid = p["id"].lower()
        pool_rows.append({
            "pool_id": pid, "chain_id": ch.chain_id, "hook_address": p["hooks"].lower(),
            "token0": p.get("token0"), "token1": p.get("token1"),
            "fee": _int(p.get("feeTier")), "tick_spacing": _int(p.get("tickSpacing")),
            "created_at": datetime.fromtimestamp(int(p["createdAtTimestamp"]), tz=timezone.utc) if p.get("createdAtTimestamp") else None,
        })
        vol, txs, b = _float(p.get("volumeUSD")), _int(p.get("txCount")), prev.get(pid)
        snap_rows.append({
            "pool_id": pid, "chain_id": ch.chain_id, "day": end, "tvl_usd": _float(p.get("totalValueLockedUSD")),
            "volume_usd_cum": vol, "tx_count_cum": txs,   # txCount includes liquidity modifications
            "volume_usd_day": _delta(vol, b.volume_usd_cum if b else None),
            "tx_count_day": _delta(txs, b.tx_count_cum if b else None), "estimated": True,
        })
    for i in range(0, len(pool_rows), 500):
        upsert(conn, pools, pool_rows[i:i + 500], keys=["pool_id", "chain_id"])
        upsert(conn, pool_snapshots, snap_rows[i:i + 500], keys=["pool_id", "chain_id", "day"])
    return len(pool_rows)


def _int(v):
    return int(v) if v not in (None, "") else None


def _float(v):
    return float(v) if v not in (None, "") else None


def _delta(cur, before):
    """That day's amount from two cumulatives; None (unknown) when either is missing, never the all-time total."""
    return None if cur is None or before is None else max(cur - before, 0)
