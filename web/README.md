# Bait / web

Next.js App Router frontend for Bait: a dark trading-terminal view of Uniswap v4 hooks, grouped by project. Spec: `../DESIGN.md`. Visual and interaction spec: `../demo/fomo-blend.template.html` (fomo's terminal layout with the classic Uniswap swap widget as the trade panel).

## Run

```sh
npm install
npm run dev            # http://localhost:3000, fixture data (no DATABASE_URL)
npm run build && npm start
```

Set `DATABASE_URL` to read Postgres. For local UI work without a database, run with `BAIT_FIXTURES=1` to serve `src/fixtures/sample.ts` (16 projects from the Oct 8 snapshot in `terminal-sample.json`, with sample buy/sell splits and top wallets); the footer then says "Sample figures". With neither set, every page renders empty, which is what the Docker build does so no sample figures are ever baked into the image.

## Routes

| Route | What |
|---|---|
| `/` | terminal: movers strip, project list, detail of the list's first project, trade panel. `?tab=trending\|new\|verified\|all`, `?chain=<slug or id>`, `?category=<slug>`. Server-renders the first 100 rows of that view; tab, category and chain then change on the client (history.pushState). The New tab opens with the "Just launched" feed (latest 20 hooked pool initializations, filtered by the chain select) |
| `/api/rows` | every listed project as packed rows `{keys, rows}` for the list's search, watchlist and client filtering; `?v=<hash>` from the page, cached a day when it matches, else a minute |
| `/api/v1/projects`, `/api/v1/projects/[slug]`, `/api/v1/hooks/[address]` | the documented read API (`../docs/api.md`): stable snake_case over the same loads the pages use; 5 min cache, CORS, in-process rate limit (600/min) |
| `/building` | dev radar: radar items grouped by kind (Launch, Audit, Hackathon, Incubator, Repo, Registry) with kind chips (`?kind=<kind>`), a "Live on Bait" pill when the item matches a project, and the "Just launched" feed. Dynamic (reads `?kind`); reads cached in-process (radar 5 min, launches 30s) |
| `/api/launches` | latest 20 `v4_pools` rows as `{launches}` for the feed's 60s client refresh; 30s cache; empty list on any error |
| `/p/[slug]` | the same terminal with that project selected (ISR 1h, hidden for spam-flagged projects; phone shows detail + trade with a back link) |
| `/p/[slug]/opengraph-image` | 1200x630 OG card (next/og), night palette |
| `/leaders` | usage leaderboards: wallets 24h, 7d volume, TVL; usage totals header |
| `/api/trades/[chain]/[pool]` | live trades from GeckoTerminal for one pool (>= $10, newest 25); 120s in-process cache; empty list on any error or 429 |
| `/week` and `/week/[date]` | weekly digest index and page (Monday dates) |
| `/feed.xml` | RSS: digests plus new projects |
| `/about` | method, scoring, routable rule, disclaimers |

Caching: `/p/[slug]` and `/week/[date]` are ISR (1h). `/`, `/leaders`, `/building`, `/feed.xml` and `/week` render per request behind a 1h stale-while-revalidate in-process cache in `queries.ts` (an expired entry is served at once while one refresh runs; the cache holds at most 400 entries and is dropped within a minute of the pipeline scoring a new day). `/api/ready` checks Postgres and the `project_daily` columns added by migration 0016 with a read-only `LIMIT 0` query, so it returns 503 before that migration is applied; it is the endpoint for Railway's healthcheck; `/api/health` reports data freshness. Client-side work: tab / category / chain filtering over `/api/rows`, list search (name or hook-address prefix), the localStorage watchlist (star on the project header, Watchlist in the dock and the star tab), and the live trade feed (refreshes every 60s while visible). Verified tab = an audit link or routable in the Uniswap app.

Phone (<820px): `/` is movers + list, `/p/[slug]` is detail + trade, `/building` is the launch feed then the radar list; bottom dock: Feed, Search, Building, Watchlist, Leaders.

## Layout

```
src/app/            routes (server components), globals.css (dark tokens from the terminal mock)
src/components/     Terminal shell, TopBar, ProjectList (list + movers), Detail, TradePanel, LiveTrades,
                    Dock, WatchStar, TerminalProvider (client state: search, watchlist), Avatar, Footer
src/db/schema.ts    Drizzle schema mirroring db/migrations (0001-0003 core + activity, 0004 v4_pools / pool_daily_onchain, 0005 radar_items)
src/db/queries.ts   getRows / getProject / getLeaders / getFeed / getDigest / listDigests (+ safe())
src/db/radar.ts     getRadar / getLaunches (radar_items, v4_pools; own short caches)
src/db/client.ts    lazy postgres.js + drizzle client
src/fixtures/       sample dataset (BAIT_FIXTURES=1 only)
src/lib/            feed rules (tabs, ranking, movers, search), routable rule, chains (+ GeckoTerminal ids,
                    explorers), formatters, presentation helpers, view types
```

Radar and launches: `radar_items` (pipeline step "radar", `0005_radar.sql`) and `v4_pools` / `pool_daily_onchain` (on-chain indexer, `0004_indexer.sql`) may be missing or empty; both reads go through `safe()` and render an empty state. Radar rows with `score < 0.5` are hidden (kept by the pipeline only to avoid re-classifying). Launch pairs use `pools.token0/1` symbols when the pool is also in `pools`, the chain's native symbol for currency `0x0`, else short addresses; fee bit `0x800000` reads "dynamic". Launches and radar items can be on any chain: `lib/chains.ts` `anyChainName` / `anyChainColor` / `nativeSymbol` / `anyExplorerTxUrl` cover the ten plus a name map for the other hooklist chains (Zora, World Chain, Soneium, Celo, Monad, MegaETH, X Layer, Arc, Tempo, Linea, Blast) with a neutral dot; unknown ids render as "Chain <id>". With `BAIT_FIXTURES=1`, `src/fixtures/radar-sample.ts` serves sample radar items and launches (launch times relative to the request).

Schema notes: `src/db/schema.ts` mirrors `../db/migrations` (the pipeline's SQLAlchemy metadata is the other copy). `pools.token0` / `token1` are the subgraph Token JSONB (`{id, symbol, name, decimals}`). `project_daily.is_spam` hides a project everywhere. 24h volume, swaps and wallets are the closed UTC day before the latest run (`project_daily.vol_24h`, `swaps_24h`, `wallets_24h`; `wallets_exact = false` renders as "≤", an upper bound) and 7d windows are the 7 closed days ending there; buys, sells and the rolling 24h volume for the pace line come from `pool_stats` (latest day; buys/sells may be null on older rows); `project_daily.is_wash` keeps a project in All and on its page with a red badge, out of Trending, New, movers and Leaders; top wallets from `pool_top_wallets` (latest day); the frozen `wallet_labels` (Nansen, early October 2026) are no longer read anywhere. The activity tables may be missing or empty: each read has its own `safe()` and the UI shows "–" or an empty state. Registry hooks span ~22 chains; only the ten in `lib/chains.ts` are shown, and every `CHAINS[id]` lookup is guarded. The frozen `signals.nansen` and `signals.exa` rows are no longer read; the API's `signals.last_mention_at` is the newest listed receipt. Optional `projects.enrichment.secondary_categories` (array) and `.color` feed the list; a stored `enrichment.parallel.audit_url` (Parallel, before 2026-10-24) still counts as an audit link.

`npm run db:generate` emits SQL from the schema into `./drizzle` (drizzle-kit) if you want to compare against `../db/migrations`.

## Browser suites

`npm run e2e` runs `e2e/smoke.spec.ts` against a fresh `next start` on the October 8 sample data (`BAIT_FIXTURES=1`,
`DATABASE_URL` cleared). `BAIT_E2E_REPRESENTATIVE=1 npm run e2e` runs `e2e/representative.spec.ts` on
`e2e/fixtures/representative.json`: dated source replays plus explicitly synthetic lifecycle and redirect cases, see
[`e2e/fixtures/README.md`](e2e/fixtures/README.md). Build before each suite; the two datasets must not share ISR
pages, so CI builds once and restores a fresh copy of `.next` between the suites. Each suite uses its own port and never reuses a running server. Results land in
`test-results/smoke` and `test-results/representative` (`BAIT_E2E_OUTPUT_DIR` overrides). Neither suite says anything
about production ingestion or deployment.

## Docker

```sh
docker build -t bait-web .
docker run -p 3000:3000 -e DATABASE_URL=postgres://... bait-web
```

Multi-stage, `node:22-alpine`, `output: 'standalone'`.

## Env

See `.env.example`. `NEXT_PUBLIC_SITE_URL` is used for RSS links and OG metadata.
