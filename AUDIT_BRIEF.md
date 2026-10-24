# Bait: audit brief

You are auditing Bait, a personal project by one builder. Report findings and recommendations. Do not change
anything: no commits, no deploys, no paid API calls.

## What Bait is

A discovery feed for Uniswap v4 hooks: what is new, what is trending, who is using it, what it does.
Cards are grouped by project, not by hook address. The UI mixes a trading-terminal feed (fomo.family style)
with the classic Uniswap swap widget.

- Live: https://bait-production-2706.up.railway.app (routes: `/`, `/p/[slug]`, `/leaders`, `/building`,
  `/week`, `/week/[date]`, `/about`, `/feed.xml`, `/api/health`, `/api/trades/[chain]/[pool]`, `/api/launches`)
- Spec and history: `DESIGN.md` (read it first; later sections override earlier ones)
- Setup: `README.md`

## How it works

- `pipeline/` (Python 3.12): nightly Railway cron at 06:00 UTC. Steps in order: ingest (Uniswap hooklist),
  snapshot (GeckoTerminal + on-chain fallback), activity (trades, top wallets), onchain (copies
  indexer tables), group (hooks to projects, incl. verified-source naming in `steps/names.py`), enrich (Claude),
  score (7d volume, trending, new, spam and wash rules), signals (GitHub, Claude blurbs), radar (hooks being built;
  Exa + Claude), digest (Mondays).
- `pipeline/bait/indexer/`: always-on HyperSync indexer, 16 chains, every hooked v4 pool plus a 14-day swap window.
- `pipeline/bait/costs.py`: spend ledger and budgets ($3/day, $40/30 days) checked before every paid call.
- `web/` (Next.js 16, Drizzle, Postgres): server-rendered, ISR 1h, in-process caches.
- `db/migrations/`: schema. The pipeline also creates and extends tables itself.

## Run it locally

```
cd pipeline && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m pytest -q
cd web && npm install && npm test && npx tsc --noEmit
cd web && BAIT_FIXTURES=1 npm run dev     # sample data at http://localhost:3000, no database needed
```

The live site reads the real database; use it for UX review.

## Scope

1. **Code correctness.** Pipeline logic, SQL, window math in `steps/score.py`, grouping and naming, indexer
   decoding and reorg/catch-up handling, web queries in `web/src/db/queries.ts`.
2. **Data accuracy.** Do the numbers on the site match chain reality? Spot-check 5 projects against an explorer
   or GeckoTerminal. Are wash and spam rules too loose or too strict? Are projects grouped and named correctly?
3. **Cost and reliability.** Can a run overspend, double-pay, or silently no-op? What happens when an API is
   down, a step fails, or the indexer falls behind? Is `/api/health` enough to notice?
4. **Security.** Secrets handling, SQL injection, server-side fetches built from user input (`/api/trades`,
   token symbol lookups), XSS from third-party strings (contract names, Exa titles, Claude text), dependency risk.
5. **Performance.** The web loads every project per request (`loadAll`); about 2,500 projects today and growing.
   Page weight, query cost, cache behavior, cold starts.
6. **UX and UI.** Desktop and phone (375px). Is the feed scannable? Does a newcomer understand a card in 5 seconds?
   Does it feel like Uniswap? Empty, loading and error states. Accessibility (contrast, keyboard, screen reader).
   Copy. Information density. What would make someone come back daily?
7. **Product.** What is missing for a credible v1, and what should be cut?

## Known issues, already tracked (do not re-report unless you have new evidence)

- The indexer has not caught up on swaps for Robinhood, Base and BNB, so usage there comes from GeckoTerminal only.
- Hooks with unverified code stay unnamed; the web labels them by busiest pair.
- Blockscout instances for Base, Arbitrum, Polygon and Robinhood return Cloudflare challenges to the server.
- Only a handful of projects have "what it does" lines yet; enrichment fills about 40 per night.
- No all-time totals and no dormant all-time wash rule yet (see DESIGN.md "Wash rules, status").
- Swap history is kept 14 days; no long-term monthly aggregates; Swap.fee is not stored.
- Author vs integrator labeling is ambiguous (DESIGN.md "Data issues backlog").

## What to send back

One markdown report:

1. **Summary.** Five sentences or fewer: overall state and the three most important things to do next.
2. **Findings.** A table ranked by severity (critical, high, medium, low). Each row has: area, `file:line` or route,
   what is wrong, a concrete failure scenario, and the recommended fix.
3. **UX review.** Desktop and phone separately. Name the route and the element. Include before and after
   suggestions; sketches in ASCII are fine.
4. **Recommendations.** What to build, change, or cut for v1, in priority order, with rough effort (S/M/L).
5. **Open questions** you could not answer from the code.

Separate verified findings (you ran it or read the exact code) from suspicions. Do not pad with style nits.
