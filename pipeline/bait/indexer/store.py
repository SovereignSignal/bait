"""Indexer writes. Every batch (one HyperSync page or one RPC window) is written together with its cursor in one
transaction, so a restart resumes exactly where the last committed batch ended: nothing skipped, nothing counted twice."""
from __future__ import annotations

from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from ..db import (HOOK_POOL_PREFIX, as_utc, hook_pool_counts, indexer_state, pool_daily_onchain, pool_lookup_queue, pool_wallet_day,
                  upsert, utcnow, v4_pools)

CHUNK = 500
WALLET_RETAIN_DAYS = 30


def _ins(conn):
    return postgresql.insert if conn.dialect.name == "postgresql" else sqlite.insert


def get_state(conn: sa.Connection, chain_id: int):
    return conn.execute(sa.select(indexer_state).where(indexer_state.c.chain_id == chain_id)).first()


def set_state(conn: sa.Connection, chain_id: int, **fields) -> None:
    row = {"chain_id": chain_id, **fields, "updated_at": utcnow()}
    upsert(conn, indexer_state, [row], keys=["chain_id"])


class CursorMoved(RuntimeError):
    """Another indexer process advanced this chain's swap cursor first. The page's transaction rolls back: counted once."""


def advance_swap_cursor(conn: sa.Connection, chain_id: int, expected: int, new: int, head: int, block_time=None) -> None:
    """Compare-and-set. Swap aggregates are increments, so the cursor may only move from the value this process read.
    block_time: block timestamp the cursor has reached (a lower bound); None keeps the stored one."""
    vals = {"swap_block": new, "head_block": head, "updated_at": utcnow()}
    if block_time is not None:
        vals["swap_block_time"] = block_time
    r = conn.execute(sa.update(indexer_state)
                     .where(indexer_state.c.chain_id == chain_id, indexer_state.c.swap_block == expected).values(**vals))
    if r.rowcount != 1:
        raise CursorMoved(f"chain_id={chain_id}: swap_block is no longer {expected}")


def upsert_pools(conn: sa.Connection, rows: list[dict]) -> int:
    for i in range(0, len(rows), CHUNK):
        upsert(conn, v4_pools, rows[i:i + CHUNK], keys=["chain_id", "pool_id"])
    return len(rows)


def insert_late_pools(conn: sa.Connection, rows: list[dict]) -> int:
    """Pools that were not rows and just swapped. A row's own "sampled" flag wins (default true: past its hook's cap,
    already counted in hook_pool_counts); a pool missing for another reason comes with sampled = false."""
    ins = _ins(conn)
    for i in range(0, len(rows), CHUNK):
        conn.execute(ins(v4_pools).values([{"sampled": True, **r} for r in rows[i:i + CHUNK]])
                     .on_conflict_do_nothing(index_elements=["chain_id", "pool_id"]))
    return len(rows)


def store_pools(conn: sa.Connection, chain_id: int, rows: list[dict], cap: int) -> tuple[list[dict], list[dict]]:
    """One page's hooked pools under the per-hook cap -> (stored, unstored). A hook keeps at most `cap` v4_pools rows, the
    first to arrive; past that its pools only add to hook_pool_counts.pools_total (every pool of the hook, stored or
    not). Runs inside the page's transaction, before the upsert, so a page that fails rolls its counters back too.
    A capped hook counts only blocks after its last_block: a page seen twice (re-backfill from 0) is counted once.
    cap <= 0: no new caps (hooks already capped keep counting)."""
    if not rows:
        return [], []
    by_hook: dict[str, list[dict]] = {}
    for r in rows:
        by_hook.setdefault(r["hook_address"], []).append(r)
    hooks = sorted(by_hook)
    counters = hook_counters(conn, chain_id, hooks)
    have: dict[str, int] = {}
    open_hooks = [h for h in hooks if h not in counters]
    if cap > 0:
        for i in range(0, len(open_hooks), CHUNK):
            have.update(conn.execute(sa.select(v4_pools.c.hook_address, sa.func.count()).where(
                v4_pools.c.chain_id == chain_id, v4_pools.c.hook_address.in_(open_hooks[i:i + CHUNK]))
                .group_by(v4_pools.c.hook_address)).all())
    stored, unstored, counts = [], [], []
    for h in hooks:
        rs, c = by_hook[h], counters.get(h)
        if c is not None:                      # past the cap: count what is new, store nothing
            fresh = [r for r in rs if c.last_block is None or (r["block_number"] or 0) > c.last_block]
            if fresh:
                unstored += fresh
                counts.append(_counted(chain_id, h, c.pools_total, c.first_block, c.last_block, c.last_seen, fresh))
            continue
        n = have.get(h, 0)
        if cap <= 0 or n + len(rs) <= cap:
            stored += rs
            continue
        # crossing the cap. A row already stored (this page seen before) is neither stored again nor counted twice.
        ids, seen = [r["pool_id"] for r in rs], set()
        for i in range(0, len(ids), CHUNK):
            seen.update(conn.execute(sa.select(v4_pools.c.pool_id).where(
                v4_pools.c.chain_id == chain_id, v4_pools.c.pool_id.in_(ids[i:i + CHUNK]))).scalars())
        new = [r for r in rs if r["pool_id"] not in seen]
        room = max(cap - n, 0)
        stored += new[:room]
        over = new[room:]
        if over:
            unstored += over
            last = max((r["block_number"] for r in rs if r.get("block_number") is not None), default=None)
            seen_at = max((as_utc(r["block_time"]) for r in rs if r.get("block_time") is not None), default=None)
            counts.append(_counted(chain_id, h, n + len(new) - len(over), None, last, seen_at, over))
    upsert(conn, hook_pool_counts, counts, keys=["chain_id", "hook_address"])
    upsert_pools(conn, stored)
    return stored, unstored


def hook_counters(conn: sa.Connection, chain_id: int, hooks) -> dict:
    """hook address -> hook_pool_counts row, for the hooks past the cap among `hooks`."""
    hooks, out = sorted(set(hooks)), {}
    for i in range(0, len(hooks), CHUNK):
        for c in conn.execute(sa.select(hook_pool_counts).where(
                hook_pool_counts.c.chain_id == chain_id, hook_pool_counts.c.hook_address.in_(hooks[i:i + CHUNK]))):
            out[c.hook_address] = c
    return out


def add_hook_usage(conn: sa.Connection, chain_id: int, adds: dict[str, list]) -> None:
    """adds: hook -> [sampled rows inserted, swaps folded into its aggregate pool, their USD]. Increments, in the page's
    transaction (after the compare-and-set), so a page that loses the race adds nothing."""
    rows = [{"c": chain_id, "h": h, "s": s, "n": n, "v": v} for h, (s, n, v) in adds.items() if s or n or v]
    if rows:
        t = hook_pool_counts.c
        conn.execute(sa.update(hook_pool_counts).where(t.chain_id == sa.bindparam("c"), t.hook_address == sa.bindparam("h"))
                     .values(sampled_total=t.sampled_total + sa.bindparam("s"), swaps_total=t.swaps_total + sa.bindparam("n"),
                             volume_usd_total=t.volume_usd_total + sa.bindparam("v")), rows)


def _counted(chain_id: int, hook: str, total: int, first, last, seen_at, rows: list[dict]) -> dict:
    """The hook_pool_counts row after counting `rows` (pools of the hook that are not stored)."""
    blocks = [r["block_number"] for r in rows if r.get("block_number") is not None]
    times = [as_utc(r["block_time"]) for r in rows if r.get("block_time") is not None]
    firsts, lasts = [b for b in (first, *blocks) if b is not None], [b for b in (last, *blocks) if b is not None]
    seens = [t for t in (as_utc(seen_at), *times) if t is not None]
    return {"chain_id": chain_id, "hook_address": hook, "pools_total": total + len(rows),
            "first_block": min(firsts) if firsts else None, "last_block": max(lasts) if lasts else None,
            "last_seen": max(seens) if seens else None}


def widen_lookup_floor(conn: sa.Connection, chain_id: int) -> None:
    """History was reset (an RPC-mode hole, a fresh backfill): pools past a cap may sit below any first_block recorded
    since, so lookups by pool id start at the chain's PoolManager deployment block when recorded (chains.py), else 0."""
    from .chains import CHAIN_BY_ID
    ch = CHAIN_BY_ID.get(chain_id)
    floor = (ch.deploy_block if ch is not None else None) or 0
    t = hook_pool_counts.c
    conn.execute(sa.update(hook_pool_counts).where(t.chain_id == chain_id, sa.or_(t.first_block.is_(None), t.first_block > floor))
                 .values(first_block=floor))


def capped_floor(conn: sa.Connection, chain_id: int) -> int | None:
    """None: no hook on this chain is past the cap, so every hooked pool is a row. Else the oldest block a hooked pool
    that is not a row can have (where the lookup by pool id starts)."""
    n, first = conn.execute(sa.select(sa.func.count(), sa.func.min(hook_pool_counts.c.first_block))
                            .where(hook_pool_counts.c.chain_id == chain_id)).one()
    return None if not n else (first or 0)


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


def queue_lookups(conn: sa.Connection, chain_id: int, owed: dict[str, tuple]) -> None:
    """owed: pool id -> (from_block, seen_block, from_time) for the ids a swap page left uncounted, written in that page's
    transaction. An id already queued keeps its from_block and from_time (its first page left out) and only moves
    seen_block up: pages in between that did not carry it owe nothing."""
    if not owed:
        return
    ins = _ins(conn)
    q = pool_lookup_queue.c
    rows = [{"chain_id": chain_id, "pool_id": p, "from_block": f, "seen_block": s, "from_time": t}
            for p, (f, s, t) in sorted(owed.items())]
    for i in range(0, len(rows), CHUNK):
        stmt = ins(pool_lookup_queue).values(rows[i:i + CHUNK])
        conn.execute(stmt.on_conflict_do_update(index_elements=["chain_id", "pool_id"], set_={
            "seen_block": sa.case((q.seen_block > stmt.excluded.seen_block, q.seen_block), else_=stmt.excluded.seen_block)}))


def queued_lookups(conn: sa.Connection, chain_id: int) -> dict[str, tuple[int, int]]:
    """pool id -> (from_block, seen_block): the owed range of every queued id of the chain, oldest first."""
    q = pool_lookup_queue.c
    return {r.pool_id: (r.from_block, r.seen_block) for r in conn.execute(
        sa.select(q.pool_id, q.from_block, q.seen_block).where(q.chain_id == chain_id).order_by(q.from_block, q.pool_id))}


def lookup_backlog(conn: sa.Connection, chain_id: int) -> tuple[int, int | None]:
    """-> (ids queued, oldest from_block among them)."""
    n, oldest = conn.execute(sa.select(sa.func.count(), sa.func.min(pool_lookup_queue.c.from_block))
                             .where(pool_lookup_queue.c.chain_id == chain_id)).one()
    return n or 0, oldest


def settle_lookups(conn: sa.Connection, chain_id: int, pool_ids) -> int:
    """Delete these ids from the queue -> rows deleted (the caller checks it against what it settled)."""
    ids, n = sorted(set(pool_ids)), 0
    for i in range(0, len(ids), CHUNK):
        n += conn.execute(sa.delete(pool_lookup_queue).where(pool_lookup_queue.c.chain_id == chain_id,
                                                             pool_lookup_queue.c.pool_id.in_(ids[i:i + CHUNK]))).rowcount or 0
    return n


def reset_swaps_from(conn: sa.Connection, chain_id: int, day: date) -> None:
    """Swap counting (re)starts at the first block of `day`: drop what was counted from that day on so the
    incremental upserts below never double count. Days of a hook's aggregate pool also come out of its all-time totals.
    The lookup queue goes too, since the new window queues afresh; owed swaps before `day` are given up with it (a reset
    is a chain's first run or an RPC-mode hole, both rare)."""
    folded = conn.execute(sa.select(pool_daily_onchain.c.pool_id, sa.func.sum(pool_daily_onchain.c.swaps),
                                    sa.func.sum(pool_daily_onchain.c.volume_usd))
                          .where(pool_daily_onchain.c.chain_id == chain_id, pool_daily_onchain.c.day >= day,
                                 pool_daily_onchain.c.pool_id.like(HOOK_POOL_PREFIX + "%"))
                          .group_by(pool_daily_onchain.c.pool_id)).all()
    add_hook_usage(conn, chain_id, {p[len(HOOK_POOL_PREFIX):]: [0, -(n or 0), -(v or 0.0)] for p, n, v in folded})
    conn.execute(sa.delete(pool_daily_onchain).where(pool_daily_onchain.c.chain_id == chain_id, pool_daily_onchain.c.day >= day))
    conn.execute(sa.delete(pool_wallet_day).where(pool_wallet_day.c.chain_id == chain_id, pool_wallet_day.c.day >= day))
    conn.execute(sa.delete(pool_lookup_queue).where(pool_lookup_queue.c.chain_id == chain_id))


def apply_swaps(conn: sa.Connection, chain_id: int, daily: dict[tuple[str, date], list], wallets: dict[tuple[str, date, str], int]) -> None:
    """daily: (pool_id, day) -> [swaps, volume_usd, unpriced_swaps, roundtrip_usd (optional)]; wallets: (pool_id, day, address) -> swaps. Adds to existing rows,
    then recounts pool_daily_onchain.wallets for the touched (pool, day) keys from pool_wallet_day."""
    ins = _ins(conn)
    wrows = [{"chain_id": chain_id, "pool_id": p, "day": d, "address": a, "swaps": n} for (p, d, a), n in wallets.items()]
    for i in range(0, len(wrows), CHUNK):
        stmt = ins(pool_wallet_day).values(wrows[i:i + CHUNK])
        stmt = stmt.on_conflict_do_update(index_elements=["chain_id", "pool_id", "day", "address"],
                                          set_={"swaps": pool_wallet_day.c.swaps + stmt.excluded.swaps})
        conn.execute(stmt)
    drows = [{"chain_id": chain_id, "pool_id": p, "day": d, "swaps": s, "wallets": 0, "volume_usd": v, "unpriced_swaps": u,
              "roundtrip_usd": float(rest[0]) if rest else 0.0}
             for (p, d), (s, v, u, *rest) in daily.items()]
    for i in range(0, len(drows), CHUNK):
        stmt = ins(pool_daily_onchain).values(drows[i:i + CHUNK])
        stmt = stmt.on_conflict_do_update(index_elements=["chain_id", "pool_id", "day"], set_={
            "swaps": pool_daily_onchain.c.swaps + stmt.excluded.swaps,
            "volume_usd": pool_daily_onchain.c.volume_usd + stmt.excluded.volume_usd,
            "unpriced_swaps": pool_daily_onchain.c.unpriced_swaps + stmt.excluded.unpriced_swaps,
            "roundtrip_usd": pool_daily_onchain.c.roundtrip_usd + stmt.excluded.roundtrip_usd})
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
