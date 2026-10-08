# Bait pipeline

Nightly Python job: ingest Uniswap v4 hooks, snapshot hooked pools per chain, record who trades the
busiest hooked pools (with Nansen wallet labels), group hooks into projects, enrich, score, pull
signals, write the Monday digest. Spec: `../DESIGN.md`
("Pipeline", "Postgres schema", "Grouping", "Scoring", "Data sources").

## Run locally

```sh
cd pipeline
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill in keys; never commit .env

# Postgres: apply ../db/migrations/0001_init.sql first (see ../db/README.md)
.venv/bin/python -m bait run --day 2026-10-06

# SQLite dev run with the fixture, no keys (tables are created automatically on sqlite):
.venv/bin/python -m bait run --day 2026-10-06 --hooklist-file fixtures/hooklist.sample.json \
    --database-url sqlite:///./bait.sqlite --force-digest

.venv/bin/python -m pytest -q -p no:cacheprovider   # 57 tests, SQLite in memory, HTTP mocked, KEYLESS=0
```

`--day` defaults to today's UTC date (what the Docker CMD relies on). Runs are idempotent per day:
every table is keyed on its natural key plus `day` and written with upserts, so re-running a night
replaces that night's rows. Exit code 0 only when every step finished with zero soft errors; a step
exception exits 1 immediately; bad flags exit 2.

Flags: `--steps ingest,snapshot,...` (subset, run in canonical order), `--chains base,ethereum`
(slugs), `--hooklist-file`, `--force-digest`, `--max-code-fetch N` (Blockscout lookups per night,
default 300), `--signals-limit N` (default 50), `--database-url`.

## Steps

| # | step | reads | writes | needs |
|---|---|---|---|---|
| 1 | ingest | hooklist.json (GitHub raw or `--hooklist-file`) | hooks (first_seen_at set once) | nothing |
| 2 | snapshot | v4 subgraph per chain, `pools(where hooks != 0x0, id_gt)` paginated by 1000; keyless GeckoTerminal for the rest | pools, pool_snapshots(day), pool_stats(day) incl. buys/sells/buyers/sellers_24h | GRAPH_API_KEY, else keyless |
| 3 | activity | pool_stats(day) ⨝ pools, wash excluded, top `ACTIVITY_POOLS` by 24h vol; GeckoTerminal `/pools/{id}/trades` | pool_top_wallets(day) (top 20 wallets/pool, 14 days kept), wallet_labels | keyless; optional NANSEN_API_KEY |
| 4 | group | hooks | projects, hooks.project_id, is_factory | optional: Blockscout, ANTHROPIC_API_KEY |
| 5 | enrich | projects with enriched_at null | what_line, category, website/github/x_handle, enrichment | ANTHROPIC_API_KEY; optional PARALLEL_API_KEY, Blockscout |
| 6 | score | pool_snapshots (day-14..day), pool_stats(day) | project_daily(day) | nothing |
| 7 | signals | New ∪ Trending (top 20 trending + new, cap 50, spam excluded) | receipts, signals, why_blurb, wallet_labels | optional EXA_API_KEY, GITHUB_TOKEN, NANSEN_API_KEY (+GRAPH_API_KEY), ANTHROPIC_API_KEY |
| 8 | digest | project_daily(day) | digests(week_of) | Monday or `--force-digest` |

Every external client is `bait/clients/<name>.py` with `from_env()`; when the key is missing it
returns `None` and the step logs `client=<name> skipped: no key in env` and does not call out.

`create_all` runs at the start of every run, then `db.add_missing_columns()` adds any column listed in
`db.ADDITIVE_COLUMNS` that an existing table lacks (Postgres `ADD COLUMN IF NOT EXISTS`, SQLite
`PRAGMA table_info`), so production picks up new columns with no manual migration.

### Activity and wallet labels (step 3)
Trades come from GeckoTerminal through the same `keyless.Http` the snapshot uses (one client per run,
`KEYLESS_SPACING` / `KEYLESS_CACHE_DIR`), one call per selected pool. Per wallet (`tx_from_address`,
lowercased): USD volume, trades, buys, sells, last trade; the top 20 per pool are written for the day
(the day's rows for a pool are replaced on re-run). Rows older than 14 days are deleted.

Nansen (only with `NANSEN_API_KEY`): the day's wallets across the selected pools, highest volume
first, skipping chains Nansen does not cover (Unichain, Ink) and wallets whose `wallet_labels.as_of`
is newer than `NANSEN_RELABEL_DAYS`. At most `NANSEN_MAX_ADDRESSES` calls per run, which is the credit
cap (100 credits per labels call, 500 with `NANSEN_PREMIUM_LABELS=1`). Empty answers are stored as
`[]` so the wallet is not paid for again inside the window. `smart_money` = any label containing
"smart money", "fund", "smart trader", "smart lp" or "smart dex". A 401/402/403 stops labeling for
the run. Step 7 (signals) reads the same table before calling Nansen and writes what it pays for.
Logged: `nansen queried= labeled= smart_money=`. With `KEYLESS=0` and no keys the step only prunes.

### Grouping rules (step 4)
1. Same normalized name -> same project (`BunniHook`, `bunni hooks` -> `bunni`).
2. Same `code_hash` (sha256 of deployed bytecode from Blockscout `/api/v2/smart-contracts/<addr>`,
   fallback `module=proxy&action=eth_getCode`) joins still-unassigned hooks to the project that already
   has that hash. Name wins over code so template forks with distinct names stay separate.
3. Long tail -> Claude Haiku 4.5 (`claude-haiku-4-5`) picks an existing project or names a new one and
   a category. Without a key, leftovers become their own address-named project so the feed still renders.
4. `is_factory` when a project has more than 20 hooks.

### Scoring (step 6)
Per pool, 7d deltas are `cum(day) - cum(baseline)` where baseline is the latest snapshot on/before
day-7; a pool created inside the window has baseline 0; on cold start (pool older than the window but
no snapshot that old) the earliest snapshot we hold is the baseline, so the first week never reports
all-time volume as 7d volume. `score_trending = log1p(vol_7d) * clamp(vol_7d / max(vol_7d_prev, 1000), 0.5, 10)`
when `vol_7d >= $10k`, else 0. `is_new`, `is_spam` as in DESIGN.md. `swaps_7d` is txCount delta
(swaps + liquidity mods; label it "txs" in the UI).

### Routable
`bait.rules.routable(hook)` implements DESIGN.md exactly: not `0x91…`, none of
beforeSwapReturnsDelta / afterSwapReturnsDelta / afterAddLiquidityReturnsDelta /
afterRemoveLiquidityReturnsDelta, not dynamicFee. The published Labs rule lists only the two swap
deltas, so this is stricter; `NON_ROUTABLE_FLAGS` is the one place to relax it. Computed, never stored.

## Claude usage
`clients/claude.py`, Anthropic SDK 1.x. Blurbs and what_line: `claude-sonnet-5-5` via
`client.beta.messages.create` with `output_config={"effort": "low", "format": json_schema}`,
`fallbacks="default"` + beta `server-side-fallback-2026-07-01`; `stop_reason == "refusal"` raises.
Classification: `claude-haiku-4-5` via `client.messages.create` with structured output only (no
effort, no thinking). Both models are called one request at a time; Message Batches would halve cost
for the nightly ~50 blurbs and is the obvious next step.

## Env vars
See `.env.example`. `DATABASE_URL` (Railway injects; `postgres://` is rewritten to
`postgresql+psycopg://`; unset falls back to `sqlite:///./bait.sqlite`), `GRAPH_API_KEY`,
`ANTHROPIC_API_KEY`, `EXA_API_KEY`, `PARALLEL_API_KEY`, `NANSEN_API_KEY` (+ `NANSEN_PREMIUM_LABELS=1`
for Smart Money tags), `GITHUB_TOKEN`, `BLOCKSCOUT_API_KEY` (optional). `BLOCKWORKS_API_KEY` is
unused by the pipeline.

Activity step: `ACTIVITY_POOLS` (pools whose trades are fetched per run, default 30),
`NANSEN_MAX_ADDRESSES` (Nansen label calls per run, default 10), `NANSEN_MONTHLY_CREDITS` (rolling 30-day budget in estimated credits, default 10000; enforced before every call), `NANSEN_RELABEL_DAYS` (days before a
labeled wallet is queried again, default 14; signals uses the same window). Keyless source:
`KEYLESS` (0 disables GeckoTerminal + public RPC), `KEYLESS_SPACING` (seconds between GeckoTerminal
calls, default 10), `KEYLESS_CACHE_DIR`, `KEYLESS_PAGES`, `KEYLESS_NEW_PAGES`, `KEYLESS_OHLCV_CAP`.

## Chains
`bait/config.py` `CHAINS`. Subgraph IDs are the community deployments DefiLlama uses; health is
unverified until the first run with a key (watch the `_meta hasIndexingErrors` warning). Robinhood
(4663) and Ink (57073) have no public v4 subgraph and are skipped with a log line; the RPC
Initialize/Swap log-scan fallback is not built. Nansen is skipped on Unichain and Ink. BNB and
Avalanche have no Blockscout instance, so code_hash lookups there return None.

## Docker
`Dockerfile`: python:3.12-slim, `pip install -r requirements.txt`, `CMD python -m bait run`
(day = today UTC). Railway cron service, ~06:00 UTC.
