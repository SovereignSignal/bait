"""Indexer writes. Every batch (one HyperSync page or one RPC window) is written together with its cursor in one
transaction, so a restart resumes exactly where the last committed batch ended: nothing skipped, nothing counted twice."""
from __future__ import annotations

from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from ..db import indexer_state, pool_daily_onchain, pool_wallet_day, upsert, utcnow, v4_pools

CHUNK = 500
WALLET_RETAIN_DAYS = 30


def _ins(conn):
    return postgresql.insert if conn.dialect.name == "postgresql" else sqlite.insert


def get_state(conn: sa.Connection, chain_id: int):
    return conn.execute(sa.select(indexer_state).where(indexer_state.c.chain_id == chain_id)).first()


def set_state(conn: sa.Connection, chain_id: int, **fields) -> None:
    row = {"chain_id": chain_id, **fields, "updated_at": utcnow()}
    upsert(conn, indexer_state, [row], keys=["chain_id"])


def upsert_pools(conn: sa.Connection, rows: list[dict]) -> int:
    for i in range(0, len(rows), CHUNK):
        upsert(conn, v4_pools, rows[i:i + CHUNK], keys=["chain_id", "pool_id"])
    return len(rows)


def pool_count(conn: sa.Connection, chain_id: int) -> int:
    return conn.execute(sa.select(sa.func.count()).select_from(v4_pools).where(v4_pools.c.chain_id == chain_id)).scalar() or 0


def pools_by_id(conn: sa.Connection, chain_id: int, pool_ids) -> dict[str, tuple[str, str]]:
    """Which of these pool ids are hooked pools we know -> (currency0, currency1)."""
    ids = sorted(set(pool_ids))
    out = {}
    for i in range(0, len(ids), CHUNK):
        for r in conn.execute(sa.select(v4_pools.c.pool_id, v4_pools.c.currency0, v4_pools.c.currency1)
                              .where(v4_pools.c.chain_id == chain_id, v4_pools.c.pool_id.in_(ids[i:i + CHUNK]))):
            out[r.pool_id] = (r.currency0, r.currency1)
    return out


def all_pools(conn: sa.Connection, chain_id: int) -> dict[str, tuple[str, str]]:
    return {r.pool_id: (r.currency0, r.currency1) for r in conn.execute(
        sa.select(v4_pools.c.pool_id, v4_pools.c.currency0, v4_pools.c.currency1)
        .where(v4_pools.c.chain_id == chain_id).order_by(v4_pools.c.pool_id))}


def reset_swaps_from(conn: sa.Connection, chain_id: int, day: date) -> None:
    """Swap counting (re)starts at the first block of `day`: drop what was counted from that day on so the
    incremental upserts below never double count."""
    conn.execute(sa.delete(pool_daily_onchain).where(pool_daily_onchain.c.chain_id == chain_id, pool_daily_onchain.c.day >= day))
    conn.execute(sa.delete(pool_wallet_day).where(pool_wallet_day.c.chain_id == chain_id, pool_wallet_day.c.day >= day))


def apply_swaps(conn: sa.Connection, chain_id: int, daily: dict[tuple[str, date], list], wallets: dict[tuple[str, date, str], int]) -> None:
    """daily: (pool_id, day) -> [swaps, volume_usd]; wallets: (pool_id, day, address) -> swaps. Adds to existing rows,
    then recounts pool_daily_onchain.wallets for the touched (pool, day) keys from pool_wallet_day."""
    ins = _ins(conn)
    wrows = [{"chain_id": chain_id, "pool_id": p, "day": d, "address": a, "swaps": n} for (p, d, a), n in wallets.items()]
    for i in range(0, len(wrows), CHUNK):
        stmt = ins(pool_wallet_day).values(wrows[i:i + CHUNK])
        stmt = stmt.on_conflict_do_update(index_elements=["chain_id", "pool_id", "day", "address"],
                                          set_={"swaps": pool_wallet_day.c.swaps + stmt.excluded.swaps})
        conn.execute(stmt)
    drows = [{"chain_id": chain_id, "pool_id": p, "day": d, "swaps": s, "wallets": 0, "volume_usd": v}
             for (p, d), (s, v) in daily.items()]
    for i in range(0, len(drows), CHUNK):
        stmt = ins(pool_daily_onchain).values(drows[i:i + CHUNK])
        stmt = stmt.on_conflict_do_update(index_elements=["chain_id", "pool_id", "day"], set_={
            "swaps": pool_daily_onchain.c.swaps + stmt.excluded.swaps,
            "volume_usd": pool_daily_onchain.c.volume_usd + stmt.excluded.volume_usd})
        conn.execute(stmt)
    by_day: dict[date, list[str]] = {}
    for p, d in daily:
        by_day.setdefault(d, []).append(p)
    for d, pids in by_day.items():
        for i in range(0, len(pids), CHUNK):
            chunk = pids[i:i + CHUNK]
            counts = conn.execute(
                sa.select(pool_wallet_day.c.pool_id, sa.func.count())
                .where(pool_wallet_day.c.chain_id == chain_id, pool_wallet_day.c.day == d, pool_wallet_day.c.pool_id.in_(chunk))
                .group_by(pool_wallet_day.c.pool_id)).all()
            if counts:
                conn.execute(
                    sa.update(pool_daily_onchain)
                    .where(pool_daily_onchain.c.chain_id == chain_id, pool_daily_onchain.c.day == d,
                           pool_daily_onchain.c.pool_id == sa.bindparam("p"))
                    .values(wallets=sa.bindparam("n")),
                    [{"p": pid, "n": n} for pid, n in counts])


def prune_wallets(conn: sa.Connection, today: date) -> int:
    return conn.execute(sa.delete(pool_wallet_day).where(pool_wallet_day.c.day < today - timedelta(days=WALLET_RETAIN_DAYS))).rowcount or 0
