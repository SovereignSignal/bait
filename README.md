# Bait

A discovery feed for Uniswap v4 hooks: what is new, what is trending, who is using it, and what it does. Hooks are
grouped into projects rather than listed by address, every figure is a closed UTC day from Bait's own on-chain indexer
or a public API, and every project page says which day its figures run through.

Live: https://bait-production-2706.up.railway.app

Bait is a personal project. It is not affiliated with Uniswap Labs or the Uniswap Foundation, it audits nothing, and
it executes no trades. A listing, badge or rank is not an endorsement.

## What it shows

- **Trending, New, Automatic routing, All.** Trending ranks 7-day volume growth over size; New is projects first seen
  within a week with real usage; the routing tab is projects with a pool that meets Bait's conservative reading of the
  Uniswap app's automatic-routing rule; All is everything with usage, wash-like rows last.
- **A project page**: hooks and chains, the busiest pools, daily volume, top wallets, buys and sells, a one-line
  "what it does" and a short "why people care" written from public sources, links, audit link, and a live trade feed.
- **Building**: hooks before they have volume, from registry submissions, repos, audits, hackathons and launches.
- **Leaders** and a **weekly digest** with RSS.
- **Wash-like volume** is flagged, never hidden: pools carrying at least half a project's volume through a handful of
  wallets, far above their liquidity, or trading symmetrically with themselves keep the project in All with a red
  badge and out of every ranking.

The rules behind each of those are stated once in [`docs/architecture.md`](docs/architecture.md) and argued, with
their history, in [`DESIGN.md`](DESIGN.md).

## How it works

```
Uniswap hooklist (registry) ─┐
HyperSync (16 chains) ───────┤   pipeline/   nightly 06:00 UTC: ingest, snapshot, activity, onchain,
GeckoTerminal, public RPC ───┤               group, enrich, score, signals, radar, digest
Blockscout, Sourcify ────────┤   indexer/    always on: every hooked v4 pool + 14 days of swaps
Exa, Claude ─────────────────┘        │
                                      ▼
                                  Postgres  ◄──  web/   Next.js, server-rendered, in-process caches
```

- `pipeline/` (Python 3.12): the nightly job and the always-on indexer. A spend guard (`bait/costs.py`) checks a daily
  and a 30-day budget before every paid call; without keys the pipeline still runs on free sources, slower.
- `web/` (Next.js 16, Drizzle): reads Postgres, nothing else. Without a database it renders empty; with
  `BAIT_FIXTURES=1` it renders sample data.
- `db/migrations/`: the schema. The pipeline also creates and extends tables on start, but a release that adds columns
  the web reads is applied as a migration first (`db/README.md`).

## Run it locally

You need Python 3.12 (if `python3 --version` says otherwise, use `python3.12` below), Node 20.9 or later (CI runs 22)
and git. Installing packages needs the network; nothing below needs a key or makes a paid call. Run the commands from
the repository root.

**On sample data, in three commands** (no database):

```sh
cd pipeline && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m pytest -q
cd ../web && npm ci && npm test
BAIT_FIXTURES=1 npm run dev        # http://localhost:3000 on sample data
```

The footer then says "Sample figures (BAIT_FIXTURES)": 16 projects from the 8 October 2026 snapshot, not live figures.
`/api/health` and `/api/ready` answer 503 `no DATABASE_URL` in this mode; that is expected. With neither
`BAIT_FIXTURES=1` nor `DATABASE_URL` set, every page renders empty.

**On your own data**, which needs a Postgres (15 or later; CI runs 17) and `psql`. The site reads Postgres only: the
pipeline's SQLite mode ([`pipeline/README.md`](pipeline/README.md)) is for working on the pipeline, and the site
cannot show it. With Docker, `docker run -d -e POSTGRES_PASSWORD=bait -e POSTGRES_DB=bait -p 5432:5432 postgres:17`
gives `DATABASE_URL=postgres://postgres:bait@localhost:5432/bait`.

```sh
cp .env.example .env               # set DATABASE_URL=postgres://user:password@localhost:5432/bait; leave the keys empty
scripts/apply-migrations.sh        # idempotent, records each file in schema_migrations
scripts/run-pipeline-local.sh      # every step for today (UTC) on the fixture registry, free sources only
cd web && ln -s ../.env .env && npm run dev    # Next.js reads web/.env, not the one in the repository root
```

- Without keys the snapshot pages GeckoTerminal's free tier, about 5 calls a minute, so a full run takes about an
  hour and `GT 429 ... retry` lines in the log are normal. `--chains arbitrum` limits it to one chain (most fixture
  hooks are on Arbitrum) and takes 8 to 15 minutes.
- The feed then lists the projects whose hooks sit in GeckoTerminal's busiest v4 pools: fixture hooks, plus
  `unlisted-hook-...` projects for hooks the registry sample does not list. Without `ANTHROPIC_API_KEY` the
  "what it does" line is the registry's description or a placeholder.
- `KEYLESS=0 scripts/run-pipeline-local.sh` skips GeckoTerminal and the public RPCs and finishes in under a minute,
  but with no pool data every project is held back as spam and the feed is empty: it only proves the wiring.
- `/api/health` keeps answering 503 until the always-on indexer has reported; the pages do not need it. Start it with
  `cd pipeline && .venv/bin/python -m bait.indexer` (see `pipeline/README.md`); without `HYPERSYNC_API_TOKEN` it only
  follows new pools over public RPCs. Run directly like this, the pipeline loads the first `.env` it finds from
  `pipeline/` upward, so a `pipeline/.env` hides the root one; `scripts/run-pipeline-local.sh` loads the root one.
- Set `NEXT_PUBLIC_SITE_URL` to the origin you serve (`http://localhost:3000` locally): RSS links and page metadata
  use it, and without it the read API's `url` fields point at the reference deployment.

Per-service details: [`pipeline/README.md`](pipeline/README.md), [`web/README.md`](web/README.md),
[`db/README.md`](db/README.md). Contributing and the test rules: [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Data sources and their limits

| Source | Used for | Limit that shapes the design |
|---|---|---|
| Uniswap hooklist | registry names, flags, audit links | names are candidates; merges need bytecode or deployer evidence |
| Envio HyperSync | every hooked pool, 14 days of swaps, 16 chains | free tier is about 20 queries a minute; Base pool lookups are the slow part |
| GeckoTerminal | pool TVL, rolling 24h figures, closed-day candles, recent trades, top wallets | about 5 calls a minute on the free tier; a keyless run of all ten chains takes about an hour |
| Public RPC, Blockscout, Sourcify, Routescan | hook resolution, bytecode, contract names, deployers | per-chain quirks listed in `pipeline/README.md` |
| DefiLlama | prices | current prices, so raw indexer USD is an estimate |
| Claude | the two text lines, classifying unnamed hooks and radar results | paid; optional and budget-capped |
| Exa | the Building tab's web results (audits, launches, hackathons, incubators) | paid; optional and budget-capped; radar only |

## Read API

A read-only JSON API serves the same figures the pages show: `/api/v1/projects`, `/api/v1/projects/{slug}` and
`/api/v1/hooks/{address}`. No key, about 600 requests a minute, documented with examples in [`docs/api.md`](docs/api.md).

## Deploy

Three services and a Postgres: a cron job, an always-on process, a web process. The reference deployment is Railway
and [`railway/README.md`](railway/README.md) has the exact commands and settings, including the gotchas. Any host
with the same shape works: set `DATABASE_URL` on all three, run `python -m bait run` daily at 06:00 UTC, keep
`python -m bait.indexer` running, serve `web/` with its Dockerfile, and point the web healthcheck at `/api/ready`.
Keys live in host variables and a local `.env`, never in the repo.

Monitoring: `/api/health` reports the last run's receipt and each chain's indexer state; a scheduled GitHub Action
(`.github/workflows/health.yml`) checks it daily and emails the repository owner on failure.

## Status

Beta. Figures can lag or be wrong when a source stalls; wash and spam rules are deliberately conservative; Base is
still marked partial while its pool backfill settles; many smaller projects have no description yet. The release plan
and what is left of it are in [`docs/mvp-release-plan.md`](docs/mvp-release-plan.md). To get a hook listed, open a pull request on the
[Uniswap hooklist](https://github.com/Uniswap/hooklist); Bait picks it up the next night.

## License

[MIT](LICENSE). Registry data in `pipeline/fixtures/hooklist.sample.json` is Uniswap's; see
`pipeline/fixtures/README.md`.
