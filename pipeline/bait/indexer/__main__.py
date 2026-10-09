"""python -m bait.indexer: loop every INDEXER_INTERVAL seconds (default 60) over all chains.

Env: DATABASE_URL, HYPERSYNC_API_TOKEN (without it: RPC fallback, Initialize only, new blocks only, 10 chains),
INDEXER_INTERVAL, INDEXER_CHAINS (comma slugs or ids), INDEXER_MAX_PAGES, INDEXER_TOPIC_FILTER_MAX,
INDEXER_TOPIC_CHUNK, INDEXER_RPC_MAX_WINDOWS.
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

from .. import db, logsetup
from ..config import env
from .chains import select_chains
from .core import ChainStats, Rpc, Settings, plan, rpc_endpoint, sync_hypersync, sync_rpc
from .hypersync import HyperSync, HyperSyncAuthError
from .prices import PriceBook
from . import store

log = logging.getLogger("bait.indexer")


class Runner:
    def __init__(self, engine, token: str | None, chains, client: httpx.Client, settings: Settings):
        self.engine = engine
        self.token = token
        self.mode, self.chains = plan(token, chains)
        self.client = client
        self.settings = settings
        self.prices = PriceBook(client)
        self.loops = 0
        self._pruned_on = None
        self._hs = {}
        self._rpc = {}

    def run_once(self) -> dict[str, ChainStats]:
        self.loops += 1
        out: dict[str, ChainStats] = {}
        for ch in self.chains:
            try:
                if self.mode == "hypersync":
                    hs = self._hs.setdefault(ch.chain_id, HyperSync(ch.hypersync, self.token, self.client))
                    out[ch.slug] = sync_hypersync(self.engine, ch, hs, self.prices, self.settings)
                else:
                    _, url, window = rpc_endpoint(ch)
                    rpc = self._rpc.setdefault(ch.chain_id, Rpc(url, self.client))
                    out[ch.slug] = sync_rpc(self.engine, ch, rpc, window, self.settings)
            except HyperSyncAuthError as e:
                log.error("chain=%s HyperSync rejected the token (check HYPERSYNC_API_TOKEN): %s", ch.slug, e)
                out[ch.slug] = ChainStats(mode=self.mode, error="auth")
            except Exception as e:  # one chain failing must not stop the others
                log.warning("chain=%s sync failed: %s: %s", ch.slug, type(e).__name__, e)
                out[ch.slug] = ChainStats(mode=self.mode, error=type(e).__name__)
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


def heartbeat(loop: int, mode: str, stats: dict[str, ChainStats]) -> str:
    parts = []
    for slug, s in stats.items():
        if s.error:
            parts.append(f"{slug}:ERR({s.error})")
            continue
        lag = f"{s.init_lag}" if s.swap_lag is None else f"{s.init_lag}/{s.swap_lag}"
        extra = f",swaps={s.swaps}" + (f",unpriced={s.unpriced}" if s.unpriced else "") if s.swaps else ""
        parts.append(f"{slug}:lag={lag},new={s.new_pools}{extra}")
    total_new = sum(s.new_pools for s in stats.values())
    return f"heartbeat loop={loop} mode={mode} new_pools={total_new} " + " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    logsetup.configure()
    ap = argparse.ArgumentParser(prog="bait.indexer")
    ap.add_argument("--once", action="store_true", help="one loop, then exit")
    ap.add_argument("--chains", default=os.environ.get("INDEXER_CHAINS"), help="comma slugs or chain ids (default all)")
    ap.add_argument("--database-url", default=None)
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        chains = select_chains(args.chains)
    except ValueError as e:
        log.error("%s", e)
        return 2
    interval = float(os.environ.get("INDEXER_INTERVAL", "") or 60)
    engine = db.make_engine(args.database_url)
    db.create_all(engine)
    db.add_missing_columns(engine)
    db.add_missing_indexes(engine)
    token = env("HYPERSYNC_API_TOKEN")
    client = httpx.Client(follow_redirects=True)
    runner = Runner(engine, token, chains, client, Settings.from_env())
    if runner.mode == "rpc":
        log.warning("HYPERSYNC_API_TOKEN not set: RPC fallback on %d chains (%s), new Initialize logs only, no backfill and "
                    "no swaps. Full coverage (HyperSync on %d chains: pool history, swaps, wallets, volume) needs the token.",
                    len(runner.chains), ",".join(c.slug for c in runner.chains), len(chains))
    else:
        log.info("HyperSync mode on %d chains: %s", len(runner.chains), ",".join(c.slug for c in runner.chains))
    stop = {"flag": False}

    def _stop(signum, _frame):
        log.info("signal %s: stopping after this loop", signum)
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
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
