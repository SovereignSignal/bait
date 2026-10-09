"""Step 3 (activity): who trades each hooked pool.

1. Pools: today's pool_stats joined to pools, wash pools excluded (keyless.is_wash), top ACTIVITY_POOLS by 24h volume.
2. Trades: GeckoTerminal /pools/{id}/trades (last ~300 trades in 24h, keyless) through the run's shared keyless.Http.
   Aggregated per wallet (tx_from_address); top 20 by volume -> pool_top_wallets(day). Rows older than 14 days deleted.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import sqlalchemy as sa

from ..db import as_utc, pool_stats, pool_top_wallets, pools, savepoint, upsert
from . import RunContext, log

TOP_WALLETS = 20
RETAIN_DAYS = 14


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"pools": 0, "pools_fetched": 0, "pools_no_trades": 0, "trades": 0, "wallets": 0, "pruned": 0,
           "errors": 0}
    from .. import keyless as K
    from .snapshot import keyless_http

    out["pruned"] = conn.execute(sa.delete(pool_top_wallets).where(pool_top_wallets.c.day < ctx.day - timedelta(days=RETAIN_DAYS))).rowcount or 0

    selected = select_pools(conn, ctx, _env_int("ACTIVITY_POOLS", 30))
    out["pools"] = len(selected)
    selected_fetch = selected
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
                out["pools_fetched"] += 1
                out["trades"] += len(trades)
                out["wallets"] += len(rows)
        except Exception as e:  # one pool must not kill the night
            out["errors"] += 1
            log.warning("activity pool=%s chain=%s failed: %s", pid, cid, e)
    return out


def select_pools(conn, ctx: RunContext, limit: int) -> list[tuple[str, int]]:
    from ..keyless import is_wash
    q = (sa.select(pool_stats.c.pool_id, pool_stats.c.chain_id, pool_stats.c.vol_24h, pool_stats.c.traders_24h)
         .join(pools, (pools.c.pool_id == pool_stats.c.pool_id) & (pools.c.chain_id == pool_stats.c.chain_id))
         .where(pool_stats.c.day == ctx.day))
    if ctx.chains:
        q = q.where(pool_stats.c.chain_id.in_(ctx.chains))
    rows = [r for r in conn.execute(q) if not is_wash(r.vol_24h or 0.0, r.traders_24h or 0)]
    rows.sort(key=lambda r: -(r.vol_24h or 0.0))
    return [(r.pool_id, r.chain_id) for r in rows[:max(limit, 0)]]


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
