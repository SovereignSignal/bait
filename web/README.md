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
| `/` | terminal: movers strip, project list, detail of the list's first project, trade panel. `?tab=trending\|new\|verified\|all`, `?chain=<slug or id>`, `?category=<slug>` |
| `/p/[slug]` | the same terminal with that project selected (ISR 1h; phone shows detail + trade with a back link) |
| `/p/[slug]/opengraph-image` | 1200x630 OG card (next/og), night palette |
| `/leaders` | usage leaderboards: wallets 24h, 7d volume, TVL, smart-money wallets; usage totals header |
| `/api/trades/[chain]/[pool]` | live trades from GeckoTerminal for one pool (>= $10, newest 25, wallet_labels joined); 120s in-process cache; empty list on any error or 429 |
| `/week` and `/week/[date]` | weekly digest index and page (Monday dates) |
| `/feed.xml` | RSS: digests plus new projects |
| `/about` | method, scoring, routable rule, disclaimers |

Data pages export `revalidate = 3600` (ISR, 1h) on top of a 1h in-process cache in `queries.ts`. Client-side work: list search (name or hook-address prefix), the localStorage watchlist (star on the project header, Watchlist in the dock and the star tab), and the live trade feed (refreshes every 60s while visible). Verified tab = an audit link or routable in the Uniswap app.

Phone (<820px): `/` is movers + list, `/p/[slug]` is detail + trade; bottom dock: Feed, Search, Watchlist, Leaders.

## Layout

```
src/app/            routes (server components), globals.css (dark tokens from the terminal mock)
src/components/     Terminal shell, TopBar, ProjectList (list + movers), Detail, TradePanel, LiveTrades,
                    Dock, WatchStar, TerminalProvider (client state: search, watchlist), Avatar, Footer
src/db/schema.ts    Drizzle schema mirroring db/migrations (0001 + 0002: pool_stats, pool_top_wallets, wallet_labels)
src/db/queries.ts   getRows / getProject / getLeaders / getWalletLabels / getFeed / getDigest / listDigests
src/db/client.ts    lazy postgres.js + drizzle client
src/fixtures/       sample dataset (BAIT_FIXTURES=1 only)
src/lib/            feed rules (tabs, ranking, movers, search), routable rule, chains (+ GeckoTerminal ids,
                    explorers), formatters, presentation helpers, view types
```

Schema notes: `src/db/schema.ts` mirrors `../db/migrations` (the pipeline's SQLAlchemy metadata is the other copy). `pools.token0` / `token1` are the subgraph Token JSONB (`{id, symbol, name, decimals}`). `project_daily.is_spam` hides a project everywhere. 24h volume and txs are deltas between the latest two `pool_snapshots` days; wallets, buys and sells come from `pool_stats` (latest day; buys/sells may be null on older rows); top wallets from `pool_top_wallets` (latest day) with `wallet_labels` joined. The three activity tables may be missing or empty: each read has its own `safe()` and the UI shows "–" or an empty state. Registry hooks span ~22 chains; only the ten in `lib/chains.ts` are shown, and every `CHAINS[id]` lookup is guarded. `signals.nansen` still feeds the OG card's smart-money pill. Optional `projects.enrichment.secondary_categories` (array) and `.color` feed the list; `enrichment.parallel.audit_url` counts as an audit link.

`npm run db:generate` emits SQL from the schema into `./drizzle` (drizzle-kit) if you want to compare against `../db/migrations`.

## Docker

```sh
docker build -t bait-web .
docker run -p 3000:3000 -e DATABASE_URL=postgres://... bait-web
```

Multi-stage, `node:20-alpine`, `output: 'standalone'`.

## Env

See `.env.example`. `NEXT_PUBLIC_SITE_URL` is used for RSS links and OG metadata.
