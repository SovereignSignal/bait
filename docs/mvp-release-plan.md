# Bait: MVP release plan (2026-10-15)

> **Status 2026-10-21.** Tracks A, C and D shipped (#32 to #36, #45 to #52); B1's retry rule, B3, B6 and B7 shipped (#47,
> #51, #54, #56); B2 has the fallback (#53). Still open: B1's one-night top-300 enrichment run, B4 (Base pool-key
> scan), and Track E (the private share). The table below is the 2026-10-15 snapshot, kept as written.

Goal: a version of Bait that can be shared with strangers and open-sourced without anyone being misled by a number
on a card, and that a reviewer or builder can self-host from the README.

## Where it stands today

| Item | State |
|---|---|
| Local vs GitHub | identical: `main` = `origin/main` = fc67dfc, clean tree, every local branch merged |
| Remote branches | 17 merged `fix/*`, `demo-smoke` branches still on GitHub (delete), 1 unmerged: `mvp/verification-2026-10-14` (PR #31) |
| Production | pipeline ran 06:00 UTC today on fc67dfc, all 10 steps ok, 0 soft errors; 15 of 16 chains backfilled; Base `backfilled=false` (lookup queue, structural) |
| Feed (live `/api/rows`) | 2,161 projects; 637 with any 7d volume; 306 at $10K+; 3 wash-flagged; 44 routable-eligible |
| Descriptions | 1,758 of 2,161 have no what-line (81%); 102 of the 306 at $10K+ (33%); 30 of 131 trending (23%); 364 of 454 New (80%) |
| Categories | 1,926 of 2,161 are `other` (89%) |
| Repo | private; no LICENSE, CONTRIBUTING, SECURITY, dependabot, CODEOWNERS; branch protection unavailable until public |
| Secrets | tracked files and full history scanned: clean; only infra string in tree is the Railway URL |
| Tests | CI: pipeline (SQLite + Postgres 17), web unit, tsc, build, Playwright e2e; health check 08:30 UTC emails on failure |
| Open PR | #31 draft, 72 files, +8,997/-255, from the overnight verification run (see Track A) |

## What "MVP" means here

A visitor with no context can, in this order: understand what Bait is in ten seconds, scan the feed and trust the
figures, open a project and see what it does and where the evidence is, look up a hook by address, and self-host it
from the README. Nothing on a card is wrong in a way a block-explorer check would expose.

## Track A: decide PR #31 (gating, this week)

Reviewed 2026-10-15 (six slice reviewers, one skeptic per critical/high finding, synthesis; full write-up on the
Notion page "Bait: review and MVP release plan (2026-10-15)").

**Verdict: do not merge as-is; split it.** The work is sound; three things are wrong.

1. `pipeline/bait/steps/score.py:168`: `revival_measured` needs every hook chain `ready` + `tx7_known`, and
   `_prior_wash` carries the last `is_wash` row forward nightly, so on Base/Robinhood/BNB and on any project with one
   stale pool row a one-day wash flag never clears. High. Fix before part 1 lands; add a test on an unready chain.
2. `web/src/db/queries.ts:422`: between web deploy and migration 0016, `/` returns 200 (empty feed) while every
   `/p/<slug>` 500s. Softened by the live config: Railway already healthchecks `/api/ready` (60 s), so the deploy
   fails and the old build keeps serving; the READMEs saying `/` are stale. Still: apply migrations before merge and
   `safe()` the detail load.
3. Branch history carries base64 patch blobs, a write-capable PR-triggered workflow and a bot-authored commit.
   Medium. Squash-merge or rebase.
4. The import runbook has no blackout guard; a forgotten start-command override eats the 06:00 run. Medium.

Split: (1) activity rules + 0016 + web consumer + readiness, (2) ledger + 0017 + importer + Bunni fixture,
(3) candle refresh + parser hardening, (4) representative e2e + CI step, (5) `/p/[slug]` ISR change alone with a
repro. Drop `mvp-*.yml`, `scripts/mvp-source-check.py`, `docs/mvp-validation-2026-10-15.md` from main.

Rollout when parts land: `scripts/apply-migrations.sh` against production first; fix the READMEs (healthcheck is
already `/api/ready`); squash-merge outside 05:30 to 07:00 UTC; verify the first nightly receipt. Note the live
pipeline start command is a wrapper: `bait run` only 05:00 to 08:00 UTC, `repair all` at any other hour, so a manual
`deploymentRestart` outside that window runs a repair. Rollback = revert the merge; old code
ignores the new columns; run `db/rollback/0017` only once every service is on old code.

## Track B: data trust (what would embarrass us)

Ranked by how visible the problem is to a first-time visitor.

1. **Descriptions on everything that trades.** 102 of the 306 projects at $10K+ have no what-line. Enrich already
   picks never-enriched projects busiest-first, but `ENRICH_MAX` defaults to 20 a night (`steps/enrich.py:105`), and
   rows with `enriched_at` set and an empty what-line are never retried. Two fixes: one night with `ENRICH_MAX=150`
   and `BAIT_STEPS=enrich` (run `python -m bait cost-preview` first; the $3/day guard still applies), and a retry rule
   for enriched-but-empty rows older than 7 days. Dust projects stay blank on purpose.
2. **Categories.** 89% `other` makes the category chips useless. Either derive category from the hook's permission
   bits and the what-line (cheap, deterministic) or hide the chips until coverage is above ~50% of active projects.
3. **Audit badge vs text.** Aegis shows "No audit" while the what-line cites Spearbit and Trail of Bits. Rule: the
   badge is the registry's audit link only; if the blurb mentions an audit the badge has not, the blurb must not say
   "audited" (prompt rule in enrich + a consistency test like `consistency.test.ts`), or add a third state
   "audit mentioned, not linked".
4. **Base partial chip.** `backfilled=false` on 8453 because pool-id lookups cost ~1 HyperSync call per Base day and
   the queue never drains. Fix is the one already written up: persist every hooked pool key from one unfiltered
   Initialize scan (~9M rows, 1 to 2 GB) so unknown-in-both-tables means unhooked and the queue disappears. One-off
   scan on a second HyperSync token, then delete `pool_lookup_queue`.
5. **Known data questions.** Hookr concentrated wallet (~$7.1M, 296 trades, one wallet) with `is_wash=false`;
   Aegis Ink Oct 13 estimated $9.4K vs $54.0K candle; Bunni hidden as spam while dormant since the 2025-09 exploit.
   All three are in PR #31's scope; whether they ship depends on Track A.
6. **Small-hook signal** (from the grant-review session). Wash flags start at $250K/day or $1M/week, so a small hook
   with no flag says nothing. Always show unique wallets (7d) and top-wallet share on the card and detail, with no
   threshold. That is one query on `pool_wallet_day`, already indexed.
7. **Deployer attribution** (same source). Show the hook's deployer address and the share of 7d volume from wallets
   that are the deployer or funded by it. Deployer = contract creation tx sender (Blockscout `addresses/{a}` gives
   `creator_address_hash`; HyperSync traces as fallback). First cut: deployer only, no funding graph.

## Track C: a documented read API (makes it a review tool)

The grant-review session already uses Bait by scraping `/api/rows`. Make that a contract:

- `GET /api/v1/projects/{slug}` and `GET /api/v1/hooks/{0xaddress}` returning the project page's data as JSON
  (name, what-line, chains, hooks, pools, 7d/24h volume with the day they run through, TVL, wallets, flags, audit link,
  routable state, evidence links). Address lookup resolves to the project.
- `GET /api/v1/projects?sort=vol7d&limit=100` as the documented replacement for `/api/rows` (keep `/api/rows` for
  the page's own use).
- Same in-process cache and gzip as `/api/rows`; token-bucket per IP (`token-bucket.ts` exists); `X-Bait-Data-Day`
  header; a `docs/api.md` with one curl per endpoint.
- Effort: S. Everything it serves is already computed.

## Track D: open-source hygiene (one sitting)

| Item | Action |
|---|---|
| LICENSE | MIT (recommended: simplest for a personal project people will fork). Apache-2.0 if you want the patent grant. Your call. |
| README | rewrite for a stranger: one-paragraph what/why, screenshot, live URL, 3-command fixture quickstart, architecture (pipeline / indexer / web / Postgres), data sources with their rate limits, cost guard, "personal project, not affiliated" line, link to DESIGN.md as history |
| DESIGN.md | 46 KB of dated appendices where "later sections override earlier ones". Keep it, but add a 1-page `docs/architecture.md` that states the current rules once (grouping, scoring, wash, routable, windows) and link each to the DESIGN.md section it came from |
| CONTRIBUTING.md | how to run tests, the three services, the fixture mode, "no paid calls in tests", PR expectations (CI green, DESIGN.md note for rule changes) |
| SECURITY.md | private report path (email), scope (server-side fetches, XSS from third-party strings), no bounty |
| .github | `dependabot.yml` (npm, pip, actions, weekly); pin actions to SHAs (`sha_pinning_required` is off); PR template; CODEOWNERS = you |
| Branch protection | free once public: require CI, block force-push to `main` |
| One-off artifacts | decide keep / move to `docs/history/` / delete: `demo/` (Oct 8 static demo + hand blurbs), `mock.html`, `docs/plans/*` (agent plans), `docs/ux-audit-2026-10-10.md`, and from PR #31: `docs/evidence/*`, `mvp-*.yml` workflows, `scripts/mvp-source-check.py` |
| Fixtures | `pipeline/fixtures/hooklist.sample.json` (320 KB) is a copy of Uniswap/hooklist, which has no license file. Trim to ~20 entries or add a one-line provenance note |
| Remote branches | delete the 17 merged branches on GitHub |
| GitHub metadata | description, topics (`uniswap`, `uniswap-v4`, `hooks`, `defi`), homepage = live URL |
| Deploy guide | `railway/README.md` is Railway-specific; add a short "any host" section: Postgres, one cron, one always-on process, one web process, env list |

## Track E: share

1. Private share to 3 to 5 people with the live URL and the repo (Ken demo prep is done; the grant-review workflow is
   already a user). One week of feedback.
2. Fix what they trip on. Then flip the repo public, add branch protection, announce.

## Order

| Week | Do |
|---|---|
| 1 (this week) | Track A decision; Track D in one sitting; B1 (enrich order + one-night top-300 run), B3 (audit badge rule) |
| 2 | B4 (Base pool-key scan), Track C (API + docs), B2 (categories or hide chips) |
| 3 | B6, B7 (wallet concentration, deployer), Track E soft share |
| 4 | fix feedback, go public |

## Decisions only you can make

- License: MIT or Apache-2.0.
- Keep / archive / delete the one-off artifacts listed in Track D.
- Whether PR #31 merges whole, split, or not at all (Track A).
- UF relationship: the About page says personal and unaffiliated, and the grant-review session now cites Bait figures
  in UF evaluations. Keep the line in README too, and keep the rule the session already follows: any Bait figure used
  in a UF decision is re-checked on an explorer.
- Whether `/api/rows` stays public once `/api/v1` exists.
