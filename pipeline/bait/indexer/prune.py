"""python -m bait.indexer prune-pools --dry-run | --yes [--keep N] [--chains ...] [--lock-wait S]

One-time trim of v4_pools for hooks that already hold more than N rows (a launchpad's one pool per coin), written before
the per-hook cap (INDEXER_POOLS_PER_HOOK). Per (chain, hook) over N it keeps the newest N rows by block and every pool with
any activity (rows in pool_daily_onchain, pool_snapshots, pool_stats or pool_top_wallets), and deletes the rest together
with their `pools` rows (the nightly copy put every pool of a known hook there). hook_pool_counts records the hook's true
pool count with the first delete, so nothing but the rows is lost; a deleted pool that later swaps for real comes back as
a sampled row. --yes works per chain under the indexer's advisory lock (bait-indexer:<chain_id>), BATCH rows per
transaction, and stops between batches on SIGTERM. It does not VACUUM: Postgres autovacuum makes the space reusable inside
the database, and the volume does not shrink.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa

from .. import db
from ..db import hook_pool_counts, pool_daily_onchain, pool_snapshots, pool_stats, pool_top_wallets, pools, upsert, v4_pools
from .chains import select_chains
from .core import _env_int

log = logging.getLogger("bait.indexer")

BATCH = 5000
ROW_BYTES = 550              # per-row estimate where Postgres cannot say (SQLite, a table never analyzed)
ACTIVITY = (pool_daily_onchain, pool_snapshots, pool_stats, pool_top_wallets)


@dataclass
class Over:
    hook: str
    rows: int                # v4_pools rows of the hook
    last: int | None         # newest block among them
    seen: datetime | None    # newest block time among them
    cut: tuple[int, str]     # (block, pool id) of the newest row that is not kept


def _idle() -> list:
    """No activity row anywhere: never swapped on chain, never snapshotted, no stats, no top wallets."""
    return [~sa.exists().where(t.c.chain_id == v4_pools.c.chain_id, t.c.pool_id == v4_pools.c.pool_id) for t in ACTIVITY]


def _candidates(chain_id: int, o: Over) -> list:
    return [v4_pools.c.chain_id == chain_id, v4_pools.c.hook_address == o.hook, v4_pools.c.block_number.is_not(None),
            sa.tuple_(v4_pools.c.block_number, v4_pools.c.pool_id) <= sa.tuple_(sa.literal(o.cut[0]), sa.literal(o.cut[1])),
            *_idle()]


def hooks_over(conn, chain_id: int, keep: int) -> list[Over]:
    """Hooks of the chain with more than `keep` rows, biggest first."""
    out = []
    n = sa.func.count()
    for r in conn.execute(sa.select(v4_pools.c.hook_address, n.label("n"), sa.func.max(v4_pools.c.block_number).label("last"),
                                    sa.func.max(v4_pools.c.block_time).label("seen"))
                          .where(v4_pools.c.chain_id == chain_id).group_by(v4_pools.c.hook_address)
                          .having(n > keep).order_by(n.desc())):
        cut = conn.execute(sa.select(v4_pools.c.block_number, v4_pools.c.pool_id).where(
            v4_pools.c.chain_id == chain_id, v4_pools.c.hook_address == r.hook_address, v4_pools.c.block_number.is_not(None))
            .order_by(v4_pools.c.block_number.desc(), v4_pools.c.pool_id.desc()).offset(keep).limit(1)).first()
        if cut is not None:
            out.append(Over(r.hook_address, r.n, r.last, db.as_utc(r.seen), (cut.block_number, cut.pool_id)))
    return out


def _db_bytes(engine) -> int | None:
    if engine.dialect.name != "postgresql":
        return None
    with engine.connect() as c:
        return c.execute(sa.text("SELECT pg_database_size(current_database())")).scalar_one()


def _row_bytes(engine, table: str) -> float:
    """Table + index + TOAST bytes per row (pg_class.reltuples), else ROW_BYTES."""
    if engine.dialect.name != "postgresql":
        return ROW_BYTES
    with engine.connect() as c:
        r = c.execute(sa.text("SELECT pg_total_relation_size(c.oid), c.reltuples FROM pg_class c WHERE c.oid = to_regclass(:t)"),
                      {"t": table}).first()
    return r[0] / r[1] if r and r[1] and r[1] > 0 else ROW_BYTES


def _mb(n) -> str:
    return "?" if n is None else f"{n / 1024 ** 2:.1f} MB"


def _lock(engine, chain_id: int, wait: float, stop):
    deadline = time.monotonic() + wait
    while True:
        lock = db.try_advisory_lock(engine, f"bait-indexer:{chain_id}")
        if lock is not None or time.monotonic() >= deadline or stop():
            return lock
        time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))


def _prune_hook(engine, chain_id: int, o: Over, batch: int, stop, out: dict) -> int:
    deleted, last = 0, None
    while not stop():
        with engine.begin() as conn:
            q = sa.select(v4_pools.c.pool_id, v4_pools.c.block_number).where(*_candidates(chain_id, o))
            if last is not None:
                q = q.where(sa.tuple_(v4_pools.c.block_number, v4_pools.c.pool_id) > sa.tuple_(sa.literal(last[0]), sa.literal(last[1])))
            rows = conn.execute(q.order_by(v4_pools.c.block_number, v4_pools.c.pool_id).limit(batch)).all()
            if not rows:
                break
            last = (rows[-1].block_number, rows[-1].pool_id)
            gone = conn.execute(sa.delete(v4_pools).where(v4_pools.c.chain_id == chain_id,
                                                          v4_pools.c.pool_id.in_([r.pool_id for r in rows]), *_idle())
                                .returning(v4_pools.c.pool_id, v4_pools.c.block_number)).all()
            if not gone:
                continue
            ids = [g.pool_id for g in gone]
            out["pools_rows"] += conn.execute(sa.delete(pools).where(pools.c.chain_id == chain_id, pools.c.pool_id.in_(ids))).rowcount or 0
            first = min(g.block_number for g in gone)
            c = conn.execute(sa.select(hook_pool_counts).where(hook_pool_counts.c.chain_id == chain_id,
                                                               hook_pool_counts.c.hook_address == o.hook)).first()
            upsert(conn, hook_pool_counts, [{
                "chain_id": chain_id, "hook_address": o.hook, "pools_total": max(o.rows, c.pools_total if c else 0),
                "first_block": min(first, c.first_block) if c and c.first_block is not None else first,
                "last_block": c.last_block if c and c.last_block is not None else o.last,
                "last_seen": c.last_seen if c and c.last_seen is not None else o.seen}], keys=["chain_id", "hook_address"],
                update=["pools_total", "first_block", "last_block", "last_seen"])
            deleted += len(gone)
            out["batches"] += 1
    return deleted


def prune_pools(engine, keep: int, apply: bool, chain_ids: list[int], batch: int = BATCH, lock_wait: float = 600.0,
                should_stop=None) -> dict:
    stop = should_stop or (lambda: False)
    out = {"hooks": 0, "rows": 0, "pools_rows": 0, "est_bytes": 0, "batches": 0, "locked": [], "db_before": _db_bytes(engine),
           "db_after": None}
    log.info("prune-pools %s keep=%d chains=%s database=%s", "apply" if apply else "dry run", keep,
             ",".join(map(str, chain_ids)), _mb(out["db_before"]))
    v4_bytes, pools_bytes = _row_bytes(engine, "v4_pools"), _row_bytes(engine, "pools")
    for cid in chain_ids:
        if not apply:
            with engine.connect() as conn:
                for o in hooks_over(conn, cid, keep):
                    n, with_pools = conn.execute(sa.select(sa.func.count(), sa.func.count(pools.c.pool_id)).select_from(
                        v4_pools.outerjoin(pools, (pools.c.chain_id == v4_pools.c.chain_id) & (pools.c.pool_id == v4_pools.c.pool_id)))
                        .where(*_candidates(cid, o))).one()
                    est = int(n * v4_bytes + with_pools * pools_bytes)
                    out["hooks"] += 1
                    out["rows"] += n
                    out["pools_rows"] += with_pools
                    out["est_bytes"] += est
                    log.info("prune-pools dry run chain_id=%d hook=%s rows=%d keep=%d delete=%d pools_rows=%d est=%s",
                             cid, o.hook, o.rows, keep, n, with_pools, _mb(est))
            continue
        lock = _lock(engine, cid, lock_wait, stop)
        if lock is None:
            out["locked"].append(cid)
            log.warning("prune-pools chain_id=%d: another process holds bait-indexer:%d; chain skipped", cid, cid)
            continue
        try:
            with engine.connect() as conn:
                over = hooks_over(conn, cid, keep)
            for o in over:
                if stop():
                    break
                before = out["pools_rows"]
                n = _prune_hook(engine, cid, o, batch, stop, out)
                out["hooks"] += 1
                out["rows"] += n
                out["est_bytes"] += int(n * v4_bytes + (out["pools_rows"] - before) * pools_bytes)
                log.info("prune-pools chain_id=%d hook=%s rows=%d keep=%d deleted=%d", cid, o.hook, o.rows, keep, n)
        finally:
            lock.close()
    if apply:
        out["db_after"] = _db_bytes(engine)
    log.info("prune-pools %s: hooks=%d rows=%d pools_rows=%d batches=%d est=%s database %s -> %s (deleted space is reused "
             "after autovacuum; the volume does not shrink)", "done" if apply else "dry run", out["hooks"], out["rows"],
             out["pools_rows"], out["batches"], _mb(out["est_bytes"]), _mb(out["db_before"]), _mb(out["db_after"]))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m bait.indexer prune-pools",
                                 description="One-time trim of v4_pools rows of hooks past the per-hook cap.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="per-hook counts and estimated bytes; changes nothing")
    mode.add_argument("--yes", action="store_true", help="delete, per chain under the indexer's lock")
    ap.add_argument("--keep", type=int, default=_env_int("INDEXER_POOLS_PER_HOOK", 2000),
                    help="newest rows kept per hook (default INDEXER_POOLS_PER_HOOK, 2000)")
    ap.add_argument("--chains", default=os.environ.get("INDEXER_CHAINS"), help="comma slugs or chain ids (default all)")
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--lock-wait", type=float, default=600.0, help="seconds to wait for each chain's indexer lock")
    args = ap.parse_args(argv)
    if args.keep < 1:
        ap.error("--keep must be at least 1")
    try:
        chains = select_chains(args.chains)
    except ValueError as e:
        log.error("%s", e)
        return 2
    engine = db.make_engine(args.database_url)
    db.create_all(engine)                      # hook_pool_counts, on a database the new indexer has not migrated yet
    stop = {"flag": False}

    def _stop(signum, _frame):
        log.info("signal %s: stopping after the current batch", signum)
        stop["flag"] = True
    old = {s: signal.signal(s, _stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        out = prune_pools(engine, args.keep, args.yes, [c.chain_id for c in chains], lock_wait=args.lock_wait,
                          should_stop=lambda: stop["flag"])
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    return 3 if out["locked"] else 0
