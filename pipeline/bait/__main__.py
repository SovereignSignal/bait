"""CLI: python -m bait run --day YYYY-MM-DD [--steps a,b] [--chains base,ethereum] [--hooklist-file f] [--force-digest]
       python -m bait init-db   (create tables; for local SQLite dev, Postgres uses db/migrations)."""
from __future__ import annotations

import argparse
import os
import logging
import sys
import time
from datetime import date, datetime, timezone

import sqlalchemy as sa

from . import db
from .config import CHAIN_BY_SLUG
from .costs import Meter, format_preview, preview
from .steps import STEP_ORDER, RunContext, log
from .steps import activity, digest, enrich, group, ingest, score, signals, snapshot
from .steps import radar
from .steps import onchain  # on-chain indexer -> web tables

STEPS = {"ingest": ingest.run, "snapshot": snapshot.run, "activity": activity.run, "group": group.run, "enrich": enrich.run,
         "score": score.run, "signals": signals.run, "digest": digest.run,
         "radar": radar.run, "onchain": onchain.run}


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="bait")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the nightly pipeline for one day (idempotent)")
    r.add_argument("--day", type=date.fromisoformat, default=None, help="YYYY-MM-DD, default: today UTC")
    r.add_argument("--steps", default=os.environ.get("BAIT_STEPS") or ",".join(STEP_ORDER),
                   help="comma list, subset of " + ",".join(STEP_ORDER) + " (env BAIT_STEPS overrides the default)")
    r.add_argument("--chains", default=None, help="comma list of chain slugs, e.g. base,ethereum (default all)")
    r.add_argument("--hooklist-file", default=None, help="local hooklist.json instead of GitHub")
    r.add_argument("--force-digest", action="store_true", help="write the digest even if --day is not a Monday")
    r.add_argument("--max-code-fetch", type=int, default=300, help="Blockscout code lookups per run")
    r.add_argument("--signals-limit", type=int, default=50, help="New ∪ Trending projects to enrich with signals")
    r.add_argument("--database-url", default=None, help="override DATABASE_URL")
    r.add_argument("--create-tables", action="store_true", help="metadata.create_all before running (SQLite dev)")
    cp = sub.add_parser("cost-preview", help="worst-case paid calls and USD for one full run now; calls nothing paid")
    cp.add_argument("--database-url", default=None)
    cp.add_argument("--signals-limit", type=int, default=50)
    i = sub.add_parser("init-db", help="create tables via SQLAlchemy metadata")
    i.add_argument("--database-url", default=None)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    args = parse_args(sys.argv[1:] if argv is None else argv)
    engine = db.make_engine(args.database_url)
    if args.cmd == "init-db":
        db.create_all(engine)
        db.add_missing_columns(engine)
        log.info("init-db ok url=%s", engine.url.render_as_string(hide_password=True))
        return 0
    db.create_all(engine)  # idempotent (checkfirst); a fresh Railway Postgres needs no manual migration
    db.add_missing_columns(engine)  # create_all never alters existing tables; additive columns go here
    meter = Meter(engine)
    if args.cmd == "cost-preview":
        with engine.connect() as conn:
            latest = conn.execute(sa.select(sa.func.max(db.project_daily.c.day))).scalar_one()
            pctx = RunContext(day=latest or datetime.now(timezone.utc).date(), signals_limit=args.signals_limit)
            print(format_preview(preview(conn, pctx, meter), meter))
        return 0

    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    bad = [s for s in steps if s not in STEPS]
    if bad:
        log.error("unknown steps: %s", bad)
        return 2
    chains = None
    if args.chains:
        try:
            chains = [CHAIN_BY_SLUG[c.strip().lower()].chain_id for c in args.chains.split(",") if c.strip()]
        except KeyError as e:
            log.error("unknown chain slug %s; known: %s", e, ",".join(CHAIN_BY_SLUG))
            return 2
    day = args.day or datetime.now(timezone.utc).date()
    ctx = RunContext(day=day, chains=chains, hooklist_file=args.hooklist_file, force_digest=args.force_digest,
                     max_code_fetch=args.max_code_fetch, signals_limit=args.signals_limit, meter=meter)
    b = meter.budgets
    log.info("spend budgets: $%.2f/day, $%.2f/30 days; spent today $%.4f, last 30 days $%.4f",
             b.daily, b.monthly, meter.spent(today=True), meter.spent(days=30))
    log.info("run day=%s steps=%s db=%s", day, ",".join(steps), engine.url.render_as_string(hide_password=True))

    soft_errors = 0
    for name in STEP_ORDER:
        if name not in steps:
            continue
        t0 = time.monotonic()
        meter.step = name
        try:
            with engine.begin() as conn:
                counts = STEPS[name](conn, ctx)
        except Exception:
            log.exception("step=%s FAILED after %.1fs", name, time.monotonic() - t0)
            meter.flush()
            log.info("run spend $%.4f (paid API calls this run; ledger: api_spend)", meter.run_usd)
            return 1
        meter.flush()
        soft_errors += counts.get("errors", 0)
        log.info("step=%s ok %s elapsed=%.1fs", name, " ".join(f"{k}={v}" for k, v in counts.items()), time.monotonic() - t0)
    log.info("run spend $%.4f (paid API calls this run; ledger: api_spend)", meter.run_usd)
    if soft_errors:
        log.error("run finished with %d soft errors (see warnings above)", soft_errors)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
