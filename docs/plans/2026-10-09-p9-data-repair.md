# Package 9: Data repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One `python -m bait repair` command family that brings the stored data up to package 8's window semantics without waiting two weeks: per-day snapshot volume for the closed days the windows read (GeckoTerminal daily candles, free), every stored run day re-scored with package 8's rules, a free spot check against GeckoTerminal, and, only after the owner's go, Claude rewrites of the what-lines the old code cut mid-word and the blurbs written with the old window text. Every step is idempotent and resumable, respects the run lock and the spend Meter, and leaves a `run_receipts` row.

**Architecture:** One new module, `pipeline/bait/repair.py`, wired into the CLI as `python -m bait repair <command>`. It reuses what exists: `keyless.Http` pacing (`KEYLESS_SPACING`) through a small `keyless.fetch_daily` split out of `fetch_ohlcv`; `score.run` unchanged (its package 8 past-day readiness rule applies by itself because `ctx.day < today`); enrich's what_line context and row update split into `enrich.what_context` / `enrich.what_values`; `signals.select_targets` and `signals._blurb_ctx` for blurbs; the Claude client's prompts split into `what_line_prompt` / `why_blurb_prompt` / `prose_estimate` so a dry run prints exactly the worst case the Meter checks before each call. One additive table, `project_daily_p9_backup`, defined in `db.py` beside `project_daily` and created by `create_all` (no migration file): the pre-repair copy of each re-scored day, so a bad re-score can be undone. It is dropped in a later cleanup (DESIGN.md backlog).

**Tech Stack:** Python 3.12, SQLAlchemy 2 Core, pytest. Postgres 15+ (`pg_ctl`, `initdb` on PATH) for `tests/test_postgres.py`. No web, indexer or migration-file changes.

**Spec:** Orchestrator brief for package 9 (2026-10-09), package 8's rollout note ("rows written before 0012 have no per-day values; re-scoring 14 past days needs a one-off GeckoTerminal OHLCV pull with limit 30"), and `web/src/lib/health.ts` (the latest receipt decides `/api/health`).

## What the repair touches (read this first)

The nightly run on UTC day `D` reports closed days: 24h = `D-1`, 7d = `D-7..D-1`, prev 7d = `D-14..D-8` (package 8). Re-scoring run day `D` therefore reads 14 closed days back. `--since N` means the run days `today-N .. today` that already have `project_daily` rows; their windows read the closed days `today-N-14 .. today-1`. Thirty candles cover that for `N <= 15`, which is the `--since` limit.

| command | reads | writes | paid | lock | receipt `notes` |
|---|---|---|---|---|---|
| `backfill-days` | pool_snapshots, pools, hooks, pool_stats; GeckoTerminal `/ohlcv/day?limit=30`, one call per pool, 10 s apart | pool_snapshots `volume_usd_day`, `estimated=false` on replaceable rows of closed days; a new row for a traded day that has none | no | yes | `repair backfill-days` |
| `rescore` | everything `score.run` reads | `project_daily(day)` per stored day, after a one-time copy into `project_daily_p9_backup` | no | yes | `repair rescore` / `repair rescore-restore` |
| `prose --what-lines` | projects (stored `enrichment`), hooks | projects `what_line` (+ category, links, `enriched_at` for "missing") | Claude only | yes | `repair prose` |
| `prose --blurbs` | `signals.select_targets` on the latest day, signals, receipts | projects `why_blurb`, `blurb_at` | Claude only | yes | `repair prose` |
| `verify` | project_daily, pool_snapshots; GeckoTerminal, at most `sample x 3` calls | nothing but its receipt | no | yes | `repair verify` |
| `all --dry-run` | as above | nothing | no | no | none |

A replaceable row is one written before 0012 with a known, non-zero cumulative (`volume_usd_day IS NULL AND volume_usd_cum > 0`), or an estimate on a closed day (`estimated IS TRUE`: GeckoTerminal's rolling 24h for pools past the OHLCV cap, or a running-day candle a later night never replaced). Never replaced: candle and on-chain rows (`estimated = false`), zero seeds (`volume_usd_cum = 0`: nothing traded before, the day adds $0 either way), rows whose cumulative is NULL (volume the onchain step marked unknown), and the running day.

## Amendments to the brief (planner, 2026-10-09)

1. **Backfill window.** The brief selects pools with a NULL row "in the last 14 closed UTC days". Re-scoring the run day `today-14` reads back to `today-28`, so the window is `today-since-14 .. today-1` (28 closed days for `--since 14`), which is what the limit of 30 candles is for.
2. **Backfill also replaces closed-day estimates** (`estimated IS TRUE`), not only NULL rows: package 8 writes pools past the OHLCV cap as yesterday only, from the rolling 24h. A candle is the exact closed day, and package 8's own precedence is "a candle row wins". Zero seeds and NULL-cumulative rows are left alone (above).
3. **Backfill runs only after a package 8 nightly** (exit 5 otherwise). Before that night, every on-chain pool's rows are still pre-0012 and every one would be pulled from GeckoTerminal for data the onchain step writes for free that night.
4. **Re-score covers stored days only.** Production has `project_daily` rows only since the first deploy (2026-10-08), so `--since 14` re-scores two or three days, not fourteen. A re-score never creates a day the nightly has not written. Re-score also refuses to run before a package 8 nightly (every window would read $0).
5. **Rollback needs a copy.** Re-running `rescore` writes the same rows from the same inputs; it does not bring back the old ones. `rescore` copies each day's rows into `project_daily_p9_backup` once (the first copy is the pre-repair state), and `rescore --restore` puts them back. The table is defined in `db.py` (coordinator decision below).
6. **"Missing" what-lines are selected by `enriched_at IS NULL`, not `what_line IS NULL`.** The 29 projects whose enrich stopped at the budget still carry the group step's hooklist placeholder; `enriched_at` is what stays NULL. The nightly enrich would also pick them up on its own (they are still candidates), so this only speeds it up.
7. **"Cut" what-lines do not require stored Parallel facts.** The old code (`strip()[:200]`) cut Claude lines, which exist for projects enriched before `ENRICH_MAX` (2026-10-08 17:21 UTC) with or without Parallel. The heuristic is: exactly 200 chars and the last char is not `.`, `!`, `?`, `…`, `"`, `'` or `)`. The old cut never added "…"; a line ending in "…" came from today's `cut_line` and is a word-boundary cut, so it is left alone. A cut line gets only its `what_line` rewritten (category kept); stored facts are passed when present. The dry run splits the counts so the owner sees how many have Parallel facts.
8. **Blurbs are rewritten when `blurb_at` predates package 8's merge** (2026-10-09 15:29:25 UTC, `--stale-before` overrides), so a rerun skips what is done, and only for targets whose row has `window_end` (scored with package 8).
9. **Receipts and `/api/health`.** Health reads the latest receipt by `finished_at`. So: a repair writes `soft_errors = 0` (its GeckoTerminal misses go in `steps`, not into the nightly's alert threshold), dry runs write no receipt, and a repair refuses to start (exit 5) when the latest nightly receipt failed, so a green repair receipt never hides a red night. A failed repair does turn health red until it is rerun or the next nightly writes; that is intended.
10. **The budget variable is `SPEND_DAILY_USD`** (and `SPEND_MONTHLY_USD`), not `BUDGET_DAILY_USD`. `--max-usd` / `--prose-budget-usd` only lower the ceiling for one run; they never raise a budget.
11. **Backfill stops between pools at 05:30 UTC by default** (`--stop-at`), so a long pull never holds the run lock when the 06:00 nightly starts. A rerun resumes.

## Decisions after review (coordinator, 2026-10-09)

Plan approved; the amendments above stand. One change: `project_daily_p9_backup` is defined in `db.py` (`create_all` creates new tables, so no migration file) instead of being created ad hoc at run time, and its later drop is noted in DESIGN.md's backlog. Exit 5 before a package 8 nightly stays; the orchestrator triggers a manual nightly first so the repair can run the same day.

## Global Constraints

- Work only in this worktree, branch `fix/p9-data-repair`. Task 0 merges `origin/main` (package 8 is merged there as fb4b73d). Baseline after it: **B = 277 passed / S = 11 skipped** (SQLite), **P = 11 passed** (Postgres). Every task states its expected counts relative to those.
- Tests: `cd pipeline && .venv/bin/python -m pytest -q`. Postgres: `cd pipeline && export PG_TEST_DIR=/tmp/bait-pg-p9 PG_TEST_PORT=5580 && eval "$(scripts/pg-test-cluster.sh start)" && .venv/bin/python -m pytest -q tests/test_postgres.py; scripts/pg-test-cluster.sh stop; unset TEST_DATABASE_URL PG_TEST_DIR PG_TEST_PORT`. Export the two variables first: a `VAR=x eval "$(...)"` prefix does not reach the command substitution, which then starts the default cluster under `$TMPDIR` on port 5499, and a later `stop` with `PG_TEST_DIR` set misses it.
- No network and no paid API calls in tests: GeckoTerminal is `FakeGT` (injected through `ctx.extras["keyless_http"]` or by monkeypatching `repair.keyless_http`), Claude is `FakeClaude`, which goes through the Meter like the real client.
- Do not touch `web/`, `pipeline/bait/indexer/`, `pipeline/bait/rules.py`, `pipeline/bait/steps/score.py`, `db/migrations/`, `fixtures/`. `pipeline/bait/db.py` gains only the backup table (Task 5). Package 8's scoring rules stay as they are; `score.run` is called, never changed.
- Commit after every task. Imperative subject, short body when the why is not obvious. Every `git commit -m` below is shorthand: end every commit message with these two trailer lines, exactly:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01A2FHLwk9NeThmyZ1JbHe3o`
- No hostnames, absolute paths, or secrets in code, docs, or commits. Do not push.

## Review Focus

1. Backfill writes only replaceable rows of closed days (`today-since-14 .. today-1`): never the running day, never a candle or on-chain row, a zero seed or a NULL-cumulative row. A day with volume and no row gets one that keeps the pool's known/unknown state (cumulatives carried from the nearest earlier row). `tx_count_day` is never invented. A pool with no candles or a failed call is left alone and retried by a rerun; a finished pool is never pulled again. (Task 4)
2. Re-score: stored days only, oldest first, one transaction per day, the first backup kept, `--restore` exact; readiness is `score._ready`'s past-day rule untouched. (Task 5)
3. Paid steps: every call passes the Meter (daily, 30-day) and the run's cap; the dry-run estimate is the exact pre-call check; no Parallel, Exa or Blockscout call; a wash project never gets a blurb; a "cut" rewrite keeps the category. (Tasks 2, 3, 6)
4. Lock and health: exit 3 under the run lock with no receipt; dry runs write nothing; exit 5 after a failed nightly or before a package 8 nightly; one receipt per sub-step with `soft_errors = 0`. (Task 8)
5. Verify compares the same pools over the same closed windows on both sides. (Task 7)

### Task 0: Sync with main and record the baseline

- [ ] **Step 1:** `git fetch origin && git merge origin/main` (package 8's merge commit; expect a fast-forward or a clean merge, no conflicts).
- [ ] **Step 2:** Run both suites (Global Constraints) and record **B / S / P**; expected 277 / 11 / 11.
- [ ] **Step 3:** No commit unless the merge produced one (then `git commit --no-edit` with the trailers appended).

### Task 1: `keyless.fetch_daily`

**Files:** Modify `pipeline/bait/keyless.py` (`fetch_ohlcv`, around line 405). Test `pipeline/tests/test_keyless.py`.
**Interfaces:** `fetch_daily(http, net, pool_id, limit=15) -> list[tuple[int, float]] | None`: one pool's daily candles oldest first as (UTC day start, USD volume); `[]` when GeckoTerminal answered with none; `None` when the call failed. `fetch_ohlcv` uses it (same path, same `limit=15`, same result).

- [ ] **Step 1: Failing test** (append to `tests/test_keyless.py`)
```python
def test_fetch_daily_parses_candles_oldest_first_and_none_on_failure():
    from bait import keyless as K

    class Http:
        def __init__(self, res):
            self.res, self.paths = res, []

        def gt(self, path):
            self.paths.append(path)
            return self.res

    h = Http({"data": {"attributes": {"ohlcv_list": [[1759968000, 1, 2, 0.5, 1.5, "1200.5"], [1759881600, 1, 1, 1, 1, None]]}}})
    assert K.fetch_daily(h, "base", "0xpool", limit=30) == [(1759881600, 0.0), (1759968000, 1200.5)]
    assert h.paths == ["/networks/base/pools/0xpool/ohlcv/day?aggregate=1&limit=30&currency=usd"]
    assert K.fetch_daily(Http({"data": {"attributes": {"ohlcv_list": []}}}), "base", "0xpool") == []
    assert K.fetch_daily(Http(None), "base", "0xpool") is None
```
- [ ] **Step 2:** `cd pipeline && .venv/bin/python -m pytest -q tests/test_keyless.py -k fetch_daily` → FAIL (`AttributeError: module 'bait.keyless' has no attribute 'fetch_daily'`).
- [ ] **Step 3: Implement.** Replace `fetch_ohlcv` in `keyless.py` with:
```python
def fetch_daily(http: Http, net: str, pool_id: str, limit: int = 15) -> list[tuple[int, float]] | None:
    """One pool's daily candles, oldest first, as (UTC day start in unix seconds, USD volume). The newest candle is the
    running day when the pool traded today. GeckoTerminal sends candles only for days with trades, and no trade counts.
    None when the call failed (HTTP error, 404, network)."""
    res = http.gt(f"/networks/{net}/pools/{pool_id}/ohlcv/day?aggregate=1&limit={limit}&currency=usd")
    if res is None:
        return None
    lst = ((res.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return sorted((int(r[0]), float(r[5] or 0)) for r in lst)


def fetch_ohlcv(http: Http, hooked: list[dict], cap: int) -> None:
    ranked = sorted(hooked, key=lambda p: -float(p["attrs"]["volume_usd"]["h24"] or 0))
    if len(ranked) > cap:
        log(f"OHLCV capped at {cap} of {len(ranked)} hooked pools; the rest carry 24h volume only")
    for p in ranked[:cap]:
        p["daily"] = fetch_daily(http, p["net"], p["attrs"]["address"]) or []
```
- [ ] **Step 4:** full suite → **B+1 = 278 passed, 11 skipped** (`tests/test_snapshot_keyless.py` still passes: same path and result).
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/keyless.py pipeline/tests/test_keyless.py
git commit -m "keyless: fetch_daily, one pool's daily candles, shared by the OHLCV fetch and the repair"
```

### Task 2: Prose prompts and their worst case, outside the client

**Files:** Modify `pipeline/bait/clients/claude.py` (`what_line`, `why_blurb`, end of file). Test `pipeline/tests/test_claude_client.py`.
**Interfaces:** `what_line_prompt(ctx) -> str`, `why_blurb_prompt(ctx) -> str` (the exact user messages, unchanged), `PROSE_MAX_TOKENS = 600`, `prose_estimate(user) -> float` (= the `anthropic_estimate` that `Claude._json` passes to `meter.check` before the call).

- [ ] **Step 1: Failing test** (append to `tests/test_claude_client.py`)
```python
def test_prose_estimate_is_the_check_the_meter_sees():
    from bait.clients import claude as C
    from bait.costs import Budgets, Meter

    class Spy(Meter):
        def __init__(self):
            super().__init__(None, budgets=Budgets(100.0, 100.0, {}))
            self.checked = []

        def check(self, provider, est_usd):
            self.checked.append(est_usd)

    ctx = {"name": "Bunni", "metrics": {"vol_7d": 1.0}}
    client, m = fake_client({"why_blurb": "One. Two."})
    c = Claude(client=client)
    c.meter = Spy()
    c.why_blurb(ctx)
    client2, _ = fake_client({"what_line": "Does a thing.", "category": "other"})
    c2 = Claude(client=client2)
    c2.meter = c.meter
    c2.what_line(ctx)
    assert m.kwargs["messages"][0]["content"] == C.why_blurb_prompt(ctx)
    assert c.meter.checked == [C.prose_estimate(C.why_blurb_prompt(ctx)), C.prose_estimate(C.what_line_prompt(ctx))]
    assert C.prose_estimate("x" * 12_000) == pytest.approx(((12_000 + len(C.SYSTEM)) // 3 + 50) * 2 / 1e6 + 600 * 10 / 1e6)
```
- [ ] **Step 2:** `.venv/bin/python -m pytest -q tests/test_claude_client.py -k prose_estimate` → FAIL (`AttributeError: module 'bait.clients.claude' has no attribute 'why_blurb_prompt'`).
- [ ] **Step 3: Implement.** In `class Claude`, replace `what_line` and `why_blurb` with:
```python
    def what_line(self, ctx: dict) -> dict:
        return self._json(CLAUDE_BLURB_MODEL, what_line_prompt(ctx), WHAT_SCHEMA, PROSE_MAX_TOKENS)

    def why_blurb(self, ctx: dict) -> str:
        return self._json(CLAUDE_BLURB_MODEL, why_blurb_prompt(ctx), WHY_SCHEMA, PROSE_MAX_TOKENS)["why_blurb"]
```
and append at the end of the module:
```python
PROSE_MAX_TOKENS = 600     # what_line and why_blurb


def what_line_prompt(ctx: dict) -> str:
    return "Write the 'what it does' line and pick a category for this hook project.\n\n" + json.dumps(ctx, indent=1, default=str)[:12000]


def why_blurb_prompt(ctx: dict) -> str:
    return ("Write 'why people care' for this hook project: two sentences grounded in the metrics, "
            "signals and receipts below. Mention a number if one is notable.\n\n"
            + json.dumps(ctx, indent=1, default=str)[:12000])


def prose_estimate(user: str) -> float:
    """The worst case Claude._json checks against the budget before a what_line / why_blurb call with this prompt."""
    from ..costs import anthropic_estimate
    return anthropic_estimate(CLAUDE_BLURB_MODEL, len(user) + len(SYSTEM), PROSE_MAX_TOKENS)
```
- [ ] **Step 4:** full suite → **B+2 = 279 passed, 11 skipped**.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/clients/claude.py pipeline/tests/test_claude_client.py
git commit -m "claude: what_line and why_blurb prompts and their worst-case estimate as functions, so a dry run prints the Meter's own check"
```

### Task 3: Enrich's what_line context and row update as functions

**Files:** Modify `pipeline/bait/steps/enrich.py:34-71` (the per-project body of `run`). Test `pipeline/tests/test_enrich.py`.
**Interfaces:** `hooks_of(conn, project_id) -> list[Row]`, `what_context(p, hs, enrichment) -> dict` (the dict Claude's what_line sees, identical key order, so the prompt is byte-identical), `what_values(p, res, enrichment) -> dict` (the projects update). `run` behaves exactly as before.

- [ ] **Step 1: Failing test** (append to `tests/test_enrich.py`)
```python
def test_what_context_is_what_enrich_sends_claude(conn, ctx):
    pid = insert_project(conn, "Ctx", first_seen=NOW - timedelta(days=30), slug="ctx")
    insert_hook(conn, "0x" + "c" * 40, 8453, project_id=pid, name="Ctx")
    conn.execute(sa.update(db.hooks).values(description="limit orders", flags={"beforeSwap": True})
                 .where(db.hooks.c.address == "0x" + "c" * 40))
    insert_pool(conn, "0xpoolctx", 8453, "0x" + "c" * 40, NOW - timedelta(days=30))
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xpoolctx", chain_id=8453, day=DAY, vol_24h=1.0, traders_24h=1))
    conn.execute(sa.update(db.projects).values(enrichment={"parallel": {"team": "T"}}).where(db.projects.c.id == pid))
    seen = []

    class Spy(FakeClaude):
        def what_line(self, ctxd):
            seen.append(ctxd)
            return super().what_line(ctxd)

    ctx.clients.update(claude=Spy(), parallel=None, blockscout=None)
    enrich.run(conn, ctx)
    p = conn.execute(sa.select(db.projects).where(db.projects.c.id == pid)).one()
    assert seen == [enrich.what_context(p, enrich.hooks_of(conn, pid), {"parallel": {"team": "T"}})]
    assert seen[0]["hooklist_descriptions"] == ["limit orders"] and seen[0]["flags_union"] == ["beforeSwap"]
    vals = enrich.what_values(p, {"what_line": "x " * 150, "category": "orders"}, {"parallel": {"website": "https://c.xyz"}})
    assert vals["what_line"].endswith("…") and len(vals["what_line"]) <= 200 and vals["website"] == "https://c.xyz"
```
- [ ] **Step 2:** `.venv/bin/python -m pytest -q tests/test_enrich.py -k what_context` → FAIL (`AttributeError: module 'bait.steps.enrich' has no attribute 'what_context'`).
- [ ] **Step 3: Implement.** In `run`, replace from `hs = conn.execute(...)` through `ctxd["enrichment"] = enrichment` / `res = claude.what_line(ctxd)` with:
```python
        hs = hooks_of(conn, p.id)
        enrichment = dict(fresh_enr.get(p.id) or {})
        try:
            with savepoint(conn):
                if bs is not None and "source" not in enrichment:
                    src = _verified_source(bs, hs)
                    if src:
                        enrichment["source"] = src
                        out["source"] += 1
                res = claude.what_line(what_context(p, hs, enrichment))
```
Replace the block from `par_res = enrichment.get("parallel") or {}` through `conn.execute(sa.update(projects).values(**vals)...)` with:
```python
        conn.execute(sa.update(projects).values(**what_values(p, res, enrichment)).where(projects.c.id == p.id))
```
Insert after `run` (before `cut_line`):
```python
def hooks_of(conn, project_id: int) -> list:
    return conn.execute(sa.select(hooks).where(hooks.c.project_id == project_id)
                        .order_by(hooks.c.verified_source.desc(), hooks.c.first_seen_at)).all()


def what_context(p, hs, enrichment: dict) -> dict:
    """What Claude's what_line call sees: the project's hooks and its stored enrichment (Parallel facts, verified source)."""
    return {
        "name": p.name, "hooks_n": len(hs), "is_factory": p.is_factory,
        "chains": sorted({CHAIN_BY_ID[h.chain_id].slug if h.chain_id in CHAIN_BY_ID else str(h.chain_id) for h in hs}),
        "hooklist_descriptions": sorted({h.description for h in hs if h.description})[:5],
        "flags_union": sorted({f for h in hs for f, v in (h.flags or {}).items() if v}),
        "dynamic_fee": any((h.properties or {}).get("dynamicFee") for h in hs),
        "audit_urls": sorted({h.audit_url for h in hs if h.audit_url}),
        "enrichment": enrichment,
    }


def what_values(p, res: dict, enrichment: dict) -> dict:
    """The projects row update for a fresh what_line: the line (cut at a word boundary), category, enrichment, links."""
    par_res = enrichment.get("parallel") or {}
    return {
        "what_line": cut_line(res["what_line"]), "category": res["category"],
        "enrichment": enrichment, "enriched_at": utcnow(),
        "website": p.website or par_res.get("website") or None,
        "github": p.github or par_res.get("github") or None,
        "x_handle": p.x_handle or (par_res.get("x_handle") or "").lstrip("@") or None,
    }
```
- [ ] **Step 4:** full suite → **B+3 = 280 passed, 11 skipped** (every existing enrich test unchanged and passing).
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/steps/enrich.py pipeline/tests/test_enrich.py
git commit -m "enrich: what_context and what_values as functions, so a repair writes a what_line exactly as the step does"
```

### Task 4: `repair.py` and backfill-days

**Files:** Create `pipeline/bait/repair.py`, `pipeline/tests/test_repair.py`.
**Interfaces:** `backfill_window(today, since)`, `replaceable(row)`, `backfill_targets(conn, today, since) -> (targets, info)` (targets `(pool_id, chain_id, vol7)` busiest first; `info["no_network"]` per chain id, `info["by_chain"]` per slug, `info["rows"]`), `fill_pool(conn, pool_id, chain_id, candles, start, end) -> (updated, inserted)`, `backfill(engine, ctx, since, max_pools=None, deadline=None, clock=_utcnow) -> dict` (keys `window, pools_needing, no_network, done, failed, no_candles, beyond_cap, left_at_deadline, rows_updated, rows_inserted, calls`), `backfill_plan(conn, today, since, max_pools, spacing) -> (lines, {"calls", "minutes"})`, `p8_rows_present(conn, today)`. Priority is the pool's last 7 nightly `pool_stats.vol_24h` summed (its 7-day volume as the nightly saw it). Pools on chains `keyless.CHAINS` does not list (monad, worldchain, soneium, megaeth, celo, zora: indexer-only) are counted, never called.

- [ ] **Step 1: Failing tests.** Create `tests/test_repair.py`:
```python
"""Package 9: python -m bait repair. GeckoTerminal and Claude are fakes; nothing here touches the network or spends."""
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from bait import db, repair
from bait.clients.claude import prose_estimate, what_line_prompt, why_blurb_prompt
from bait.costs import Budgets
from bait.steps import RunContext, score
from tests.conftest import DAY, NOW

BASE, MONAD = 8453, 143
D = timedelta(days=1)


def _t(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


class FakeGT:
    """keyless.Http stand-in: candles per pool id; None = the call failed."""

    def __init__(self, candles: dict):
        self.candles, self.paths = candles, []

    def gt(self, path):
        self.paths.append(path)
        pid = path.split("/pools/")[1].split("/")[0]
        c = self.candles.get(pid)
        return None if c is None else {"data": {"attributes": {"ohlcv_list": [[_t(d), 1, 1, 1, 1, v] for d, v in c.items()]}}}


def _project(c, slug, hook, chain=BASE, first_seen=NOW - timedelta(days=60), **cols):
    pid = c.execute(sa.insert(db.projects).values(slug=slug, name=slug.title(), norm_name=slug, first_seen_at=first_seen,
                                                  **cols)).inserted_primary_key[0]
    c.execute(sa.insert(db.hooks).values(address=hook, chain_id=chain, project_id=pid, name=slug, first_seen_at=NOW,
                                         flags={}, properties={}))
    return pid


def _pool(c, pool_id, hook, chain=BASE, created=NOW - timedelta(days=60)):
    c.execute(sa.insert(db.pools).values(pool_id=pool_id, chain_id=chain, hook_address=hook, created_at=created))


def _snap(c, pool_id, day, chain=BASE, tvl=5_000.0, cum=None, vol_day=None, est=None, tx_day=None):
    c.execute(sa.insert(db.pool_snapshots).values(pool_id=pool_id, chain_id=chain, day=day, tvl_usd=tvl, volume_usd_cum=cum,
                                                  tx_count_cum=None, volume_usd_day=vol_day, tx_count_day=tx_day, estimated=est))


def _rows(e, pool_id):
    with e.connect() as c:
        return {r.day: r for r in c.execute(sa.select(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == pool_id))}


def _ctx(http=None, day=DAY, now=NOW):
    ctx = RunContext(day=day, now=now, clients={k: None for k in ("graph", "blockscout", "claude", "exa", "parallel", "github", "names")})
    if http is not None:
        ctx.extras["keyless_http"] = http
    return ctx

# --- backfill-days ------------------------------------------------------------------------------
def test_backfill_writes_candles_to_unfilled_closed_days_and_never_the_running_day(engine):
    with engine.begin() as c:
        p = _project(c, "fill", "0xh1")
        _pool(c, "0xp1", "0xh1")
        _snap(c, "0xp1", DAY - 5 * D, vol_day=7.0, est=False, cum=100.0)   # a candle row already: stays
        _snap(c, "0xp1", DAY - 4 * D, cum=0.0)                             # a zero seed: nothing traded before, stays
        _snap(c, "0xp1", DAY - 3 * D, cum=500.0)                           # written before 0012
        _snap(c, "0xp1", DAY - 2 * D, cum=600.0)                           # written before 0012, no candle: $0
        _snap(c, "0xp1", DAY - 1 * D, vol_day=9_999.0, est=True, cum=700.0)   # GeckoTerminal's rolling 24h, an estimate
        _snap(c, "0xp1", DAY, vol_day=50.0, est=True, cum=750.0)           # the running day
    http = FakeGT({"0xp1": {DAY - 6 * D: 50.0, DAY - 5 * D: 1_000.0, DAY - 3 * D: 300.0, DAY - 1 * D: 200.0, DAY: 40.0}})
    out = repair.backfill(engine, _ctx(http), since=2)
    assert http.paths == ["/networks/base/pools/0xp1/ohlcv/day?aggregate=1&limit=30&currency=usd"]
    assert (out["done"], out["rows_updated"], out["rows_inserted"], out["calls"]) == (1, 3, 1, 1)
    r = _rows(engine, "0xp1")
    assert [(r[DAY - i * D].volume_usd_day, r[DAY - i * D].estimated) for i in (3, 2, 1)] == [(300.0, False), (0.0, False), (200.0, False)]
    assert (r[DAY].volume_usd_day, r[DAY].estimated) == (50.0, True)             # the running day is never written
    assert (r[DAY - 5 * D].volume_usd_day, r[DAY - 4 * D].volume_usd_day) == (7.0, None)
    ins = r[DAY - 6 * D]                                                         # a traded day with no row: inserted
    assert (ins.volume_usd_day, ins.estimated, ins.tvl_usd, ins.volume_usd_cum, ins.tx_count_day) == (50.0, False, 5_000.0, 0.0, None)
    again = repair.backfill(engine, _ctx(FakeGT({})), since=2)                   # resumable: nothing left to pull
    assert (again["pools_needing"], again["calls"]) == (0, 0)
    with engine.begin() as c:
        score.run(c, _ctx())
        d = c.execute(sa.select(db.project_daily).where(db.project_daily.c.project_id == p)).one()
    assert (d.vol_24h, d.vol_7d) == (200.0, 50.0 + 7.0 + 300.0 + 200.0)


def test_backfill_targets_busiest_first_skip_unscored_pools_and_unlisted_chains(engine):
    with engine.begin() as c:
        _project(c, "busy", "0xhb")
        _project(c, "quiet", "0xhq")
        _project(c, "monad", "0xhm", chain=MONAD)
        for pid, hook, chain, vol in (("0xbusy", "0xhb", BASE, 9_000.0), ("0xquiet", "0xhq", BASE, 10.0),
                                      ("0xmon", "0xhm", MONAD, 1e6), ("0xorphan", "0xnone", BASE, 1e9)):
            _pool(c, pid, hook, chain=chain)
            _snap(c, pid, DAY - 3 * D, chain=chain, cum=100.0)
            c.execute(sa.insert(db.pool_stats).values(pool_id=pid, chain_id=chain, day=DAY, vol_24h=vol, traders_24h=5))
        targets, info = repair.backfill_targets(c, DAY, since=2)
    assert [t[0] for t in targets] == ["0xbusy", "0xquiet"] and info["no_network"] == {"143": 1}
    http = FakeGT({"0xbusy": {DAY - 3 * D: 5.0}})
    out = repair.backfill(engine, _ctx(http), since=2, max_pools=1)
    assert (out["done"], out["beyond_cap"], out["no_network"]) == (1, 1, 1) and len(http.paths) == 1
    out = repair.backfill(engine, _ctx(FakeGT({"0xquiet": {}})), since=2)      # GeckoTerminal has no candles: rows kept
    assert (out["no_candles"], out["done"]) == (1, 0) and _rows(engine, "0xquiet")[DAY - 3 * D].volume_usd_day is None
    out = repair.backfill(engine, _ctx(FakeGT({})), since=2)                    # the call failed: retried by a rerun
    assert out["failed"] == 1
    out = repair.backfill(engine, _ctx(FakeGT({})), since=2, deadline=NOW, clock=lambda: NOW)
    assert (out["left_at_deadline"], out["calls"]) == (1, 0)


def test_backfill_plan_counts_calls_and_minutes_and_warns_before_a_package_8_nightly(engine):
    with engine.begin() as c:
        for i in range(3):
            _project(c, f"p{i}", f"0xh{i}")
            _pool(c, f"0xp{i}", f"0xh{i}")
            _snap(c, f"0xp{i}", DAY - 3 * D, cum=100.0)
        lines, plan = repair.backfill_plan(c, DAY, since=14, max_pools=2, spacing=10)
    text = "\n".join(lines)
    assert plan == {"calls": 2, "minutes": pytest.approx(20 / 60)}
    assert f"closed days {DAY - 28 * D}..{DAY - D}" in text and "pools needing candles: 3" in text and "1 left for a rerun" in text
    assert "WARNING: no per-day snapshot rows" in text
    with engine.begin() as c:
        _snap(c, "0xp0", DAY, vol_day=1.0, est=True, cum=101.0)
        assert "WARNING" not in "\n".join(repair.backfill_plan(c, DAY, 14, None, 10)[0])
```
- [ ] **Step 2:** `.venv/bin/python -m pytest -q tests/test_repair.py` → FAIL (collection error: `ImportError: cannot import name 'repair' from 'bait'`).
- [ ] **Step 3: Implement.** Create `pipeline/bait/repair.py` (the module docstring and imports cover the whole package; later tasks append sections):
```python
"""Package 9: one-off data repair. `python -m bait repair <command>`; see pipeline/README.md "Repair".

backfill-days  GeckoTerminal daily candles (free, paced) -> pool_snapshots.volume_usd_day for closed days, where a row has
               no per-day value (written before 0012) or only an estimate. One call per pool, busiest first; resumable.
rescore        score.run for each stored run day in the range, oldest first, after a one-time backup of that day's rows.
prose          Claude only, paid: what_line for projects whose Parallel facts are stored but whose line was never written,
               or whose line the old code cut mid-word at 200 chars; why_blurb for the signals targets whose blurb predates
               the package 8 window text. Every call passes the spend Meter; --max-usd caps this run on top of it.
verify         Free: Bait's 24h / 7d / prev-7d against GeckoTerminal candle sums for a few projects, as a markdown table.
all            The plan (--dry-run), or backfill-days -> rescore -> verify, then prose only within --prose-budget-usd.

Every command that changes or checks data takes the run lock (exit 3 while the nightly runs) and writes one run_receipts
row per sub-step (day = today, notes = "repair <name>"). Dry runs read only and write nothing.
"""
from __future__ import annotations

import argparse
import math
import os
import random
from datetime import date, datetime, time as dtime, timedelta, timezone

import sqlalchemy as sa

from . import db
from . import keyless as K
from .clients.claude import prose_estimate, what_line_prompt, why_blurb_prompt
from .costs import BudgetExceeded, Meter
from .db import (api_spend, as_utc, hooks, pool_daily_onchain, pool_snapshots, pool_stats, pools, project_daily,
                 project_daily_p9_backup, projects, run_receipts, upsert)
from .steps import RunContext, enrich, log, score, signals
from .steps.snapshot import keyless_http

PRIOR_DAYS = 14            # a run day's windows reach 14 closed days back (7d and prev 7d)
OHLCV_LIMIT = 30           # daily candles per GeckoTerminal call, one call per pool
MAX_SINCE = OHLCV_LIMIT - PRIOR_DAYS - 1   # 15: the oldest run day whose windows 30 candles still cover
# Blurbs written before package 8 merged used the old window text ("swaps in 7 days" that were 24h).
P8_MERGED_AT = datetime(2026, 10, 9, 15, 29, 25, tzinfo=timezone.utc)
NET_BY_CHAIN = {cid: net for net, (cid, *_rest) in K.CHAINS.items()}
SLUG_BY_CHAIN = {cid: slug for _net, (cid, slug, *_rest) in K.CHAINS.items()}
EXIT_PRECONDITION = 5

_D = timedelta(days=1)
_COLS = [c.name for c in project_daily.c]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

# ------------------------------------------------------------------------------------------------
# backfill-days
# ------------------------------------------------------------------------------------------------
def backfill_window(today: date, since: int) -> tuple[date, date]:
    """Closed days a re-score of the run days today-since .. today reads: (today - since - 14) .. (today - 1).
    `today` itself is the running day and is never written."""
    return today - timedelta(days=since + PRIOR_DAYS), today - _D


def replaceable(r) -> bool:
    """A row a candle may fill: written before 0012 with a known, non-zero cumulative (a zero cumulative is a seed: nothing
    traded up to that day, so the day adds $0 either way; a NULL cumulative is volume the onchain step marked unknown), or
    an estimate on a closed day (GeckoTerminal's rolling 24h, or a running-day candle that was never replaced)."""
    return r.estimated is True or (r.volume_usd_day is None and r.volume_usd_cum is not None and r.volume_usd_cum > 0)


def backfill_targets(conn, today: date, since: int) -> tuple[list[tuple[str, int, float]], dict]:
    """Pools of scored projects with a replaceable row in the window, as (pool_id, chain_id, 7-day volume), busiest first
    (7-day volume = the last 7 nightly pool_stats.vol_24h). Pools on chains GeckoTerminal does not list are counted, not
    returned."""
    start, end = backfill_window(today, since)
    c = pool_snapshots.c
    cond = sa.or_(c.estimated.is_(True), sa.and_(c.volume_usd_day.is_(None), c.volume_usd_cum.is_not(None), c.volume_usd_cum > 0))
    need: dict[tuple[str, int], int] = {}
    for r in conn.execute(sa.select(c.pool_id, c.chain_id, sa.func.count().label("n"))
                          .where(c.day >= start, c.day <= end, cond).group_by(c.pool_id, c.chain_id)):
        need[(r.pool_id, r.chain_id)] = r.n
    scored = {(r.pool_id, r.chain_id) for r in conn.execute(
        sa.select(pools.c.pool_id, pools.c.chain_id)
        .select_from(pools.join(hooks, (hooks.c.address == pools.c.hook_address) & (hooks.c.chain_id == pools.c.chain_id)))
        .where(hooks.c.project_id.is_not(None)))}
    vol7 = {(r.pool_id, r.chain_id): r.v or 0.0 for r in conn.execute(
        sa.select(pool_stats.c.pool_id, pool_stats.c.chain_id, sa.func.sum(pool_stats.c.vol_24h).label("v"))
        .where(pool_stats.c.day > today - 7 * _D, pool_stats.c.day <= today)
        .group_by(pool_stats.c.pool_id, pool_stats.c.chain_id))}
    info = {"rows": 0, "no_network": {}, "by_chain": {}}
    out = []
    for key, n in need.items():
        if key not in scored:
            continue
        cid = key[1]
        if cid not in NET_BY_CHAIN:
            info["no_network"][str(cid)] = info["no_network"].get(str(cid), 0) + 1
            continue
        info["rows"] += n
        slug = SLUG_BY_CHAIN[cid]
        info["by_chain"][slug] = info["by_chain"].get(slug, 0) + 1
        out.append((key[0], cid, vol7.get(key, 0.0)))
    out.sort(key=lambda t: (-t[2], t[1], t[0]))
    return out, info


def fill_pool(conn, pool_id: str, chain_id: int, candles: list[tuple[int, float]], start: date, end: date) -> tuple[int, int]:
    """Write one pool's candles to its replaceable rows in start..end (volume_usd_day, estimated False); a day without a
    candle had no trades ($0). A day with volume and no row gets one, carrying TVL and the legacy cumulatives from the
    nearest earlier row (TVL from the nearest later one when there is none), so it never changes a pool's known/unknown
    state. tx_count_day is left as it is: GeckoTerminal candles carry no trade counts. Returns (updated, inserted)."""
    vols: dict[date, float] = {}
    for ts, v in candles:
        vols[datetime.fromtimestamp(ts, tz=timezone.utc).date()] = v
    complete = len(candles) < OHLCV_LIMIT       # fewer than asked: the response is the pool's whole trading history
    first = min(vols)
    c = pool_snapshots.c
    rows = {r.day: r for r in conn.execute(sa.select(pool_snapshots).where(c.pool_id == pool_id, c.chain_id == chain_id))}
    updates, inserts = [], []
    d = start
    while d <= end:
        if complete or d >= first:
            v = vols.get(d, 0.0)
            r = rows.get(d)
            if r is not None:
                if replaceable(r):
                    updates.append({"p": pool_id, "c": chain_id, "d": d, "v": v})
            elif v > 0:
                earlier = [x for x in rows if x < d]
                later = [x for x in rows if x > d]
                base = rows[max(earlier)] if earlier else None
                tvl = base.tvl_usd if base is not None else (rows[min(later)].tvl_usd if later else None)
                inserts.append({"pool_id": pool_id, "chain_id": chain_id, "day": d, "tvl_usd": tvl,
                                "volume_usd_cum": base.volume_usd_cum if base is not None else 0.0,
                                "tx_count_cum": base.tx_count_cum if base is not None else None,
                                "volume_usd_day": v, "tx_count_day": None, "estimated": False})
        d += _D
    if updates:
        conn.execute(sa.update(pool_snapshots)
                     .where(c.pool_id == sa.bindparam("p"), c.chain_id == sa.bindparam("c"), c.day == sa.bindparam("d"))
                     .values(volume_usd_day=sa.bindparam("v"), estimated=False), updates)
    if inserts:
        upsert(conn, pool_snapshots, inserts, keys=["pool_id", "chain_id", "day"], update=[])
    return len(updates), len(inserts)


def backfill(engine, ctx: RunContext, since: int, max_pools: int | None = None, deadline: datetime | None = None,
             clock=_utcnow) -> dict:
    """Pull candles for every target pool (or the busiest max_pools), one committed transaction per pool, so a stop at any
    point keeps what is done and a rerun skips it. Stops between pools at `deadline`."""
    today = ctx.day
    start, end = backfill_window(today, since)
    with engine.connect() as conn:
        targets, info = backfill_targets(conn, today, since)
    take = targets if max_pools is None else targets[:max_pools]
    out = {"window": f"{start}..{end}", "pools_needing": len(targets), "no_network": sum(info["no_network"].values()),
           "done": 0, "failed": 0, "no_candles": 0, "beyond_cap": len(targets) - len(take), "left_at_deadline": 0,
           "rows_updated": 0, "rows_inserted": 0, "calls": 0}
    http = keyless_http(ctx)
    log.info("repair backfill-days: %d pools need candles for %s..%s; pulling %d", len(targets), start, end, len(take))
    for i, (pid, cid, _v) in enumerate(take):
        if deadline is not None and clock() >= deadline:
            out["left_at_deadline"] = len(take) - i
            log.warning("repair backfill-days: stop time %s reached, %d pools left; rerun to resume", deadline, len(take) - i)
            break
        candles = K.fetch_daily(http, NET_BY_CHAIN[cid], pid, limit=OHLCV_LIMIT)
        out["calls"] += 1
        if candles is None:
            out["failed"] += 1
            continue
        if not candles:
            out["no_candles"] += 1          # GeckoTerminal has no volume for it: leave the rows alone
            continue
        with engine.begin() as conn:
            u, n = fill_pool(conn, pid, cid, candles, start, end)
        out["done"] += 1
        out["rows_updated"] += u
        out["rows_inserted"] += n
        if (i + 1) % 50 == 0:
            log.info("repair backfill-days: %d/%d pools, %d rows updated, %d inserted, %d failed",
                     i + 1, len(take), out["rows_updated"], out["rows_inserted"], out["failed"])
    return out


def backfill_plan(conn, today: date, since: int, max_pools: int | None, spacing: float) -> tuple[list[str], dict]:
    start, end = backfill_window(today, since)
    targets, info = backfill_targets(conn, today, since)
    calls = len(targets) if max_pools is None else min(max_pools, len(targets))
    minutes = calls * spacing / 60
    chains = ", ".join(f"{k} {v}" for k, v in sorted(info["by_chain"].items(), key=lambda kv: -kv[1])) or "none"
    lines = [f"backfill-days: closed days {start}..{end} (the running day {today} is never written)",
             f"  pools needing candles: {len(targets)} ({info['rows']} rows); by chain: {chains}"]
    if info["no_network"]:
        lines.append(f"  skipped, chain not on GeckoTerminal: {sum(info['no_network'].values())} pools "
                     f"(chain ids {', '.join(sorted(info['no_network']))})")
    left = len(targets) - calls
    lines.append(f"  GeckoTerminal calls: {calls}{f' (--pools {max_pools}; {left} left for a rerun)' if left else ''}, "
                 f"{spacing:g} s apart: about {minutes:.0f} min, free")
    if not p8_rows_present(conn, today):
        lines.append("  WARNING: no per-day snapshot rows from the last day. Run this after a package 8 nightly; before "
                     "it, every on-chain pool still looks unfilled and the call count above is inflated.")
    return lines, {"calls": calls, "minutes": minutes}


def p8_rows_present(conn, today: date) -> bool:
    """A package 8 nightly ran within the last day: it writes estimated on every row it touches."""
    return conn.execute(sa.select(sa.func.count()).select_from(pool_snapshots).where(
        pool_snapshots.c.estimated.is_not(None), pool_snapshots.c.day >= today - _D)).scalar_one() > 0
```
- [ ] **Step 4:** full suite → **B+6 = 283 passed, 11 skipped**.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/repair.py pipeline/tests/test_repair.py
git commit -m "repair: backfill-days, GeckoTerminal daily candles into the closed days a re-score reads, busiest pools first, resumable"
```

### Task 5: rescore, with a backup and a restore

**Files:** Modify `pipeline/bait/db.py` (after `project_daily`), `pipeline/bait/repair.py` (append). Test `pipeline/tests/test_repair.py` (append).
**Interfaces:** `db.project_daily_p9_backup` (project_daily's columns and primary key, no defaults, no foreign key; created by `create_all`), `rescore_days(conn, today, since=None, day=None) -> list[date]` (stored days only), `trending_top(conn, day, n=10)`, `backup_day(conn, day) -> int` (rows in the backup for that day after the call; 0 when a copy already existed), `restore_day(conn, day) -> int | None`, `pending_note(engine, today, since) -> str`, `onchain_coverage(conn)`, `rescore(engine, days, now, echo=print) -> {day: counts + backed_up, top10_in, top10_out}`. Each day runs `score.run(conn, RunContext(day=d, now=now))` in its own transaction; `score._ready` applies package 8's past-day rule because `d < now.date()`. Row counts are read back with `count(*)`: psycopg reports `rowcount = -1` for `INSERT ... SELECT` (Task 8's Postgres test).

- [ ] **Step 1: Failing test** (append to `tests/test_repair.py`)
```python
# --- rescore ------------------------------------------------------------------------------------
def _old_row(c, pid, day, **cols):
    vals = dict(project_id=pid, day=day, tvl_usd=1.0, vol_7d=0.0, vol_7d_prev=0.0, swaps_7d=0, pools_n=1, score_trending=0.0,
                is_new=False, is_spam=True)
    vals.update(cols)
    c.execute(sa.insert(db.project_daily).values(**vals))


def test_rescore_backs_up_once_reports_the_top_10_change_and_restore_puts_the_old_rows_back(engine):
    end = DAY - D
    with engine.begin() as c:
        p = _project(c, "riser", "0xhr")
        _pool(c, "0xpr", "0xhr")
        for i in range(14):
            _snap(c, "0xpr", end - i * D, tvl=50_000.0, cum=1.0, vol_day=20_000.0 if i < 7 else 2_000.0, est=False)
        _snap(c, "0xpr", DAY, tvl=50_000.0, cum=1.0, vol_day=5.0, est=True)
        _old_row(c, p, DAY)                                   # scored by the old code: spam, no window_end
        assert repair.rescore_days(c, DAY, since=14) == [DAY]
    lines = []
    per = repair.rescore(engine, [DAY], NOW, echo=lines.append)
    assert per[DAY.isoformat()]["top10_in"] == ["riser"] and per[DAY.isoformat()]["backed_up"] == 1
    assert lines[0].startswith(f"{DAY}  projects 1  new 0  trending 1  wash 0") and "in riser" in lines[1]
    with engine.connect() as c:
        row = c.execute(sa.select(db.project_daily)).one()
    assert row.window_end == end and row.vol_7d == 140_000.0 and not row.is_spam and row.score_trending > 0
    assert repair.rescore(engine, [DAY], NOW, echo=lines.append)[DAY.isoformat()]["backed_up"] == 0   # first copy kept
    with engine.begin() as c:
        assert repair.restore_day(c, DAY) == 1
        assert repair.restore_day(c, DAY - D) is None
        row = c.execute(sa.select(db.project_daily)).one()
    assert row.is_spam and row.window_end is None and row.score_trending == 0.0
```
- [ ] **Step 2:** `.venv/bin/python -m pytest -q tests/test_repair.py -k rescore` → FAIL (`AttributeError: module 'bait.repair' has no attribute 'rescore_days'`).
- [ ] **Step 3: Implement.** In `db.py`, after the `project_daily` table:
```python
# Package 9 (`repair rescore`): the pre-repair copy of each re-scored day, so `repair rescore --restore` can put it back.
# Same columns as project_daily, no defaults or foreign key (a copy, never read by the feed). create_all creates it; drop
# it in the cleanup that removes the legacy cumulative columns (DESIGN.md "Data issues backlog").
project_daily_p9_backup = sa.Table(
    "project_daily_p9_backup", metadata,
    *[sa.Column(c.name, c.type, primary_key=c.primary_key) for c in project_daily.c],
)
```
Append to `repair.py`:
```python
# ------------------------------------------------------------------------------------------------
# rescore
# ------------------------------------------------------------------------------------------------
def rescore_days(conn, today: date, since: int | None = None, day: date | None = None) -> list[date]:
    """Run days that already have rows (a re-score never creates a day the nightly has not written), oldest first."""
    q = sa.select(project_daily.c.day).distinct()
    q = q.where(project_daily.c.day == day) if day is not None else q.where(
        project_daily.c.day >= today - timedelta(days=since), project_daily.c.day <= today)
    return sorted(r[0] for r in conn.execute(q))


def trending_top(conn, day: date, n: int = 10) -> list[str]:
    """Trending as the feed ranks it (signals.select_targets): score > 0, not spam, not wash; score, then TVL."""
    rows = conn.execute(sa.select(projects.c.slug, project_daily.c.score_trending, project_daily.c.tvl_usd)
                        .join(project_daily, project_daily.c.project_id == projects.c.id)
                        .where(project_daily.c.day == day, project_daily.c.is_spam.is_(False),
                               project_daily.c.is_wash.is_(False), project_daily.c.score_trending > 0)).all()
    return [r.slug for r in sorted(rows, key=lambda r: (-(r.score_trending or 0), -(r.tvl_usd or 0), r.slug))[:n]]


def backup_day(conn, day: date) -> int:
    """Copy the day's project_daily rows into project_daily_p9_backup once: the first copy is the pre-repair state."""
    b = project_daily_p9_backup
    if conn.execute(sa.select(sa.func.count()).select_from(b).where(b.c.day == day)).scalar_one():
        return 0
    conn.execute(sa.insert(b).from_select(_COLS, sa.select(*[project_daily.c[n] for n in _COLS])
                                          .where(project_daily.c.day == day)))
    # counted, not rowcount: psycopg reports -1 for INSERT ... SELECT
    return conn.execute(sa.select(sa.func.count()).select_from(b).where(b.c.day == day)).scalar_one()


def restore_day(conn, day: date) -> int | None:
    """Put the backed-up rows of `day` back (projects merged away since are skipped). None when there is no backup."""
    b = project_daily_p9_backup
    if not conn.execute(sa.select(sa.func.count()).select_from(b).where(b.c.day == day)).scalar_one():
        return None
    conn.execute(sa.delete(project_daily).where(project_daily.c.day == day))
    conn.execute(sa.insert(project_daily).from_select(_COLS, sa.select(*[b.c[n] for n in _COLS]).where(
        b.c.day == day, b.c.project_id.in_(sa.select(projects.c.id)))))
    return conn.execute(sa.select(sa.func.count()).select_from(project_daily).where(project_daily.c.day == day)).scalar_one()


def pending_note(engine, today: date, since: int) -> str:
    """Re-scoring before backfill-days has finished reads $0 for the days still unfilled: say so up front."""
    with engine.connect() as conn:
        n = len(backfill_targets(conn, today, max(min(since, MAX_SINCE), 0))[0])
    return (f"backfill-days: {n} pools still need candles; their unfilled days count as $0 in this re-score" if n
            else "backfill-days: complete for these days")


def onchain_coverage(conn) -> dict[int, date]:
    return {r.chain_id: r.first for r in conn.execute(
        sa.select(pool_daily_onchain.c.chain_id, sa.func.min(pool_daily_onchain.c.day).label("first"))
        .group_by(pool_daily_onchain.c.chain_id))}


def rescore(engine, days: list[date], now: datetime, echo=print) -> dict:
    """score.run per day, oldest first, each day in its own transaction after its backup. Prints one block per day."""
    per: dict[str, dict] = {}
    with engine.connect() as conn:
        cov = onchain_coverage(conn)
    for d in days:
        with engine.begin() as conn:
            before = trending_top(conn, d)
            backed = backup_day(conn, d)
            counts = score.run(conn, RunContext(day=d, now=now))
            after = trending_top(conn, d)
        came, went = [s for s in after if s not in before], [s for s in before if s not in after]
        wash = " ".join(f"{k[5:]}={v}" for k, v in counts.items() if k.startswith("wash_") and v)
        partial = sorted(SLUG_BY_CHAIN.get(c, str(c)) for c, first in cov.items() if first > d - 7 * _D)
        echo(f"{d}  projects {counts['projects']}  new {counts['new']}  trending {counts['trending']}  "
             f"wash {counts['wash']}{f' ({wash})' if wash else ''}  backed up {backed}")
        echo(f"            top 10: {len(after) - len(came)} kept" + (f"; in {', '.join(came)}" if came else "")
             + (f"; out {', '.join(went)}" if went else ""))
        if partial:
            echo(f"            on-chain history starts after this day's 7-day window opens on: {', '.join(partial)}")
        per[d.isoformat()] = {**counts, "backed_up": backed, "top10_in": came, "top10_out": went}
    return per
```
- [ ] **Step 4:** full suite → **B+7 = 284 passed, 11 skipped**.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/db.py pipeline/bait/repair.py pipeline/tests/test_repair.py
git commit -m "repair: rescore stored run days oldest first after a one-time backup; --restore puts the pre-repair rows back"
```

### Task 6: prose (paid, Claude only)

**Files:** Modify `pipeline/bait/repair.py` (append). Test `pipeline/tests/test_repair.py` (append).
**Interfaces:** `CappedMeter(engine, cap_usd, **meter_kw)` (a `Meter` whose `check` also refuses a call that would take this run past `cap_usd`), `stored_parallel(enrichment)`, `cut_mid_word(line)`, `latest_day(conn)`, `what_line_targets(conn) -> [(project_row, "missing" | "cut")]`, `blurb_targets(conn, day, signals_limit, stale_before) -> (targets, {"targets", "fresh", "old_row"})`, `what_line_estimates`, `blurb_estimates`, `write_what_lines(engine, claude, targets)`, `write_blurbs(engine, claude, targets, now)`, `typical_nightly_usd(engine, now)`, `budget_lines(meter, worst, nightly, flag)`, `prose_plan(...) -> (lines, worst_usd)`, `prose(engine, ctx, what, blurbs, limit, signals_limit, stale_before) -> dict` (raises when there is no `ANTHROPIC_API_KEY`). Each Claude call happens outside any transaction and each result is committed on its own, so a budget stop keeps everything already paid for. No Parallel, Exa or Blockscout call anywhere in this section.

- [ ] **Step 1: Failing tests** (append to `tests/test_repair.py`)
```python
# --- prose --------------------------------------------------------------------------------------
class FakeClaude:
    """Goes through the meter like the real client: check the worst case, then record what was spent."""

    def __init__(self, meter, usd=0.004):
        self.meter, self.usd, self.what, self.why = meter, usd, [], []

    def what_line(self, ctx):
        self.meter.check("anthropic", prose_estimate(what_line_prompt(ctx)))
        self.meter.record("anthropic", self.usd)
        self.what.append(ctx)
        return {"what_line": f"{ctx['name']} routes swaps through a hook.", "category": "orders"}

    def why_blurb(self, ctx):
        self.meter.check("anthropic", prose_estimate(why_blurb_prompt(ctx)))
        self.meter.record("anthropic", self.usd)
        self.why.append(ctx)
        return f"{ctx['name']} moved ${ctx['metrics']['vol_7d']:,.0f} in the 7 closed days. More next week.  "


CUT = ("word " * 40)[:199] + "x"          # 200 chars, mid-word, the old strip()[:200]


def _prose_seed(c):
    ids = {}
    ids["missing"] = _project(c, "missing", "0xm", what_line="placeholder from the hooklist",
                              enrichment={"parallel": {"website": "https://m.xyz", "team": "M"}})
    ids["failed"] = _project(c, "failed", "0xf", enrichment={"parallel": {"_failed": "run1", "reason": "404"}})
    ids["cut_small"] = _project(c, "cut-small", "0xc1", what_line=CUT, category="other", enriched_at=NOW)
    ids["cut_big"] = _project(c, "cut-big", "0xc2", what_line=CUT, category="other", enriched_at=NOW)
    _project(c, "sentence", "0xc3", what_line=CUT[:-1] + ".", enriched_at=NOW)
    _project(c, "ellipsis", "0xc4", what_line=CUT[:-1] + "…", enriched_at=NOW)
    for k, vol in (("cut_small", 10.0), ("cut_big", 1e6)):
        _old_row(c, ids[k], DAY, vol_7d=vol, is_spam=False)
    return ids


def test_what_line_targets_missing_first_then_cut_mid_word_busiest_first(engine):
    with engine.begin() as c:
        _prose_seed(c)
        got = [(p.slug, kind) for p, kind in repair.what_line_targets(c)]
    assert got == [("missing", "missing"), ("cut-big", "cut"), ("cut-small", "cut")]
    assert repair.cut_mid_word(CUT) and not repair.cut_mid_word(CUT[:-1] + "…") and not repair.cut_mid_word("short")


def test_prose_what_lines_use_stored_facts_and_stop_at_the_repair_cap(engine):
    with engine.begin() as c:
        ids = _prose_seed(c)
        targets = repair.what_line_targets(c)
    meter = repair.CappedMeter(None, cap_usd=0.0085, budgets=Budgets(3.0, 40.0, {}))
    claude = FakeClaude(meter)
    out = repair.write_what_lines(engine, claude, targets)
    # each call is checked at its worst case (~$0.007) on top of what this run spent: the second call no longer fits $0.0085
    assert out == {"missing": 1, "cut": 0, "errors": 0, "budget_stop": 1}
    assert claude.what[0]["enrichment"] == {"parallel": {"website": "https://m.xyz", "team": "M"}}
    with engine.connect() as c:
        rows = {r.slug: r for r in c.execute(sa.select(db.projects))}
    m = rows["missing"]
    assert (m.what_line, m.category, m.website) == ("Missing routes swaps through a hook.", "orders", "https://m.xyz")
    assert m.enriched_at is not None and rows["cut-big"].what_line == CUT
    out = repair.write_what_lines(engine, FakeClaude(repair.CappedMeter(None, None, budgets=Budgets(3.0, 40.0, {}))),
                                  [(rows["cut-big"], "cut")])
    with engine.connect() as c:
        big = c.execute(sa.select(db.projects).where(db.projects.c.slug == "cut-big")).one()
    assert out["cut"] == 1 and big.what_line == "Cut-Big routes swaps through a hook." and big.category == "other"


def test_blurbs_go_to_stale_signals_targets_scored_with_package_8_windows_only(engine):
    with engine.begin() as c:
        old = _project(c, "old-blurb", "0xo", blurb_at=repair.P8_MERGED_AT - D, why_blurb="22,874 swaps in 7 days.")
        fresh = _project(c, "fresh-blurb", "0xn", blurb_at=repair.P8_MERGED_AT + D, why_blurb="ok")
        wash = _project(c, "wash", "0xw", blurb_at=None)
        unscored = _project(c, "unscored", "0xu", blurb_at=None)
        for pid, sc in ((old, 9.0), (fresh, 8.0), (wash, 7.0)):
            _old_row(c, pid, DAY, is_spam=False, score_trending=sc, vol_7d=70_000.0, window_end=DAY - D, is_wash=pid == wash)
        _old_row(c, unscored, DAY, is_spam=False, score_trending=6.0)               # window_end NULL: the old score
        targets, info = repair.blurb_targets(c, DAY, 50, repair.P8_MERGED_AT)
    assert [p.slug for p, _ in targets] == ["old-blurb"] and info == {"targets": 3, "fresh": 1, "old_row": 1}
    claude = FakeClaude(repair.CappedMeter(None, None, budgets=Budgets(3.0, 40.0, {})))
    assert repair.write_blurbs(engine, claude, targets, NOW) == {"blurbs": 1, "errors": 0}
    assert "closed UTC days ending" in claude.why[0]["metrics"]["windows"]
    with engine.connect() as c:
        r = c.execute(sa.select(db.projects).where(db.projects.c.id == old)).one()
    assert r.why_blurb == "Old-Blurb moved $70,000 in the 7 closed days. More next week." and db.as_utc(r.blurb_at) == NOW


def test_prose_plan_shows_counts_worst_case_and_both_budget_options(engine):
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        _prose_seed(c)
        c.execute(sa.insert(db.api_spend).values(ts=now, provider="anthropic", step="enrich", units=1, usd=1.02, detail=""))
        expect = sum(repair.what_line_estimates(c, repair.what_line_targets(c)))
    meter = repair.CappedMeter(engine, None, budgets=Budgets(3.0, 40.0, {}))
    lines, worst = repair.prose_plan(engine, meter, now, True, True, None, 50, repair.P8_MERGED_AT)
    text = "\n".join(lines)
    assert worst == pytest.approx(expect)
    assert "prose what-lines: 1 missing (stored Parallel facts, no Claude line), 2 cut mid-word" in text
    assert "spent today $1.02 of $3.00" in text and "option A, no budget change" in text
    assert f"option B, one run: the owner sets SPEND_DAILY_USD=3.00 on the pipeline service" in text   # 1.02 + worst fits
    assert f"--max-usd {repair._cents_up(worst):.2f}" in text
    lines = repair.budget_lines(meter, 2.5, 1.02, "--max-usd")
    assert "SPEND_DAILY_USD=3.52" in lines[2] and "about 2 day(s)" in lines[1]
```
- [ ] **Step 2:** `.venv/bin/python -m pytest -q tests/test_repair.py -k "prose or what_line or blurbs"` → FAIL (`AttributeError: module 'bait.repair' has no attribute 'what_line_targets'`, and the same for `CappedMeter`, `blurb_targets`, `prose_plan`).
- [ ] **Step 3: Implement.** Append to `repair.py`:
```python
# ------------------------------------------------------------------------------------------------
# prose
# ------------------------------------------------------------------------------------------------
class CappedMeter(Meter):
    """The run's Meter plus an extra cap on what this repair run may spend (--max-usd / --prose-budget-usd)."""

    def __init__(self, engine, cap_usd: float | None, **kw):
        super().__init__(engine, **kw)
        self.cap_usd = cap_usd

    def check(self, provider: str, est_usd: float) -> None:
        if self.cap_usd is not None and self.run_usd + est_usd > self.cap_usd:
            log.warning("spend stop: %s call (~$%.4f) would take this repair past its $%.2f cap", provider, est_usd, self.cap_usd)
            raise BudgetExceeded(f"{provider}: repair cap ${self.cap_usd:.2f}")
        super().check(provider, est_usd)


def stored_parallel(enrichment) -> bool:
    par = (enrichment or {}).get("parallel")
    return bool(par) and "_failed" not in par


def cut_mid_word(line: str | None) -> bool:
    """The old code stored strip()[:200]: a line of exactly 200 chars that does not end a sentence was cut. cut_line's own
    cuts end in an ellipsis and are left alone."""
    return line is not None and len(line) == 200 and line[-1] not in ".!?…\"')"


def latest_day(conn) -> date | None:
    return conn.execute(sa.select(sa.func.max(project_daily.c.day))).scalar_one()


def what_line_targets(conn) -> list[tuple[object, str]]:
    """("missing") Parallel facts stored but no Claude line yet (enriched_at NULL, the budget stopped enrich), then ("cut")
    lines cut mid-word; each busiest first by the latest 7-day volume."""
    day = latest_day(conn)
    vol = {r.project_id: r.vol_7d or 0.0 for r in conn.execute(
        sa.select(project_daily.c.project_id, project_daily.c.vol_7d).where(project_daily.c.day == day))} if day else {}
    missing, cut = [], []
    for p in conn.execute(sa.select(projects).order_by(projects.c.id)):
        if p.enriched_at is None and stored_parallel(p.enrichment):
            missing.append(p)
        elif cut_mid_word(p.what_line):
            cut.append(p)
    key = lambda p: (-vol.get(p.id, 0.0), p.id)   # noqa: E731
    return [(p, "missing") for p in sorted(missing, key=key)] + [(p, "cut") for p in sorted(cut, key=key)]


def blurb_targets(conn, day: date, signals_limit: int, stale_before: datetime) -> tuple[list[tuple], dict]:
    """The signals step's targets for `day` (Trending top 20 then New; spam and wash never) whose blurb predates
    `stale_before`. A target whose row has no window_end was scored before package 8: re-score first."""
    rows = signals.select_targets(conn, RunContext(day=day, signals_limit=signals_limit))
    out, info = [], {"targets": len(rows), "fresh": 0, "old_row": 0}
    for p, daily in rows:
        if daily.window_end is None:
            info["old_row"] += 1
        elif p.blurb_at is not None and as_utc(p.blurb_at) >= stale_before:
            info["fresh"] += 1
        else:
            out.append((p, daily))
    return out, info


def what_line_estimates(conn, targets) -> list[float]:
    return [prose_estimate(what_line_prompt(enrich.what_context(p, enrich.hooks_of(conn, p.id), dict(p.enrichment or {}))))
            for p, _kind in targets]


def blurb_estimates(conn, targets) -> list[float]:
    return [prose_estimate(why_blurb_prompt(signals._blurb_ctx(conn, p, daily))) for p, daily in targets]


def write_what_lines(engine, claude, targets) -> dict:
    """One Claude call per target from stored facts only (no Parallel, no Blockscout); each line committed on its own.
    "missing" gets enrich's full write (line, category, links, enriched_at); "cut" gets the line only."""
    out = {"missing": 0, "cut": 0, "errors": 0}
    for p, kind in targets:
        enr = dict(p.enrichment or {})
        try:
            with engine.connect() as conn:
                hs = enrich.hooks_of(conn, p.id)
            res = claude.what_line(enrich.what_context(p, hs, enr))
        except BudgetExceeded:
            out["budget_stop"] = 1
            break
        except Exception as e:
            out["errors"] += 1
            log.warning("repair prose what_line project=%s failed: %s", p.slug, e)
            continue
        vals = enrich.what_values(p, res, enr) if kind == "missing" else {"what_line": enrich.cut_line(res["what_line"])}
        with engine.begin() as conn:
            conn.execute(sa.update(projects).values(**vals).where(projects.c.id == p.id))
        out[kind] += 1
    return out


def write_blurbs(engine, claude, targets, now: datetime) -> dict:
    out = {"blurbs": 0, "errors": 0}
    for p, daily in targets:
        try:
            with engine.connect() as conn:
                bctx = signals._blurb_ctx(conn, p, daily)
            blurb = claude.why_blurb(bctx)
        except BudgetExceeded:
            out["budget_stop"] = 1
            break
        except Exception as e:
            out["errors"] += 1
            log.warning("repair prose blurb project=%s failed: %s", p.slug, e)
            continue
        with engine.begin() as conn:
            conn.execute(sa.update(projects).values(why_blurb=blurb.strip()[:400], blurb_at=now).where(projects.c.id == p.id))
        out["blurbs"] += 1
    return out


def typical_nightly_usd(engine, now: datetime) -> float:
    """Mean paid spend per day over the last 7 days, repair runs left out."""
    with engine.connect() as c:
        total = c.execute(sa.select(sa.func.coalesce(sa.func.sum(api_spend.c.usd), 0.0)).where(
            api_spend.c.ts >= now - timedelta(days=7), sa.not_(api_spend.c.step.like("repair%")))).scalar_one()
    return float(total or 0.0) / 7


def _cents_up(v: float) -> float:
    return math.ceil(round(v * 100, 6)) / 100


def budget_lines(meter: Meter, worst: float, nightly: float, flag: str) -> list[str]:
    """Two ways to fit `worst` under the daily budget: spread it over days after the nightly, or raise the budget for one
    run. The extra cap (flag) never raises a budget; it only stops this run earlier."""
    b = meter.budgets
    today, d30 = meter.spent(today=True), meter.spent(days=30)
    per_day = max(b.daily - nightly, 0.0)
    when = ("never: the nightly uses the whole daily budget" if per_day <= 0
            else f"about {max(math.ceil(worst / per_day), 1)} day(s)")
    lines = [f"  spent today ${today:.2f} of ${b.daily:.2f}; last 30 days ${d30:.2f} of ${b.monthly:.2f}",
             f"  option A, no budget change: run after the nightly (last 7 days: ${nightly:.2f}/day); about ${per_day:.2f} a "
             f"day is left, so the worst case takes {when} (one {flag} run per day; reruns skip what is done)"]
    daily = max(b.daily, _cents_up(today + worst))
    monthly = _cents_up(d30 + worst)
    lines.append(f"  option B, one run: the owner sets SPEND_DAILY_USD={daily:.2f}"
                 + (f" and SPEND_MONTHLY_USD={monthly:.2f}" if monthly > b.monthly else "")
                 + f" on the pipeline service for this run only, passes {flag} {_cents_up(worst):.2f}, "
                   "and restores the default before 06:00 UTC")
    return lines


def prose_plan(engine, meter: Meter, now: datetime, what: bool, blurbs: bool, limit: int | None, signals_limit: int,
               stale_before: datetime, flag: str = "--max-usd") -> tuple[list[str], float]:
    lines, worst = [], 0.0
    with engine.connect() as conn:
        if what:
            t = what_line_targets(conn)
            t = t[:limit] if limit is not None else t
            est = what_line_estimates(conn, t)
            n_missing = sum(1 for _, k in t if k == "missing")
            w = sum(est)
            worst += w
            lines.append(f"prose what-lines: {n_missing} missing (stored Parallel facts, no Claude line), {len(t) - n_missing} "
                         f"cut mid-word; worst case ${w:.2f} (max ${max(est, default=0):.4f} a call), Claude only")
        if blurbs:
            day = latest_day(conn)
            t, info = blurb_targets(conn, day, signals_limit, stale_before) if day else ([], {"targets": 0, "fresh": 0, "old_row": 0})
            t = t[:limit] if limit is not None else t
            est = blurb_estimates(conn, t)
            w = sum(est)
            worst += w
            lines.append(f"prose blurbs: {len(t)} of {info['targets']} signals targets on {day} predate {stale_before:%Y-%m-%d %H:%M} UTC "
                         f"({info['fresh']} fresh, {info['old_row']} on a row scored before package 8: re-score first); "
                         f"worst case ${w:.2f}, Claude only, no Exa")
    lines += budget_lines(meter, worst, typical_nightly_usd(engine, now), flag)
    return lines, worst


def prose(engine, ctx: RunContext, what: bool, blurbs: bool, limit: int | None, signals_limit: int,
          stale_before: datetime) -> dict:
    claude = ctx.client("claude")
    if claude is None:
        raise RuntimeError("no ANTHROPIC_API_KEY")
    out: dict = {}
    if what:
        with engine.connect() as conn:
            t = what_line_targets(conn)
        out["what_lines"] = write_what_lines(engine, claude, t[:limit] if limit is not None else t)
    if blurbs and not out.get("what_lines", {}).get("budget_stop"):
        with engine.connect() as conn:
            day = latest_day(conn)
            t, info = blurb_targets(conn, day, signals_limit, stale_before) if day else ([], {})
        out["blurbs"] = {**write_blurbs(engine, claude, t[:limit] if limit is not None else t, ctx.now), **info}
    ctx.meter.flush()
    out["usd"] = round(ctx.meter.run_usd, 4)
    return out
```
- [ ] **Step 4:** full suite → **B+11 = 288 passed, 11 skipped**.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/repair.py pipeline/tests/test_repair.py
git commit -m "repair: prose rewrites from stored facts (missing and mid-word what-lines, pre-package-8 blurbs), metered and capped, with a dry-run plan"
```

### Task 7: verify

**Files:** Modify `pipeline/bait/repair.py` (append). Test `pipeline/tests/test_repair.py` (append).
**Interfaces:** `verify_sample(conn, day, sample, seed) -> [(label, row)]` (top trending, top new, the busiest wash-flagged project, then seeded random projects with pools; spam never), `verify(engine, ctx, sample, per_project, seed, echo=print) -> {"day", "projects", "pools", "calls", "failed"}`. It prints a markdown table: per project and window (24h, 7d, prev 7d), Bait's stored project figure (all pools), Bait's per-day sum over the checked pools, GeckoTerminal's candle sum over the same pools and closed days, and the delta. At most `sample x per_project` GeckoTerminal calls.

- [ ] **Step 1: Failing test** (append to `tests/test_repair.py`)
```python
# --- verify -------------------------------------------------------------------------------------
def test_verify_prints_bait_and_geckoterminal_for_the_same_pools_and_closed_windows(engine):
    end = DAY - D
    with engine.begin() as c:
        p = _project(c, "top", "0xht")
        _pool(c, "0xpt", "0xht")
        for i in range(14):
            _snap(c, "0xpt", end - i * D, cum=1.0, vol_day=1_000.0, est=False)
        _old_row(c, p, DAY, is_spam=False, score_trending=5.0, vol_24h=1_000.0, vol_7d=7_000.0, vol_7d_prev=7_000.0,
                 window_end=end)
    http = FakeGT({"0xpt": {end - i * D: (1_100.0 if i < 7 else 1_000.0) for i in range(14)} | {DAY: 99.0}})
    lines = []
    out = repair.verify(engine, _ctx(http), sample=5, per_project=3, seed=1, echo=lines.append)
    assert out == {"day": DAY.isoformat(), "projects": 1, "pools": 1, "calls": 1, "failed": 0}
    assert "| top | top trending | 1/1 | 24h | $1,000 | $1,000 | $1,100 | -9.1% |" in lines
    assert "| top | top trending | 1/1 | prev 7d | $7,000 | $7,000 | $7,000 | +0.0% |" in lines
```
- [ ] **Step 2:** `.venv/bin/python -m pytest -q tests/test_repair.py -k verify` → FAIL (`AttributeError: module 'bait.repair' has no attribute 'verify'`).
- [ ] **Step 3: Implement.** Append to `repair.py`:
```python
# ------------------------------------------------------------------------------------------------
# verify
# ------------------------------------------------------------------------------------------------
def verify_sample(conn, day: date, sample: int, seed: int) -> list[tuple[str, object]]:
    """Top trending, top new, the busiest wash-flagged project, then random projects with pools (seeded, so a rerun on
    the same day picks the same ones). Spam is never picked."""
    rows = conn.execute(sa.select(projects.c.id, projects.c.slug, project_daily)
                        .join(project_daily, project_daily.c.project_id == projects.c.id)
                        .where(project_daily.c.day == day, project_daily.c.is_spam.is_(False))
                        .order_by(projects.c.id)).all()
    live = [r for r in rows if not r.is_wash]
    picks: list[tuple[str, object]] = []

    def take(label, cands):
        for r in cands:
            if all(r.id != q.id for _, q in picks):
                picks.append((label, r))
                return

    take("top trending", sorted([r for r in live if (r.score_trending or 0) > 0], key=lambda r: (-(r.score_trending or 0), -(r.tvl_usd or 0))))
    take("top new", sorted([r for r in live if r.is_new], key=lambda r: -(r.tvl_usd or 0)))
    take("wash", sorted([r for r in rows if r.is_wash], key=lambda r: -(r.vol_7d or 0)))
    picks = picks[:sample]
    rest = [r for r in rows if (r.pools_n or 0) > 0 and all(r.id != q.id for _, q in picks)]
    picks += [("random", r) for r in random.Random(seed).sample(rest, min(len(rest), max(sample - len(picks), 0)))]
    return picks


def _pct(bait: float, gt: float) -> str:
    if gt == 0:
        return "0" if bait == 0 else "n/a"
    return f"{(bait - gt) / gt * 100:+.1f}%"


def _usd(v) -> str:
    return "–" if v is None else f"${v:,.0f}"


def verify(engine, ctx: RunContext, sample: int, per_project: int, seed: int | None, echo=print) -> dict:
    with engine.connect() as conn:
        day = latest_day(conn)
        if day is None:
            echo("verify: no project_daily rows")
            return {"projects": 0}
        end = day - _D
        windows = (("24h", end, end), ("7d", end - 6 * _D, end), ("prev 7d", end - 13 * _D, end - 7 * _D))
        picks = verify_sample(conn, day, sample, day.toordinal() if seed is None else seed)
        plan = []
        for label, r in picks:
            ps = conn.execute(sa.select(pools.c.pool_id, pools.c.chain_id).select_from(
                pools.join(hooks, (hooks.c.address == pools.c.hook_address) & (hooks.c.chain_id == pools.c.chain_id)))
                .where(hooks.c.project_id == r.id)).all()
            per_pool = {}
            for p in ps:
                if p.chain_id not in NET_BY_CHAIN:
                    continue
                snap = {s.day: s.volume_usd_day for s in conn.execute(sa.select(pool_snapshots.c.day, pool_snapshots.c.volume_usd_day)
                        .where(pool_snapshots.c.pool_id == p.pool_id, pool_snapshots.c.chain_id == p.chain_id,
                               pool_snapshots.c.day >= windows[2][1], pool_snapshots.c.day <= end))}
                per_pool[(p.pool_id, p.chain_id)] = snap
            busiest = sorted(per_pool, key=lambda k: -sum(v or 0.0 for d, v in per_pool[k].items() if d >= windows[1][1]))[:per_project]
            plan.append((label, r, len(ps), {k: per_pool[k] for k in busiest}))
    http = keyless_http(ctx)
    out = {"day": day.isoformat(), "projects": len(plan), "pools": 0, "calls": 0, "failed": 0}
    echo(f"Bait vs GeckoTerminal, run day {day}: closed UTC days ending {end}. Bait (checked pools) and GeckoTerminal sum the "
         f"same pools' daily volume; Bait stored is the project row (all pools).")
    echo("")
    echo("| project | picked as | pools checked | window | Bait stored | Bait (checked pools) | GeckoTerminal | delta |")
    echo("|---|---|---|---|---|---|---|---|")
    for label, r, n_pools, chosen in plan:
        gt: dict[date, float] = {}
        ok = 0
        for (pid, cid) in chosen:
            candles = K.fetch_daily(http, NET_BY_CHAIN[cid], pid, limit=OHLCV_LIMIT)
            out["calls"] += 1
            if candles is None:
                out["failed"] += 1
                continue
            ok += 1
            for ts, v in candles:
                d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
                gt[d] = gt.get(d, 0.0) + v
        out["pools"] += ok
        stored = {"24h": r.vol_24h, "7d": r.vol_7d, "prev 7d": r.vol_7d_prev}
        for name, a, b in windows:
            bait = sum(v or 0.0 for snap in chosen.values() for d, v in snap.items() if a <= d <= b)
            g = sum(v for d, v in gt.items() if a <= d <= b)
            echo(f"| {r.slug} | {label} | {ok}/{n_pools} | {name} | {_usd(stored[name])} | {_usd(bait)} | {_usd(g)} | {_pct(bait, g)} |")
    return out
```
- [ ] **Step 4:** full suite → **B+12 = 289 passed, 11 skipped**.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/repair.py pipeline/tests/test_repair.py
git commit -m "repair: verify, Bait against GeckoTerminal candle sums for the same pools and closed windows, as a markdown table"
```

### Task 8: The CLI, the lock, receipts and guards

**Files:** Modify `pipeline/bait/repair.py` (append), `pipeline/bait/__main__.py:1-2` (docstring), `:14` (imports), `:51-53` (`parse_args`), `:75-76` (`main`). Test `pipeline/tests/test_repair.py` (append), `pipeline/tests/test_postgres.py` (append).
**Interfaces:** `repair.add_parsers(subparsers)`, `repair.main(args, engine) -> int`, `deadline_at(hhmm, now)`. Exit codes: 0 ok; 1 a sub-step raised (its receipt says which); 2 bad arguments (`--since` outside 1..15, `prose` without `--what-lines`/`--blurbs`; argparse's own errors also exit 2); 3 the run lock is held (no receipt); 5 precondition: the latest nightly receipt failed, or (backfill-days, rescore, all) no package 8 per-day rows from the last day. Dry runs take no lock and write nothing. `all --yes` stops after backfill-days when it hit `--stop-at` (rerun to resume), and runs prose only with `--prose-budget-usd`.

The full surface:
```
python -m bait repair backfill-days [--since 14] [--pools N] [--stop-at 05:30] [--dry-run] [--database-url U]
python -m bait repair rescore       [--since 14 | --day YYYY-MM-DD] [--restore] [--dry-run] [--database-url U]
python -m bait repair prose         [--what-lines] [--blurbs] (--dry-run | --yes) [--limit N] [--max-usd X]
                                    [--signals-limit 50] [--stale-before ISO] [--database-url U]
python -m bait repair verify        [--sample 5] [--pools-per-project 3] [--seed N] [--database-url U]
python -m bait repair all           (--dry-run | --yes) [--since 14] [--pools N] [--stop-at 05:30] [--sample 5]
                                    [--limit N] [--prose-budget-usd X] [--database-url U]
```

- [ ] **Step 1: Failing tests.** Append to `tests/test_repair.py`:
```python
# --- CLI ----------------------------------------------------------------------------------------
def _today():
    return datetime.now(timezone.utc).date()


def _cli_db(tmp_path, nightly_rc=0, p8=True):
    url = f"sqlite:///{tmp_path}/r.sqlite"
    e = db.make_engine(url)
    db.create_all(e)
    db.add_missing_columns(e)
    t = _today()
    with e.begin() as c:
        p = _project(c, "cli", "0xhc")
        _pool(c, "0xpc", "0xhc")
        _snap(c, "0xpc", t - 3 * D, cum=100.0)
        if p8:
            _snap(c, "0xpc", t, vol_day=1.0, est=True, cum=101.0)
        _old_row(c, p, t)
        c.execute(sa.insert(db.run_receipts).values(day=t, started_at=NOW, finished_at=NOW, exit_code=nightly_rc, soft_errors=0, steps={}))
    return url, e


def _receipts(e):
    with e.connect() as c:
        return c.execute(sa.select(db.run_receipts).where(db.run_receipts.c.notes.is_not(None)).order_by(db.run_receipts.c.id)).all()


def test_repair_respects_the_run_lock_the_since_range_and_a_failed_nightly(tmp_path, monkeypatch):
    from bait.__main__ import main
    url, e = _cli_db(tmp_path)
    assert main(["repair", "rescore", "--since", "16", "--database-url", url]) == 2
    assert main(["repair", "prose", "--dry-run", "--database-url", url]) == 2          # neither --what-lines nor --blurbs
    monkeypatch.setattr(db, "try_run_lock", lambda engine: None)
    assert main(["repair", "rescore", "--database-url", url]) == 3
    monkeypatch.undo()
    (tmp_path / "failed").mkdir()
    url2, e2 = _cli_db(tmp_path / "failed", nightly_rc=1)
    assert main(["repair", "rescore", "--database-url", url2]) == repair.EXIT_PRECONDITION
    (tmp_path / "pre8").mkdir()
    url3, e3 = _cli_db(tmp_path / "pre8", p8=False)
    assert main(["repair", "backfill-days", "--database-url", url3]) == repair.EXIT_PRECONDITION
    assert main(["repair", "rescore", "--database-url", url3]) == repair.EXIT_PRECONDITION     # would read $0 windows
    assert _receipts(e) == [] and _receipts(e2) == [] and _receipts(e3) == []


def test_repair_all_dry_run_prints_the_plan_and_writes_nothing(tmp_path, capsys):
    from bait.__main__ import main
    url, e = _cli_db(tmp_path)
    assert main(["repair", "all", "--since", "14", "--dry-run", "--database-url", url]) == 0
    text = capsys.readouterr().out
    for part in ("backfill-days: closed days", "GeckoTerminal calls: 1", "rescore: 1 stored run day(s)", "verify: up to 15",
                 "prose what-lines:", "prose blurbs:", "option A", "option B", "--prose-budget-usd"):
        assert part in text, part
    assert _receipts(e) == []
    with e.connect() as c:
        assert c.execute(sa.select(db.pool_snapshots.c.volume_usd_day).where(db.pool_snapshots.c.day == _today() - 3 * D)).scalar_one() is None


def test_repair_all_yes_runs_the_free_steps_with_a_receipt_each_and_stops_before_prose(tmp_path, monkeypatch, capsys):
    from bait.__main__ import main
    url, e = _cli_db(tmp_path)
    t = _today()
    monkeypatch.setattr(repair, "keyless_http", lambda ctx: FakeGT({"0xpc": {t - 3 * D: 123.0}}))
    assert main(["repair", "all", "--since", "14", "--yes", "--stop-at", "", "--database-url", url]) == 0
    assert "prose not run" in capsys.readouterr().out
    got = _receipts(e)
    assert [r.notes for r in got] == ["repair backfill-days", "repair rescore", "repair verify"]
    assert all(r.exit_code == 0 and r.soft_errors == 0 and r.day == t for r in got)
    assert got[0].steps["backfill-days"]["rows_updated"] == 1
    assert _rows(e, "0xpc")[t - 3 * D].volume_usd_day == 123.0
```
Append to `tests/test_postgres.py`:
```python
def test_repair_backfill_backup_and_restore_on_postgres(pg_engine):
    import os

    from bait import repair
    from bait.__main__ import main
    from tests.test_repair import D, FakeGT, _ctx, _old_row, _pool, _project, _rows, _snap
    with pg_engine.begin() as c:
        p = _project(c, "pg", "0xhpg")
        _pool(c, "0xppg", "0xhpg")
        _snap(c, "0xppg", DAY - 3 * D, cum=100.0)
        _snap(c, "0xppg", DAY - 1 * D, vol_day=9.0, est=True, cum=110.0)
        _old_row(c, p, DAY)
    out = repair.backfill(pg_engine, _ctx(FakeGT({"0xppg": {DAY - 3 * D: 30.0, DAY - 2 * D: 20.0}})), since=2)
    assert (out["rows_updated"], out["rows_inserted"]) == (2, 1)
    r = _rows(pg_engine, "0xppg")
    assert [r[DAY - i * D].volume_usd_day for i in (3, 2, 1)] == [30.0, 20.0, 0.0]
    repair.rescore(pg_engine, [DAY], NOW, echo=lambda s: None)
    with pg_engine.begin() as c:
        assert c.execute(sa.select(db.project_daily.c.window_end)).scalar_one() == DAY - D
        assert repair.restore_day(c, DAY) == 1
        assert c.execute(sa.select(db.project_daily.c.window_end)).scalar_one() is None
    held = db.try_run_lock(pg_engine)
    try:
        assert main(["repair", "rescore", "--database-url", os.environ["TEST_DATABASE_URL"]]) == 3
    finally:
        held.close()
```
- [ ] **Step 2:** `.venv/bin/python -m pytest -q tests/test_repair.py -k "respects or dry_run_prints or yes_runs"` → FAIL, 3 tests (`SystemExit: 2`: argparse says `invalid choice: 'repair'`).
- [ ] **Step 3: Implement.** Append to `repair.py`:
```python
# ------------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------------
def add_parsers(sub) -> None:
    def common(p):
        p.add_argument("--database-url", default=None)

    b = sub.add_parser("backfill-days", help="free: GeckoTerminal daily candles into pool_snapshots for closed days")
    b.add_argument("--since", type=int, default=14, help=f"run days to cover (1..{MAX_SINCE}); writes since + 14 closed days")
    b.add_argument("--pools", type=int, default=None, help="pull at most N pools, busiest first (default all)")
    b.add_argument("--stop-at", default="05:30", help="UTC HH:MM: stop between pools here so the 06:00 nightly gets the lock")
    b.add_argument("--dry-run", action="store_true")
    common(b)
    r = sub.add_parser("rescore", help="free: score.run for stored run days, oldest first, after a backup")
    r.add_argument("--since", type=int, default=14, help=f"run days today-N .. today that have rows (1..{MAX_SINCE})")
    r.add_argument("--day", type=date.fromisoformat, default=None, help="one run day instead of --since")
    r.add_argument("--restore", action="store_true", help="put the pre-repair rows of those days back instead")
    r.add_argument("--dry-run", action="store_true")
    common(r)
    p = sub.add_parser("prose", help="PAID: Claude what_line / why_blurb rewrites from stored facts")
    p.add_argument("--what-lines", action="store_true")
    p.add_argument("--blurbs", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="at most N projects per kind, busiest first")
    p.add_argument("--max-usd", type=float, default=None, help="cap on this run's spend, on top of the daily/30-day budgets")
    p.add_argument("--signals-limit", type=int, default=50)
    p.add_argument("--stale-before", type=datetime.fromisoformat, default=P8_MERGED_AT,
                   help="blurbs written before this UTC time are rewritten (default: package 8's merge)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--yes", action="store_true")
    common(p)
    v = sub.add_parser("verify", help="free: Bait vs GeckoTerminal for a few projects, as a markdown table")
    v.add_argument("--sample", type=int, default=5)
    v.add_argument("--pools-per-project", type=int, default=3)
    v.add_argument("--seed", type=int, default=None, help="random picks; default: the run day, so reruns agree")
    common(v)
    a = sub.add_parser("all", help="the whole plan (--dry-run), or backfill-days, rescore, verify (and prose with a budget)")
    a.add_argument("--since", type=int, default=14)
    a.add_argument("--pools", type=int, default=None)
    a.add_argument("--stop-at", default="05:30")
    a.add_argument("--sample", type=int, default=5)
    a.add_argument("--limit", type=int, default=None, help="prose: at most N projects per kind")
    a.add_argument("--prose-budget-usd", type=float, default=None, help="run prose too, spending at most this")
    ga = a.add_mutually_exclusive_group(required=True)
    ga.add_argument("--dry-run", action="store_true")
    ga.add_argument("--yes", action="store_true")
    common(a)


def deadline_at(hhmm: str | None, now: datetime) -> datetime | None:
    """The next HH:MM UTC after `now`."""
    if not hhmm:
        return None
    h, m = (int(x) for x in hhmm.split(":"))
    at = datetime.combine(now.date(), dtime(h, m), tzinfo=timezone.utc)
    return at if at > now else at + _D


def _receipt(engine, name: str, started: datetime, rc: int, out: dict) -> None:
    # soft_errors stays 0: /api/health reads the latest receipt, and a repair's GeckoTerminal misses are not a failed nightly
    db.write_receipt(engine, day=started.date(), started_at=started, finished_at=_utcnow(), exit_code=rc, soft_errors=0,
                     steps={name: out}, code_sha=os.environ.get("RAILWAY_GIT_COMMIT_SHA"), notes=f"repair {name}")


def _failed_nightly(engine):
    with engine.connect() as c:
        r = c.execute(sa.select(run_receipts).where(run_receipts.c.notes.is_(None))
                      .order_by(run_receipts.c.finished_at.desc()).limit(1)).first()
    return r if r is not None and r.exit_code != 0 else None


def _spacing() -> float:
    return float(os.environ.get("KEYLESS_SPACING", "10"))


def _step(engine, name: str, fn) -> tuple[int, dict]:
    started = _utcnow()
    rc, out = 1, {}
    try:
        out = fn()
        rc = 0
        return rc, out
    except Exception as e:
        log.exception("repair %s failed", name)
        out = {"failed": type(e).__name__}
        return rc, out
    finally:
        _receipt(engine, name, started, rc, out)


def main(args: argparse.Namespace, engine) -> int:
    cmd, now = args.repair_cmd, _utcnow()
    today = now.date()
    since = getattr(args, "since", None)
    if since is not None and not 1 <= since <= MAX_SINCE:
        log.error("--since must be 1..%d: %d candles cover since + %d closed days", MAX_SINCE, OHLCV_LIMIT, PRIOR_DAYS)
        return 2
    if cmd == "prose" and not (args.what_lines or args.blurbs):
        log.error("prose: pass --what-lines and/or --blurbs")
        return 2
    if getattr(args, "dry_run", False):
        return _dry_run(cmd, args, engine, now)
    lock = db.try_run_lock(engine)
    if lock is None:
        log.error("another bait run holds the run lock (the nightly?); repair not started")
        return 3
    try:
        bad = _failed_nightly(engine)
        if bad is not None:
            log.error("the last nightly receipt (day %s) exited %s; fix or rerun the nightly first, so a repair receipt "
                      "never hides it from /api/health", bad.day, bad.exit_code)
            return EXIT_PRECONDITION
        if cmd in ("backfill-days", "all") or (cmd == "rescore" and not args.restore):
            with engine.connect() as conn:
                if not p8_rows_present(conn, today):
                    log.error("no per-day snapshot rows from the last day: run after a package 8 nightly (before it, every "
                              "window would read $0)")
                    return EXIT_PRECONDITION
        return _run(cmd, args, engine, now)
    finally:
        lock.close()


def _ctx(now: datetime, meter=None) -> RunContext:
    return RunContext(day=now.date(), now=now, meter=meter)


def _run(cmd: str, args, engine, now: datetime) -> int:
    today = now.date()
    if cmd == "backfill-days":
        rc, _ = _step(engine, "backfill-days", lambda: backfill(engine, _ctx(now), args.since, args.pools,
                                                                deadline_at(args.stop_at, now)))
        return rc
    if cmd == "rescore":
        with engine.connect() as conn:
            days = rescore_days(conn, today, args.since, args.day)
        if args.restore:
            def go():
                done = {}
                for d in days:
                    with engine.begin() as conn:
                        done[d.isoformat()] = restore_day(conn, d)
                    print(f"{d}  restored {done[d.isoformat()]} rows" if done[d.isoformat()] is not None else f"{d}  no backup")
                return {"restored": done}
            return _step(engine, "rescore-restore", go)[0]
        print(pending_note(engine, today, args.since if args.day is None else (today - args.day).days))
        return _step(engine, "rescore", lambda: {"days": rescore(engine, days, now)})[0]
    if cmd == "verify":
        return _step(engine, "verify", lambda: verify(engine, _ctx(now), args.sample, args.pools_per_project, args.seed))[0]
    if cmd == "prose":
        meter = CappedMeter(engine, args.max_usd)
        meter.step = "repair-prose"
        return _step(engine, "prose", lambda: prose(engine, _ctx(now, meter), args.what_lines, args.blurbs, args.limit,
                                                     args.signals_limit, as_utc(args.stale_before)))[0]
    # all: the free steps, then prose only within an explicit budget
    rc, out = _step(engine, "backfill-days", lambda: backfill(engine, _ctx(now), args.since, args.pools,
                                                              deadline_at(args.stop_at, now)))
    if rc or out.get("left_at_deadline"):
        return rc
    with engine.connect() as conn:
        days = rescore_days(conn, today, args.since)
    rc = _step(engine, "rescore", lambda: {"days": rescore(engine, days, now)})[0]
    if rc:
        return rc
    rc = _step(engine, "verify", lambda: verify(engine, _ctx(now), args.sample, 3, None))[0]
    if rc or args.prose_budget_usd is None:
        if not rc:
            print("prose not run: pass --prose-budget-usd X after the owner's go (see `repair all --dry-run`)")
        return rc
    meter = CappedMeter(engine, args.prose_budget_usd)
    meter.step = "repair-prose"
    return _step(engine, "prose", lambda: prose(engine, _ctx(now, meter), True, True, args.limit, 50, P8_MERGED_AT))[0]


def _dry_run(cmd: str, args, engine, now: datetime) -> int:
    today = now.date()
    lines: list[str] = []
    with engine.connect() as conn:
        if cmd in ("backfill-days", "all"):
            lines += backfill_plan(conn, today, args.since, args.pools, _spacing())[0]
        if cmd in ("rescore", "all"):
            days = rescore_days(conn, today, args.since, getattr(args, "day", None))
            lines.append(f"rescore: {len(days)} stored run day(s) {', '.join(map(str, days)) or '-'}; free, seconds per day; "
                         "each day is backed up once before its first re-score")
        if cmd == "rescore":
            lines.append(pending_note(engine, today, args.since))
        if cmd == "all":
            sample = args.sample
            lines.append(f"verify: up to {sample * 3} GeckoTerminal calls ({sample} projects x 3 pools), about "
                         f"{sample * 3 * _spacing() / 60:.0f} min, free")
    if cmd in ("prose", "all"):
        meter = CappedMeter(engine, None)
        what, blurbs = (True, True) if cmd == "all" else (args.what_lines, args.blurbs)
        stale = as_utc(getattr(args, "stale_before", P8_MERGED_AT))
        flag = "--prose-budget-usd" if cmd == "all" else "--max-usd"
        lines += prose_plan(engine, meter, now, what, blurbs, args.limit, getattr(args, "signals_limit", 50), stale, flag)[0]
    if cmd == "all":
        lines.append("`repair all --yes` runs backfill-days, rescore and verify; prose only with --prose-budget-usd.")
    print("\n".join(lines))
    return 0
```
`__main__.py`: the docstring's second line becomes
```python
       python -m bait init-db   (create tables; for local SQLite dev, Postgres uses db/migrations).
       python -m bait repair backfill-days|rescore|prose|verify|all ...   (package 9 one-off data repair; see repair.py)"""
```
`from . import db, logsetup` → `from . import db, logsetup, repair`. In `parse_args`, after the `init-db` parser:
```python
    rp = sub.add_parser("repair", help="one-off data repair (package 9): backfill-days, rescore, prose, verify, all")
    repair.add_parsers(rp.add_subparsers(dest="repair_cmd", required=True))
```
In `main`, after the schema block (`return 4`) and before `meter = Meter(engine)`:
```python
    if args.cmd == "repair":
        return repair.main(args, engine)
```
(after the schema check on purpose: a repair needs package 8's columns, and exit 4 already covers a locked migration.)
- [ ] **Step 4:** full suite → **B+15 = 292 passed, S+1 = 12 skipped**; Postgres → **P+1 = 12 passed**. Smoke on a throwaway SQLite database (no keys, no network):
```bash
cd pipeline && export KEYLESS=0 BAIT_LOG_FORMAT=text
T=$(date -u +%F); DB=sqlite:///./p9-smoke.sqlite
.venv/bin/python -m bait run --day $T --hooklist-file fixtures/hooklist.sample.json --database-url $DB --max-code-fetch 0 > /dev/null 2>&1
.venv/bin/python -m bait repair all --since 14 --dry-run --database-url $DB; echo "rc=$?"
.venv/bin/python -m bait repair rescore --database-url $DB; echo "rc=$?"
rm -f p9-smoke.sqlite; unset KEYLESS BAIT_LOG_FORMAT
```
→ the dry run prints `backfill-days: closed days <today-28>..<today-1> (the running day <today> is never written)`, `pools needing candles: 0`, the WARNING about package 8 per-day rows (the fixture run writes no snapshots), `rescore: 1 stored run day(s) <today>`, `verify: up to 15 GeckoTerminal calls`, both prose lines, `option A` and `option B`, then `rc=0`; the rescore prints the error line `no per-day snapshot rows from the last day: run after a package 8 nightly ...` and `rc=5`.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/repair.py pipeline/bait/__main__.py pipeline/tests/test_repair.py pipeline/tests/test_postgres.py
git commit -m "repair: python -m bait repair, under the run lock, one receipt per sub-step, refusing to run after a failed nightly or before package 8's"
```

### Task 9: Docs

**Files:** `pipeline/README.md` (exit codes list, new section before `## Docker`), `railway/README.md` (new section at the end), `DESIGN.md` (Data issues backlog).

- [ ] **Step 1: Edit `pipeline/README.md`.** In the exit-code list, after `- 4: schema not ready (a required column is missing).` add `- 5: (repair only) precondition: the last nightly receipt failed, or no package 8 per-day rows from the last day.` Insert before `## Docker`:
```markdown
## Repair (package 9, one-off)

`python -m bait repair` brings stored data up to package 8's windows. Every command except `--dry-run` takes the run
lock (exit 3 while the nightly runs), refuses to start after a failed nightly receipt or before a package 8 nightly
(exit 5), and writes one `run_receipts` row per sub-step (`notes` = `repair <name>`, `soft_errors` 0).

| command | what it does | cost |
|---|---|---|
| `repair all --since 14 --dry-run` | prints the plan: pools and minutes to backfill, days to re-score, verify calls, prose counts and worst-case USD with the two budget options | free, writes nothing |
| `repair backfill-days --since 14 [--pools N]` | GeckoTerminal daily candles (one call per pool, `KEYLESS_SPACING` apart, busiest first) into `pool_snapshots.volume_usd_day` for the closed days `today-since-14 .. today-1`: rows written before 0012 and closed-day estimates only; stops between pools at `--stop-at` (05:30 UTC); a rerun resumes | free, about 10 s a pool |
| `repair rescore --since 14` | `score.run` for every stored run day in `today-since .. today`, oldest first, after copying each day once into `project_daily_p9_backup`; prints counts, wash reasons and the Trending top-10 change; `--restore` puts the copy back | free, seconds a day |
| `repair verify --sample 5` | Bait's 24h / 7d / prev 7d against GeckoTerminal candle sums for the same pools and closed days, as a markdown table | free, up to 15 calls |
| `repair prose --what-lines --blurbs --dry-run\|--yes [--limit N] [--max-usd X]` | Claude rewrites from stored facts: what-lines never written after a budget stop or cut mid-word at 200 chars; blurbs of the current signals targets written before package 8 | paid, Meter-checked |

GeckoTerminal candles carry no trade counts, so backfilled days keep `tx_count_day` NULL and those projects' `swaps_7d`
stays NULL (unknown) until seven nights of counts exist. Signals and digests are never rewritten by the repair.
```
- [ ] **Step 2: Edit `railway/README.md`.** Append:
```markdown
## One-off repairs (package 9)

Repairs run inside the `pipeline` service (Postgres has no public endpoint): set its start command to the repair
command, redeploy, wait for the process to exit, then clear the start command so the image `CMD` (`python -m bait run`)
is back before the 06:00 UTC cron. Start with `python -m bait repair all --since 14 --dry-run`; paid steps (`repair
prose`) run only after the owner's go. See `pipeline/README.md` "Repair".
```
- [ ] **Step 3: Edit `DESIGN.md`.** In "Data issues backlog", after the item "Remove `volume_usd_cum` / `tx_count_cum` once package 9 has re-scored: ...", add: "- Drop `project_daily_p9_backup` (package 9's pre-repair copy of re-scored days, defined in `pipeline/bait/db.py`) in the same cleanup, once the re-scored days are verified."
- [ ] **Step 4:** full suite → **292 passed, 12 skipped** (docs only).
- [ ] **Step 5: Commit**
```bash
git add pipeline/README.md railway/README.md DESIGN.md
git commit -m "docs: python -m bait repair, what each command writes and costs, and how to run one on Railway"
```

## Final report (the implementer returns this)

For each task: commit SHA, test counts before and after, anything that deviated from the plan and why. Then the output of `python -m bait repair all --since 14 --dry-run` from Task 8's smoke, `git log --oneline origin/main..HEAD` and `git diff --stat origin/main..HEAD`. Do not push.

## Operational notes for the orchestrator (production)

**Before anything.** Package 9 merged to `main` and the `pipeline` service redeployed with it. At least one package 8 nightly has finished with exit 0 (the first is 2026-10-10 06:00 UTC if package 8 deployed on 2026-10-09; check `/api/health` shows that day's receipt). The commands refuse (exit 5) otherwise.

**How to run one.** On the `pipeline` service: `update-service` with `startCommand` = the command below, redeploy, follow the logs until the process exits, read the receipt, then `update-service` with `startCommand` cleared (empty: the Dockerfile `CMD`), keeping `cronSchedule` `0 6 * * *`. The start command must be cleared before 06:00 UTC, or the cron runs the repair instead of the nightly. Never run a step while the nightly is running (it exits 3 anyway, without a receipt).

| order | `startCommand` | wall time | what to watch |
|---|---|---|---|
| 1 | `python -m bait repair all --since 14 --dry-run` | under a minute | The plan: pools needing candles (by chain), calls and minutes, stored run days, prose counts and worst-case USD, both budget options. Paste it to the owner. |
| 2 | `python -m bait repair backfill-days --since 14 --pools 600` | about 100 min at 10 s a call | `repair backfill-days: N/M pools, ... rows updated, ... inserted, ... failed` every 50 pools; `[keyless] GT ...` warnings. Receipt `repair backfill-days`: `done`, `failed`, `no_candles`, `beyond_cap`, `left_at_deadline`. Rerun the same command for `failed` or `beyond_cap` pools (it skips finished ones). |
| 3 | `python -m bait repair rescore --since 14` | a few minutes | First line says whether backfill is complete for these days. Per day: `projects / new / trending / wash (reasons)`, `top 10: K kept; in ...; out ...`, and a line when a chain's on-chain history starts inside that day's 7-day window. Receipt `repair rescore`. |
| 4 | `python -m bait repair verify --sample 5` | about 3 min | The markdown table; paste it into the tracker. Deltas within a few percent on keyless pools; on-chain pools can differ more (the indexer prices swaps itself). |
| (2-4 at once) | `python -m bait repair all --since 14 --yes --pools 600` | about 105 min | All of the above in order, one receipt per step; it stops after backfill when it hits 05:30 UTC. |
| 5, after the owner's go | `python -m bait repair prose --what-lines --blurbs --yes --max-usd <X from the dry run>` | about 5 s a call | `spend stop:` warnings mean a budget or the cap stopped it (what is written stays). Receipt `repair prose`: counts and `usd`. Rerun on a later day to continue. |

**Budget for prose.** `--max-usd` only lowers the ceiling; the $3 daily and $40 30-day budgets still apply. Option A: run step 5 after a nightly, with no budget change; it spends what the nightly left (the dry run estimates how many days the worst case takes) and a rerun continues. Option B: the owner sets `SPEND_DAILY_USD` (and, if the dry run says so, `SPEND_MONTHLY_USD`) on the `pipeline` service to the value the dry run prints, the run happens, then both go back to their defaults (3.00 / 40.00) before 06:00 UTC.

**Rolling back.** A bad re-score: `python -m bait repair rescore --since 14 --restore` (or `--day YYYY-MM-DD --restore`) puts back the rows copied before the first re-score of each day. Fixing the inputs and re-running `rescore` writes fresh rows from the same backup-protected days (`project_daily` is an upsert per project and day). Backfilled snapshot values have no copy: they replaced only rows with no per-day value or an estimate, the next nightlies rewrite on-chain pools and candled pools from day-14 on their own, and `rescore` reads whatever is there. Drop `project_daily_p9_backup` (remove it from `db.py` and `DROP TABLE` it) in the cleanup that removes the legacy cumulative columns.

**Estimates from the live facts (2026-10-09).** Backfill: about 600 pools (the brief's count; the dry run gives the real one after the first package 8 nightly) at 10 s → about 100 min, free; `--pools 600` keeps the busiest. Re-score: only the stored days since 2026-10-08, two or three, about a minute each. Verify: at most 15 calls, under 3 min. Prose worst case (`prose_estimate`: Sonnet 5.5 at $2 / $10 per million tokens, prompt length / 3 tokens, the full 600 output tokens): about $0.007 a call for a short prompt up to $0.0143 at the 12,000-char cap. The 29 missing what-lines: under $0.42. Blurbs, at most the 50 signals targets: under $0.72. Lines cut mid-word: the count is whatever the dry run finds (the brief says about 2,500); at 2,500 the worst case is about $18 to $36, more than a day's budget and a large share of the 30-day one, so they go in daily `--limit` chunks (option A) or with an explicit owner raise (option B). Real spend has run well under the worst case (short outputs), but plan on the worst case.
