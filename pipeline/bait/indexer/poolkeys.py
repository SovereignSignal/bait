"""python -m bait.indexer scan-pool-keys --dry-run | --yes [--chains ...] [--pages N] [--stop-at HH:MM]

One-off backfill of v4_pool_keys (0022): every hooked pool initialized before the chain's Initialize cursor that is neither
a v4_pools row nor a key yet (past its hook's cap before keys existed, or pruned) gets a key, from one unfiltered walk of
the PoolManager's Initialize logs over [deploy block, initialize_block). Resumable: indexer_state.keys_block is the
cursor. When the walk reaches initialize_block the chain is marked keys_complete, and from then on a swap page settles an
unknown pool id from the table (a key: hooked, counted; no key: unhooked, skipped) instead of queueing a HyperSync lookup.
The live Initialize phase writes keys for every pool it does not store, so the table stays complete.

Cost: one HyperSync call per page of Initialize logs (Base: ~9M hooked pools, so thousands of pages); run it on a second
HyperSync token (HYPERSYNC_API_TOKEN_SCAN, else HYPERSYNC_API_TOKEN) as a temporary start command of the `indexer`
service, same image, while the indexer itself keeps running (see railway/README.md). It writes only v4_pool_keys and the
two indexer_state columns, in page-sized transactions, and stops between pages on SIGTERM (exit 4) or at --stop-at.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from datetime import datetime, time as dtime, timezone

import httpx
import sqlalchemy as sa

from .. import db
from ..db import v4_pool_keys, v4_pools
from . import store
from .chains import IndexedChain, select_chains
from .core import hooked_pools_from_page
from .hypersync import HyperSync, initialize_query, page

log = logging.getLogger("bait.indexer")

CHUNK = 1000


def scan_chain(engine: sa.Engine, chain: IndexedChain, hs: HyperSync, max_pages: int | None, stop, dry_run: bool) -> dict:
    """Walk Initialize logs from the cursor (or the deploy block) to the chain's initialize_block, keying every hooked pool
    that is not a row. -> counts. Marks keys_complete when the walk reaches initialize_block."""
    out = {"pages": 0, "hooked": 0, "keyed": 0, "from": None, "to": None, "complete": False}
    with engine.connect() as conn:
        state = store.get_state(conn, chain.chain_id)
    if state is None or not state.initialize_block:
        log.info("chain=%s: no Initialize cursor yet, nothing to key", chain.slug)
        return out
    end = int(state.initialize_block)
    cur = int(state.keys_block) if getattr(state, "keys_block", None) is not None else (chain.deploy_block or 0)
    out["from"] = cur
    while cur < end and not stop() and (max_pages is None or out["pages"] < max_pages):
        logs, blocks, _, nb, _ = page(hs.query(initialize_query(chain.pool_manager, cur, end)))
        out["pages"] += 1
        if nb is None or nb <= cur:
            raise RuntimeError(f"HyperSync made no progress at block {cur} (next_block={nb})")
        nb = min(nb, end)
        rows = hooked_pools_from_page(chain.chain_id, logs, blocks)
        out["hooked"] += len(rows)
        if dry_run:
            cur = nb
            continue
        with engine.begin() as conn:
            ids = [r["pool_id"] for r in rows]
            stored = set()
            for i in range(0, len(ids), CHUNK):
                stored.update(conn.execute(sa.select(v4_pools.c.pool_id).where(
                    v4_pools.c.chain_id == chain.chain_id, v4_pools.c.pool_id.in_(ids[i:i + CHUNK]))).scalars())
            new = [r for r in rows if r["pool_id"] not in stored]
            out["keyed"] += store.upsert_pool_keys(conn, new)
            store.set_state(conn, chain.chain_id, keys_block=nb)
        cur = nb
    out["to"] = cur
    if cur >= end and not dry_run:
        with engine.begin() as conn:
            store.set_state(conn, chain.chain_id, keys_block=cur, keys_complete=True)
        out["complete"] = True
    return out


def key_count(conn: sa.Connection, chain_id: int) -> int:
    return conn.execute(sa.select(sa.func.count()).select_from(v4_pool_keys).where(v4_pool_keys.c.chain_id == chain_id)).scalar() or 0


def _past(hhmm: str | None) -> bool:
    if not hhmm:
        return False
    h, m = (int(x) for x in hhmm.split(":"))
    now = datetime.now(timezone.utc).time()
    return now >= dtime(h, m)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="bait.indexer scan-pool-keys", description=__doc__.split("\n\n")[1])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="walk and count, write nothing")
    g.add_argument("--yes", action="store_true", help="write keys and move the cursor")
    ap.add_argument("--chains", default=os.environ.get("INDEXER_CHAINS"), help="comma slugs or chain ids (default all)")
    ap.add_argument("--pages", type=int, default=None, help="stop after this many HyperSync pages per chain (default: to the end)")
    ap.add_argument("--stop-at", default=None, help="UTC HH:MM to stop between pages (a repair must not overlap the nightly)")
    ap.add_argument("--database-url", default=None)
    args = ap.parse_args(argv)
    chains = select_chains(args.chains)
    engine = db.make_engine(args.database_url)
    db.create_all(engine)
    db.add_missing_columns(engine)
    token = os.environ.get("HYPERSYNC_API_TOKEN_SCAN") or os.environ.get("HYPERSYNC_API_TOKEN")
    if not token:
        log.error("HYPERSYNC_API_TOKEN_SCAN or HYPERSYNC_API_TOKEN is required")
        return 2
    stopped = {"flag": False}

    def _stop(signum, _frame):
        stopped["flag"] = True
        log.info("signal %s: stopping between pages", signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    stop = lambda: stopped["flag"] or _past(args.stop_at)   # noqa: E731
    client = httpx.Client(follow_redirects=True)
    rc = 0
    for chain in chains:
        if stop():
            rc = 4
            break
        hs = HyperSync(chain.hypersync, token, client)
        try:
            out = scan_chain(engine, chain, hs, args.pages, stop, args.dry_run)
        except Exception as e:  # one chain's failure does not stop the others
            log.error("chain=%s scan-pool-keys failed: %r", chain.slug, e)
            rc = 1
            continue
        with engine.connect() as conn:
            n = key_count(conn, chain.chain_id)
        log.info("chain=%s scan-pool-keys %s: blocks %s-%s in %d pages, %d hooked pools seen, %d keyed (table now %d), complete=%s",
                 chain.slug, "dry-run" if args.dry_run else "wrote", out["from"], out["to"], out["pages"], out["hooked"],
                 out["keyed"], n, out["complete"])
        if stop():
            rc = 4
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
