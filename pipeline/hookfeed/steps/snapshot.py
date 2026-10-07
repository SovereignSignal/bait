"""Step 2: per chain with a confirmed subgraph, paginate hooked pools -> pools + pool_snapshots(day)."""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from ..config import CHAINS
from ..db import pool_snapshots, pools, upsert
from . import RunContext, log


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    g = ctx.client("graph")
    out = {"chains": 0, "skipped": 0, "pools": 0, "errors": 0}
    if g is None:
        out["skipped"] = len(CHAINS)
        return out
    for ch in CHAINS:
        if ctx.chains and ch.chain_id not in ctx.chains:
            continue
        if not ch.subgraph_id:
            log.info("snapshot chain=%s skipped: no public v4 subgraph", ch.slug)
            out["skipped"] += 1
            continue
        try:
            n = _snapshot_chain(conn, ctx, g, ch)
            out["chains"] += 1
            out["pools"] += n
            log.info("snapshot chain=%s pools=%d", ch.slug, n)
        except Exception as e:  # one bad subgraph must not kill the night
            out["errors"] += 1
            log.warning("snapshot chain=%s failed: %s", ch.slug, e)
    return out


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
