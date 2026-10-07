# Hook feed — design v1

Working name: hookfeed. Personal project (Sov). Hosted on Railway. Not part of UF ops.

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
pool_snapshots(pool_id, chain_id, day, tvl_usd, volume_usd_cum, tx_count_cum,
               primary key (pool_id, chain_id, day))
project_daily(project_id, day, tvl_usd, vol_7d, vol_7d_prev, swaps_7d, pools_n,
              score_trending, is_new, primary key (project_id, day))
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

- New: project first_seen <= 7d AND (max pool TVL > $1k OR swaps_7d >= 10)
- Trending: vol_7d >= $10k; score = log1p(vol_7d) * clamp(vol_7d / max(vol_7d_prev, 1000), 0.5, 10)
  tie-break TVL delta; top 20
- Alive: GitHub commit <= 30d OR Exa hit <= 30d
- Spam filter: no pools, or all pools under $100 TVL and < 10 swaps lifetime -> hidden from All

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
hookfeed/
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
