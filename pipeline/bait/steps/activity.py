"""Step 3 (activity): who trades each hooked pool.

1. Pools: today's pool_stats joined to pools, top ACTIVITY_POOLS by 24h volume among pools the bare 24h wash test
   (rules.is_wash_24h on a wallet lower bound) does not flag, then up to ACTIVITY_WASH_POOLS (10) of the flagged ones:
   the symmetric-trader signal in score needs their top wallets.
2. Trades: GeckoTerminal /pools/{id}/trades (last ~300 trades in 24h, keyless) through the run's shared keyless.Http.
   Aggregated per wallet (tx_from_address); top 20 by volume -> pool_top_wallets(day). Rows older than 14 days deleted.
   When the call returned fewer than 300 trades it holds every trade of the last 24 h, so pool_stats.wallets_24h gets the
   exact distinct count.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import sqlalchemy as sa

from ..db import HOOK_POOL_PREFIX, as_utc, pool_stats, pool_top_wallets, pools, savepoint, upsert
from ..rules import is_wash_24h, wallets_lower_bound
from . import RunContext, log

TOP_WALLETS = 20
RETAIN_DAYS = 14
TRADES_PER_CALL = 300   # GeckoTerminal /trades returns at most this many, all from the last 24 h


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"pools": 0, "pools_fetched": 0, "pools_no_trades": 0, "trades": 0, "wallets": 0, "pruned": 0,
           "wallets_distinct": 0, "wash_pools": 0, "errors": 0}
    from .. import keyless as K
    from .snapshot import keyless_http

    out["pruned"] = conn.execute(sa.delete(pool_top_wallets).where(pool_top_wallets.c.day < ctx.day - timedelta(days=RETAIN_DAYS))).rowcount or 0

    selected, wash = select_pools(conn, ctx, _env_int("ACTIVITY_POOLS", 30), _env_int("ACTIVITY_WASH_POOLS", 10))
    out["pools"], out["wash_pools"] = len(selected), len(wash)
    selected_fetch = selected = selected + wash
    if selected and "keyless_http" not in ctx.extras and os.environ.get("KEYLESS", "1") == "0":
        log.info("activity: KEYLESS=0, trades skipped")
        selected_fetch = []
    net_by_chain = {cid: net for net, (cid, *_rest) in K.CHAINS.items()}
    http = keyless_http(ctx) if selected_fetch else None
    for pid, cid in selected_fetch:
        net = net_by_chain.get(cid)
        if net is None:
            continue
        try:
            with savepoint(conn):
                trades = K.fetch_trades(http, net, pid)
                if trades is None:
                    out["pools_no_trades"] += 1
                    continue
                rows = aggregate(trades, pid, cid, ctx.day)
                conn.execute(sa.delete(pool_top_wallets).where(pool_top_wallets.c.pool_id == pid, pool_top_wallets.c.chain_id == cid,
                                                               pool_top_wallets.c.day == ctx.day))
                upsert(conn, pool_top_wallets, rows, keys=["pool_id", "chain_id", "day", "address"])
                if len(trades) < TRADES_PER_CALL:   # every trade of the last 24 h: an exact distinct count for this pool
                    conn.execute(sa.update(pool_stats).where(pool_stats.c.pool_id == pid, pool_stats.c.chain_id == cid,
                                                             pool_stats.c.day == ctx.day).values(wallets_24h=distinct_wallets(trades)))
                    out["wallets_distinct"] += 1
                out["pools_fetched"] += 1
                out["trades"] += len(trades)
                out["wallets"] += len(rows)
        except Exception as e:  # one pool must not kill the night
            out["errors"] += 1
            log.warning("activity pool=%s chain=%s failed: %s", pid, cid, e)
    return out


def select_pools(conn, ctx: RunContext, limit: int, wash_limit: int = 0) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """(top `limit` pools the bare 24h wash test does not flag, top `wash_limit` it does), each by 24h volume."""
    q = (sa.select(pool_stats)
         .join(pools, (pools.c.pool_id == pool_stats.c.pool_id) & (pools.c.chain_id == pool_stats.c.chain_id))
         .where(pool_stats.c.day == ctx.day, pool_stats.c.pool_id.not_like(HOOK_POOL_PREFIX + "%")))   # no trades feed
    if ctx.chains:
        q = q.where(pool_stats.c.chain_id.in_(ctx.chains))
    rows = sorted(conn.execute(q), key=lambda r: -(r.vol_24h or 0.0))
    flagged = [is_wash_24h(r.vol_24h, wallets_lower_bound(r.wallets_24h, r.buyers_24h, r.sellers_24h, r.traders_24h)) for r in rows]
    keep = [(r.pool_id, r.chain_id) for r, f in zip(rows, flagged) if not f]
    wash = [(r.pool_id, r.chain_id) for r, f in zip(rows, flagged) if f]
    return keep[:max(limit, 0)], wash[:max(wash_limit, 0)]


def distinct_wallets(trades: list[dict]) -> int:
    return len({str(t.get("tx_from_address") or "").strip().lower() for t in trades} - {""})


def aggregate(trades: list[dict], pool_id: str, chain_id: int, day) -> list[dict]:
    """Per-wallet totals from GeckoTerminal trade attributes; top TOP_WALLETS by volume."""
    agg: dict[str, dict] = {}
    for t in trades:
        addr = str(t.get("tx_from_address") or "").strip().lower()
        if not addr:
            continue
        a = agg.setdefault(addr, {"pool_id": pool_id, "chain_id": chain_id, "day": day, "address": addr,
                                  "volume_usd": 0.0, "trades": 0, "buys": 0, "sells": 0, "last_trade_at": None})
        try:
            a["volume_usd"] += float(t.get("volume_in_usd") or 0)
        except (TypeError, ValueError):
            pass
        a["trades"] += 1
        kind = str(t.get("kind") or "").lower()
        if kind == "buy":
            a["buys"] += 1
        elif kind == "sell":
            a["sells"] += 1
        ts = _ts(t.get("block_timestamp"))
        if ts is not None and (a["last_trade_at"] is None or ts > a["last_trade_at"]):
            a["last_trade_at"] = ts
    ranked = sorted(agg.values(), key=lambda a: (-a["volume_usd"], -a["trades"], a["address"]))
    return ranked[:TOP_WALLETS]


def _ts(v) -> datetime | None:
    if not v:
        return None
    try:
        return as_utc(datetime.fromisoformat(str(v).replace("Z", "+00:00")))
    except ValueError:
        return None
