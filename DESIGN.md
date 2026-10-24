# Hook feed — design v1

Working name: bait. Personal project (Sov). Hosted on Railway. Not part of UF ops.

## What it is

One feed for Uniswap v4 hooks. Discover new hooks, see what people are using,
understand why it matters, try it. Cards grouped by project, not by hook address.

## Product

Routes
- `/`            feed, tabs New / Trending / All, chain filter, category filter
- `/p/[slug]`    project page: blurb, hooks, pools table, metrics history, flags, source, receipts
- `/week/[date]` weekly digest page (Mon), also `/feed.xml`
- `/about`       method, scoring, disclaimers

Card
- name, category chip, chains
- one line: what it does            (Claude, from verified source + hooklist description)
- two sentences: why people care    (Claude, from Exa hits + metrics + enrichment)
- metrics: TVL, 7d volume, 7d delta, pools, swaps 7d
- signals: smart money (Nansen), alive (GitHub/Exa 30d), routable, audited
- receipts: top 2 links from Exa, last 30d
- Try: app pool deep link if routable, else project app link
- share: per-project OG image

Non-goals v1: own router/swap widget, accounts, reviews, own indexer.

## Data sources

| Layer | Source | Cost |
|---|---|---|
| identity | github.com/Uniswap/hooklist hooklist.json | free |
| usage | Uniswap v4 subgraphs (The Graph gateway) per chain | free key |
| source code | Blockscout /api/v2/smart-contracts/<addr> | free |
| smart money | Nansen labels + netflows on top pools' LPs/swappers | credits; chain-conditional |
| social/web | Exa, last 30d, per New/Trending project | credits |
| enrichment | Parallel, once per new project (team, site, github, X, audit) | credits |
| prices | DefiLlama open endpoints (sanity check) | free |
| repo activity | GitHub API commits/stars | free |
| context tile | Blockworks: Uniswap DEX volume, Unichain chain metrics | credits |

Skipped: Dune, X API, Farcaster/Neynar.

## Postgres schema

```sql
projects(id, slug, name, category, what_line, why_blurb, website, github, x_handle,
         enrichment jsonb, enriched_at, blurb_at, first_seen_at, is_factory bool)
hooks(address, chain_id, name, description, flags jsonb, properties jsonb,
      verified_source bool, audit_url, deployer, code_hash, project_id, first_seen_at,
      primary key (address, chain_id))
pools(pool_id, chain_id, hook_address, token0, token1, fee, tick_spacing, created_at)
pool_snapshots(pool_id, chain_id, day, tvl_usd, volume_usd_day, tx_count_day, estimated,
               volume_usd_cum, tx_count_cum, primary key (pool_id, chain_id, day))
project_daily(project_id, day, window_end, tvl_usd, vol_24h, vol_7d, vol_7d_prev, swaps_24h, swaps_7d,
              wallets_24h, wallets_exact, pools_n, score_trending, is_new, is_spam, is_wash, wash_reason,
              primary key (project_id, day))
signals(project_id, kind, value jsonb, as_of)      -- nansen, github, exa
receipts(project_id, url, title, source, published_at)
```

Routable is computed, never stored: address not starting 0x91 AND none of
beforeSwapReturnsDelta / afterSwapReturnsDelta / afterAddLiquidityReturnsDelta /
afterRemoveLiquidityReturnsDelta AND not dynamicFee. Published rule, no list exists.

## Grouping (the hard part)

4,942 hooks, ~1,640 distinct names. EulerSwap 748, phiethhooks 418, Saturn 320.
1. same normalized name -> same project
2. same code_hash (eth_getCode via Blockscout) -> same project
3. remaining long tail: one-time Claude classification (Haiku 4.5) into project + category
4. one team's projects merge: same name stem plus a shared signal (see "Same-team merge", 2026-10-14)
5. factories flagged is_factory; card shows "748 hooks" as a stat, one card

Categories: dynamic-fee, launchpad, orders (limit/TWAMM), lp-management,
compliance, oracle, mev-lvr, lending, gaming-nft, other.

## Scoring

**Windows (package 8).** The nightly run on UTC day D reports days that are over:

| label | window | stored as |
|---|---|---|
| 24h | the UTC day D-1 | `project_daily.vol_24h`, `swaps_24h`, `wallets_24h` |
| 7d | D-7 .. D-1 | `project_daily.vol_7d`, `swaps_7d` |
| prev 7d | D-14 .. D-8 | `project_daily.vol_7d_prev` |

`project_daily.window_end = D - 1`. `pool_snapshots` holds per-day values (`volume_usd_day`, `tx_count_day`) written
to the day they belong to; the row for D itself is the running day and is never read. GeckoTerminal's `volume_usd.h24`
is a rolling window ending at run time: it is `pool_stats.vol_24h_rolling` (the pace line) and, only for pools past the
OHLCV cap, D-1's volume marked `estimated`. Swap counts are NULL unless every day is known; wallets are distinct across
a project's pools on ready chains, otherwise an upper bound (`wallets_exact = false`, shown as "≤").

**Labels (2026-10-14).** The UI never calls the closed day "24h": the project page names it ("Vol Oct 12", "Wallets Oct 12",
bars dated through Oct 12 UTC) and compact rows say "1d" next to "7d". The Uniswap app's pool "Past day" is a rolling 24 h
ending when you look, so it differs from Bait's closed day by up to two days of trading. Checked on Aegis USD₮0/USDC (Ink,
0xfff5…1805): Bait $292.3K for Oct 12 = GeckoTerminal's Oct 12 candle $292.3K = on-chain $292.8K (664 Swap logs, one leg);
the app read $35.8K at about 01:00 UTC Oct 14, matching the on-chain rolling 24 h then. Not double-counted.

- New: project first_seen <= 7d AND (max pool TVL > $1k OR swaps_7d >= 10, counted over the known days, a lower bound)
- Trending: vol_7d >= $10k; score = log1p(vol_7d) * clamp(vol_7d / max(vol_7d_prev, 1000), 0.5, 10)
  tie-break TVL delta; top 20
- Alive: GitHub commit <= 30d OR Exa hit <= 30d
- Spam filter: no pools, or all pools under $100 TVL and < 10 swaps lifetime -> hidden everywhere (404). A project
  whose lifetime or 7-day volume is $1M or more is never spam, so a wash-like whale (Optimism 0x69af: $436M through 8
  swaps at $0 TVL) is listed in All with its red badge instead of 404ing. Wash-like volume is not spam: see "Wash rules
  (package 8)".

**Historical listing and dormancy (2026-10-15).** Recent snapshot cumulative columns are bounded/reset by their source;
they are not all-time totals. The existing current spam rule still consumes them for compatibility. Separately,
`project_daily.has_historical_usage` preserves a listing when retained evidence strictly before the current seven days
qualifies: at least 10 exact `pool_daily_onchain` Swap events and at least $100 priced volume in one real pool. Folded
`hook:<address>` aggregates cannot satisfy that per-pool threshold; they can still supply the guarded $1M exemption. This $100
historical *volume* floor is a new dust guard; it is distinct from the current $100 *TVL* test. The existing $1M project
volume exemption can also be established from retained exact on-chain or non-estimated UTC-day snapshot volume; without
the qualifying exact swap proof it keeps the `unverified` wash warning. For each pool the larger source total is used,
never both overlapping totals. Cumulative snapshots, rolling estimates, old TVL, names, registry membership, source
verification and audits do not establish this historical exemption. `pool_daily_onchain` is not pruned and snapshot
days older than the rewritten recent window are retained; no synthetic lifetime counter is required.

`activity_status` is `active` when a recent positive swap count or priced volume is observed, including positive unpriced
on-chain swaps; it does not imply complete coverage. It is `dormant` only with qualifying historical usage, every mapped
pool's full seven days of zero swap counts (including capped aggregate pools), a known hook pool census, and ready,
fresh, backfilled indexers with coverage spanning the window and no owed lookups on every project chain. Other cases are
`unknown`; legacy unscored rows have NULL status. A missing pool/day, stale chain or unknown coverage never proves zero.
Snapshot swap counts contradicted by a larger raw on-chain count are NULL until the copy catches up. A volume window
without any known daily value is NULL; where some pool-days are known, the displayed sum remains a partial observed
total, not proof of full coverage. Historical evidence never adds to recent volume, swaps, TVL or growth.

Historically qualified records stay in All/search/watchlists/details when coverage is unknown as well as when dormant;
they are excluded from New/Trending while inactive/unknown. Wash controls remain independent. The latest prior wash
warning is retained with `wash_historical = true` until a measured revival (positive known priced 7-day volume with no
unknown-volume pool, on any chain, ready or not) lets current wash rules judge it, or until 14 days
(`WASH_MEMORY_DAYS`) have passed since the last night a wash rule fired on fresh evidence; carried rows never restart
that clock, so a flag cannot become permanent on a chain whose coverage never completes (Base, Robinhood, BNB). A
partial/unpriced revival cannot erase that warning. Neither a historical badge nor a dormant label constitutes a safety
endorsement.

An offline, reviewed-file import can add missing historical proof to `historical_swap_evidence` (migration 0017),
without writing raw daily usage, snapshots, indexer state or coverage. Each immutable physical log retains its source,
raw ABI payload, block identity and optional historical-price contribution. The caller must pin the reviewed file's
SHA-256; this is an integrity check, not an automatic authenticity or safety certificate. Version 1 decodes standard
v4 Swap logs and the supported custom input/output Swap signature; other ABIs require a reviewed adapter. It does
not grant any named-project or hook-address exemption.

This ledger supplies a separate qualification branch: at least ten distinct positive transactions and $100 of known
USD lower-bound contributions in the same real pool, strictly before the current seven days. Standard/custom logs
and repeated same-pool swaps in one transaction count once, with the maximum supported contribution, deliberately
undercounting when needed. Ledger amounts/counts never combine with raw/snapshot qualification totals or recent metrics.
Unknown contributions and the full historical total remain unknown. Exact imports are no-ops; changed evidence,
conflicting block/log identities and accepted/excluded contradictions fail atomically. Imported proof never establishes
coverage or clears wash findings. The next nightly score run consumes it; optional explicit `project_ids` restricts a
targeted score run to existing project/day rows for bounded repairs. See the pipeline README and
`docs/runbooks/historical-evidence.md`.

Migration 0016 adds the fields to project_daily and any existing repair backup; old rows remain unclassified until a score run. Fresh and
migrated backup tables use false defaults for the new booleans so legacy explicit-column writers remain restorable.
The web readiness endpoint checks these columns before serving this release.

## Pipeline (Railway cron service, Python, nightly ~06:00 UTC)

1. ingest hooklist.json -> upsert hooks; diff -> new hook candidates
2. subgraphs per chain: pools where hooks != 0x0, paginate id_gt; fields id, hooks,
   token0/1, feeTier, tickSpacing, createdAtTimestamp, totalValueLockedUSD, volumeUSD, txCount
   -> pools + pool_snapshots(day)
3. group hooks -> projects (rules, then Claude for unknowns)
4. enrich new projects: Parallel -> Blockscout source -> Claude (Sonnet 5.5) what_line + category
5. compute project_daily + scores
6. for New ∪ Trending (~50/night): Exa 30d -> receipts; GitHub -> signal; Nansen -> signal
   (skip Nansen on chains it doesn't cover); Claude why_blurb regenerated if signals changed
7. Monday: write /week/[date] digest row

Plain CLI (`python -m pipeline run --day YYYY-MM-DD`), idempotent per day, runnable locally
against the same Postgres before cron fires.

## Frontend

Next.js App Router on Railway, Drizzle -> Postgres, ISR revalidate 1h (project and week pages 10 min, 2026-10-14).
Mobile-first. OG image route per project. RSS at /feed.xml. No client-side data fetching.

## Repo layout

```
bait/
  pipeline/   python, uv, railway cron service
  web/        next.js service
  db/         sql migrations
  railway.toml (two services + postgres plugin)
```

Env: DATABASE_URL (injected), GRAPH_API_KEY, ANTHROPIC_API_KEY, EXA_API_KEY,
PARALLEL_API_KEY, NANSEN_API_KEY, GITHUB_TOKEN, BLOCKWORKS_API_KEY.

## Verify in hour one

- v4 subgraph exists for Unichain and Robinhood chain (two of top four chains by hook count).
  Fallback: RPC log scan of PoolManager Initialize/Swap for missing chains (v2).
- Nansen chain coverage; smart-money tile goes chain-conditional.
- Pool.txCount is swaps+mods, not swaps only; acceptable for v1, label as "txs".
- App deep link format for v4 pools: app.uniswap.org/explore/pools/<chain>/<poolId>.

## Milestones

- Session 1: Postgres + steps 1-2 + raw list page live on Railway
- Session 2: grouping + scoring + New/Trending tabs
- Session 3: Claude blurbs + signals + project pages + OG images -> ship
- Week 2: digest page + RSS; email later if wanted (Resend)

## Disclaimers (on every project page)

Not audited by us. Flags shown as published. Routable badge is a rule, not a guarantee.

## UI direction (added 2026-10-07)

Reminiscent of the classic Uniswap v3-era interface. Mock: see artifact link in chat / `web/mock.html`.

Visual system
- Canvas: white (#fff) light, #0d111c dark (classic v3 dark). Pink radial glow fixed behind the top of the page.
- Pink: #ff007a classic (hover #e60069, tint #fdeaf1). Dark tint #3a1322 / #ff7cb8.
- Surfaces: card #fff / #191b1f; wells #f5f6fc / #212429; borders #d2d9ee / #40444f; text2 #7780a0 / #c3c5cb.
- Type: Inter (the classic interface face), 14 base, 20 for the "what it does" line, 18 card titles, 24 h1. tabular-nums on every number.
- Radii: card 24, wells 20, CTA 20, chips/tokens 999. Shadow soft with a faint pink tint.

Page
- Sticky nav: mark + "Hook Feed", center pill tab bar New / Trending / All (swap-nav style),
  right: chain selector pill (token-selector style) + pink-tint "Submit a hook" (Connect-button slot).
- Single centered 480px column, the swap-widget width. Context tiles (3) above, category chips, then cards.

Card = swap widget
- Header: logo circle + name + category line; right: rank/new badge.
- Well 1 "What it does": one line at 20px (the amount slot). Foot: chain pills styled like token selectors; flags in small gray.
- Overlapping arrow button between wells (the swap-direction button).
- Well 2 "Why people care": two sentences at 15px. Signals row: smart money (pink), alive (green), routable, audited.
- Rate line: "7d volume $X +Y%" with a Details toggle, expands to TVL / pools / swaps / hooks / source (the fee-details pattern).
- CTA: full-width pink pill "Swap on Uniswap" when routable; gray well-colored "Open <project>" when not.
- Receipts: two link rows, source + age.
- Footer disclaimer on every page.

Rules
- Pink is for the one action and the smart-money signal only. Green/amber are semantic, never decorative.
- No hero. The first card is the hero.
- Project pages reuse the card as the header and add the pools table and history below it.
- OG image = the card at 1200x630.

## Update 2026-10-08: name, keyless source, wash rule

- Product name is **Bait**, and so are the repo (`SovereignSignal/bait`) and packages (renamed from bait 2026-10-08).
- **Keyless source** (`pipeline/bait/keyless.py`): GeckoTerminal v4 pools per chain (top by 24h volume, top by
  24h swaps, newest) give poolId, volume, reserve, unique wallets and creation time. The hook is resolved on-chain
  with `PositionManager.poolKeys(bytes25)` over public RPCs (addresses from developers.uniswap.org v4 deployments).
  Covers Robinhood chain and Ink, which have no public v4 subgraph, so it is also the planned fallback for those two.
  Limits: GeckoTerminal free tier throttles to roughly 10 calls/min; pools never touched by PositionManager cannot be
  resolved and are counted as unresolved; 7-day series only for the top hooked pools (OHLCV cap, logged).
- **Wash rules:** see "Wash rules (package 8)". Wash-like projects are excluded from Trending, New and Leaders, shown
  in All and on their page with a red badge and the reason. First real run found a Base pool doing $160M/day from 5
  wallets.
- **Demo:** `demo/build.py` bakes a snapshot into `demo/template.html` (same tokens and card anatomy as the web app)
  for review as a single page. The Next.js app on Railway remains the product.

## Activity and wallet labels (added 2026-10-08)

Nightly step `activity`, right after `snapshot`: who actually uses each hook.

- **pool_stats** gains `buys_24h`, `sells_24h`, `buyers_24h`, `sellers_24h` (GeckoTerminal `transactions.h24`,
  keyless path). Existing databases get the columns from `db.add_missing_columns()` on the next run;
  DDL in `db/migrations/0003_activity.sql`.
- **pool_top_wallets** `(pool_id, chain_id, day, address)`: for the top `ACTIVITY_POOLS` (30) non-wash pools (plus up to `ACTIVITY_WASH_POOLS` (10) 24h-wash candidates) by 24h
  volume, GeckoTerminal's last ~300 trades are rolled up per wallet (USD volume, trades, buys, sells, last trade).
  Top 20 wallets per pool per day; 14 days retained.
- **wallet_labels** `(address, chain_id)`: Nansen labels (JSON list of strings), `smart_money` flag, `source`, `as_of`.
  An empty list means "asked, nothing", so the wallet is not re-queried for `NANSEN_RELABEL_DAYS` (30, minimum 30).
- **Credit cap:** at most `NANSEN_MAX_ADDRESSES` (10) Nansen calls per run and a rolling 30-day budget of `NANSEN_MONTHLY_CREDITS` (10,000 estimated credits: about 100 per labeled answer, 500 with premium labels, empty answers free) shared by every step that calls Nansen, highest-volume wallets first; Unichain and
  Ink are skipped (no Nansen coverage). The signals step reads the same table first, so a wallet is paid for once per
  window across both steps. Without `NANSEN_API_KEY` the trades layer still runs (keyless) and labels are skipped.
- All addresses lowercase. The web app reads these three tables directly (project page "who trades it", smart-money
  signal); the pipeline never reads them back except for the relabel window.

## Dev radar (added 2026-10-08)

Bait ranks hooks by usage, which is late: by the time a hook trades, it was submitted, pushed, demoed or audited weeks
earlier. `/building` shows that earlier layer.

- **Pipeline step `radar`** (after signals, before digest) writes `radar_items`, one row per URL
  (`db/migrations/0005_radar.sql`): Uniswap/hooklist issues and PRs from the last 30 days (registry_submission, chain +
  hook address parsed from the issue form, the bot PR folded into its issue), GitHub repos building v4 hooks pushed in
  the last 30 days (repo; forks, Uniswap's own repos, small templates and per-owner floods dropped), and Exa results from
  the last 60 days classified by Claude Haiku into hackathon / incubator / audit / launch_post with a one-sentence summary
  and a 0-1 relevance score (under 0.5 hidden). Every source is capped (`RADAR_*`), skipped without its key, and never
  fails the run. 90-day retention. `project_slug` links an item to a project already on Bait.
- **Just launched**: the latest hooked pool initializations from the on-chain indexer's `v4_pools`, across every chain it
  covers, on top of the New tab and on `/building`; server-rendered (20 rows), refreshed every 60s from
  `/api/launches` (30s cache). Pair, fee (dynamic when bit `0x800000`), hook or project name, explorer link to the tx.
  A hook past the indexer's per-hook cap shows only its pools that became rows (a real swap), each at its Initialize time,
  folded into one line with "+N more pools".
- Not reviewed by us: the radar is a pointer to look, like the rest of Bait.

## On-chain indexer (added 2026-10-08)

GeckoTerminal pages and subgraphs only see the pools they choose to list. Bait now runs its own indexer so every hooked
v4 pool on every chain it covers shows up as soon as it is initialized, with exact on-chain usage.

- **Process:** `python -m bait.indexer` (`pipeline/bait/indexer/`), a Railway always-on service `indexer` next to the
  nightly `pipeline` cron. Engine: Envio HyperSync's JSON API (`HYPERSYNC_API_TOKEN`) on 16 chains: Ethereum, Optimism,
  BNB, Unichain, Polygon, Monad, World Chain, Soneium, MegaETH, Robinhood, Base, Arbitrum, Celo, Avalanche, Ink, Zora.
- **What it records:** every PoolManager `Initialize` with `hooks != 0x0` from block 0 (`v4_pools`); `Swap` logs of those
  pools from 14 days back, then live, aggregated per UTC day: swaps, unique wallets (transaction `from`, not the router
  that is the Swap `sender`) and USD volume (`pool_daily_onchain`, wallets per day in `pool_wallet_day`, 30 days).
  Per-chain cursors in `indexer_state`, committed with each batch, so restarts resume exactly.
- **Volume is approximate by design:** valued at current DefiLlama prices, not prices at swap time; unpriced swaps are
  counted (`unpriced_swaps`), and a mostly-unpriced pool-day or a custom-accounting hook keeps the keyless number. TVL is
  not computed (GeckoTerminal/subgraph reserve is kept).
- **Into the feed:** nightly step `onchain` (after activity) adds unlisted hooks (capped per night), upserts `pools`,
  and for fully backfilled, fresh chains replaces keyless usage with on-chain numbers in `pool_snapshots` (cumulative
  series for the 7d math) and `pool_stats` (24h volume + wallets for the wash rule). The web's "Just launched" list reads
  `v4_pools` directly.
- **Per-hook cap, and the pair of caps (2026-10-09):** `v4_pools` reached ~5.9M rows and Postgres grew 3.6 GB to 12.4 GB in a
  day, mostly one Base launchpad hook with one pool per coin. A hook now keeps at most `INDEXER_POOLS_PER_HOOK` (2,000) rows,
  the first to arrive; past that `hook_pool_counts.pools_total` counts every pool of the hook, so the product still says
  how many pools a launchpad has (`hooks.pools_total`, `pools_n`) without a row per coin. A first-come cap invites
  griefing: anyone can initialize junk pools on a victim hook until the cap is full, so its later real pools are never
  stored at Initialize. The defence is that a pool past the cap that swaps is looked up by pool id and inserted
  (`sampled`), and its swaps from before the lookup are owed, not dropped (see "Update 2026-10-14"). That path
  alone would let an attacker bypass the cap with one dust swap per junk pool, so it has its own limits: a late row needs a
  priced swap of at least `INDEXER_LATE_INSERT_MIN_USD` ($10; unpriced never qualifies) and stops at
  `INDEXER_SAMPLED_POOLS_PER_HOOK` (1,000) per hook. Nothing is lost past both caps: those swaps and wallets are kept per day
  on the hook's aggregate pool `hook:<address>` (and in `swaps_total` / `volume_usd_total`), which the nightly step turns
  into a series and `pool_stats` with `vol_source = 'hook_total'`, so the project's displayed volume, swaps and wallets stay
  true. Row growth is bounded at 2,000 + 1,000 rows per hook. The $10 minimum is not a real cost: a $10 swap against your
  own liquidity costs only gas and the hook's fees, so filling a hook's 1,000 sampled slots is cheap. Slots are first-come
  and permanent for now; the follow-up is slot rotation (by rolling volume, or demoting sampled rows that went idle).
  No wash rule can judge folded pools: the aggregate loses per-pool concentration (one wallet cycling one junk pool looks
  like ordinary activity spread over many). So folded usage counts toward a project's displayed totals and toward whether
  its volume is known, but never toward `score_trending`.
  `INDEXER_POOLS_PER_HOOK` and `INDEXER_TOPIC_FILTER_MAX` are coupled: a chain with any capped hook always scans swaps
  (topic mode only asks for stored pool ids), so a cap at or below the topic limit moves a small chain to scan mode once
  one hook reaches it. Existing excess is trimmed once by `python -m bait.indexer prune-pools`.
- **Without the token:** the indexer degrades to following new Initialize logs over the public RPCs on 10 chains (no
  history, no swaps) and says so in its startup log.


## Cost controls (2026-10-08)

Paid providers: Anthropic (Sonnet 5.5 blurbs, Haiku 4.5 classification), Parallel (core, $0.025/run), Exa ($0.017 per
search with highlights), Nansen (credits). All metered through one ledger with daily ($3) and 30-day ($40) budgets
enforced before each call; `cost-preview` reports worst case per run from live data (first preview: $2.89, under the
daily budget). Grouping never sends bare-address hooks to Claude (they carry nothing to classify) and sends a 25-name
shortlist instead of up to 400 names.

## Data issues backlog (from a UF backtest on Bait data, 2026-10-08)

- HyperSync token is free tier (~15 requests per window); the indexer needs all of it. One-off scans need a second token.
- Swap history starts 14 days before each chain's swap phase and `pool_daily_onchain` is never pruned; add a per-pool
  per-month aggregate for all-time / monthly views.
- Swap.fee is decoded but not stored; store fee_usd per pool-day (real LP fees incl. dynamic fees).
- Indexer memory: the per-chain pool resolver keeps up to 50K full pool rows (`recent`) and 1M unhooked pool ids
  (`absent`); store compact keys instead when the indexer's memory matters.
- Sampled slot rotation (see "Per-hook cap"): slots are first-come and permanent today.
- Dormant wash: Labs' Dune hook explorer (query 5685626) attributes ~$225B of $272B all-time volume to a few dead
  ...888 pools (1-4 pools, $0 in 7 days). Before showing all-time totals, exclude all-time > $1B with <= 4 pools and
  $0 7d. Bunni (761 pools) and EulerSwap (1,241 pools) show ~$0 in 7 days: never rank by all-time.
- Labels: "project" is ambiguous between hook author and integrator (Dune puts Doppler's
  DecayMulticurveInitializerHook 0xbb7784a4... under Bankr). If Dune labels are ingested, keep both, never overwrite.
  Bait already names hooks Dune leaves unlabeled (StablePairHook, FablesRamp/ETH, HookrModularHookV6, LaunchHook).
  Labs' own hooks and "Unknown Deployer" buckets need their own category.
- Keyless candle rows older than 15 days were overwritten with the old zero seed every night until package 8, and rows
  before 0012 carry no per-day values. Re-scoring 14 past days (package 9) needs a one-off GeckoTerminal OHLCV pull with
  limit 30 for the keyless pools first.
- Remove `volume_usd_cum` / `tx_count_cum` once package 9 has re-scored: windows read only the per-day columns (the
  cumulative NULL is still p5's "volume unknown from here on" marker and `tx_count_cum` is is_spam's lifetime count).
- Drop `project_daily_p9_backup` (package 9's pre-repair copy of re-scored days, defined in `pipeline/bait/db.py`) in the
  same cleanup, once the re-scored days are verified.
- To check: optimism 0x69afa...7fff ($642M in 7 days, all of its history, 3 pools: wash check); robinhood
  0xe5e70...e044 (10,082 pools, $231M in 7 days, launchpad pattern, unlabeled everywhere).

## Naming unlisted hooks (2026-10-09)

Hooks with no hooklist entry used to show as "Unlisted hook 0x…" (31 of the top 50 New/Trending projects on 10/8).
The group step now looks up verified-source contract names (free): Sourcify, then Blockscout PRO when
BLOCKSCOUT_API_KEY is set (free key; the Base/Arbitrum/Polygon/Robinhood Blockscout instances sit behind a Cloudflare
challenge), then the per-chain Blockscout instance, then Routescan (Avalanche). 400 hooks per run (NAMES_MAX), busiest
first, misses retried after 14 days. Same contract name on one chain = one project; curated projects are never merged
into, a clash gets the chain appended; proxies and template names (UniswapV4Hook, BaseHook) are ignored.
Dry run on 10/8 data: 400 checked, 202 named, 117 projects renamed, 28 merged; 10 of the top 50 stay unnamed (all
unverified). The web labels those by their busiest pair: "WETH/USDC hook · 0x7bc2…2aec".

## Wash rules (package 8, 2026-10-09)

Wash is its own flag (`project_daily.is_wash`, `wash_reason`), separate from spam: the project stays in All and keeps its
page with a red "Wash-like volume" badge; it never reaches Trending, New, movers, Leaders, the digest or a Claude blurb.
A project gets a reason when the pools that rule flags carry >= 50% of its volume in that rule's window.

- Wallet counts are a lower bound: the distinct count when known, else max(buyers, sellers), never buyers + sellers.
  For the 7-day rules off ready chains it is the largest daily bound of the week's 7 pool_stats days (one quiet day is not
  "0 wallets"); with no wallet data at all, the swaps behind the 7-day volume are an upper bound (8 swaps prove < 50),
  but only when they are exact: the indexer's UTC days on ready chains. Keyless counts are rolling 06:00-to-06:00
  buys + sells filed under the day before (a launch's first hours land in a day the pool has no row for), so they
  never bound wallets, and no swap bound is used against a known wallet lower bound above the rule's threshold.
  Missing pool_wallet_day rows are unknown, never 0 wallets.
- `24h`: pool >= $250K in 24 h through <= 25 wallets, and corroborated.
- `7d` (indexer, ready chains): pool >= $1M over 7 days through <= 10 distinct wallets, and corroborated. Also,
  <= 10 exact swaps carrying >= $1M prove <= 10 wallets; that concentration is its own corroboration.
- Corroborated: 7-day volume > 50x a known TVL, or no liquidity (>= $1M in 7 days with a known TVL <= $1,000), or
  symmetric traders. Few wallets alone also describes private / RFQ hooks (Arrakis private hook) with real liquidity.
  No liquidity never counts for a custom-accounting hook (address bit 3): its liquidity lives in the hook. A missing
  GeckoTerminal reserve is unknown TVL, not $0.
- `concentrated`: >= $1M in 7 days carried by at most 2 known distinct wallets: pool_wallet_day on ready chains;
  elsewhere an exact count (pool_stats.wallets_24h) on every day that carries volume, with the union of those days'
  pool_top_wallets addresses as the week's count (rotating pairs are not concentrated); or <= 2 exact swaps. No TVL
  corroboration, custom-accounting hooks included: one wallet moving $1M through its own pool is not usage; an RFQ hook
  with 5-10 real counterparties stays clear. A legitimate two-counterparty desk moving $1M+ will carry the
  "concentrated" badge and never rank: an accepted cost, and the badge text is accurate.
- `vol_tvl`: 7-day volume > 300x a known TVL, >= $10K, fewer than 50 wallets (Base launchpads churn 345x through ~30K).
- `no_liquidity`: >= $1M in 7 days, a known TVL <= $1,000, fewer than 50 wallets. Unknown TVL never counts.
- `symmetric`: the pool's top 10 wallets that day each have equal buys and sells and carry > 50% of its rolling 24h
  volume (>= $10K), with at least 5 such wallets or 3 round trips each. Data from the activity step: the top 30 non-wash
  pools plus up to 10 24h-wash candidates (`ACTIVITY_WASH_POOLS`).
- `uniform`: >= $1M rolling 24h, the pool's top 10 wallets (at least 8) each made exactly one trade, their sizes within 3%
  of each other, and the pool's volume per wallet is at least 80% of their median size (a buy cap gives a uniform top but
  a much lower average). Hookr MOW/WETH 10/13: ten one-trade buys of $22,450-$22,515, $17.1M from 809 wallets.
- `unverified`: not a rule. A project that escaped the spam rule only by moving $1M or more, matched no rule and has no
  wallet data at all is listed as wash-like rather than ranked.
- Live cases: OUROBOROS/CRCL on Robinhood (866x, 13 buyers + 13 sellers, symmetric bots) -> 24h, vol_tvl, symmetric;
  Optimism 0x69af...7fff ($436M from 1 wallet in 8 swaps at $0 TVL; every permission bit set, so a custom-accounting
  hook and no_liquidity does not judge it) -> concentrated; the Base 0x0f48 launchpad
  family and private RFQ hooks -> none.
- Dormant all-time rule (> $1B all-time, <= 4 pools, $0 in 7 days): not built. Bait ranks on 7-day volume only and
  shows no all-time totals; build it together with the monthly aggregate, before any all-time view ships.
- Robinhood 0xe5e70...e044 is V2MemeHook (named). It shows $0 because the indexer has not caught up on Robinhood
  swaps (also Base and BNB); those chains rely on the 24h rule until it does.

## Update 2026-10-09: permissions from the address, three-state routing, registry membership

- **Permissions.** A hook's 14 permission flags are the low 14 bits of its address (v4 `Hooks.sol`), enforced on-chain. The pipeline derives them for every hook it discovers (`rules.flags_from_address`) and backfills hooks stored with empty flags. Declared hooklist flags are informational; the bits win.
- **Routing eligibility** is `eligible | ineligible | unknown`, evaluated per hook and pool: `0x91` prefix or any returnsDelta bit → ineligible; a pool's `fee & 0x800000` decides dynamic fee when a pool is known, else `properties.dynamicFee` when present; neither known → unknown. A project is eligible if any of its pools is. The badge is still a rule, not a guarantee.
- **Registry membership** is stored (`hooks.hooklisted`), set every night by ingest from the full hooklist. The "✓ Hooklist" badge and the Registry activity entry render only when true; otherwise "Not in hooklist" / "Discovered on-chain".
- **Tab.** "Verified" is now "In Uniswap app" (eligible projects only). Audit status is a separate badge, not part of the tab.
- **Trade panel.** The decorative swap form is gone. The pool action card shows the pair, chain and fee, and a primary "Swap on Uniswap" only for an eligible pool with at least $1K TVL and two identified tokens; otherwise "View pool on Uniswap" plus the reason.

## Grouping and naming, package 7 (2026-10-09)

- **One code hash.** `code_hash` is `0x` + sha256 of the deployed bytes everywhere (`rules.code_hash`). Legacy keyless
  sha1 rows are re-hashed from public RPC by the group step (2,000 per run, new hooks first, then a window that moves
  daily, `REHASH_BUDGET_S` wall clock, null when the chain has no code).
- **Proxy status is a bytecode fact** (`hooks.is_proxy`, `hooks.proxy_kind`): a 45-byte runtime that starts with the
  EIP-1167 clone prefix, or a runtime under 2,048 bytes that reads the EIP-1967 slot; Blockscout's or Sourcify's own
  verdict wins when given. ERC-1967 proxy bytecode is shared by unrelated deployments, so a code_hash is evidence only
  when `is_proxy` is false or the hook is an EIP-1167 clone (its hash embeds the implementation); unknown is not. Auto-named projects that joined proxies by
  bytecode were split, and their generic stored contract names looked up again (proxies resolve to implementations).
- **Bytecode template families (rule 2b, 2026-10-13).** Launchpads deploy one hook per token from generated source, so
  every member has its own code_hash (immutables, metadata) and each landed as its own "Unlisted hook" card: 856 of
  5,633 projects were New on 10/13, 726 of them unnamed, mostly Robinhood and Ethereum memecoin hooks. `hooks.code_shape`
  (`rules.code_shape`) is sha256 of the runtime without CBOR metadata and with PUSH32 operands zeroed; selectors and
  constant addresses stay. Same shape on one chain, non-proxy, is evidence. Unlisted projects fold into the template's
  one verified-name project, else its oldest unlisted family ("Unlisted hook family 0x…"); two verified names on one
  template stay apart, curated projects never take part, old slugs redirect. New hooks join their template's project or
  form a family. Shapes come from public-RPC eth_getCode, `SHAPE_MAX` (3,000) per run, unassigned first. Dry run on live
  data: 1,385 unlisted projects into 184 families; 451 of the 856 New projects folded away.
- **Regroup.** Ingest flags a grouped hook (`hooks.regroup`) when the hooklist names or lists it; a code-hash write flags
  it too. The group step moves it into the curated project with that name, or renames its auto-named project in place,
  then pulls same non-proxy bytecode out of auto-named projects. Curated projects never give hooks up.
- **Names are candidates, merges need evidence**: shared non-proxy bytecode or a shared deployer that is not a CREATE2
  factory (`rules.merge_evidence`). A shared name alone, hooklist or verified source, is not evidence. Two same-named
  contracts on one chain without evidence stay apart; the later one gets a short-address suffix.
- **Generic names** (`rules.GENERIC`, matched on lowercased alphanumerics) never group by name: proxies, template
  defaults and the OpenZeppelin uniswap-hooks / v4 example names (LimitOrder, DynamicFee, FullRange, TWAMM, ...);
  bare words like Curve or Vault are real names. A hooklist name is also generic when its 2 to 20 entries show at least
  two same-chain deployer conflicts and no shared non-proxy bytecode (one team often uses a deployer per chain). A
  generic Claude answer becomes an address card. A generic-named project is split only when it has no enrichment and
  fewer than 3 hooks (otherwise the log says "would split"); its slug, receipts and enrichment follow most of its hooks.
- **Aliases.** Every rename, merge and regroup writes `project_aliases`; `/p/<old>` redirects through it. Merges move
  receipts and fill the target's missing enrichment.
- **Lookups.** The first useful contract name wins; a generic one is the fallback. A transient failure (network, 429,
  5xx) is asked again next run; Cloudflare 403 is a miss. The log line carries per-source outcomes.
- **Spend.** Paid POSTs retry only on connect errors and 429; a response lost after sending is recorded as ambiguous
  spend.
- **Out of scope:** a teams alias map. FablesRamp, FablesRampETH and FablesRWA have three stems, so the same-team
  merge (below) leaves them three cards even though they share a website.
- **Same-team merge (rule 4, 2026-10-14).** The registry lists one team's hooks under per-chain and per-version names,
  so "Aegis", "Aegis v2 Hook (Unichain)" and "Aegis v3 Hook (Unichain)" were three cards and Clanker fifteen.
  `rules.name_stem` drops versions ("v2", "V6", "v2.3.0"), chain words after the first word ("(Unichain)", "Base",
  "Robinhood Chain", "X Layer"; never the first word, Zora and Base are names), "L2" / "mainnet", the static / dynamic
  fee mode and "hook": Clanker Static Fee Hook v2 (Base) and ClankerHookDynamicFee are both `clanker`. "ETH" is an
  asset, not a chain. A stem is a candidate, never evidence: projects with one stem merge only when they share a
  signal that one name root owns (`stem_root`, the stem's first word):
  - a deployer that is not a CREATE2 factory, on any chain (hooklist `deployer`);
  - non-proxy bytecode, or its template (`code_shape`), on any chain: a team redeploys one template per chain, and the
    shape zeroes the per-chain immutables;
  - the same hooklist audit URL;
  - two of website host, GitHub owner and X handle, with none of the three disagreeing. Enrichment is found by
    searching the name, so one matching link mostly repeats the name. Shared hosts and platform accounts (github.com,
    uniswap.org, @Uniswap, ...) never count.

  "Owns" is the guard against templates: a signal that names with two roots carry (an OpenZeppelin hook deployed under
  two names, an auditor's portfolio URL) or that a generic name carries is nobody's. Unlisted projects neither count
  for nor against (rule 2b folds those). Generic names, names.py's "Name 0x1234…abcd" two-team splits, and auto-named
  projects holding proxies never take part. Curated projects do: this is the one rule that moves them. The kept
  project is the curated one with the richest enrichment, then the fewest deployment words ("Aegis" over "Aegis v2
  Hook (Unichain)"), then the most hooks; it takes the oldest `first_seen_at`, the others' missing enrichment and their
  receipts, and every old slug redirects (`project_aliases`, reason `merge:team`). Its name loses the deployment words
  its members disagree on ("Clanker Hook"; two chains of "Aegis v3 Hook (...)" become "Aegis v3 Hook"), unless another
  project already has that name. The rule runs last in the group step, so a hook under a new per-chain name folds in
  the night it appears, and the shape fill asks for rule 4 candidates' shapes right after unassigned hooks. It is
  idempotent. The project page lists the merged registry names as chips under the badges (`lib/variants`), a
  trailing "(Chain)" folded into the chip's chain list.

## Update 2026-10-10: the live layout, freshness, payload, indexer resilience

This section supersedes the visual and page parts of "UI direction (2026-10-07)"; the rules there (pink for the one
action and smart money, semantic green and amber) still hold.

- **Layout.** A dark terminal, not a light 480px column: three panes on desktop, the project list (340px), the project
  detail (flexible) and the pool and trade side panel (380px), max 1520px wide. Below 1180px the side panel drops;
  at 820px and under it is one column, list or detail by route, with the dock at the bottom. Theme tokens live in
  `web/src/app/globals.css` (`--panel`, `--fg2`, `--amber`, ...).
- **Freshness.** The project header says "Figures through <D-1>, updated nightly": the run dated D scores the closed UTC
  day D-1. When `/api/health` fails it adds one amber chip, a pipeline problem first ("Nightly update delayed" or
  "Nightly update incomplete"), else "Indexer catching up"; a swap-lag warning alone shows none. The footer names the
  same day. One pure function decides (`lib/health.freshness`) over the same reads as `/api/health` (`db/health.ts`),
  cached 60 s. The Live trades dot is green only when the feed answered and its newest trade is under an hour old.
- **Live trades freshness (2026-10-14).** `/api/trades` serves a cached pool list stale for at most 10 minutes; past
  that it refetches before answering and falls back to the old list only when GeckoTerminal fails (an idle pool's first
  visitor saw Hookr MOW/WETH trades "3d" old, Aegis 8h old, while GeckoTerminal had newer ones). Each browser poll gives
  up after 12 s with "Trade feed did not answer", so the panel never stays on "Loading trades…". When the newest trade
  is over an hour old the panel says so ("No trades over $10 in the last hour; the newest is 10h old.") and, then or
  when the list is empty, adds the indexer's closed-day figure for the pool, so it never contradicts the stat tiles
  silently. GeckoTerminal itself was not stale on 2026-10-14: its Robinhood feed for MOW/WETH matched its own h24 counts
  (the pool simply went quiet after 15:52 UTC), and the Ink feed for Aegis held a 00:36 trade Bait's cache had not.
- **Stat tiles** follow the pane by container query (6 columns from 720px, 3 by default, 2 under 420px) and never
  ellipsize a value. Week over week above +999% reads ">999%" (prose: "more than 999%"); weeks under $1K are no basis.
- **Payload.** Rows stay lean: whole dollars, no hook addresses. `GET /api/hooks` serves the address index; the list
  fetches it the first time a search starts with "0x". Superseded 2026-10-14 for the rows themselves: see "Feed payload
  and client filtering" below.
- **Indexer resilience.** Every HyperSync request has a wall-clock deadline (`HYPERSYNC_DEADLINE`, 120 s), since the
  idle timeout cannot end a request whose socket stays fed. Pool lookups spend at most their budget per chain per loop,
  checked before every call, and a cut scan resumes from its block next loop; the per-page log line shows rate-limit
  units spent and left. A prune lowers a hook's lookup floor to its oldest deleted pool, so the first loops after one
  pay off long scans in budgeted slices (BNB and Robinhood after 2026-10-09: a few hours behind head, health ok).
- **Builds.** Base images come from ECR Public (`public.ecr.aws/docker/library/...`), and Dockerfiles carry no
  `# syntax=` line: Railway's builders could not reach Docker Hub on 2026-10-09.

## Update 2026-10-14: swap pages never wait for pool lookups

Base, the biggest hook chain, sat ~708K blocks (~16 days) behind on swaps while every other chain was caught up. A swap
page in scan mode with pool ids that were neither rows nor known unhooked was held until a HyperSync Initialize scan by
pool id over `[floor, page end)` settled them. On Base each ~200-block page carried ~200 unknown ids (~25 new to the
process), the floor sits near the PoolManager's deployment, and a scan of that range is ~45 calls; with 30 lookup calls
per loop the cursor gained ~80-200 blocks per loop against a gap that grows ~30 blocks a minute. It could never catch up.

- **Rule now:** a swap page commits what it can count (rows, and keys of pools past the cap the process holds) and owes
  the rest: each other unknown id goes in `pool_lookup_queue` with the block range its swaps were left out of, in the
  page's transaction. After the pages, the resolver works the queue within the same per-loop call budget, in chunks of
  `INDEXER_LOOKUP_CHUNK` (2,000) ids per scan; a scan costs the same calls for one id or thousands, so one scan settles
  everything queued during the previous one. An unhooked id is deleted. A hooked one gets its swaps re-read by pool id
  over its own range and counted (late row or hook aggregate, as on a page) in one transaction with its queue delete,
  behind a compare-and-set on the unchanged swap cursor. No swap is skipped or counted twice, across restarts too.
- **Honest completeness:** `backfilled` flips only when both cursors are at the head and no queued id owes swaps from
  before the block where the swap cursor first reached it. Not "queue empty": after catch-up a Base scan spans several
  loops while new ids arrive every loop, so the queue is rarely empty. The live edge is covered per day instead: the
  nightly step (and a past-day re-score) treats a day as final only when no queued id owes swaps from before its end
  (`from_time`), so usage on a chain still settling keeps the keyless numbers, as before. The resolver asks the oldest
  owed ids first, so a backlog is never starved by newer ids.
- **Fairness:** unchanged. `INDEXER_MAX_PAGES` (40) still bounds swap pages per chain per loop and the lookup budget
  still bounds resolver calls; a busy chain now spends its slice on pages instead of waiting on scans. Chains without a
  capped hook never queue anything (every hooked pool is a row there).
- **Options weighed.** (a) "After the Initialize backfill, an id that is not a row is unhooked": true only on chains with
  no capped hook, which is already how they run (no floor, no lookups). Where a hook is past the per-hook cap or was
  pruned, its pools are hooked and deliberately not rows; treating them as unhooked would drop a launchpad's swaps and
  undo the griefing defence. (b) Resolve by pool id with one topic1-filtered query: already the lookup; the cost is the
  block range it walks, so the fix is more ids per scan, not a different query. (c) Decouple ingestion from resolution:
  adopted, with the queue in Postgres so a restart cannot lose what is owed. Not adopted: a full in-memory or stored index
  of every pool id past the cap (millions on Base; it would undo the cap's storage saving).
- **Unverified on this date:** HyperSync's limit on topic values per query. 2,000 ids is ~140 KB of JSON; if HyperSync
  refuses a lookup body (HTTP 4xx), the process halves the chunk (down to 100) and logs it.

## Update 2026-10-14: one figure per fact across pages

- **Unnamed labels.** An unnamed project is labelled by one pool everywhere (list, Leaders, project page, week page, OG
  image, RSS): most 7-day volume over closed days, then most TVL, then the lowest pool id (`lib/unlisted.compareTopPool`,
  mirrored by the `topPools` query). The project page's Trade panel shows that same pool (`lib/present.tradePool`), not
  the first pool by TVL. Digests store the Monday name; the week page and RSS render today's name for the same slug. A
  family's pair label ("X/Y hook family · 0x…") is still unnamed, so it stays out of Trending, New and the movers.
- **Traded projects.** /leaders "Projects traded, last 7 days" and the digest intro count the same set: non-spam,
  non-wash, 7-day volume > 0 (`totals.traded`, with TVL and volume summed over it). Digests written before 2026-10-14
  only carry `totals.projects` (every tracked project) and say "projects tracked".
- **Pool TVL.** A pool row's TVL is its latest snapshot up to the running day, as score sums it into the header. When the
  rows shown do not add up to the header (pools past the 50 loaded, untracked chains, a capped hook's aggregate), the
  note under the table says how much is elsewhere.
- **Audit badge.** "Audit linked" needs an http(s) link, and its title names the link. Hooklist audit URLs count; a
  Parallel one only when Parallel named a team and the project is not an unnamed address, and never Uniswap's own
  v4-core / v4-periphery audits.
- **One snapshot per page.** A project page's detail is cached per list load and pinned to that load's day, and takes
  its name and rank from the rows it ships with. Project, OG and week pages revalidate every 10 minutes (was 1 h).

## Update 2026-10-14: feed payload and client filtering

- **Pages ship one page of rows.** "/" and "/p/[slug]" no longer carry every project. A page server-renders the first
  100 rows of its own view (`lib/feed.feedSeed`: the URL's tab, chain and category; the default tab on the static
  project page) plus the category chips and the counts. On 2026-10-14 the full set (2,318 rows) was ~0.99 MB of every
  "/" and "/p/[slug]" response and of every project-link prefetch, and "/?tab=all" also rendered all 2,318 rows as HTML
  (2.44 MB). Per row that was ~430 bytes on every terminal page plus ~560 bytes of HTML on All; now it is 0 past the
  seeded 100.
- **The full set comes from `GET /api/rows`** (`lib/rows.ts`): packed as arrays under one key list (~170 bytes a row,
  ~25 gzipped, about 58 KB for the set), fetched once per visit on mount and kept across client navigations. The page
  passes a content hash (`?v=`); a reply that matches it is cached for a day, anything else for a minute.
- **Tab, category and chain change on the client.** The rules in `lib/feed.ts` are pure, so the list re-filters the
  loaded rows in the same frame and `history.pushState` moves the URL (Next's `useSearchParams` follows it), so links,
  reloads and Back still work and the first paint stays server-rendered. On a project page the list re-filters beside
  the open project (`/p/<slug>?tab=all`) instead of leaving it. The detail pane on "/" keeps the project the server
  picked for the first URL. Tab and chip links do not prefetch.
- **Lists page by 100** ("Show 100 more · N in all"); a new view starts again at one page, scrolled to the top.
- **Search box** is uncontrolled, so letters typed before hydration are kept and handed to the list on mount. Enter
  before the rows or the address index arrive goes to the top match once it appears.
- **Watchlist tab** fits the 320 px list column on one row: the In Uniswap app tab shows as "In app" there (full name
  kept as its accessible name and tooltip), with 6 px tab padding and a 2 px gap; the bar wraps rather than clips if a
  label grows. An empty watchlist reads "Star a project to keep it here."

## Wallet concentration (2026-10-15)

`project_daily.wallets_7d` and `top_wallet_share_7d` (migration 0018): distinct wallets across a project's pools over
the seven closed days, and the busiest wallet's share of the project's swaps in that window, from `pool_wallet_day`
(wallet = tx.from). Known only when every chain the project is on is ready and its indexed history reaches the start
of the window, otherwise NULL: a partial count would read as a small, concentrated project. The share is of swaps,
not volume, because per-wallet volume is a sample of ~300 trades a day while swap counts are complete on a ready chain.
Shown at any size on the project page ("Wallets 7d", "Top wallet"), with an amber "One wallet, N% of swaps" badge at
50% or more; it is information, not a wash finding, which still needs the package 8 thresholds. Asked for by the grant
review workflow: a small hook with no wash badge said nothing about who was trading it.

## Deployer attribution (2026-10-15)

`hooks.deployer` was registry-only. The group step now fills it for grouped hooks the registry did not cover, from
the explorer's contract creator (`DEPLOYER_LOOKUPS`, 200 a night, busiest projects first; a definitive miss is retried
after 14 days, a bot challenge or 5xx is not stamped), with `deployer_source` saying which. A registry value is never
overwritten. `project_daily.deployer_swap_share_7d` (migration 0019) is the share of the project's window swaps made by
its known deployer wallets on their own chains, CREATE2 factories excluded; NULL when no deployer is known or coverage
is unknown, 0.0 when a deployer is known and did not trade. The project page names the deployer per hook with an
explorer link and shows "Deployer's swaps"; an amber badge appears at 50% or more. First cut: the deployer wallet
itself, not wallets it funded.

Update 2026-10-16: the first nightly checked 200 hooks and stamped 1. Base and Robinhood's Blockscout instances answer
this server with a Cloudflare challenge (403), Ethereum and Unichain answer; busiest-first ordering put Base and
Robinhood hooks in nearly every slot, and as "transient" failures they were first in line again the next night. The
lookup now probes each chain once a night, skips a blocking chain without stamping its hooks, and fills the cap from
chains that answer (candidates read at 5x the cap). Routescan's getcontractcreation answers only Ethereum among these
chains, so it is not a fallback; Base, Robinhood, Arbitrum, Polygon and BNB deployers stay unknown until an explorer
answers or a trace-based source is added.

## Category fallback (2026-10-15)

89% of listed projects sat in "other" because a category only came from enrichment (a few dozen projects a night).
`rules.fallback_category` now guesses one for every project enrichment has not described, at the end of the group
step each night: words in the hooks' names, descriptions and verified contract names first (orders, lending, MEV/LVR,
oracle, compliance, LP management, gaming/NFT, launchpad, dynamic fee), then the launchpad pattern (a factory, or a hook
that initialized `LAUNCHPAD_MIN_POOLS` = 100 pools), then a dynamic fee (registry property or a pool's fee flag).
Nothing matching stays unguessed, never "other" by rule. `projects.category_source` (migration 0020) says `rules` or
`enrich`; enrichment overwrites a guess and the guess is re-made nightly while the project is unenriched.

Update 2026-10-16: the first nightly left 172 of the 276 projects at $10K+ in "other", because enrichment had answered
"other" and the fallback never touched it. The rules now also refine enrichment's "other" (a model that found no
category in its sources is not evidence against a dynamic-fee property or a launchpad pattern); a specific category
from enrichment is never replaced, and a lapsed guess on an enriched project goes back to "other". Measured on that
night's data: 42 of the 172 move.

## UX pass fixes (2026-10-16)

- `project_daily.tvl_usd` is NULL when no pool of the project has a known TVL (it was the sum of known pools, so "none
  known" read as $0 on 377 trading projects). One known pool at $0 is still a measured zero. The why-blurb prompt says a
  null metric is unknown, and a blurb that calls an unknown TVL zero is rewritten the next time the project is a
  signals target.
- `wallets_24h` is a measured 0 (exact) for a project whose every chain is ready and covers the window and that had no
  wallet on the closed day; it was NULL beside a measured "Wallets 7d 0".
- "Audit mentioned, not linked" counts a mention only when none of the four words before it negates it ("no audit is
  listed" is not a mention); 22 of 28 such badges were negations.
- The "One wallet" badge is dropped when the deployer's share equals the top wallet's share: same wallet, one badge.

## Round-trip wash (2026-10-21)

On 2026-10-16 and 17 an unlisted family of eleven zero-fee hooks on Robinhood chain moved $1.39B through 388 swaps in 9
transactions and sat at the top of the feed by 7-day volume, unflagged. One transaction was 80 swaps of 5M USDG back and
forth in a USDG/USDG pool; the second "USDG" copies the real token's first and last four hex digits. The real USDG moved,
so every swap was priced, and nothing was wrong with the figure as a sum of swaps: it was wash volume that cost nothing.

- **Why no rule fired.** The few-wallet rules need corroboration (volume over 50x a known TVL, no liquidity, or symmetric
  top wallets) so private and RFQ hooks are not flagged. GeckoTerminal valued the lookalike side at $210M of TVL, so
  volume over TVL read 2.4x; Robinhood has no top-wallet data; and 21 swaps per pool is more than the 10 that prove
  concentration by count. The lookalike rule did not see the pool because USDG was not in the chain's canonical list.
- **Rule now: `roundtrip`.** The indexer groups each page's swaps by (pool, transaction) and, when a group of at least two
  swaps nets to under 1% of its gross on both tokens, records the group's USD in `pool_daily_onchain.roundtrip_usd`
  (0021). Score flags a pool when its week is at least half such volume at $1M or more, with the usual half-of-project
  share. No wallet count or TVL is needed: nothing was bought or sold, so the round trips are the finding. A desk whose
  transactions do buy or sell is untouched; an arbitrage that touches a pool twice in opposite directions nets to its
  profit, not to nothing, and in any case rarely makes half a pool's week.
- **Also:** Robinhood USDG (`0x5fc5…d168`, DefiLlama confidence 0.99) joins the canonical stables, so the lookalike rule
  sees the USDG/USDG pool and the pool's TVL reads unknown; the two spoofs (`0x5fc5910f…d168`, `0x5fc5a1f1…d168`) are
  caught by address in every pool they sit in, PONS and CASHCAT included. And the same-symbol half of the lookalike
  rule no longer needs a canonical list: two "USDG" with two addresses in one pool is a lookalike on any chain. Bridged
  variants carry their own symbols (USDC.e, USDbC), so the rule does not collide with them.
- **Window.** Rows written before 0021 carry 0, so the 10/16-17 volume stays unflagged until it leaves the 7-day window
  on 10/24; the rule prevents the next one. A repair that re-reads swaps would fill it; none exists yet.
## Web fixes from the 2026-10-21 handoff

- **API redirects were absolute and wrong.** `GET /api/v1/projects/<retired slug>` answered 308 with
  `Location: https://0.0.0.0:8080/...`: the route built `new URL(path, req.url)`, and behind Railway's proxy `req.url`
  is the container's own address. `apiRedirect` now sends a relative Location; page redirects already did.
- **Database connections are recycled** (`max_lifetime` 10 minutes) and closes are logged as `[db] connection N closed`.
  Twice a long-lived web container had every data page stall while Postgres showed the web's trivial queries finished
  and parked in `ClientWrite`; #64 turned that into a 20 s timeout, this makes a stuck connection short-lived and
  visible. If it recurs, capture `pg_stat_activity` for `application_name = 'bait-web'` at that moment.
## Pool keys: unknown-in-both-tables means unhooked (2026-10-21)

Base stayed `partial` after the 2026-10-14 change: ~80K pool ids owed swaps to `pool_lookup_queue` on 2026-10-20, and the
resolver settles them at roughly one HyperSync scan per chunk per loop, against a free tier the live indexer already
uses up. The ids are mostly unhooked pools (not rows by design) and launchpad pools past the per-hook cap (not rows by
design either); the indexer has seen every one of them at Initialize, it just kept nothing about the second kind and
had to ask HyperSync again at swap time.

- **Rule now.** `v4_pool_keys` holds the key (pool id, hook, currencies, fee, spacing, block) of every hooked pool that
  is not a `v4_pools` row. The Initialize phase writes it for the pools it does not store (`store_pools`' unstored
  rows), and `python -m bait.indexer scan-pool-keys` backfills history from the deploy block in one unfiltered walk,
  resumable through `indexer_state.keys_block`, marking `keys_complete` when it reaches the Initialize cursor. On a
  keyed chain a swap page settles an unknown id from the table: a key is a hooked pool (counted as a late row or
  folded, as before); no key is an unhooked pool. Nothing is queued, nothing is looked up. Rows stay bounded as before
  (the cap is on rows, not keys; a key is ~550 bytes).
- **Why the table and not the resolver's memory.** `absent` and `recent` are per process and bounded (1M and 50K); a
  restart or a floor move forgets them, and Base alone has millions of ids. The queue was the honest version of that
  forgetting; the table removes the need for it.
- **Off until the scan completes.** `keys_complete` is per chain and false by default, so a deploy changes nothing until
  the one-off runs to the end (railway/README.md). Chains without a capped hook never consult the table (every hooked
  pool is a row there). A later `prune-pools` deletes rows that have no key, so the scan is rerun after a prune.
- **Not done here.** Deleting `pool_lookup_queue` and the resolver: the queue still drains the ids owed from before the
  scan, and chains without keys still need the lookups. Once every capped chain is keyed, both can go.

## 2026-10-24: Providers pruned

The owner dropped three external sources. What changed and what stays:

- **Parallel is gone.** No client, no `PARALLEL_API_KEY`, no run ids or tombstones, no price row or budget key; the cost
  preview no longer lists it. Enrich is Blockscout verified source plus one Claude `what_line` per busy project.
  `enrichment["parallel"]` and `parallel_run_id` / `parallel_run_at` already in Postgres are kept as they are (no
  migration): they are never sent to Claude, never become a website, GitHub or X link, and a pending run id no longer
  holds a project back. The web still counts a stored Parallel `audit_url` as an audit link under the old guard (a team
  was identified and the project is named). New projects get no website, GitHub or X link, so the GitHub signal and the
  "alive" flag reach only projects that already have one. `repair prose` lost its "missing" kind (Parallel paid, no
  line); the nightly enrich covers those.
- **The per-project Exa receipts search is gone.** `signals` no longer searches Exa, writes `receipts` or the `exa`
  signal. `alive` is a GitHub commit in the last 30 days. Stored receipts stay and still render on project pages and
  in the API (dated; nothing is added). Stored `exa` and `nansen` signal rows are frozen and left out of the blurb
  prompt. Exa now serves only the radar step (the Building tab), which the cost preview shows.
- **`last_mention_at` comes from the receipts.** The API read it from the stored `exa` signal, which outlived its
  receipts and sat on unnamed hook families (`unlisted-hook-family-0x0f48`: 2026-10-08, no receipts). It is now the
  newest `published_at` among the receipts the same response lists, null when there are none.
- **Nansen is off the site.** The smart-wallets badge, the Smart money pills and label tags on top wallets and live
  trades, the Leaders board and the OG pill are removed; `metrics.smart_wallets_24h` is removed from the read API. The
  labels were a static October file with no hits on today's top wallets. `wallet_labels` and its columns stay; only the
  API's undocumented `top_wallets[].labels` / `smart_money` still read it.
- **Env.** `BLOCKWORKS_API_KEY` was never read and is gone from the examples. `HYPERSYNC_API_TOKEN_SCAN` is read by
  `scan-pool-keys` (falling back to `HYPERSYNC_API_TOKEN`) and is now listed. `GRAPH_API_KEY` stays.

## 2026-10-24: Providers pruned, review follow-up

- **Nansen is out of the read API too.** `top_wallets[]` is now `{address, chain_id, volume_usd, trades, buys, sells,
  last_trade_at}`: `labels` and `smart_money` are removed like `metrics.smart_wallets_24h` (`api_version` stays 1;
  docs/api.md lists the three under "Removed fields"). Nothing reads `wallet_labels` any more; the table and its rows
  stay until a migration drops them. This replaces the last sentence of the Nansen bullet above.
- **`alive` is unknown without a GitHub reading.** With Exa gone, a GitHub commit date is its only input, and new
  projects no longer get a GitHub link. A project with no `github` signal now gets `{"alive": null}` instead of false,
  and the blurb prompt leaves an unknown `alive` out. As false it read as a fact: a live blurb said "the alive signal
  reads false, so treat the activity as unverified" of a project with no GitHub link. A repo with no commit in 30 days
  is still false. On the first nightly every signals target without a GitHub reading changes value once, so up to the
  signals cap (50) blurbs are rewritten that night, inside the cost preview's existing why_blurb upper bound.

## 2026-10-24: Deployer attribution

- **The deployer is the creation transaction's sender.** The lookup has been held since 10/16 (`DEPLOYER_LOOKUPS=0` on
  the production pipeline) because Blockscout's `creator_address_hash` for a CREATE2-mined hook is the factory:
  StablePairHook on Ethereum gives Arachnid's proxy `0x4e59…956c`, which deploys for anyone. Sourcify is now the first
  source (keyless, every chain, paced like the names step): `?fields=deployment` gives `deployment.deployer`, the
  sender. Recorded 2026-10-24 in `pipeline/fixtures/deployers-2026-10-24.json`: StablePairHook on Ethereum gives
  `0xe49a…5b95`, an EOA and the wallet the registry names (Blockscout's creation transaction has the same `from`);
  Clanker's static fee hook on Base `0xd466…1bd3`; UniswapV4KEMHook on Robinhood `0x6971…3cbc` (sent to a factory
  contract, and Sourcify still gives the EOA); StablePairHook on Arbitrum is not verified there (404); Aegis on Base is
  verified from its runtime alone, with every deployment field null. The sender is also the wallet the indexer counts
  swaps by (tx.from): a contract creator never sends a swap, so its share always read "none".
- **Blockscout is the fallback** for what Sourcify cannot name: the address record's `creation_transaction_hash`, then
  that transaction's `from` (one more call on the same instance), else the creator. A shared CREATE2 deployer
  (`rules.SHARED_DEPLOYERS`, which now also lists the Safe singleton factory, Zoltu's proxy, Create2Deployer and
  CREATE3Factory) is never stored, from any source and from the registry either (one live hooklist entry, a
  FactoryHook on Robinhood, names Arachnid's proxy): the hook stays unknown until a source names the sender. Rows that
  already hold one are cleared and unstamped by the next run. `deployer_source` is `hooklist`, `sourcify` or `explorer`.
- **Ingest no longer erases looked-up deployers.** It rewrote `hooks.deployer` from the registry every night, NULL for
  the entries that name none (4,718 of 4,994 on 10/24), and the stamp stayed: a looked-up deployer of a listed hook
  lasted one night in fourteen. The registry still wins where it names a deployer; one it stops naming is cleared and
  unstamped.
- **Budget and blocked chains.** `DEPLOYER_LOOKUPS` (200) is now the first 200 candidates, busiest first, each asked of
  Sourcify and, when it cannot answer, of Blockscout. A blocked instance (Base, Arbitrum, Polygon and Robinhood answer
  this server with a Cloudflare challenge) still costs one probe a night. One change: a hook the blocked instance
  leaves open is stamped when Sourcify answered it (a miss, rechecked in 14 days) rather than left unstamped;
  otherwise the same unverified Base and Robinhood hooks would head the queue every night, the 10/16 failure in another
  form. Without Sourcify the old rule holds (not stamped, not counted).
- **Display.** Score already left shared deployers out of `deployer_swap_share_7d` (NULL, not 0, when no other deployer
  is known). The project page's Deployer line now leaves them out too (`present.ts` mirrors the list; a pipeline test
  keeps the two equal) and names the source: registry, Sourcify or explorer.
- **Tests stay offline.** The CLI end-to-end test ran the names and deployer lookups against the live explorers and
  Sourcify (about 7,600 connection attempts per run with the network blocked); it now sets `NAMES_MAX=0` and
  `DEPLOYER_LOOKUPS=0`.
- **TODO.** Unverified hooks on the chains whose Blockscout challenges us stay unknown. Two routes: Blockscout PRO
  (`api.blockscout.com/{chain}`, which the names step already uses when `BLOCKSCOUT_API_KEY` is set) for the address
  and transaction records, or the creation transaction from HyperSync traces (the indexer reads logs only, so traces
  are not plumbed). Wallets the deployer funded are still out of scope.
- **To turn it on:** delete the `DEPLOYER_LOOKUPS=0` override on the production pipeline service and redeploy it.

Update 2026-10-24 (review): the lookup queue takes turns. Ordered by project volume alone, factory families' clones
took the budget. The 500 busiest projects held 4,918 hooks, 4,080 of them in 16 families, and 3,502 of those are
unverified clones on Base and Robinhood (Sourcify 404 for all 10 sampled, explorer challenged): a miss every time.
Rechecked every 14 days, they outnumber what 200 a night can ask in that window (about 3,000), so the queue stalled
behind them; replaying that shape for 45 nights left 292 of the 500 projects never looked up. Projects now take turns,
20 hooks each, busiest project first and newest hook first: every project's first 20 hooks come before any project's
21st, and a family's later clones get what is left. Replayed, each of the 500 is asked within 10 nights. Tests are now
held offline by `tests/conftest.py`: a name lookup or connect beyond loopback is refused and fails the test that tried.
The old guard only set `KEYLESS=0`, which is how the CLI test's explorer and Sourcify calls went unnoticed.

## 2026-10-24: Category coverage

On the 2026-10-23 feed, 56 of the 181 projects with $10K+ of 7-day volume were `other`, all unlisted. None had a
dynamic-fee pool, none was a factory or reached 100 pools, no hook had a registry name or description, and the 11
verified contract names (LaunchHook, SynthHookV2, ZeroTraceTaxHook, ...) matched no word rule, so nothing could fire.
45 of them show the web's placeholder line for an unidentified project.

- **Rule: "launch" in a hook's own name.** The word in a registry or verified contract name ("LaunchHook", "Fair Launch
  Hook") says launchpad, checked in the launchpad slot of the word rules so an earlier category's words still win. Never
  in a description: OHMVAULT's "80% at launch, decaying to 5%" is a fee schedule. In the feed, 27 of the 29 projects
  with the word in their name and a category were launchpads; the other two were categories enrichment wrote (one
  oracle, one dynamic fee), which a guess never replaces.
- **Rule: the launch pattern** (`rules.launch_pattern`), last, when nothing else says. Every hook of the project has
  beforeInitialize (it decides who creates its pools); the stored pools (folded `hook:` rows excluded) number at least 5,
  80% of them hold one shared token and the other coin is unique in 90% of those (one pool per coin); and either
  beforeInitialize is each hook's only permission, or each hook returns a swap delta, has no afterInitialize, and every
  pool's LP fee is 0 (the hook takes the fee). Against the categories in the feed: 3 of 3 gate-only and 51 of 54
  fee-taking projects were launchpads; the other three lines describe fee hooks on one token's or a launch platform's
  pools (LiquidGuard, Lumoria, Pangea). Without the beforeInitialize gate the share falls to 77-87%: tokenized-stock and
  leveraged-token venues on Ink and Robinhood have the same pool shape. afterInitialize is excluded because an oracle
  seeds its observations there (BackGeoOracle matched everything else).
- **Not added.** A dynamic fee from swap permissions, or from stable and same-symbol pairs: none of the 56 has a
  dynamic-fee pool, and a pool's dynamic flag already decides dynamic fee. Hooks with many pools at varied static fees
  (afterInitialize, afterAddLiquidity, swaps), the six full-liquidity-and-swap hooks on Ethereum's ELEVATE pools, and the
  single-pool hooks: permissions say what a hook can do, not what it is for.
- **Effect.** 6 of the 56 become launchpads: two LaunchHook contracts by name, ClankInitializationGuardHook by the gate,
  SynthHookV2, ChimeraHook404 and ERC20THook by the fee-taking pattern. Of the 926 projects the public API let us check
  (any volume, hooklisted, or a category), 10 `other` move, all to launchpad; no specific category changes.
- **Web: no category is shown as none.** `other` is the absence of a category, so it is never a chip, a filter or a
  label: the feed offers no Other chip (an old `?category=other` link lists everything), and the project page, leaders
  and share image print no "Other". The API still returns `other`. "Unclassified" was the alternative; a label for none
  still reads as a category, and the feed rows already omitted it.
- **The placeholder line is not stored.** "On-chain hook activity. Project identity and purpose have not been
  independently confirmed." is written by the web (`researchProject`) over whatever line is stored, for an unidentified
  project (an `unlisted-hook` slug or a "… hook · 0x…" name). The pipeline never writes it, so the enrich retry (empty
  lines only, `ENRICH_RETRY_DAYS`) needs no change.
