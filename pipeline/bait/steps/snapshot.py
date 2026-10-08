"""Step 2: per chain with a confirmed subgraph, paginate hooked pools -> pools + pool_snapshots(day)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from ..config import CHAINS
from ..db import hooks, pool_snapshots, pools, upsert
from . import RunContext, log


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    g = ctx.client("graph")
    out = {"chains": 0, "skipped": 0, "pools": 0, "errors": 0, "keyless_chains": 0, "keyless_pools": 0}
    wanted = [ch for ch in CHAINS if not ctx.chains or ch.chain_id in ctx.chains]
    covered: set[int] = set()
    for ch in wanted:
        if g is None or not ch.subgraph_id:
            continue
        try:
            n = _snapshot_chain(conn, ctx, g, ch)
            out["chains"] += 1
            out["pools"] += n
            covered.add(ch.chain_id)
            log.info("snapshot chain=%s pools=%d", ch.slug, n)
        except Exception as e:  # one bad subgraph must not kill the night
            out["errors"] += 1
            log.warning("snapshot chain=%s failed: %s", ch.slug, e)
    rest = [ch.chain_id for ch in wanted if ch.chain_id not in covered]
    if rest and os.environ.get("KEYLESS", "1") != "0":
        try:
            _keyless(conn, ctx, rest, out)
        except Exception as e:
            out["errors"] += 1
            log.warning("snapshot keyless failed: %s", e)
    else:
        out["skipped"] += len(rest)
    return out


def _keyless(conn, ctx: RunContext, chain_ids: list[int], out: dict) -> None:
    """Chains with no subgraph (or no GRAPH_API_KEY): GeckoTerminal + on-chain hook resolution. See keyless.py."""
    from .. import keyless as K
    nets = [net for net, (cid, *_rest) in K.CHAINS.items() if cid in chain_ids]
    log.info("snapshot keyless chains=%s", ",".join(K.CHAINS[n][1] for n in nets))
    http = K.Http(gt_spacing=float(os.environ.get("KEYLESS_SPACING", "10")), cache_dir=os.environ.get("KEYLESS_CACHE_DIR") or None)
    found = K.collect(http, int(os.environ.get("KEYLESS_PAGES", "4")), int(os.environ.get("KEYLESS_NEW_PAGES", "2")), nets=nets)
    K.resolve_hooks(http, found)
    K.resolve_via_logs(http, found)
    hooked = [p for p in found.values() if p.get("key") and int(p["key"]["hooks"], 16) != 0]
    K.fetch_ohlcv(http, hooked, int(os.environ.get("KEYLESS_OHLCV_CAP", "70")))

    known = {(r.address, r.chain_id) for r in conn.execute(sa.select(hooks.c.address, hooks.c.chain_id))}
    new_hooks, pool_rows, snap_rows = {}, [], []
    stats = ctx.extras.setdefault("keyless_pool_stats", {})
    for p in hooked:
        a, k = p["attrs"], p["key"]
        cid = K.CHAINS[p["net"]][0]
        pid, hook = a["address"].lower(), k["hooks"].lower()
        if (hook, cid) not in known and (hook, cid) not in new_hooks:
            new_hooks[(hook, cid)] = {"address": hook, "chain_id": cid, "name": f"Unlisted hook {hook[:6]}…{hook[-4:]}",
                                      "description": "", "flags": {}, "properties": {}, "verified_source": None,
                                      "audit_url": None, "deployer": None, "first_seen_at": ctx.now}
        sym0, sym1 = _symbols(a.get("name", ""))
        pool_rows.append({"pool_id": pid, "chain_id": cid, "hook_address": hook,
                          "token0": {"id": k["currency0"], "symbol": sym0}, "token1": {"id": k["currency1"], "symbol": sym1},
                          "fee": k["fee"], "tick_spacing": k["tickSpacing"],
                          "created_at": _ts(a.get("pool_created_at"))})
        tvl = float(a.get("reserve_in_usd") or 0)
        tx = a.get("transactions", {}).get("h24", {})
        txs24 = int(tx.get("buys", 0)) + int(tx.get("sells", 0))
        vol24 = float(a["volume_usd"]["h24"] or 0)
        stats[(pid, cid)] = (vol24, int(tx.get("buyers", 0)) + int(tx.get("sellers", 0)))
        # Daily candles become a cumulative-volume series so step 5 computes 7d/prev-7d exactly as it does for subgraphs.
        series = [(datetime.fromtimestamp(t, tz=timezone.utc).date(), v) for t, v in (p.get("daily") or [])]
        if not series:
            series = [(ctx.day - timedelta(days=1), 0.0), (ctx.day, vol24)]
        cum = 0.0
        days = {series[0][0] - timedelta(days=1): 0.0}  # cumulative is end-of-day; seed the day before the first candle
        for d, v in series:
            cum += v
            days[d] = cum
        if ctx.day not in days:
            days[ctx.day] = cum
        for d, c in days.items():
            if d > ctx.day:
                continue
            snap_rows.append({"pool_id": pid, "chain_id": cid, "day": d, "tvl_usd": tvl, "volume_usd_cum": c,
                              "tx_count_cum": txs24 if d == ctx.day else 0})
    if new_hooks:
        upsert(conn, hooks, list(new_hooks.values()), keys=["address", "chain_id"], update=[])
    for i in range(0, len(pool_rows), 500):
        upsert(conn, pools, pool_rows[i:i + 500], keys=["pool_id", "chain_id"])
    for i in range(0, len(snap_rows), 500):
        upsert(conn, pool_snapshots, snap_rows[i:i + 500], keys=["pool_id", "chain_id", "day"])
    out["keyless_chains"] = len(nets)
    out["keyless_pools"] = len(pool_rows)
    out["pools"] += len(pool_rows)
    log.info("snapshot keyless hooked_pools=%d unlisted_hooks=%d gt_calls=%d", len(pool_rows), len(new_hooks), http.gt_calls)


def _symbols(name: str) -> tuple[str | None, str | None]:
    if " / " not in name:
        return None, None
    a, b = name.split(" / ", 1)
    return a.strip() or None, (b.split(" ")[0].strip() or None)


def _ts(iso: str | None):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None


def _snapshot_chain(conn, ctx, g, ch) -> int:
    try:
        meta = g.meta(ch.subgraph_id)
        if meta.get("hasIndexingErrors"):
            log.warning("snapshot chain=%s subgraph hasIndexingErrors=true block=%s", ch.slug, meta.get("block", {}).get("number"))
    except Exception as e:
        log.warning("snapshot chain=%s _meta unavailable: %s", ch.slug, e)
    pool_rows, snap_rows = [], []
    for p in g.hooked_pools(ch.subgraph_id):
        pid = p["id"].lower()
        pool_rows.append({
            "pool_id": pid, "chain_id": ch.chain_id, "hook_address": p["hooks"].lower(),
            "token0": p.get("token0"), "token1": p.get("token1"),
            "fee": _int(p.get("feeTier")), "tick_spacing": _int(p.get("tickSpacing")),
            "created_at": datetime.fromtimestamp(int(p["createdAtTimestamp"]), tz=timezone.utc) if p.get("createdAtTimestamp") else None,
        })
        snap_rows.append({
            "pool_id": pid, "chain_id": ch.chain_id, "day": ctx.day,
            "tvl_usd": _float(p.get("totalValueLockedUSD")), "volume_usd_cum": _float(p.get("volumeUSD")),
            "tx_count_cum": _int(p.get("txCount")),
        })
    for i in range(0, len(pool_rows), 500):
        upsert(conn, pools, pool_rows[i:i + 500], keys=["pool_id", "chain_id"])
        upsert(conn, pool_snapshots, snap_rows[i:i + 500], keys=["pool_id", "chain_id", "day"])
    return len(pool_rows)


def _int(v):
    return int(v) if v not in (None, "") else None


def _float(v):
    return float(v) if v not in (None, "") else None
