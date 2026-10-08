# Bait

One feed for Uniswap v4 hooks: discover new hooks, see what people are using, understand why it
matters, try it. Cards grouped by project, not by hook address.

Personal project (Sov), hosted on Railway. Spec: [`DESIGN.md`](DESIGN.md). UI mock and design
tokens: [`mock.html`](mock.html). Not part of UF ops.

## Layout

```
bait/
  DESIGN.md            the spec; read it first
  mock.html            UI mock, source of the design tokens
  db/                  SQL migrations (db/migrations/000N_*.sql), applied with psql
  pipeline/            Python nightly job, Railway cron service: python -m bait run
  web/                 Next.js App Router site, Railway web service (Drizzle -> Postgres, ISR 1h)
  railway/README.md    how the three Railway services are created and configured
  scripts/             apply-migrations.sh, run-pipeline-local.sh
  .env.example         every env var, one comment each, no values
```

Each of `db/`, `pipeline/`, `web/` has its own README with the details.

## Local dev, in order

Requirements: Node 20, Python 3.12, `psql` (postgresql-client). A Postgres is optional for a
first run: the pipeline falls back to SQLite, and the web app to fixture data (with `BAIT_FIXTURES=1`), when `DATABASE_URL`
is unset.

1. Env

   ```sh
   cp .env.example .env     # fill in what you have; keys you leave empty are skipped
   ```

2. Database (skip for SQLite / fixtures)

   Point `DATABASE_URL` at any Postgres: a local one, or the Railway Postgres through its
   `DATABASE_PUBLIC_URL` (Public Access must be on). Then

   ```sh
   scripts/apply-migrations.sh
   ```

   applies `db/migrations/*.sql` in order and records each file in `schema_migrations`, so
   re-running is a no-op.

3. Pipeline

   ```sh
   python3 -m venv pipeline/.venv
   pipeline/.venv/bin/pip install -r pipeline/requirements.txt

   # fixture hooklist, SQLite, no keys needed: ingest + snapshot only
   scripts/run-pipeline-local.sh --steps ingest,snapshot --day 2026-10-06 \
     --database-url sqlite:///./dev.sqlite

   # same against the Postgres from step 2 (DATABASE_URL from .env), all steps
   scripts/run-pipeline-local.sh --day 2026-10-06

   (cd pipeline && .venv/bin/python -m pytest -q)
   ```

   `fixtures/routable_cases.json` is the shared routable-rule fixture; `pipeline/tests/test_routable_shared.py`
   and `web/src/lib/routable.test.ts` (`npm test`) both assert it so the two implementations cannot drift.

   `run-pipeline-local.sh` loads `.env`, cds into `pipeline/` and runs
   `python -m bait run --hooklist-file fixtures/hooklist.sample.json` plus whatever flags you
   pass (`--steps`, `--chains`, `--day`, `--database-url`, `--force-digest`). Runs are
   idempotent per day. Without `GRAPH_API_KEY` the snapshot step logs
   `client=graph skipped` and writes nothing, which is expected.

4. Web

   ```sh
   cd web && npm install && npm run dev      # http://localhost:3000
   ```

   No `DATABASE_URL` = empty pages. Add `BAIT_FIXTURES=1` for sample data with a "Sample figures" note. Set `DATABASE_URL` to read the database the
   pipeline just filled.

## Deploy

Three Railway services: the Postgres plugin, `pipeline` (cron, root `/pipeline`, `0 6 * * *`),
`web` (root `/web`, healthcheck `/`). Exact CLI commands, dashboard clicks, variable references
and the doc citations are in [`railway/README.md`](railway/README.md). Secrets live only in
Railway variables and your local `.env`, never in the repo.

## First night checklist

1. Migrations applied against the Railway Postgres (`scripts/apply-migrations.sh` with
   `DATABASE_PUBLIC_URL`); `\dt` shows `projects, hooks, pools, pool_snapshots, project_daily,
   signals, receipts, digests, schema_migrations`.
2. `pipeline` and `web` deployed; `web` healthcheck green; the site renders an empty feed.
3. Run the pipeline by hand once, ingest + snapshot only, before any cron tick. Either
   `Run now` on the pipeline service's latest deployment in the dashboard, or locally against
   the same Postgres:

   ```sh
   scripts/run-pipeline-local.sh --steps ingest,snapshot
   ```

   That uses the 300-hook fixture. For the live hooklist.json from GitHub run the CLI directly:
   `cd pipeline && .venv/bin/python -m bait run --steps ingest,snapshot`.
4. Check counts and the subgraph health warnings:

   ```sql
   select count(*) from hooks;                                   -- 300 from the fixture, ~4,900 live
   select chain_id, count(*) from pools group by 1 order by 1;   -- 8 chains; Robinhood + Ink skipped by design
   select day, count(*) from pool_snapshots group by 1;          -- one row per hooked pool for today
   ```

   Look in the run log for `hasIndexingErrors` warnings per chain and for
   `client=<name> skipped: no key in env` lines you did not expect.
5. If counts look right, run the remaining steps once (`--steps group,enrich,score,signals`)
   and reload the site. Then leave `cronSchedule` set; the next 06:00 UTC tick is the first
   unattended run. Railway skips a tick if the previous run is still `Active`.

## Open items carried from research

- Base / Unichain / BNB / Arbitrum / Avalanche / Polygon / Optimism subgraph IDs are the
  community deployments DefiLlama uses; health is unverified until the first run with a key.
- Robinhood (4663) and Ink (57073) have no public v4 subgraph; the RPC Initialize/Swap log-scan
  fallback is v2.
- The v4 pool deep link `app.uniswap.org/explore/pools/<chain>/<poolId>` with a bytes32 poolId
  is unconfirmed in a browser.
- Routable rule in code is stricter than the published Labs rule (adds the two liquidity-delta
  flags); `NON_ROUTABLE_FLAGS` in `pipeline/bait/rules.py` is where to relax it.

Disclaimers on every project page: not audited by us; flags shown as published; the routable
badge is a rule, not a guarantee.
