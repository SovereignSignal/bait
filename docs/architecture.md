# Bait architecture and current rules

The rules as they stand today, in one place. `DESIGN.md` is the running spec with the history and the reasoning;
where the two disagree, this page is wrong and should be fixed in the same PR as the rule.

## Services

| Service | Code | Runs | Writes |
|---|---|---|---|
| pipeline | `pipeline/bait`, `python -m bait run` | nightly 06:00 UTC (Railway cron) | every feed table; a `run_receipts` row per run |
| indexer | `pipeline/bait/indexer`, `python -m bait.indexer` | always on, one loop a minute | `v4_pools`, `pool_daily_onchain`, `pool_wallet_day`, `indexer_state` |
| web | `web/`, Next.js | per request, in-process caches | nothing |

Nightly steps, in order (`pipeline/bait/steps/`): `ingest` (hooklist registry), `snapshot` (per-pool TVL and volume
from GeckoTerminal, subgraphs or public RPC), `activity` (trades and top wallets), `onchain` (copy the indexer's exact
usage into the feed tables), `group` (hooks into projects), `enrich` (the "what it does" line and category, from the
registry, the chain and verified source), `score` (windows, trending, new, spam, wash, activity status), `signals`
(GitHub activity, alive, the "why people care" line), `radar` (hooks being built; the only Exa caller), `digest`
(Mondays). Parallel and the per-project Exa receipts search were removed on 2026-10-24; the links, receipts and signal
rows they wrote stay stored and are not refreshed. Each step is idempotent per day: rows are keyed on their natural key plus `day` and upserted.

Spend: `pipeline/bait/costs.py` keeps an `api_spend` ledger and refuses a paid call past `SPEND_DAILY_USD` (3) or
`SPEND_MONTHLY_USD` (40). `python -m bait cost-preview` prints the worst case for a run.

## Identity: hooks to projects

Spec: `DESIGN.md` "Grouping", "Grouping and naming, package 7", "Same-team merge".

1. Hooks sharing a hooklist name are one project, unless unrelated teams share the name (deployer clashes on one chain
   and no shared bytecode).
2. Hooks with the same non-proxy bytecode (`code_hash`, sha256 of the deployed bytes) are one project. A proxy's hash
   is not evidence, except an EIP-1167 clone, whose hash embeds the implementation.
3. Launchpad templates: hooks with the same `code_shape` (runtime without metadata, constants zeroed) on one chain fold
   into the template's named project or an "Unlisted hook family".
4. One team's per-chain and per-version names merge when they share a name stem and one signal the name root owns: a
   non-factory deployer, bytecode or template, the same audit URL, or two of website host, GitHub owner and X handle.
5. Generic names (template defaults, OpenZeppelin examples, bare "Hook") never group by name. The long tail with only a
   description is classified once by a small model.

A verified contract name is a candidate, never evidence. Every rename and merge writes `project_aliases`, and old
slugs redirect.

## Usage figures

- Windows are closed UTC days: `vol_24h` is the last closed day, `vol_7d` the last seven, `vol_7d_prev` the seven
  before. A pool with no row on a day adds $0 of volume but an unknown swap count; a pool whose volume the onchain step
  could not measure is left out of every window, and when that is every pool the project's volume is NULL, never $0.
- The indexer is the source where it has caught up (`indexer_state.backfilled`, fresh, cursor past the window);
  GeckoTerminal's daily figures stand in elsewhere, and for hooks that keep their own accounting (custom accounting
  bit). Raw indexer USD uses current DefiLlama prices.
- Wallet counts are distinct across a project's pools where the indexer is exact (`wallets_exact`), otherwise an upper
  bound shown with "≤". Unknown TVL is never treated as zero. `wallets_7d` and `top_wallet_share_7d` (the busiest
  wallet's share of the week's swaps) are known only when the indexer has every swap of the project's hooks through the
  window: each hook's chain is ready, or its swap cursor is past the window and the hook is under the per-hook pool cap
  (nothing of it can be owed to the lookup queue); registry hooks on chains the indexer does not cover are out of scope;
  `deployer_swap_share_7d` is the same for the wallets that deployed the project's hooks (`hooks.deployer`: the
  registry's, else the sender of the hook's creation transaction from Sourcify or Blockscout; a shared CREATE2
  deployer is never one).
- Historical listing (`has_historical_usage`): at least 10 exact Swap events and $100 of priced volume in one real pool
  strictly before the current week keeps a project listed when it goes quiet. `activity_status` is `active` on any
  positive swap or volume in the window, `dormant` only with historical usage plus complete seven-day zero coverage on
  ready chains, else `unknown`.

## Ranking rules (`pipeline/bait/rules.py`, `pipeline/bait/steps/score.py`)

- **Trending**: `vol_7d >= $10K`, `vol_7d_prev >= $1K`, first seen over a week ago, no wash flag, not dormant.
  Score `log1p(vol_7d) * clamp(vol_7d / max(vol_7d_prev, 1000), 0.5, 10)`; tie-break on TVL; top 20 on the home page.
  Folded launchpad volume (a capped hook's aggregate pool) is displayed, never ranked.
- **New**: first seen within 7 days, and a pool above $1K TVL or at least 10 swaps in the week. Unnamed hooks and
  wash-flagged projects are excluded, and the New tab and the weekly digest skip a project whose 7-day volume is a
  measured $0 (an unknown week stays). The digest's Trending list is named projects only, as on the Trending tab.
- **Spam** (hidden, 404): no pools, or every pool under $100 TVL with fewer than 10 swaps lifetime, unless the project
  moved $1M or more or has historical usage.
- **Wash-like** (`WASH_REASONS`, badge in All, out of every ranking): pools carrying at least half the project's volume
  match one of: `24h` ($250K or more in a day through 25 wallets or fewer, corroborated by volume over 50 times a
  known TVL, no liquidity, or symmetric trading); `7d` ($1M in a week through 10 wallets or fewer, same corroboration,
  or carried by 10 swaps or fewer); `concentrated` ($1M in a week through at most two wallets); `vol_tvl` (7-day
  volume over 300 times a known TVL with fewer than 50 wallets); `no_liquidity` ($1M in a week through a pool with at
  most $1K of known TVL and fewer than 50 wallets); `symmetric` (top wallets buy and sell equally and carry most of
  the pool); `uniform` (top wallets' volumes within 3% of each other, each near the pool's per-wallet average);
  `unverified` ($1M or more through pools too small to list, with no wallet data); `lookalike` (a token in the pool
  copies USDC, USDT or the wrapped native coin by symbol or by the first and last four hex digits of its address);
  `roundtrip` (on indexed chains, at least half the pool's week is swaps that came and went inside single
  transactions, netting to under 1% of their gross on both tokens; `pool_daily_onchain.roundtrip_usd`). A
  finding is remembered through quiet or unknown nights for 14 days after it was last measured, then forgotten unless
  measured again.
- **TVL trust** (`rules.is_lookalike_pool`, `rules.is_idle_tvl`): GeckoTerminal's `reserve_in_usd` values every side at
  the pool's own price. Score treats a pool's TVL as unknown, and blanks it on that day's snapshot so the page agrees,
  when the pool has a lookalike token or holds $1M or more that traded under 0.1% of itself over a fully measured week.
- **Routable** (`eligibility`): computed from the permission bits in the hook address. A hook is `eligible` when it
  sets none of the returns-delta bits (the published rule's two, plus `afterAddLiquidityReturnsDelta` and
  `afterRemoveLiquidityReturnsDelta`, stricter than Labs' rule), is not a `0x91…` address, and has a static-fee pool;
  `unknown` without a known pool. Uniswap's published lists come first (`web/src/db/routing-lists.ts`, read from
  Uniswap/uniroute-public every 6 hours): a denylisted hook is never routable, an allowlisted one always is. Every
  pool's fee counts (one grouped query per load), so the list, the API and the project page agree; a hook's
  `dynamicFee` property is a fallback only when none of its pools is known.

Categories: dynamic-fee, launchpad, orders, lp-management, compliance, oracle, mev-lvr, lending, gaming-nft, other,
assigned during enrichment; until then `rules.fallback_category` guesses from hook properties, pool fee flags, the
factory pattern and name words (`projects.category_source` = `rules`).

## Web

Server-rendered from Postgres only. `/` ships the first 100 rows and the client fetches the full packed set from
`/api/rows` for search, watchlist and filtering. Project and week pages are ISR (1 hour); the other pages sit behind a
1-hour stale-while-revalidate in-process cache, one per server process and shared with the route handlers, that starts
loading when the server boots and reloads in the background within a minute of the pipeline scoring a new day.
`/api/ready` is the healthcheck (503 until the schema the build reads exists); `/api/health` is data freshness. The
routing-eligibility and receipt-relevance rules are implemented twice (Python and TypeScript) and both assert the
shared fixtures in `fixtures/` (no step writes receipts any more; the web still filters the stored ones).

## Where things are decided

| Question | File |
|---|---|
| thresholds and wash rules | `pipeline/bait/rules.py` |
| windows, trending, activity status | `pipeline/bait/steps/score.py` |
| grouping and naming | `pipeline/bait/steps/group.py`, `pipeline/bait/steps/names.py` |
| which chains and sources | `pipeline/bait/config.py`, `pipeline/bait/keyless.py`, `pipeline/bait/indexer/chains.py` |
| what a card shows | `web/src/lib/present.ts`, `web/src/lib/feed.ts` |
| copy on the About page | `web/src/app/about/page.tsx` (must agree with this file) |
