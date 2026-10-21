# Package 1: Pipeline hotfixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the nightly `python -m bait run` survive its first unattended cron tick without crashing, paying a provider twice, or silently losing a step's writes.

**Architecture:** Eight small, independent fixes inside `pipeline/bait/`, each with a regression test. No schema change except reusing the existing `projects.enrichment` JSON column. Postgres-only behaviours (savepoints, advisory lock, side-connection writes) are guarded on `conn.dialect.name == "postgresql"` so the SQLite test suite keeps running unchanged, and one new Postgres-backed test file exercises them for real.

**Tech Stack:** Python 3.12, SQLAlchemy 2 Core, psycopg 3, pytest. A local Postgres (15+, `pg_ctl` and `initdb` on PATH) is needed for the Postgres tests.

**Spec:** The two audits of 2026-10-08. Findings covered here: signals `hits` crash; Parallel 404 treated as "still running"; Parallel results lost when Claude fails or budget stops; run_ids persisted only at batch end inside the step transaction; per-item `try/except` without savepoints poisons the Postgres transaction; no run lock; ingest all-or-nothing; Nansen retired (credits exhausted, key revoked and removed from Railway).

## Global Constraints

- Python 3.12. Run tests with `cd pipeline && .venv/bin/python -m pytest -q` (the venv already exists in this worktree).
- SQLite is the default test backend. Postgres tests live in `tests/test_postgres.py` and skip unless `TEST_DATABASE_URL` is set.
- Never call a paid API or set a real key. Tests use fakes only.
- Commit after every task. Commit message style matches the repo: imperative subject, a short body when the why is not obvious. End every commit message with these two trailer lines, exactly:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01A2FHLwk9NeThmyZ1JbHe3o`
- No hostnames, absolute paths, or secrets in code, docs, or commits.
- Do not change `web/`, `db/migrations/`, or `pipeline/bait/indexer/`. Those belong to other packages.
- Do not push. The orchestrator reviews the branch and pushes.

## Review Focus

Inputs the spec implies but no existing test exercises; each gets a test in the owning task below.

1. A signals target with no name tokens (an "Unlisted hook 0x…" project) as the FIRST target while Exa is configured: must not raise, must not reuse a previous project's hits. (Task 1)
2. Parallel `/result` returning 404 for a stored `parallel_run_id`: the project must proceed to Claude in the same run and never be resubmitted. (Task 2)
3. Claude raising after Parallel results were collected: the paid Parallel output must already be in the database. (Task 3)
4. A Postgres statement error inside a per-item loop: earlier and later items in the same step must persist and the step must report `errors >= 1`. (Task 4)
5. Two pipeline processes starting at once against Postgres: the second must exit with code 3 without running any step. (Task 5)

---

### Task 1: signals: `hits` is undefined for unnamed projects

**Files:**
- Modify: `pipeline/bait/steps/signals.py:27-37`
- Test: `pipeline/tests/test_signals_digest.py`

**Interfaces:**
- Consumes: `FakeExa`, `seed_daily`, `insert_project`, `insert_hook` already defined in `tests/test_signals_digest.py` and `tests/conftest.py`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Append to `pipeline/tests/test_signals_digest.py`:

```python
def test_signals_unnamed_first_target_does_not_crash_or_leak_hits(conn, ctx):
    # An unlisted hook has no name tokens, so Exa is never queried for it. Before the fix, `hits` was unbound on
    # the first such project (UnboundLocalError -> soft error) and stale on later ones.
    unnamed = insert_project(conn, "Unlisted hook 0xabcd…1234", NOW - timedelta(days=40), slug="unlisted-hook-0xabcd-1234")
    named = insert_project(conn, "Hot", NOW - timedelta(days=40), slug="hot")
    for pid in (unnamed, named):
        insert_hook(conn, f"0x{pid}", 1, project_id=pid)
    seed_daily(conn, unnamed, score=50.0, tvl=5e6)      # ranks first
    seed_daily(conn, named, score=30.0, tvl=1e6)
    hit = {"url": "https://blog.example.com/hot", "title": "Hot hook ships", "source": "blog.example.com",
           "published_at": NOW - timedelta(days=3), "highlights": []}
    exa = FakeExa([hit])
    ctx.clients.update(exa=exa, github=None, claude=None, nansen=None)
    out = signals.run(conn, ctx)
    assert out["errors"] == 0
    assert exa.calls == 1                                  # only the named project was searched
    kinds = {(r.project_id, r.kind) for r in conn.execute(sa.select(db.signals.c.project_id, db.signals.c.kind))}
    assert (unnamed, "exa") not in kinds                   # no leaked receipt signal on the unnamed project
    assert (named, "exa") in kinds
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_signals_digest.py::test_signals_unnamed_first_target_does_not_crash_or_leak_hits`
Expected: FAIL, `assert out["errors"] == 0` (the step logged `UnboundLocalError` as a soft error).

- [ ] **Step 3: Fix**

In `pipeline/bait/steps/signals.py`, inside `for p, daily in targets:` add `hits = None` right after `changed = False`:

```python
    for p, daily in targets:
        changed = False
        hits = None
        try:
```

- [ ] **Step 4: Run the full suite**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: 119 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/bait/steps/signals.py pipeline/tests/test_signals_digest.py
git commit -m "signals: initialise hits per project so unnamed targets never crash or inherit receipts"
```

---

### Task 2: Parallel: 404 and stale run_ids are terminal, never resubmitted

**Files:**
- Modify: `pipeline/bait/clients/parallel.py:58-76`
- Modify: `pipeline/bait/steps/enrich.py:100-161` (`_parallel_batch`)
- Test: `pipeline/tests/test_enrich.py`

**Interfaces:**
- Produces: `bait.clients.parallel.RunGone(Exception)` raised by `Parallel.result()` on HTTP 404.
- Produces: `enrichment["parallel"] == {"_failed": "<run_id>", "reason": "404"|"expired"}` marks a paid run that will never deliver. `"parallel" in enrichment` therefore stays true and `_parallel_batch` never resubmits it.
- Produces: `enrichment["parallel_run_at"]` ISO timestamp written next to `parallel_run_id`; `PARALLEL_RUN_MAX_AGE_H` env (default 48) bounds how long a pending run is polled.

- [ ] **Step 1: Write the failing tests**

Append to `pipeline/tests/test_enrich.py`:

```python
def _seed_one(conn, name="Gone", slug="gone", enrichment=None):
    pid = insert_project(conn, name, first_seen=NOW - timedelta(days=30), slug=slug)
    addr = "0x" + "6" * 40
    insert_hook(conn, addr, 8453, project_id=pid, name=name)
    insert_pool(conn, "0xp" + slug, 8453, addr, NOW - timedelta(days=30))
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xp" + slug, chain_id=8453, day=DAY, vol_24h=5000.0, traders_24h=50))
    if enrichment is not None:
        conn.execute(sa.update(db.projects).values(enrichment=enrichment).where(db.projects.c.id == pid))
    return pid


class _Resp:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data or {}

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_parallel_404_is_terminal_project_still_gets_claude_and_is_never_resubmitted(conn, ctx, monkeypatch):
    from bait.clients.parallel import Parallel
    from bait.costs import Budgets, Meter
    monkeypatch.setenv("ENRICH_MAX", "1")
    pid = _seed_one(conn, enrichment={"parallel_run_id": "trun_dead", "parallel_run_at": NOW.isoformat()})

    class Http:
        def __init__(self):
            self.posts, self.gets = 0, 0

        def post(self, url, **kw):
            self.posts += 1
            return _Resp(200, {"run_id": "trun_new"})

        def get(self, url, **kw):
            self.gets += 1
            return _Resp(404, {"error": "run not found"})

    h = Http()
    par = Parallel("k", http_client=h, poll_s=0, max_wait_s=5); par.meter = Meter(None, budgets=Budgets(5.0, 50.0, {}))
    claude = FakeClaude()
    ctx.clients.update(claude=claude, parallel=par, blockscout=None)
    out = enrich.run(conn, ctx)
    row = conn.execute(sa.select(db.projects).where(db.projects.c.id == pid)).one()
    assert h.posts == 0 and h.gets == 1                        # no resubmit, one lookup
    assert out["parallel_failed"] == 1 and out["errors"] == 0
    assert row.enrichment["parallel"] == {"_failed": "trun_dead", "reason": "404"}
    assert "parallel_run_id" not in row.enrichment
    assert claude.calls == ["Gone"] and row.enriched_at is not None   # Claude ran without Parallel facts
    # a second run must not touch Parallel at all
    out2 = enrich.run(conn, ctx)
    assert h.posts == 0 and h.gets == 1 and out2["candidates"] == 0


def test_parallel_pending_run_older_than_max_age_is_expired_not_polled(conn, ctx, monkeypatch):
    from bait.clients.parallel import Parallel
    from bait.costs import Budgets, Meter
    monkeypatch.setenv("ENRICH_MAX", "1")
    monkeypatch.setenv("PARALLEL_RUN_MAX_AGE_H", "48")
    old = (NOW - timedelta(hours=49)).isoformat()
    pid = _seed_one(conn, name="Old", slug="old", enrichment={"parallel_run_id": "trun_old", "parallel_run_at": old})

    class Http:
        def __init__(self):
            self.posts, self.gets = 0, 0

        def post(self, url, **kw):
            self.posts += 1
            return _Resp(200, {"run_id": "x"})

        def get(self, url, **kw):
            self.gets += 1
            return _Resp(200, {"output": {"content": {"team": "late"}}})

    h = Http()
    par = Parallel("k", http_client=h, poll_s=0, max_wait_s=5); par.meter = Meter(None, budgets=Budgets(5.0, 50.0, {}))
    ctx.clients.update(claude=FakeClaude(), parallel=par, blockscout=None)
    out = enrich.run(conn, ctx)
    row = conn.execute(sa.select(db.projects).where(db.projects.c.id == pid)).one()
    assert h.posts == 0 and h.gets == 0                        # neither resubmitted nor polled
    assert out["parallel_failed"] == 1
    assert row.enrichment["parallel"] == {"_failed": "trun_old", "reason": "expired"} and row.enriched_at is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_enrich.py -k "404 or max_age"`
Expected: both FAIL (`RunGone` import error or `KeyError: 'parallel_failed'`).

- [ ] **Step 3: Implement the client side**

In `pipeline/bait/clients/parallel.py`, add after the imports:

```python
class RunGone(Exception):
    """The run id is unknown to Parallel (HTTP 404): failed or expired. It will never deliver; do not poll again."""
```

Replace the status handling in `result()`:

```python
            if r.status_code == 200:
                d = r.json()
                out = (d.get("output") or {}).get("content") or {}
                return {k: (v or "") for k, v in out.items()} | {"_run_id": run_id}
            if r.status_code == 404:
                raise RunGone(run_id)
            if r.status_code not in (202, 408, 425):
                r.raise_for_status()
            time.sleep(self.poll_s)
```

- [ ] **Step 4: Implement the step side**

In `pipeline/bait/steps/enrich.py`, `_parallel_batch`:

Add `out.setdefault("parallel_failed", 0)` at the top of `run()` next to the other counters (put `"parallel_failed": 0` in the `out` dict literal).

Replace the `pending`/`fresh` split with age-aware selection, and mark expired runs:

```python
    from datetime import datetime, timedelta
    max_age = timedelta(hours=_env_hours("PARALLEL_RUN_MAX_AGE_H", 48))
    pending, fresh = [], []
    for p in need:
        enr = dict(p.enrichment or {})
        rid = enr.get("parallel_run_id")
        if not rid:
            fresh.append(p)
            continue
        started = _parse_iso(enr.get("parallel_run_at"))
        if started is not None and ctx.now - started > max_age:
            _mark_failed(conn, p, enr, rid, "expired", out)
            continue
        pending.append(p)                                # already paid: free to collect
```

`_parallel_batch` has no `ctx` today: change its signature to `_parallel_batch(conn, ctx, par, todo, out)` and update the call in `run()` to match.

Add these helpers at module level in `enrich.py`:

```python
def _env_hours(name: str, default: int) -> int:
    import os
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _parse_iso(s):
    from datetime import datetime
    if not s:
        return None
    try:
        return as_utc(datetime.fromisoformat(str(s).replace("Z", "+00:00")))
    except ValueError:
        return None


def _mark_failed(conn, p, enr: dict, run_id: str, reason: str, out: dict) -> None:
    """A paid run that will never deliver. Keep a tombstone under "parallel" so it is never resubmitted."""
    enr.pop("parallel_run_id", None)
    enr.pop("parallel_run_at", None)
    enr["parallel"] = {"_failed": run_id, "reason": reason}
    _persist_enrichment(conn, p.id, enr)
    out["parallel_failed"] += 1
    log.warning("enrich parallel project=%s run=%s %s: continuing without Parallel facts", p.slug, run_id, reason)
```

`_persist_enrichment` gets its real implementation in Task 3; for this task define it minimally as:

```python
def _persist_enrichment(conn, pid: int, enrichment: dict) -> None:
    conn.execute(sa.update(projects).values(enrichment=enrichment).where(projects.c.id == pid))
```

Import `as_utc` from `..db` at the top of `enrich.py`.

In `one(p)`, catch `RunGone`:

```python
    from ..clients.parallel import RunGone

    def one(p):
        run_id = (p.enrichment or {}).get("parallel_run_id")
        try:
            if not run_id:
                run_id = par.submit(p.name, hints[p.id])
                submitted[p.id] = run_id
            return p.id, par.result(run_id)
        except RunGone:
            gone[p.id] = run_id
            return p.id, None
        except BudgetExceeded:
            return p.id, None
        except Exception as e:  # one task failing must not lose the others
            log.warning("enrich parallel project=%s failed: %s", p.slug, e)
            return p.id, None
```

Declare `gone: dict[int, str] = {}` next to `submitted`. After the executor block, before the `waiting` loop, tombstone the gone runs and exclude them from `waiting`:

```python
    for p in need:
        if p.id in gone:
            _mark_failed(conn, p, dict(p.enrichment or {}), gone[p.id], "404", out)
```

and in the `waiting` loop skip `p.id in gone`. The per-project loop in `run()` must then see the tombstone: it re-reads `p.enrichment` from the `todo` row, which is stale. Fix by re-selecting enrichment for projects touched by the batch. Simplest: in `run()`, after `_parallel_batch`, build `fresh_enr = {r.id: r.enrichment for r in conn.execute(sa.select(projects.c.id, projects.c.enrichment).where(projects.c.id.in_([p.id for p in todo])))}` and use `enrichment = dict(fresh_enr.get(p.id) or {})` instead of `dict(p.enrichment or {})`.

When recording a fresh submission in the `waiting` loop, also store the start time:

```python
            enr = dict(p.enrichment or {}) | {"parallel_run_id": rid, "parallel_run_at": ctx.now.isoformat()}
```

- [ ] **Step 5: Run the full suite**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: 121 passed. The existing `test_parallel_timeouts_are_not_failures_and_paid_runs_are_collected_not_repaid` must still pass (its stored run_id has no `parallel_run_at`, so it is never expired).

- [ ] **Step 6: Commit**

```bash
git add pipeline/bait/clients/parallel.py pipeline/bait/steps/enrich.py pipeline/tests/test_enrich.py
git commit -m "enrich: Parallel 404 and runs older than PARALLEL_RUN_MAX_AGE_H are terminal tombstones, never resubmitted

Parallel's docs: 404 on /result means the run failed or is unknown. It was treated as
'still running', so a dead run wedged its project for 600 s a night and took one of the
ENRICH_MAX slots forever."
```

---

### Task 3: Paid Parallel results and run_ids are durable the moment they exist

**Files:**
- Modify: `pipeline/bait/steps/enrich.py` (`_persist_enrichment`, `_parallel_batch`, `run`)
- Test: `pipeline/tests/test_enrich.py`

**Interfaces:**
- Produces: `_persist_enrichment(conn, pid, enrichment)` writes on its own committed connection when `conn.dialect.name == "postgresql"`, otherwise through `conn` (SQLite has one writer). Mirrors `costs.Meter.record`.
- Produces: every collected Parallel result is written as `enrichment["parallel"]` inside `_parallel_batch`, before any Claude call. Every fresh submission's run_id is written right after `submit()` returns, inside `one()`, not at batch end.

- [ ] **Step 1: Write the failing tests**

Append to `pipeline/tests/test_enrich.py`:

```python
def test_collected_parallel_result_survives_claude_failure_and_is_not_resubmitted(conn, ctx, monkeypatch):
    from bait.costs import Budgets, Meter
    monkeypatch.setenv("ENRICH_MAX", "1")
    pid = _seed_one(conn, name="Fragile", slug="fragile")

    class Par:
        processor = "core"

        def __init__(self, meter):
            self.meter, self.submits = meter, 0

        def submit(self, name, hints):
            self.submits += 1
            self.meter.record("parallel", 0.025)
            return "trun_ok"

        def result(self, run_id, max_wait_s=None):
            return {"team": "Fragile Labs", "_run_id": run_id}

    class BrokenClaude:
        def what_line(self, ctxd):
            raise RuntimeError("anthropic 529 overloaded")

    meter = Meter(None, budgets=Budgets(5.0, 50.0, {}))
    par = Par(meter)
    ctx.clients.update(claude=BrokenClaude(), parallel=par, blockscout=None)
    out = enrich.run(conn, ctx)
    row = conn.execute(sa.select(db.projects).where(db.projects.c.id == pid)).one()
    assert out["errors"] == 1 and row.enriched_at is None
    assert row.enrichment["parallel"]["team"] == "Fragile Labs"      # paid result kept despite Claude failing
    # next night: Claude works, Parallel is not paid again
    ctx.clients.update(claude=FakeClaude())
    out2 = enrich.run(conn, ctx)
    row = conn.execute(sa.select(db.projects).where(db.projects.c.id == pid)).one()
    assert par.submits == 1 and out2["enriched"] == 1 and row.enriched_at is not None
    assert meter.run_usd == pytest.approx(0.025)


def test_budget_stop_mid_loop_keeps_every_collected_parallel_result(conn, ctx, monkeypatch):
    from bait.costs import Budgets, BudgetExceeded, Meter
    monkeypatch.setenv("ENRICH_MAX", "3")
    for i, n in enumerate(("A", "B", "C")):
        pid = insert_project(conn, n, first_seen=NOW - timedelta(days=30), slug=n.lower())
        addr = f"0x{i + 7:040x}"
        insert_hook(conn, addr, 8453, project_id=pid, name=n)
        insert_pool(conn, f"0xq{i}", 8453, addr, NOW - timedelta(days=30))
        conn.execute(sa.insert(db.pool_stats).values(pool_id=f"0xq{i}", chain_id=8453, day=DAY, vol_24h=1000.0 * (3 - i), traders_24h=50))

    class Par:
        processor = "core"

        def __init__(self, meter):
            self.meter = meter

        def submit(self, name, hints):
            self.meter.record("parallel", 0.025)
            return f"trun_{name}"

        def result(self, run_id, max_wait_s=None):
            return {"team": run_id, "_run_id": run_id}

    class StingyClaude:
        """First what_line succeeds, the second raises BudgetExceeded (daily budget hit mid-loop)."""

        def __init__(self):
            self.calls = []

        def what_line(self, ctxd):
            self.calls.append(ctxd["name"])
            if len(self.calls) == 2:
                raise BudgetExceeded("claude: daily budget")
            return {"what_line": "x", "category": "dynamic-fee"}

    meter = Meter(None, budgets=Budgets(5.0, 50.0, {}))
    ctx.clients.update(claude=StingyClaude(), parallel=Par(meter), blockscout=None)
    out = enrich.run(conn, ctx)
    assert out["budget_stop"] == 1
    rows = {r.slug: r for r in conn.execute(sa.select(db.projects))}
    assert all(rows[s].enrichment.get("parallel", {}).get("team") == f"trun_{s.upper()}" for s in ("a", "b", "c"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_enrich.py -k "survives_claude or budget_stop_mid_loop"`
Expected: both FAIL on the `enrichment["parallel"]` assertions (results were only written after Claude succeeded).

- [ ] **Step 3: Implement**

In `pipeline/bait/steps/enrich.py` replace the minimal `_persist_enrichment` from Task 2 with:

```python
def _persist_enrichment(conn, pid: int, enrichment: dict) -> None:
    """Durable write of paid facts. Postgres: own connection + commit, so a later exception, budget stop, or kill
    in this step cannot roll back a result we already paid for. SQLite allows one writer and the step holds it,
    so the step connection is used (tests)."""
    stmt = sa.update(projects).values(enrichment=enrichment).where(projects.c.id == pid)
    if conn.dialect.name == "postgresql":
        with conn.engine.begin() as c:
            c.execute(stmt)
    else:
        conn.execute(stmt)
```

In `_parallel_batch`, inside `one(p)`, persist the run_id immediately after a successful `submit()`:

```python
            if not run_id:
                run_id = par.submit(p.name, hints[p.id])
                submitted[p.id] = run_id
                _persist_enrichment(conn, p.id, dict(p.enrichment or {}) | {"parallel_run_id": run_id, "parallel_run_at": ctx.now.isoformat()})
```

Note `one()` runs in worker threads. On Postgres `_persist_enrichment` opens its own connection, which is thread-safe. On SQLite it writes through `conn` from a worker thread; `make_engine` already sets `check_same_thread=False` and `StaticPool`, and the existing `test_parallel_runs_side_by_side_and_respects_budget` already exercises threads against `conn`. Keep it.

After the executor block, persist every collected result before returning:

```python
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = {pid: res for pid, res in ex.map(one, need) if res}
    by_id = {p.id: p for p in need}
    for pid, res in results.items():
        enr = dict(by_id[pid].enrichment or {})
        enr.pop("parallel_run_id", None)
        enr.pop("parallel_run_at", None)
        enr["parallel"] = res
        _persist_enrichment(conn, pid, enr)
        out["parallel"] += 1
```

Then in `run()`:
- Delete the block `if "parallel" not in enrichment and par_results.get(p.id): enrichment["parallel"] = ...; out["parallel"] += 1` (the batch already wrote it and counted it).
- Keep reading `enrichment` from `fresh_enr` (Task 2), which now contains the result.
- In the `except BudgetExceeded:` branch, the `if enrichment != dict(p.enrichment or {})` write becomes unnecessary for Parallel; keep it only for the Blockscout `source` key, i.e. leave the line but it is harmless.

`_parallel_batch` still returns `(results, waiting)`; `run()` uses only `waiting` now. Rename the unpacking to `_, waiting = ...`.

- [ ] **Step 4: Run the full suite**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: 123 passed. Check `test_parallel_runs_side_by_side_and_respects_budget` still asserts `out["parallel"] == 3`.

- [ ] **Step 5: Commit**

```bash
git add pipeline/bait/steps/enrich.py pipeline/tests/test_enrich.py
git commit -m "enrich: persist Parallel run_ids on submit and results on collect, before any Claude call

Results used to live in memory until what_line succeeded; an Anthropic outage or a
mid-loop budget stop threw away paid output and the next night paid again."
```

---

### Task 4: Per-item failures no longer poison the Postgres step transaction

**Files:**
- Modify: `pipeline/bait/db.py` (add `savepoint`)
- Modify: `pipeline/bait/steps/snapshot.py:22-37`, `pipeline/bait/steps/activity.py:55-69`, `pipeline/bait/steps/signals.py:29-62`, `pipeline/bait/steps/radar.py:96-101`, `pipeline/bait/steps/enrich.py` (per-project `try`)
- Create: `pipeline/tests/test_postgres.py`
- Modify: `pipeline/tests/conftest.py` (add `pg_engine` fixture)
- Create: `pipeline/scripts/pg-test-cluster.sh`

**Interfaces:**
- Produces: `bait.db.savepoint(conn)` context manager. On Postgres it is `conn.begin_nested()`; elsewhere a no-op. Wrap the body of every per-item `try:` with it so a failed statement rolls back only that item.
- Produces: `TEST_DATABASE_URL` env read by `tests/conftest.py::pg_engine`; tests in `tests/test_postgres.py` skip when unset.

- [ ] **Step 1: Add the Postgres test fixture and cluster script**

Create `pipeline/scripts/pg-test-cluster.sh`:

```bash
#!/usr/bin/env bash
# Throwaway Postgres for the integration tests. Usage: scripts/pg-test-cluster.sh start|stop
# Prints the TEST_DATABASE_URL to export. Needs pg_ctl/initdb on PATH (Postgres 15+).
set -euo pipefail
DIR="${PG_TEST_DIR:-${TMPDIR:-/tmp}/bait-pg-test}"
PORT="${PG_TEST_PORT:-5499}"
case "${1:-}" in
  start)
    if [ ! -d "$DIR" ]; then initdb -D "$DIR" -A trust -U postgres >/dev/null; fi
    pg_ctl -D "$DIR" -o "-p $PORT -k $DIR -c listen_addresses=" -l "$DIR/log" start >/dev/null
    echo "export TEST_DATABASE_URL=postgresql+psycopg://postgres@/postgres?host=$DIR&port=$PORT"
    ;;
  stop)
    pg_ctl -D "$DIR" stop >/dev/null && rm -rf "$DIR"
    ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac
```

`chmod +x pipeline/scripts/pg-test-cluster.sh`.

Append to `pipeline/tests/conftest.py`:

```python
import os


@pytest.fixture
def pg_engine():
    """Real Postgres for behaviour SQLite cannot show (aborted transactions, savepoints, advisory locks).
    Skips unless TEST_DATABASE_URL is set; see scripts/pg-test-cluster.sh."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    e = db.make_engine(url)
    db.metadata.drop_all(e)
    db.create_all(e)
    db.add_missing_columns(e)
    yield e
    db.metadata.drop_all(e)
    e.dispose()
```

Move the `import os` to the top of the file with the other imports.

- [ ] **Step 2: Write the failing test**

Create `pipeline/tests/test_postgres.py`:

```python
"""Postgres-only behaviour. Run with: eval "$(scripts/pg-test-cluster.sh start)" && .venv/bin/python -m pytest -q tests/test_postgres.py"""
from datetime import timedelta

import pytest
import sqlalchemy as sa

from bait import db
from bait.steps import RunContext, signals
from tests.conftest import DAY, FIXTURE, NOW


def _ctx():
    return RunContext(day=DAY, now=NOW, hooklist_file=FIXTURE, max_code_fetch=0,
                      clients={k: None for k in ("graph", "blockscout", "claude", "exa", "parallel", "github", "names")})


def test_one_bad_item_does_not_lose_the_step_on_postgres(pg_engine):
    # Three signals targets. The middle one's receipt write violates a constraint. Before savepoints, Postgres
    # aborted the whole transaction: the first project's signals vanished too and the log still said "ok".
    with pg_engine.begin() as conn:
        pids = []
        for i, name in enumerate(("Alpha", "Beta", "Gamma")):
            r = conn.execute(sa.insert(db.projects).values(slug=name.lower(), name=name, norm_name=name.lower(),
                                                           first_seen_at=NOW - timedelta(days=40)))
            pid = r.inserted_primary_key[0]
            pids.append(pid)
            conn.execute(sa.insert(db.hooks).values(address=f"0x{i:040x}", chain_id=1, project_id=pid, name=name,
                                                    first_seen_at=NOW, flags={}, properties={}))
            conn.execute(sa.insert(db.project_daily).values(project_id=pid, day=DAY, tvl_usd=1e6 - i, vol_7d=0, vol_7d_prev=0,
                                                            swaps_7d=0, pools_n=1, score_trending=30.0 - i, is_new=False, is_spam=False))

    class Exa:
        def search_recent(self, query, days=30, n=10, now=None):
            if "Beta" in query:
                # url None violates receipts.url NOT NULL -> IntegrityError inside the per-project try
                return [{"url": None, "title": "Beta", "source": "x", "published_at": NOW, "highlights": []}]
            return [{"url": f"https://e.com/{query[:5]}", "title": f"{query[1:6]} hook ships", "source": "e.com",
                     "published_at": NOW - timedelta(days=1), "highlights": []}]

    ctx = _ctx()
    ctx.clients["exa"] = Exa()
    with pg_engine.begin() as conn:
        out = signals.run(conn, ctx)
    assert out["errors"] == 1
    with pg_engine.connect() as conn:
        got = {r.project_id for r in conn.execute(sa.select(db.signals.c.project_id).where(db.signals.c.kind == "alive"))}
    assert got == set(pids)                       # Alpha and Gamma persisted; Beta's alive signal too (written after its failed receipt)
```

Note: whether Beta's `alive` row exists depends on where the exception lands; the assertion that matters is that Alpha and Gamma persisted. If Beta's `alive` row is absent after the fix, change the assertion to `assert {pids[0], pids[2]} <= got`.

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd pipeline && eval "$(scripts/pg-test-cluster.sh start)" && .venv/bin/python -m pytest -q tests/test_postgres.py
```
Expected: FAIL. Either `got == set()` (transaction aborted, `engine.begin()` rolled back silently) or `InFailedSqlTransaction` raised from a later statement.

- [ ] **Step 4: Implement `savepoint` and wrap the loops**

Add to `pipeline/bait/db.py` (near `upsert`):

```python
from contextlib import contextmanager, nullcontext


def savepoint(conn: sa.Connection):
    """Per-item isolation inside a step transaction. Postgres aborts the whole transaction on the first
    statement error, so each item's writes run in a SAVEPOINT that rolls back alone. SQLite: no-op (pysqlite's
    savepoint support is unreliable and the test suite runs there)."""
    return conn.begin_nested() if conn.dialect.name == "postgresql" else nullcontext()
```

Wrap each per-item `try:` body. The pattern, applied verbatim at each site:

`snapshot.py` per chain (`for ch in wanted:`):
```python
        try:
            with savepoint(conn):
                n = _snapshot_chain(conn, ctx, g, ch)
```
and the keyless call:
```python
        try:
            with savepoint(conn):
                _keyless(conn, ctx, rest, out)
```

`activity.py` per pool:
```python
        try:
            with savepoint(conn):
                trades = K.fetch_trades(http, net, pid)
                if trades is None:
                    out["pools_no_trades"] += 1
                    continue
                ...existing body unchanged, indented one level...
```
(`continue` inside a `with` is fine.)

`signals.py` per project: wrap the body of the first `try:` (from `if exa is not None and name_tokens(p.name):` through `changed |= _set_signal(... "alive" ...)`) in `with savepoint(conn):`. Also wrap the blurb `try:` body the same way.

`radar.py` per source:
```python
        try:
            with savepoint(conn):
                fn(conn, ctx, gh, out)
```

`enrich.py` per project: wrap the body of the `try:` that calls `claude.what_line` in `with savepoint(conn):`, keeping the `except BudgetExceeded` / `except Exception` clauses as they are.

Import `savepoint` from `..db` in each of the five step modules.

- [ ] **Step 5: Run both suites**

```bash
cd pipeline && .venv/bin/python -m pytest -q && .venv/bin/python -m pytest -q tests/test_postgres.py
```
Expected: 123 passed (SQLite; the Postgres test shows as skipped there) and 1 passed under `TEST_DATABASE_URL`.

- [ ] **Step 6: Commit**

```bash
git add pipeline/bait/db.py pipeline/bait/steps/snapshot.py pipeline/bait/steps/activity.py pipeline/bait/steps/signals.py pipeline/bait/steps/radar.py pipeline/bait/steps/enrich.py pipeline/tests/conftest.py pipeline/tests/test_postgres.py pipeline/scripts/pg-test-cluster.sh
git commit -m "steps: savepoint per item so one failed write cannot abort the whole step on Postgres

Per-item try/except caught the Python error but Postgres had already aborted the
transaction; engine.begin() then rolled back everything and the log said ok."
```

---

### Task 5: One pipeline run at a time (Postgres advisory lock)

**Files:**
- Modify: `pipeline/bait/__main__.py:86-104`
- Modify: `pipeline/bait/db.py` (add `try_run_lock`)
- Test: `pipeline/tests/test_postgres.py`, `pipeline/tests/test_cli.py`

**Interfaces:**
- Produces: `bait.db.try_run_lock(engine) -> sa.Connection | None`. Postgres: opens a dedicated connection and runs `SELECT pg_try_advisory_lock(hashtext('bait-run'))`; returns the connection (keep it open for the run) or `None` if another session holds it. Non-Postgres: returns a sentinel open connection so callers need no dialect checks.
- Produces: exit code `3` from `python -m bait run` when the lock is held.

- [ ] **Step 1: Write the failing tests**

Append to `pipeline/tests/test_postgres.py`:

```python
def test_second_runner_cannot_take_the_run_lock(pg_engine):
    first = db.try_run_lock(pg_engine)
    assert first is not None
    try:
        assert db.try_run_lock(pg_engine) is None
    finally:
        first.close()
    again = db.try_run_lock(pg_engine)         # released with the connection
    assert again is not None
    again.close()
```

Append to `pipeline/tests/test_cli.py` (check the file's existing imports; it already imports `main` from `bait.__main__` or runs via `subprocess`; follow its style):

```python
def test_run_exits_3_when_lock_is_held(monkeypatch, tmp_path):
    from bait import __main__ as cli, db
    monkeypatch.setattr(db, "try_run_lock", lambda engine: None)
    rc = cli.main(["run", "--database-url", f"sqlite:///{tmp_path}/x.sqlite", "--steps", "ingest",
                   "--hooklist-file", FIXTURE, "--day", "2026-10-06"])
    assert rc == 3
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd pipeline && .venv/bin/python -m pytest -q tests/test_cli.py -k lock && .venv/bin/python -m pytest -q tests/test_postgres.py -k lock
```
Expected: FAIL with `AttributeError: module 'bait.db' has no attribute 'try_run_lock'`.

- [ ] **Step 3: Implement**

Add to `pipeline/bait/db.py`:

```python
RUN_LOCK_KEY = "bait-run"


def try_run_lock(engine: sa.Engine) -> sa.Connection | None:
    """One nightly run at a time. Postgres: session-level advisory lock held by the returned connection; close it
    to release. Returns None when another runner holds it. Other dialects: no lock, returns an open connection."""
    conn = engine.connect()
    if conn.dialect.name != "postgresql":
        return conn
    got = conn.execute(sa.text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": RUN_LOCK_KEY}).scalar_one()
    if got:
        return conn
    conn.close()
    return None
```

In `pipeline/bait/__main__.py`, after `day = args.day or ...` and before building `ctx`:

```python
    lock = db.try_run_lock(engine)
    if lock is None:
        log.error("another bait run holds the run lock; exiting (manual run overlapping the cron?)")
        return 3
```

Wrap the rest of `main()` from `ctx = RunContext(...)` to the final `return 0` in `try: ... finally: lock.close()`. Keep the early `return 1` paths inside the `try`; `finally` releases the lock on every exit.

- [ ] **Step 4: Run both suites**

```bash
cd pipeline && .venv/bin/python -m pytest -q && .venv/bin/python -m pytest -q tests/test_postgres.py
```
Expected: 124 passed; 2 passed under Postgres.

- [ ] **Step 5: Commit**

```bash
git add pipeline/bait/db.py pipeline/bait/__main__.py pipeline/tests/test_postgres.py pipeline/tests/test_cli.py
git commit -m "run: Postgres advisory lock so a manual run and the cron cannot pay for the same work twice"
```

---

### Task 6: Ingest validates entries one at a time and a fetch failure is a soft error

**Files:**
- Modify: `pipeline/bait/clients/hooklist.py:31-37`
- Modify: `pipeline/bait/steps/ingest.py:13-14,31`
- Test: `pipeline/tests/test_ingest.py`

**Interfaces:**
- Produces: `hooklist.load_entries(path=None, url=HOOKLIST_URL, http_client=None) -> tuple[list[HookEntry], int]` returning valid entries and the count skipped. `hooklist.load(...)` keeps its signature and returns only the list (callers elsewhere unchanged).
- Produces: `ingest.run` returns `{"hooks", "new", "updated", "invalid", "errors"}`; a fetch/parse failure logs an error, sets `errors=1`, and returns without touching `hooks`.

- [ ] **Step 1: Write the failing tests**

Append to `pipeline/tests/test_ingest.py` (follow its existing imports; it uses `conn`, `ctx`, `fixture_entries`):

```python
def test_one_invalid_hooklist_entry_is_skipped_not_fatal(conn, ctx, fixture_entries, tmp_path):
    import json
    bad = dict(fixture_entries[0])
    bad["hook"] = {"address": "0xdead", "chain": "base"}          # chainId missing -> validation error
    path = tmp_path / "hooklist.json"
    path.write_text(json.dumps([bad] + fixture_entries[1:]))
    ctx.hooklist_file = str(path)
    out = ingest.run(conn, ctx)
    assert out["invalid"] == 1 and out["errors"] == 0
    assert out["hooks"] == len(fixture_entries) - 1


def test_hooklist_fetch_failure_is_a_soft_error_and_keeps_existing_hooks(conn, ctx, monkeypatch):
    from bait.clients import hooklist
    ingest.run(conn, ctx)                                        # seed from the fixture
    before = conn.execute(sa.select(sa.func.count()).select_from(db.hooks)).scalar_one()

    def boom(**kw):
        raise RuntimeError("GitHub raw 503")
    monkeypatch.setattr(hooklist, "load_entries", boom)
    out = ingest.run(conn, ctx)
    assert out["errors"] == 1 and out["hooks"] == 0
    assert conn.execute(sa.select(sa.func.count()).select_from(db.hooks)).scalar_one() == before
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_ingest.py -k "invalid or fetch_failure"`
Expected: FAIL (`pydantic.ValidationError` propagates; `AttributeError: load_entries`).

- [ ] **Step 3: Implement**

`pipeline/bait/clients/hooklist.py`:

```python
import logging

from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger("bait")


def load_entries(path: str | None = None, url: str = HOOKLIST_URL, http_client=None) -> tuple[list[HookEntry], int]:
    """Valid entries plus the number skipped. One malformed entry must not take the whole registry down."""
    if path:
        raw = json.loads(Path(path).read_text())
    else:
        c = http_client or http.client()
        raw = http.get_json(c, url)
    entries, skipped = [], 0
    for i, e in enumerate(raw):
        try:
            entries.append(HookEntry.model_validate(e))
        except ValidationError as err:
            skipped += 1
            log.warning("hooklist entry %d skipped: %s", i, str(err).splitlines()[0])
    return entries, skipped


def load(path: str | None = None, url: str = HOOKLIST_URL, http_client=None) -> list[HookEntry]:
    return load_entries(path, url, http_client)[0]
```

`pipeline/bait/steps/ingest.py`:

```python
def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"hooks": 0, "new": 0, "updated": 0, "invalid": 0, "errors": 0}
    try:
        entries, out["invalid"] = hooklist.load_entries(path=ctx.hooklist_file)
    except Exception as e:  # registry unreachable: keep last night's hooks, let the other steps run
        out["errors"] = 1
        log.error("ingest: hooklist unavailable, keeping existing hooks: %s", e)
        return out
    ...existing body, with the final `out = {...}` replaced by `out.update(hooks=len(rows), new=len(new), updated=len(rows) - len(new))`...
```

- [ ] **Step 4: Run the full suite**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: 126 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/bait/clients/hooklist.py pipeline/bait/steps/ingest.py pipeline/tests/test_ingest.py
git commit -m "ingest: skip malformed hooklist entries one at a time; a fetch failure is a soft error, not a dead night"
```

---

### Task 7: Retire Nansen

**Files:**
- Delete: `pipeline/bait/clients/nansen.py`
- Modify: `pipeline/bait/steps/activity.py` (remove `label_wallets`, `_FATAL_STATUS`, Nansen imports, docstring item 3, the `nansen = ctx.client("nansen")` block)
- Modify: `pipeline/bait/steps/signals.py` (remove `_nansen`, `_labels_cached`, the `if nansen is not None:` block, `nansen` from the client tuple, the `is_smart_money` and `wallet_labels` imports if unused, the `"nansen"`/`"nansen_skipped_chain"` counters)
- Modify: `pipeline/bait/steps/__init__.py` (remove `"nansen"` from the factory map and the import)
- Modify: `pipeline/bait/costs.py` (remove the Nansen preview row and `N` import; fix the module docstring line 12)
- Modify: `pipeline/bait/config.py` (remove the `nansen: bool` field from the chain dataclass and every chain entry)
- Modify: `pipeline/bait/db.py:120` comment on `wallet_labels`
- Modify: `pipeline/tests/test_activity.py` (delete `FakeNansen`, `test_nansen_labels_cap_window_chains_and_flags`, `test_nansen_errors_are_counted_not_raised`; drop `nansen=None` from `ctx.clients` calls), `pipeline/tests/test_signals_digest.py` (drop `out["nansen"] == 0` assertions and `nansen=None`), `pipeline/tests/test_costs.py` (drop the `("activity", "nansen")` assertion), `pipeline/tests/conftest.py` (drop `"nansen"` from the clients dict)
- Modify: `pipeline/README.md`, `pipeline/.env.example`, `.env.example`, `railway/README.md`, `README.md` (remove Nansen lines; note that `wallet_labels` is a frozen cache read by the web)

**Interfaces:**
- Keeps: the `wallet_labels` table and its `smart_money` column. The web reads them; the pipeline no longer writes them. Nothing in `web/` changes in this package.

- [ ] **Step 1: Delete the client and its call sites**

`git rm pipeline/bait/clients/nansen.py`. Then remove every reference listed above. In `activity.py` the step ends after the pool loop:

```python
        except Exception as e:  # one pool must not kill the night
            out["errors"] += 1
            log.warning("activity pool=%s chain=%s failed: %s", pid, cid, e)
    return out
```

Remove the `"nansen_*"` keys from the `out` dict literal in `activity.run` (`nansen_queried`, `nansen_labeled`, `nansen_smart_money`, `nansen_fresh`, `nansen_skipped_chain`, `nansen_budget_left`, `nansen_budget_stop`). Grep to be sure: `grep -rn -i nansen pipeline/bait` must return only the `db.py` comment.

Update the `db.py` comment:

```python
# Wallet labels from the retired Nansen lane (credits exhausted 2026-10-08). Frozen: the web still reads existing rows;
# the pipeline no longer writes here. Delete the table when the web stops reading it.
```

- [ ] **Step 2: Fix the tests and run**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: all pass; the count drops by 2 (the two deleted Nansen tests): 124 passed. `grep -rn -i nansen pipeline/tests` returns nothing.

- [ ] **Step 3: Docs**

Remove the Nansen paragraphs and env vars from `pipeline/README.md`, `pipeline/.env.example`, `.env.example`, `railway/README.md`, `README.md`. In `pipeline/README.md` step 3's row, replace "with Nansen wallet labels" with "top wallets per pool". Add one line under the activity step: "Wallet labels (`wallet_labels`) are a frozen cache from the retired Nansen lane; the web reads them, nothing writes them."

- [ ] **Step 4: Commit**

```bash
git add -A pipeline .env.example railway/README.md README.md
git commit -m "Retire Nansen: remove the client, the activity and signals label paths, and their env vars

The key is revoked and removed from Railway after it exhausted the credit balance.
wallet_labels stays as a frozen cache the web still reads."
```

---

### Task 8: Docs that this package made stale

**Files:**
- Modify: `pipeline/README.md:22` (test count), `README.md` (note `TEST_DATABASE_URL` and `scripts/pg-test-cluster.sh` under the pipeline test line), `pipeline/.env.example` and `.env.example` (add `PARALLEL_RUN_MAX_AGE_H`, default 48, one comment line)

- [ ] **Step 1: Edit**

`pipeline/README.md:22`: replace `# 93 tests` with `# SQLite in memory; Postgres-only tests need TEST_DATABASE_URL (scripts/pg-test-cluster.sh)` and drop the stale count entirely.

`README.md` after the pytest line:

```
   Postgres-only tests (savepoints, the run lock) skip unless `TEST_DATABASE_URL` is set:
   `eval "$(pipeline/scripts/pg-test-cluster.sh start)"` starts a throwaway cluster and prints the export.
```

`.env.example` (both copies), in the enrich section:

```
# Hours a submitted Parallel run is polled before it is written off (default 48). Older run_ids become tombstones and the project is enriched without Parallel facts.
PARALLEL_RUN_MAX_AGE_H=
```

- [ ] **Step 2: Final full run**

```bash
cd pipeline && .venv/bin/python -m pytest -q && eval "$(scripts/pg-test-cluster.sh start)" && .venv/bin/python -m pytest -q tests/test_postgres.py && scripts/pg-test-cluster.sh stop
```
Expected: 124 passed; 2 passed. Record both numbers in the final report.

- [ ] **Step 3: Commit**

```bash
git add README.md pipeline/README.md pipeline/.env.example .env.example
git commit -m "docs: Postgres test cluster, PARALLEL_RUN_MAX_AGE_H, drop stale test count"
```

---

## Final report (the implementer returns this)

For each task: commit SHA, test counts before and after, anything that deviated from the plan and why. Then `git log --oneline main..HEAD` and `git diff --stat main..HEAD`. Do not push.
