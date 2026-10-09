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
4. factories flagged is_factory; card shows "748 hooks" as a stat, one card

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

- New: project first_seen <= 7d AND (max pool TVL > $1k OR swaps_7d >= 10, counted over the known days, a lower bound)
- Trending: vol_7d >= $10k; score = log1p(vol_7d) * clamp(vol_7d / max(vol_7d_prev, 1000), 0.5, 10)
  tie-break TVL delta; top 20
- Alive: GitHub commit <= 30d OR Exa hit <= 30d
- Spam filter: no pools, or all pools under $100 TVL and < 10 swaps lifetime -> hidden everywhere (404). A project
  whose lifetime or 7-day volume is $1M or more is never spam, so a wash-like whale (Optimism 0x69af: $436M through 8
  swaps at $0 TVL) is listed in All with its red badge instead of 404ing. Wash-like volume is not spam: see "Wash rules
  (package 8)".

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

Next.js App Router on Railway, Drizzle -> Postgres, ISR revalidate 1h.
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
  (`sampled`), and a swap page with pool ids left unresolved waits for the next loop instead of dropping swaps. That path
  alone would let an attacker bypass the cap with one dust swap per junk pool, so it has its own limits: a late row needs a
  priced swap of at least `INDEXER_LATE_INSERT_MIN_USD` ($10; unpriced never qualifies) and stops at
  `INDEXER_SAMPLED_POOLS_PER_HOOK` (1,000) per hook. Nothing is lost past both caps: those swaps and wallets are kept per day
  on the hook's aggregate pool `hook:<address>` (and in `swaps_total` / `volume_usd_total`), which the nightly step turns
  into a series and `pool_stats` with `vol_source = 'hook_total'`, so the project's volume, swaps and wallets stay true. The
  residual cost is bounded: at most 2,000 + 1,000 rows per hook, and every late row cost its creator a real $10 swap.
  Existing excess is trimmed once by `python -m bait.indexer prune-pools`.
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
- **Out of scope:** a teams alias map (FablesRamp, FablesRampETH, FablesRWA stay separate cards).
