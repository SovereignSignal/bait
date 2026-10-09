"""python -m bait.indexer: loop every INDEXER_INTERVAL seconds (default 60) over all chains.
`python -m bait.indexer prune-pools --dry-run|--yes [--keep N]`: one-time trim of v4_pools, see indexer/prune.py.

Env: DATABASE_URL, HYPERSYNC_API_TOKEN (without it: RPC fallback, Initialize only, new blocks only, 10 chains),
INDEXER_INTERVAL, INDEXER_CHAINS (comma slugs or ids), INDEXER_MAX_PAGES, INDEXER_TOPIC_FILTER_MAX,
INDEXER_TOPIC_CHUNK, INDEXER_RPC_MAX_WINDOWS, INDEXER_POOLS_PER_HOOK (v4_pools rows per hook; past it pools are counted),
INDEXER_POOL_LOOKUPS (HyperSync calls per chain per loop to find pools past the cap that swap),
INDEXER_LATE_INSERT_MIN_USD (smallest priced swap that makes such a pool a row), INDEXER_SAMPLED_POOLS_PER_HOOK (second cap).
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

import httpx
import sqlalchemy as sa

from .. import db, logsetup
from ..config import env
from .chains import select_chains
from .core import ChainStats, PoolResolver, Rpc, Settings, plan, rpc_endpoint, sync_hypersync, sync_rpc
from .hypersync import HyperSync, HyperSyncAuthError
from .prices import PriceBook, PricesUnavailable
from . import store

log = logging.getLogger("bait.indexer")


class Runner:
    def __init__(self, engine, token: str | None, chains, client: httpx.Client, settings: Settings, should_stop=None,
                 migrate=None):
        self.engine = engine
        self.token = token
        self.mode, self.chains = plan(token, chains)
        self.client = client
        self.settings = settings
        self.should_stop = should_stop or (lambda: False)
        self.migrate = migrate                         # -> bool; retried each loop until it succeeds
        self._schema_ok = migrate is None
        self.schema_locked = 0                         # consecutive loops the migration waited on a table lock
        self._price_fails: dict[int, int] = {}           # chain id -> consecutive loops ending in PricesUnavailable
        self.prices = PriceBook(client)
        self.loops = 0
        self._pruned_on = None
        self._hs = {}
        self._rpc = {}
        self._resolvers: dict[int, PoolResolver] = {}   # per chain, across loops: pools past the cap that may swap

    def run_once(self) -> dict[str, ChainStats]:
        self.loops += 1
        out: dict[str, ChainStats] = {}
        if not self._schema_ok:
            self._schema_ok = self.migrate()
            if not self._schema_ok:
                self.schema_locked += 1
                log.log(logging.ERROR if self.schema_locked >= 3 else logging.WARNING,
                        "schema migration waiting on a table lock (%d loops in a row); no chain synced, retrying next loop",
                        self.schema_locked)
                log.info("%s", heartbeat(self.loops, self.mode, out, schema_locked=self.schema_locked))
                return out
            self.schema_locked = 0
        for ch in self.chains:
            if self.should_stop():
                log.info("stop requested: leaving the loop before chain=%s", ch.slug)
                break
            # Railway starts the new container before stopping the old one: never two processes on one chain.
            try:
                lock = db.try_advisory_lock(self.engine, f"bait-indexer:{ch.chain_id}")
            except sa.exc.OperationalError as e:  # transient connect error: skip the chain this loop, keep the process
                log.warning("chain=%s could not take the chain lock: %s", ch.slug, e)
                out[ch.slug] = ChainStats(mode=self.mode, error="lock")
                continue
            if lock is None:
                log.info("chain=%s is being synced by another indexer process; skipped this loop", ch.slug)
                out[ch.slug] = ChainStats(mode=self.mode, error="locked")
                continue
            try:
                if self.mode == "hypersync":
                    hs = self._hs.setdefault(ch.chain_id, HyperSync(ch.hypersync, self.token, self.client))
                    out[ch.slug] = sync_hypersync(self.engine, ch, hs, self.prices, self.settings, should_stop=self.should_stop,
                                                  resolver=self._resolvers.setdefault(ch.chain_id, PoolResolver()))
                else:
                    _, url, window = rpc_endpoint(ch)
                    rpc = self._rpc.setdefault(ch.chain_id, Rpc(url, self.client))
                    out[ch.slug] = sync_rpc(self.engine, ch, rpc, window, self.settings)
                self._price_fails.pop(ch.chain_id, None)
            except HyperSyncAuthError as e:
                log.error("chain=%s HyperSync rejected the token (check HYPERSYNC_API_TOKEN): %s", ch.slug, e)
                out[ch.slug] = ChainStats(mode=self.mode, error="auth")
            except PricesUnavailable as e:  # the page is retried next loop; a short DefiLlama blip is routine
                n = self._price_fails[ch.chain_id] = self._price_fails.get(ch.chain_id, 0) + 1
                if n >= 3:
                    log.warning("chain=%s DefiLlama unavailable for %d consecutive loops; swap cursor held: %s", ch.slug, n, e)
                else:
                    log.info("chain=%s DefiLlama unavailable (%d in a row); page retried next loop: %s", ch.slug, n, e)
                out[ch.slug] = ChainStats(mode=self.mode, error="prices")
            except Exception as e:  # one chain failing must not stop the others
                log.warning("chain=%s sync failed: %s: %s", ch.slug, type(e).__name__, e)
                out[ch.slug] = ChainStats(mode=self.mode, error=type(e).__name__)
            finally:
                lock.close()
        today = datetime.now(timezone.utc).date()
        if self._pruned_on != today:
            try:
                with self.engine.begin() as conn:
                    n = store.prune_wallets(conn, today)
                self._pruned_on = today
                if n:
                    log.info("pruned %d pool_wallet_day rows older than %d days", n, store.WALLET_RETAIN_DAYS)
            except Exception as e:
                log.warning("prune failed: %s", e)
        log.info("%s", heartbeat(self.loops, self.mode, out))
        return out


def heartbeat(loop: int, mode: str, stats: dict[str, ChainStats], schema_locked: int = 0) -> str:
    parts = []
    for slug, s in stats.items():
        if s.error:
            parts.append(f"{slug}:ERR({s.error})")
            continue
        lag = f"{s.init_lag}" if s.swap_lag is None else f"{s.init_lag}/{s.swap_lag}"
        extra = f",swaps={s.swaps}" + (f",unpriced={s.unpriced}" if s.unpriced else "") if s.swaps else ""
        cap = "".join(f",{k}={v}" for k, v in (("capped", s.capped), ("late", s.late), ("folded", s.folded), ("lookups", s.lookups)) if v)
        parts.append(f"{slug}:lag={lag},new={s.new_pools}{cap}{extra}")
    total_new = sum(s.new_pools for s in stats.values())
    locked = f"schema_locked={schema_locked} " if schema_locked else ""
    return f"heartbeat loop={loop} mode={mode} new_pools={total_new} {locked}" + " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    logsetup.configure()
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["prune-pools"]:
        from .prune import main as prune_main
        return prune_main(argv[1:])
    ap = argparse.ArgumentParser(prog="bait.indexer")
    ap.add_argument("--once", action="store_true", help="one loop, then exit")
    ap.add_argument("--chains", default=os.environ.get("INDEXER_CHAINS"), help="comma slugs or chain ids (default all)")
    ap.add_argument("--database-url", default=None)
    args = ap.parse_args(argv)
    try:
        chains = select_chains(args.chains)
    except ValueError as e:
        log.error("%s", e)
        return 2
    interval = float(os.environ.get("INDEXER_INTERVAL", "") or 60)
    engine = db.make_engine(args.database_url)

    def migrate() -> bool:
        """Additive schema; DDL only for what is missing, under a lock timeout. False = a table lock was not granted."""
        try:
            db.create_all(engine)
            db.add_missing_columns(engine)
            db.add_missing_indexes(engine)
            return True
        except db.SchemaLocked:
            return False

    token = env("HYPERSYNC_API_TOKEN")
    client = httpx.Client(follow_redirects=True)
    stop = {"flag": False}

    def _stop(signum, _frame):
        log.info("signal %s: stopping after the current page", signum)
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    runner = Runner(engine, token, chains, client, Settings.from_env(), should_stop=lambda: stop["flag"], migrate=migrate)
    if runner.mode == "rpc":
        log.warning("HYPERSYNC_API_TOKEN not set: RPC fallback on %d chains (%s), new Initialize logs only, no backfill and "
                    "no swaps. Full coverage (HyperSync on %d chains: pool history, swaps, wallets, volume) needs the token.",
                    len(runner.chains), ",".join(c.slug for c in runner.chains), len(chains))
    else:
        log.info("HyperSync mode on %d chains: %s", len(runner.chains), ",".join(c.slug for c in runner.chains))
    while True:
        t0 = time.monotonic()
        runner.run_once()
        if args.once or stop["flag"]:
            return 0
        wait = interval - (time.monotonic() - t0)
        end = time.monotonic() + max(wait, 1.0)
        while time.monotonic() < end and not stop["flag"]:
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))
        if stop["flag"]:
            return 0


if __name__ == "__main__":
    sys.exit(main())
