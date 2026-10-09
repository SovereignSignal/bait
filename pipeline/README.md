# Bait pipeline

Nightly Python job: ingest Uniswap v4 hooks, snapshot hooked pools per chain, record who trades the
busiest hooked pools (top wallets per pool), fold in the on-chain indexer's exact usage, group hooks into projects, enrich, score, pull
signals, scan the dev radar (registry submissions, repos, audits, launches), write the Monday digest. Spec: `../DESIGN.md`
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

.venv/bin/python -m pytest -q -p no:cacheprovider   # SQLite in memory, HTTP mocked, KEYLESS=0; Postgres-only tests need TEST_DATABASE_URL (scripts/pg-test-cluster.sh)
```

Direct dependencies in `requirements.txt` are pinned; to refresh, bump in a venv, run the suite, and copy the versions from `pip freeze`.

`--day` defaults to today's UTC date (what the Docker CMD relies on). Runs are idempotent per day:
every table is keyed on its natural key plus `day` and written with upserts, so re-running a night
replaces that night's rows.

Exit codes:

- 0: every requested step completed. Soft errors (for example one GitHub 403) do not change this; they are
  counted in the `run_receipts` row (`soft_errors`, per-step counts) and logged at WARNING.
- 1: a step raised; the run stops at that step.
- 2: bad arguments (unknown step or chain slug).
- 3: another run holds the run lock.
- 4: schema not ready (a required column is missing).
- 5: (repair only) precondition: the last nightly receipt failed, or no package 8 per-day rows from the last day.

Flags: `--steps ingest,snapshot,...` (subset, run in canonical order), `--chains base,ethereum`
(slugs), `--hooklist-file`, `--force-digest`, `--max-code-fetch N` (Blockscout lookups per night,
default 300), `--signals-limit N` (default 50), `--database-url`.

## Steps

| # | step | reads | writes | needs |
|---|---|---|---|---|
| 1 | ingest | hooklist.json (GitHub raw or `--hooklist-file`) | hooks (first_seen_at set once) | nothing |
| 2 | snapshot | v4 subgraph per chain, `pools(where hooks != 0x0, id_gt)` paginated by 1000; keyless GeckoTerminal for the rest | pools, pool_snapshots per UTC day (candles; pools past the OHLCV cap only yesterday, estimated; subgraph at day - 1), pool_stats(day) incl. buys/sells/buyers/sellers_24h and vol_24h_rolling | GRAPH_API_KEY, else keyless |
| 3 | activity | pool_stats(day) ⨝ pools, top `ACTIVITY_POOLS` non-wash pools by 24h vol plus up to `ACTIVITY_WASH_POOLS` 24h-wash candidates; GeckoTerminal `/pools/{id}/trades` | pool_top_wallets(day) (top 20 wallets/pool, 14 days kept); pool_stats.wallets_24h when the fetch saw every trade of the day | keyless |
| 3b | onchain | v4_pools, pool_daily_onchain, indexer_state (from `python -m bait.indexer`) | hooks (unlisted, capped), pools, pool_snapshots, pool_stats(day) overriding keyless usage | nothing; no-op when the indexer tables are empty |
| 4 | group | hooks | projects, hooks.project_id, is_factory | optional: Blockscout, ANTHROPIC_API_KEY |
| 5 | enrich | projects with enriched_at null | what_line, category, website/github/x_handle, enrichment | ANTHROPIC_API_KEY; optional PARALLEL_API_KEY, Blockscout |
| 6 | score | pool_snapshots (window_end-13..window_end), pool_stats(day), pool_top_wallets(day), pool_daily_onchain / pool_wallet_day (ready chains) | project_daily(day) incl. window_end, vol_24h, swaps, wallets, is_wash / wash_reason | nothing |
| 7 | signals | New ∪ Trending (top 20 trending + new, cap 50, spam excluded) | receipts, signals, why_blurb | optional EXA_API_KEY, GITHUB_TOKEN, ANTHROPIC_API_KEY |
| 8 | radar | Uniswap/hooklist issues + PRs (30d), GitHub repo/code search (30d), Exa (60d) classified by Claude Haiku | radar_items (one row per URL, 90 days kept) | optional GITHUB_TOKEN (else anonymous unless KEYLESS=0), EXA_API_KEY + ANTHROPIC_API_KEY |
| 9 | digest | project_daily(day) | digests(week_of) | Monday or `--force-digest` |

Every external client is `bait/clients/<name>.py` with `from_env()`; when the key is missing it
returns `None` and the step logs `client=<name> skipped: no key in env` and does not call out.

`create_all` runs at the start of every run, then `db.add_missing_columns()` adds any column listed in
`db.ADDITIVE_COLUMNS` that an existing table lacks (Postgres `ADD COLUMN IF NOT EXISTS`, SQLite
`PRAGMA table_info`), so production picks up new columns with no manual migration.

### Activity (step 3)
Trades come from GeckoTerminal through the same `keyless.Http` the snapshot uses (one client per run,
`KEYLESS_SPACING` / `KEYLESS_CACHE_DIR`), one call per selected pool. Per wallet (`tx_from_address`,
lowercased): USD volume, trades, buys, sells, last trade; the top 20 per pool are written for the day
(the day's rows for a pool are replaced on re-run). Rows older than 14 days are deleted.

Wallet labels (`wallet_labels`) are a frozen cache from the retired Nansen lane; the web reads them, nothing writes them.
With `KEYLESS=0` and no keys the step only prunes.

### On-chain indexer + step `onchain` (step 3b)
`python -m bait.indexer` (package `bait/indexer/`) is a separate long-running process (Railway service `indexer`, see
`../railway/README.md`). Tables (DDL `../db/migrations/0004_indexer.sql`, metadata in `db.py` "# --- indexer ---"):
`v4_pools` (hooked pools only), `pool_daily_onchain` (never pruned: kept for all-time and monthly totals; `unpriced_swaps`
counts swaps with no priced side), `pool_wallet_day` (30 days kept), `indexer_state` (per-chain cursors; `swap_block_time` =
block time the swap cursor reached). Additive changes: `../db/migrations/0010_indexer_safety.sql`.

- **Engine:** Envio HyperSync JSON API over httpx (`indexer/hypersync.py`), `POST https://<chain>.hypersync.xyz/query`
  with `Authorization: Bearer $HYPERSYNC_API_TOKEN`, paginated by `next_block`; `GET /height` for the head. 16 chains:
  Ethereum, Optimism, BNB, Unichain, Polygon, Monad, World Chain, Soneium, MegaETH, Robinhood, Base, Arbitrum, Celo,
  Avalanche, Ink, Zora (`indexer/chains.py`, PoolManager addresses from developers.uniswap.org v4 deployments).
- **Initialize:** every PoolManager `Initialize` log from block 0 to the head, then followed; rows with `hooks != 0x0`
  upserted into `v4_pools`. Each chain stays `lag` blocks behind the archive head (3 on Ethereum, 10-40 on L2s) as reorg
  margin.
- **Swap:** only hooked pools, from the first block of the UTC day 14 days back (found by binary search on HyperSync
  block timestamps), then followed. Up to `INDEXER_TOPIC_FILTER_MAX` (2000) hooked pools on a chain the query filters
  `topic1` by pool id in chunks of `INDEXER_TOPIC_CHUNK` (500); above that it scans every v4 Swap and keeps hooked pools by
  a DB lookup per page. Wallet = transaction `from` (joined via `field_selection.transaction`), not the Swap `sender`
  (usually a router). Per UTC day: swaps, unique wallets (`pool_wallet_day` rows), USD volume.
- **USD volume is approximate by design:** each swap is valued at the token's *current* DefiLlama price
  (`coins.llama.fi/prices/current`, batched 80 ids, cached 30 min; native 0x0 -> the chain's coingecko id). Prices with
  confidence < 0.5, above $10M, or without decimals are ignored; with both sides priced the larger value is used unless
  they differ by more than 20x (then the smaller); a single swap above $500M counts as unpriced; swaps with no priced side
  count as swaps and wallets, add $0 and are counted in `unpriced_swaps`; wrapped native and major stablecoins fall back
  to the native coin price / $1 (`indexer/prices.py`); when the DefiLlama call fails the page is not committed and is
  retried next loop.
- **Safety:** each page is written with its cursor in one transaction, so a restart resumes exactly (no gaps, no double
  counts); at most `INDEXER_MAX_PAGES` (40) pages per chain per loop so a backfilling chain cannot starve the rest; one
  chain failing never stops the others. A heartbeat line per loop: `heartbeat loop= mode= new_pools= <chain>:lag=<init>/<swap>,new=,swaps=`.
  Each chain syncs under a Postgres advisory lock (`bait-indexer:<chain_id>`) and the swap cursor moves by
  compare-and-set, so a second process (deploy overlap) skips the chain or rolls its page back. SIGTERM is honoured
  between pages. A swap log without a block timestamp fails the page.
- **No token:** RPC fallback on the 10 chains with a public RPC in `keyless.LOGS` (Base and BNB use publicnode, the
  `keyless.LOGS` endpoints answered 429): Initialize only, new blocks only (cursor starts at the head, no backfill, no
  swaps), narrow `eth_getLogs` windows. Logged at startup. If it falls more than `INDEXER_RPC_MAX_WINDOWS` windows behind
  it skips ahead and clears the swap cursor, so the next HyperSync run re-backfills from block 0.

Step `onchain` (after activity) copies this into the tables the web app and score read:
1. Hooks in `v4_pools` missing from `hooks` become unlisted hook rows (name None, like snapshot keyless), busiest first,
   at most `ONCHAIN_MAX_NEW_HOOKS` (500) per night because each one can cost a Claude call in group's long tail;
   code_hash via `keyless.code_hashes` where a public RPC exists (KEYLESS != 0).
2. `pools` upserted from `v4_pools` for hooks we hold (every chain; the web filters unknown chain ids), incrementally
   (per-chain `onchain_watermarks` on `block_number` as the fast path, plus a nightly pool-level anti-join for known
   hooks' pools that have no `pools` row, which catches rows a backfill inserts below the watermark); a row is rewritten
   only when a value changed. token0/token1 are set on insert only, so GeckoTerminal/subgraph symbols survive; `created_at` =
   Initialize block time.
3. Usage, only for chains whose `indexer_state.backfilled` is true and `updated_at` is within `ONCHAIN_MAX_STALE_HOURS`
   (6) and `swap_block_time` has passed the end of the reference day (the 7-day wash rule uses the same chains): pools with `pool_daily_onchain` rows in day-14..day get one `pool_snapshots` row per day from `start`
   (`volume_usd_day`, `tx_count_day`; the legacy cumulative columns zero-seeded the day before), replacing the pool's
   rows from `start` on; older rows and the seed day's daily values are kept so a past day can be re-scored (TVL keeps the
   latest GeckoTerminal/subgraph reserve, the indexer does not compute TVL), and `pool_stats(day)` `vol_24h` /
   `traders_24h` / `wallets_24h` (distinct) from the last complete UTC day, `window_end` = its end (buys/sells columns
   kept). Pools whose keyless series is kept still get exact `tx_count_day` from the indexer. `swaps_7d` for these pools is real swaps, not
   txCount. While a chain's on-chain history is shorter than the window, pools that keyless already snapshots keep their
   keyless series and only `pool_stats` is overridden. Custom-accounting hooks (address bit 3) and pool-days with more
   than half their swaps unpriced keep keyless volume; `pool_stats.vol_source` is `onchain`, `hook` or `unpriced`
   (NULL = keyless). The rule is per day: from the first such day on the series carries NULL volume (swap counts kept),
   or the keyless series is kept when that day is on or before the reported day; `pool_stats` agrees with the series.

### Grouping rules (step 4)
1. Same normalized name -> same project (`BunniHook`, `bunni hooks` -> `bunni`).
2. Same `code_hash` (sha256 of deployed bytecode from Blockscout `/api/v2/smart-contracts/<addr>`,
   fallback `module=proxy&action=eth_getCode`) joins still-unassigned hooks to the project that already
   has that hash. Name wins over code so template forks with distinct names stay separate.
3. Long tail -> Claude Haiku 4.5 (`claude-haiku-4-5`) picks an existing project or names a new one and
   a category. Without a key, leftovers become their own address-named project so the feed still renders.
4. `is_factory` when a project has more than 20 hooks.

### Scoring (step 6)
The run on day D reports closed UTC days: "24h" is D-1 (`window_end`), "7d" is D-7..D-1, "prev 7d" D-14..D-8.
Volume windows sum `volume_usd_day` (a missing day adds $0; a pool whose cumulative is NULL is left out of every
window). `swaps_24h` / `swaps_7d` are NULL unless every day is known. `wallets_24h` is distinct across the project's
pools on ready chains (`wallets_exact`), else an upper bound. Wash is `is_wash` + `wash_reason` (DESIGN.md "Wash rules").
`python -m bait run --steps score --day <d>` reads stored rows only and is repeatable. `score_trending = log1p(vol_7d) * clamp(vol_7d / max(vol_7d_prev, 1000), 0.5, 10)`
when `vol_7d >= $10k`, else 0. `is_new`, `is_spam` as in DESIGN.md.

### Dev radar (step `radar`)
What is being built before it has volume; the web app shows it at `/building`. Table `radar_items` (DDL
`../db/migrations/0005_radar.sql`), one row per URL, `kind` in registry_submission / repo / hackathon / incubator /
audit / launch_post. Each source is capped, skipped with a log line when its key is missing, and wrapped so a failure
is logged and counted in `source_errors` without failing the run (radar is discovery, not core data).

1. **registry_submission**: `GET /repos/Uniswap/hooklist/issues?state=all&sort=created` newest first, up to
   `RADAR_REGISTRY_PAGES` (3) pages of 100, stopping at the first item older than 30 days. Chain, hook address, name and
   description come from the issue form (`### Chain`, `### Hook Address`, ...), else the title (`hook: Name (chain)`,
   `hook: 0x... on chain`), else a PR's `hooks/<chain>/<address>.json` path. The generator bot's PR that `Closes #N`
   is folded into issue N (so the issue reads Merged / Open, PR in review / Closed) instead of being its own row.
   Volume is high (about 200 submissions a day in Oct 2026, mostly Robinhood launchpads), so 3 pages cover roughly a
   day and a half; older items keep their last state. Chain slugs map to ids for every hooklist chain, not only the ten.
2. **repo**: up to `RADAR_GITHUB_QUERIES` (8) search calls: four repository searches (`"uniswap v4" hook
   in:name,description`, `topic:uniswap-v4-hooks`, `hook in:name,description topic:uniswap-v4`,
   `topic:uniswap-v4-hook`, all `pushed:>` 30 days ago), then code search (`BaseHook v4-core`, `getHookPermissions
   v4-periphery`, token only), whose repos are resolved with batched `repo:a/b repo:c/d` searches. Spaced 2.1s
   (6.5s anonymous and for code search) for the 30/min and 10/min limits. Kept: not a fork or archived, not owned by
   Uniswap, pushed in the last 30 days, and at least 1 star or a push in the last 14 days; names containing
   template/tutorial/workshop/course need more than 20 stars; at most 3 repos per owner (launchpads mint one repo per
   token). README matches and the bare `topic:uniswap-v4` were tried and dropped: mostly bots, SDKs and screeners.
3. **hackathon / incubator / audit / launch_post**: `RADAR_EXA_QUERIES` (6) Exa searches over 60 days (ETHGlobal
   showcase, Hook Incubator cohort, audit reports incl. Cantina / Spearbit / OpenZeppelin / Certora / Code4rena /
   Sherlock, launch announcements). URLs already stored only get `last_seen` bumped; up to `RADAR_CLASSIFY_MAX` (30)
   new ones go to Claude Haiku (`classify_radar`: kind, one-sentence summary, relevance 0-1). Results under 0.5 are
   stored with their score so they are not paid for again, and the web app hides them. Needs both keys; Exa is not
   called without ANTHROPIC_API_KEY.
4. `project_slug` is recomputed each run: hook address + chain via `hooks.project_id -> projects.slug`, repos via
   `projects.github`. Rows with `last_seen` older than 90 days are deleted.

Logged: `registry= repos= exa_new= classified= dropped= bumped= github_calls= exa_calls= linked= pruned= source_errors=`.

### Routable
`bait.rules.eligibility(hook, pool_fee=None)` implements DESIGN.md (2026-10-09 update) and returns
`eligible | ineligible | unknown`: not `0x91…`, none of the beforeSwapReturnsDelta / afterSwapReturnsDelta /
afterAddLiquidityReturnsDelta / afterRemoveLiquidityReturnsDelta bits in the address (permissions come from the
low 14 address bits, `flags_from_address`, never the declared flags), and a static fee (the pool fee when known, else
`properties.dynamicFee`; neither known is `unknown`). `routable(hook)` is `eligibility(hook) == "eligible"`. The
published Labs rule lists only the two swap deltas, so this is stricter; `NON_ROUTABLE_FLAGS` is the one place to relax
it. Computed, never stored. Registry membership is stored: `hooks.hooklisted` is set nightly by ingest.

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
`ANTHROPIC_API_KEY`, `EXA_API_KEY`, `PARALLEL_API_KEY`, `GITHUB_TOKEN`, `BLOCKSCOUT_API_KEY` (optional). `BLOCKWORKS_API_KEY` is
unused by the pipeline.

Radar step: `RADAR_REGISTRY_PAGES` (3), `RADAR_GITHUB_QUERIES` (8), `RADAR_EXA_QUERIES` (6),
`RADAR_CLASSIFY_MAX` (30); see "Dev radar" above.

Activity step: `ACTIVITY_POOLS` (pools whose trades are fetched per run, default 30), `ACTIVITY_WASH_POOLS` (24h-wash
candidates fetched as well, for the symmetric-trader check; default 10),
`ENRICH_MAX` (projects enriched with Parallel + Claude per run, busiest first, only projects with live pools; default 40), Keyless source:
`KEYLESS` (0 disables GeckoTerminal + public RPC), `KEYLESS_SPACING` (seconds between GeckoTerminal
calls, default 10), `KEYLESS_CACHE_DIR`, `KEYLESS_PAGES`, `KEYLESS_NEW_PAGES`, `KEYLESS_OHLCV_CAP` (pools past it get yesterday's volume from the rolling 24h,
marked `estimated`).

Indexer (`python -m bait.indexer`): `HYPERSYNC_API_TOKEN` (Envio; without it the RPC fallback runs), `INDEXER_INTERVAL`
(seconds per loop, 60), `INDEXER_CHAINS` (comma slugs or ids, default all 16), `INDEXER_MAX_PAGES` (40),
`INDEXER_TOPIC_FILTER_MAX` (2000), `INDEXER_TOPIC_CHUNK` (500), `INDEXER_RPC_MAX_WINDOWS` (30). Step `onchain`:
`ONCHAIN_MAX_NEW_HOOKS` (500), `ONCHAIN_MAX_STALE_HOURS` (6), `DISK_LIMIT_GB` (50; nightly `pg_database_size` line,
warning at 60%).

## Chains
`bait/config.py` `CHAINS`. Subgraph IDs are the community deployments DefiLlama uses; health is
unverified until the first run with a key (watch the `_meta hasIndexingErrors` warning). Robinhood
(4663) and Ink (57073) have no public v4 subgraph and are skipped by the subgraph path (keyless covers them, and the
on-chain indexer covers every chain it serves, see step 3b). BNB and
Avalanche have no Blockscout instance, so code_hash lookups there return None.

## Repair (package 9, one-off)

`python -m bait repair` brings stored data up to package 8's windows. Every command except `--dry-run` takes the run
lock (exit 3 while the nightly runs), refuses to start after a failed nightly receipt or before a package 8 nightly
(exit 5), and writes one `run_receipts` row per sub-step (`notes` = `repair <name>`, `soft_errors` 0).

| command | what it does | cost |
|---|---|---|
| `repair all --since 14 --dry-run` | prints the plan: pools and minutes to backfill, days to re-score, verify calls, prose counts and worst-case USD with the two budget options | free, writes nothing |
| `repair backfill-days --since 14 [--pools N]` | GeckoTerminal daily candles (one call per pool, `KEYLESS_SPACING` apart, busiest first) into `pool_snapshots.volume_usd_day` for the closed days `today-since-14 .. today-1`: rows written before 0012 and closed-day estimates only; stops between pools at `--stop-at` (05:30 UTC); a rerun resumes | free, about 10 s a pool |
| `repair rescore --since 14` | `score.run` for every stored run day in `today-since .. today`, oldest first, after copying each day once into `project_daily_p9_backup`; prints counts, wash reasons and the Trending top-10 change; `--restore` puts the copy back | free, seconds a day |
| `repair verify --sample 5` | Bait's 24h / 7d / prev 7d against GeckoTerminal candle sums for the same pools and closed days, as a markdown table | free, up to 15 calls |
| `repair prose --what-lines --blurbs --dry-run\|--yes [--limit N] [--max-usd X]` | Claude rewrites from stored facts: what-lines never written after a budget stop or cut mid-word at 200 chars; blurbs of the current signals targets written before package 8 | paid, Meter-checked |

GeckoTerminal candles carry no trade counts, so backfilled days keep `tx_count_day` NULL and those projects' `swaps_7d`
stays NULL (unknown) until seven nights of counts exist. Signals and digests are never rewritten by the repair.

## Docker
`Dockerfile`: python:3.12-slim, `pip install -r requirements.txt`, `CMD python -m bait run`
(day = today UTC). Railway cron service, ~06:00 UTC.

## Paid API costs

Every paid call goes through `bait/costs.py`: a price table (checked 2026-10-08), a durable ledger (`api_spend`,
written on its own connection so it survives a rolled-back step), and hard budgets checked **before** each call.

| Variable | Default | Effect |
|---|---|---|
| `SPEND_DAILY_USD` | 3.00 | All providers, UTC day. `0` = hold: paid calls skipped, free steps still run. |
| `SPEND_MONTHLY_USD` | 40.00 | All providers, rolling 30 days. |
| `SPEND_DAILY_USD_<PROVIDER>` / `SPEND_MONTHLY_USD_<PROVIDER>` | unset | Optional per provider (`ANTHROPIC`, `PARALLEL`, `EXA`). |
| `ENRICH_MAX` | 40 | Projects enriched (Parallel core + Sonnet) per run; live-pool projects only. |
| `GROUP_CLAUDE_MAX` | 100 | Haiku classifications per run; bare-address hooks are never sent. |
| `RADAR_EXA_QUERIES` / `RADAR_CLASSIFY_MAX` | 6 / 30 | Radar Exa searches and Haiku classifications per run. |

`python -m bait cost-preview` prints, without calling anything paid, how many paid calls each step would make against
the current database and the worst-case USD for one full run, plus spend today and in the last 30 days.
Every run logs `run spend $X` at the end.
