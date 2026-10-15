# Contributing to Bait

Bait is a personal project with one maintainer. Small, well-tested pull requests land fastest. Open an issue first for
anything that changes a rule (grouping, scoring, wash, routing, activity): those are product decisions, and the issue
is where they get made.

## Setup

Node 20, Python 3.12. A Postgres is optional. See `README.md` for the three-command fixture start; the per-service
details are in `pipeline/README.md`, `web/README.md` and `db/README.md`.

```sh
# pipeline: SQLite in memory, HTTP mocked, no keys, no network
cd pipeline && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m pytest -q

# web: unit tests, types, build
cd web && npm ci && npm test && npm run typecheck && npm run build

# web: browser suites (build first; each starts its own server on sample data)
cd web && npm run e2e && BAIT_E2E_REPRESENTATIVE=1 npm run e2e
```

Postgres-only tests skip unless `TEST_DATABASE_URL` (pipeline) or `BAIT_TEST_DATABASE_URL` (web) points at a disposable
database. CI runs them against Postgres 17.

## Rules for changes

- **No paid calls in tests.** Anthropic, Exa, Parallel and GitHub clients are mocked. A test that needs a key is wrong.
- **No secrets in the repo.** Keys live in `.env` (ignored) and in Railway variables. `.env.example` lists every
  variable with a comment and no value; keep it current when you add one.
- **Rule changes come with a DESIGN.md note.** `DESIGN.md` is the running spec: later sections override earlier ones,
  and each dated section says what changed and why. `docs/architecture.md` states the current rules once; update it in
  the same PR.
- **Shared fixtures stay shared.** `fixtures/routable_cases.json` and `fixtures/relevance_cases.json` are asserted by
  both the Python and TypeScript implementations so they cannot drift. Add cases there, not in one side's tests.
- **Schema changes are migrations.** Add `db/migrations/000N_*.sql`, mirror it in `pipeline/bait/db.py`
  (`ADDITIVE_COLUMNS` for new columns) and `web/src/db/schema.ts`, and say in the PR whether the web reads the new
  columns, because that decides the deploy order (`db/README.md`).
- **Numbers on a card must be defensible.** If a change can show a figure that a block explorer would contradict,
  it needs a test that pins the figure, and the About page copy must still be true.
- **Style.** Python: `ruff`-clean, type hints, module docstrings that say what the module reads and writes. TypeScript:
  `tsc --noEmit` clean, no `any` outside the DB boundary. Comments explain why, not what.

## Pull requests

CI must be green: pipeline tests (SQLite and Postgres), web unit tests, typecheck, build, both browser suites. Squash
merges only. Describe what a visitor would notice, what a self-hoster must do (env, migration), and what you did not
verify.

## Reporting data problems

A wrong figure, a mis-grouped hook or a missing project is a bug. Open an issue with the project slug, the chain, the
pool or hook address, what Bait shows, and what the explorer or GeckoTerminal shows, with dates. Security issues go
through `SECURITY.md` instead.
