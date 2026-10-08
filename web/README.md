# Bait / web

Next.js App Router frontend for Bait: one feed for Uniswap v4 hooks, grouped by project. Spec: `../DESIGN.md`. Visual spec: `../mock.html`.

## Run

```sh
npm install
npm run dev            # http://localhost:3000, fixture data (no DATABASE_URL)
npm run build && npm start
```

Set `DATABASE_URL` to read Postgres. For local UI work without a database, run with `BAIT_FIXTURES=1` to serve `src/fixtures/sample.ts` (the four mock projects plus six sample rows); the feed then shows a "Sample figures" note. With neither set, every page renders empty, which is what the Docker build does so no sample figures are ever baked into the image.

## Routes

| Route | What |
|---|---|
| `/` | feed; `?tab=new\|trending\|all`, `?chain=<slug or id>`, `?category=<slug>` |
| `/p/[slug]` | project page: card header, pools, 30-day TVL + volume sparklines, hooks with flags, signals, receipts |
| `/p/[slug]/opengraph-image` | 1200x630 OG card (next/og) |
| `/week` and `/week/[date]` | weekly digest index and page (Monday dates) |
| `/feed.xml` | RSS: digests plus new projects |
| `/about` | method, scoring, routable rule, disclaimers |

Data pages export `revalidate = 3600` (ISR, 1h). No client-side data fetching.

## Layout

```
src/app/            routes (server components), globals.css (token system from mock.html)
src/components/     Nav, ContextTiles, Filters, ProjectCard, Sparkline, PoolsTable, HookList, Footer (CSS Modules)
src/db/schema.ts    Drizzle schema mirroring DESIGN.md (projects, hooks, pools, pool_snapshots, project_daily, signals, receipts, digests)
src/db/queries.ts   getFeed / getProject / getContext / getDigest / listDigests; fixture fallback when DATABASE_URL is unset
src/db/client.ts    lazy postgres.js + drizzle client
src/fixtures/       sample dataset
src/lib/            routable rule, chain slug map, formatters, view types
```

Schema notes: `src/db/schema.ts` mirrors `../db/migrations/0001_init.sql` exactly (the pipeline's SQLAlchemy metadata is the other copy). `pools.token0` / `token1` are the subgraph Token JSONB (`{id, symbol, name, decimals}`); the pools table reads `->>'symbol'`. `project_daily.is_spam` drives the All-tab filter. `signals` is one row per `(project_id, kind)`; value shapes read here: nansen `{labeled: [{labels}]}` (smart-money count = labelled addresses matching the pipeline's markers), github `{last_commit_at}`, exa `{latest}`. `digests(week_of, body)` holds the pipeline's `{totals, new[], trending[]}` body; title and intro are rendered, not stored. Optional `projects.enrichment.secondary_categories` (array) and `.color` feed the card; `enrichment.parallel.audit_url` counts as an audit link.

`npm run db:generate` emits SQL from the schema into `./drizzle` (drizzle-kit) if you want to compare against `../db/migrations`.

## Docker

```sh
docker build -t bait-web .
docker run -p 3000:3000 -e DATABASE_URL=postgres://... bait-web
```

Multi-stage, `node:20-alpine`, `output: 'standalone'`.

## Env

See `.env.example`. `NEXT_PUBLIC_SITE_URL` is used for RSS links and OG metadata.
