# Package 4: Monitoring and CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A missed or broken nightly run is noticed the same morning, a stalled chain cannot hide behind a healthy one, logs carry real severities, dependencies are pinned, and untested code cannot reach `main`.

**Architecture:** Both Python entrypoints log JSON lines with a `level` field to stdout (Railway reads severity from it). The pipeline writes one `run_receipts` row per run with per-step counts, exit code and the deployed commit. `/api/health` reads the receipt and per-chain indexer state instead of two `max()`s, and the GitHub Actions check runs after the real deadline. A CI workflow runs both suites (Postgres included) on every push and PR.

**Tech Stack:** Python 3.12 `logging`, SQLAlchemy Core, GitHub Actions (`postgres:17` service), Node 20.

**Spec:** Astra M1 (health timing, one chain masking another, score ≠ run complete), Astra M9 (no CI gate, unpinned deps), Sov infra findings (stderr severity, `/api/health` blind spots). The Railway-side changes that go with this package (`checkSuites`, healthcheck path, watch paths) are done by the orchestrator after merge, not by this plan.

## Global Constraints

- Pipeline tests `cd pipeline && .venv/bin/python -m pytest -q`; Postgres tests via `eval "$(scripts/pg-test-cluster.sh start)"` then `tests/test_postgres.py`; web `cd web && npm test && npx tsc --noEmit && npm run build`.
- Never call a paid API. No secrets, hostnames or absolute paths. Do not push.
- Commit per task; trailer lines on every commit, verbatim:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01A2FHLwk9NeThmyZ1JbHe3o`
- In `pipeline/bait/indexer/__main__.py` change only the logging setup lines; nothing else in `indexer/`.

## Review Focus

1. A run that crashes mid-step must still leave a receipt with `exit_code != 0`. (Task 2)
2. At 08:30 UTC with no receipt for today, `/api/health` must be 503; at 07:00 UTC it must not alert. (Task 3)
3. One chain with a stale `updated_at` must make `/api/health` 503 even if other chains are fresh. (Task 3)
4. A WARNING log line must come out as JSON with `"level":"warning"`. (Task 1)
5. A failing Python test must fail the CI workflow. (Task 4)

---

### Task 1: JSON logs with a level field, on stdout

**Files:**
- Create: `pipeline/bait/logsetup.py`, `pipeline/tests/test_logsetup.py`
- Modify: `pipeline/bait/__main__.py:50`, `pipeline/bait/indexer/__main__.py:89-90`

**Interfaces:**
- Produces: `logsetup.configure(name: str) -> None`. `BAIT_LOG_FORMAT=text` keeps the current human format (local dev); default is one JSON object per line: `{"ts": iso, "level": "info|warning|error", "logger": name, "msg": text}` plus `"exc"` when there is a traceback. Always stdout.

- [ ] **Step 1: Failing test**

```python
import io, json, logging

from bait import logsetup


def test_json_formatter_emits_level_and_message():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logsetup.JsonFormatter())
    log = logging.getLogger("bait.test")
    log.handlers[:] = [h]
    log.propagate = False
    log.setLevel(logging.INFO)
    log.warning("chain=%s lag=%d", "base", 42)
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("failed")
    lines = [json.loads(x) for x in buf.getvalue().splitlines()]
    assert lines[0]["level"] == "warning" and lines[0]["msg"] == "chain=base lag=42" and lines[0]["logger"] == "bait.test"
    assert lines[1]["level"] == "error" and "ValueError: boom" in lines[1]["exc"]
```

- [ ] **Step 2: Implement**

```python
"""Logging for the Railway services: JSON lines on stdout with a level field (Railway maps severity from it).
BAIT_LOG_FORMAT=text gives the old human format for local runs."""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
             "level": record.levelname.lower(), "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False)


def configure(name: str = "bait") -> None:
    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get("BAIT_LOG_FORMAT", "json") == "text":
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    else:
        handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
```

Replace the `logging.basicConfig(...)` line in both entrypoints with `logsetup.configure()` (import `from . import logsetup` in `bait/__main__.py`, `from .. import logsetup` in `indexer/__main__.py`; drop the now-duplicate httpx line there). Run the suite → PASS.

- [ ] **Step 3: Commit**

```bash
git add pipeline
git commit -m "logging: JSON lines with a level field on stdout, so Railway stops tagging every heartbeat as an error"
```

---

### Task 2: Run receipts

**Files:**
- Create: `db/migrations/0009_run_receipts.sql`
- Modify: `pipeline/bait/db.py` (table), `pipeline/bait/__main__.py` (write the receipt in the `finally`)
- Test: `pipeline/tests/test_cli.py`

**Interfaces:**
- Produces table `run_receipts(id serial pk, day date, started_at, finished_at, exit_code int, soft_errors int, steps jsonb, code_sha text, notes text)`. `steps` is `{step: counts_dict}` for every step that ran; a step that raised gets `{"failed": "<message first line>"}`. `code_sha` is `RAILWAY_GIT_COMMIT_SHA` (null locally).
- Produces `db.write_receipt(engine, **fields)` on its own connection (never inside a step transaction).

- [ ] **Step 1: Failing test**

```python
def test_run_writes_a_receipt_even_when_a_step_fails(tmp_path, monkeypatch):
    from bait import __main__ as cli, db
    from bait.steps import snapshot
    url = f"sqlite:///{tmp_path}/r.sqlite"
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc1234")
    monkeypatch.setattr(snapshot, "run", lambda conn, ctx: (_ for _ in ()).throw(RuntimeError("gecko down")))
    rc = cli.main(["run", "--day", "2026-10-06", "--hooklist-file", FIXTURE, "--database-url", url, "--steps", "ingest,snapshot"])
    assert rc == 1
    e = db.make_engine(url)
    with e.connect() as c:
        r = c.execute(sa.select(db.run_receipts)).one()
    assert r.exit_code == 1 and r.code_sha == "abc1234" and r.steps["ingest"]["hooks"] == 300
    assert "gecko down" in r.steps["snapshot"]["failed"]
```

Also add a happy-path assertion to `test_cli_end_to_end_sqlite_idempotent`: two runs → two receipts with `exit_code == 0`.

- [ ] **Step 2: Implement**

`db.py`:

```python
run_receipts = sa.Table(
    "run_receipts", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("day", sa.Date, nullable=False),
    sa.Column("started_at", TS, nullable=False),
    sa.Column("finished_at", TS, nullable=False),
    sa.Column("exit_code", sa.Integer, nullable=False),
    sa.Column("soft_errors", sa.Integer, nullable=False, server_default="0"),
    sa.Column("steps", JSONType, nullable=False),
    sa.Column("code_sha", sa.Text),
    sa.Column("notes", sa.Text),
)


def write_receipt(engine: sa.Engine, **fields) -> None:
    """One row per run, on its own connection, after everything else; a failed step must not lose it."""
    try:
        with engine.begin() as c:
            c.execute(sa.insert(run_receipts).values(**fields))
    except Exception:  # the receipt is for monitoring; never turn a finished run into a crash
        logging.getLogger("bait").exception("receipt write failed")
```

(`import logging` at the top of `db.py` if absent.) Migration `0009_run_receipts.sql` mirrors it with `CREATE TABLE IF NOT EXISTS`.

`__main__.py`: record `started = utcnow()` before the lock, a `steps_out: dict[str, dict] = {}` filled with `counts` after each step or `{"failed": str(e).splitlines()[0]}` in the except (capture `e`), track `rc` for every return path, and in the `finally` after `lock.close()`:

```python
        db.write_receipt(engine, day=day, started_at=started, finished_at=utcnow(), exit_code=rc, soft_errors=soft_errors,
                         steps=steps_out, code_sha=os.environ.get("RAILWAY_GIT_COMMIT_SHA"))
```

Structure the function so `rc` is assigned before each `return` (`rc = 1; return rc`). Run the suite → PASS.

- [ ] **Step 3: Commit**

```bash
git add db/migrations/0009_run_receipts.sql pipeline
git commit -m "run receipts: one row per nightly run with per-step counts, exit code and the deployed commit"
```

---

### Task 3: `/api/health` reads receipts and per-chain indexer state; the check runs after the deadline

**Files:**
- Modify: `web/src/app/api/health/route.ts`, `web/src/db/schema.ts` (add `runReceipts`), `.github/workflows/health.yml`
- Create: `web/src/lib/health.ts`, `web/src/lib/health.test.ts` (pure decision function)

**Interfaces:**
- Produces `evaluateHealth(input, now): { ok: boolean; reasons: string[]; warnings: string[] }` where `input = { receipt: { day, exitCode, softErrors, codeSha } | null; chains: Array<{ chainId, backfilled, headBlock, swapBlock, updatedAt }> }`.
- Rules: deadline 08:00 UTC. Before the deadline, yesterday's receipt with `exitCode === 0` is fine. After it, today's receipt is required and `exitCode` must be 0. `softErrors > 0` is a warning, `softErrors >= 10` a reason. Any chain with `updatedAt` older than 2 h → reason naming the chain. Any backfilled chain with `headBlock - swapBlock > 50_000` → warning naming the chain and lag. No chains at all → reason.

- [ ] **Step 1: Failing test**

```ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { evaluateHealth } from "./health";

const chain = (over = {}) => ({ chainId: 8453, backfilled: true, headBlock: 1000, swapBlock: 990, updatedAt: new Date("2026-10-09T08:00:00Z"), ...over });

test("after the deadline a missing or failed receipt for today is a failure", () => {
  const now = new Date("2026-10-09T08:30:00Z");
  assert.equal(evaluateHealth({ receipt: null, chains: [chain()] }, now).ok, false);
  assert.equal(evaluateHealth({ receipt: { day: "2026-10-08", exitCode: 0, softErrors: 0, codeSha: "x" }, chains: [chain()] }, now).ok, false);
  assert.equal(evaluateHealth({ receipt: { day: "2026-10-09", exitCode: 1, softErrors: 0, codeSha: "x" }, chains: [chain()] }, now).ok, false);
  assert.equal(evaluateHealth({ receipt: { day: "2026-10-09", exitCode: 0, softErrors: 2, codeSha: "x" }, chains: [chain()] }, now).ok, true);
});

test("before the deadline yesterday's good receipt is fine", () => {
  const now = new Date("2026-10-09T07:00:00Z");
  assert.equal(evaluateHealth({ receipt: { day: "2026-10-08", exitCode: 0, softErrors: 0, codeSha: "x" }, chains: [chain()] }, now).ok, true);
});

test("one stale chain fails health even when others are fresh; big swap lag is a warning", () => {
  const now = new Date("2026-10-09T08:30:00Z");
  const ok = { day: "2026-10-09", exitCode: 0, softErrors: 0, codeSha: "x" };
  const r = evaluateHealth({ receipt: ok, chains: [chain(), chain({ chainId: 1, updatedAt: new Date("2026-10-09T05:00:00Z") })] }, now);
  assert.equal(r.ok, false);
  assert.match(r.reasons.join(" "), /chain 1/);
  const w = evaluateHealth({ receipt: ok, chains: [chain({ swapBlock: 100 })] }, now);
  assert.equal(w.ok, true);
  assert.match(w.warnings.join(" "), /8453/);
});
```

Add to the `test` script. Run → FAIL.

- [ ] **Step 2: Implement**

`web/src/lib/health.ts` per the rules above (pure, no imports from the db). `schema.ts`: add `runReceipts` with the columns from Task 2. `route.ts`: query the latest receipt (`order by finished_at desc limit 1`) and all `indexer_state` rows, call `evaluateHealth`, respond `{ ok, checked_at, receipt, chains: [...lag summary], reasons, warnings }` with 200/503 and `no-store`. Keep `safe` wrappers; a failed query becomes a reason.

`.github/workflows/health.yml`: cron `30 8 * * *` (after the deadline), and also fail when the body has `"ok":false`:

```yaml
          code=$(curl -s -o body.json -w '%{http_code}' --max-time 60 "$URL/api/health")
          cat body.json; echo
          test "$code" = "200" && grep -q '"ok":true' body.json
```

with `URL: https://bait-production-2706.up.railway.app` as a workflow `env`.

Run `npm test && npx tsc --noEmit && npm run build` → PASS.

- [ ] **Step 3: Commit**

```bash
git add web .github/workflows/health.yml
git commit -m "health: decide from the run receipt and every chain's indexer state; the daily check runs after the 08:00 UTC deadline"
```

---

### Task 4: CI gate

**Files:**
- Create: `.github/workflows/ci.yml`

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
jobs:
  pipeline:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    services:
      postgres:
        image: postgres:17
        env: { POSTGRES_PASSWORD: ci, POSTGRES_DB: bait }
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U postgres" --health-interval 5s --health-timeout 5s --health-retries 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: pip, cache-dependency-path: pipeline/requirements.txt }
      - run: pip install -r pipeline/requirements.txt
      - run: cd pipeline && python -m pytest -q
        env: { KEYLESS: "0" }
      - run: cd pipeline && python -m pytest -q tests/test_postgres.py
        env: { TEST_DATABASE_URL: "postgresql+psycopg://postgres:ci@localhost:5432/bait" }
  web:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 20, cache: npm, cache-dependency-path: web/package-lock.json }
      - run: cd web && npm ci
      - run: cd web && npm test
      - run: cd web && npx tsc --noEmit
      - run: cd web && npm run build
        env: { NEXT_TELEMETRY_DISABLED: "1" }
```

Check `tests/conftest.py` or the README for whether `KEYLESS=0` is required for the suite (the pipeline README says it is); keep the env if so. Commit:

```bash
git add .github/workflows/ci.yml
git commit -m "ci: pipeline (SQLite + Postgres) and web (test, typecheck, build) on every push and PR"
```

---

### Task 5: Pin Python dependencies

**Files:**
- Modify: `pipeline/requirements.txt` (pinned `==` versions from `.venv/bin/pip freeze`, one comment line: how to refresh), `pipeline/README.md` (one line)

Keep only the direct dependencies that are there today, pinned to the versions currently installed in `.venv` (`.venv/bin/pip freeze | grep -i -E '^(sqlalchemy|psycopg|httpx|anthropic|pydantic|tenacity|python-dotenv|pytest)=='`). Keep the `psycopg[binary]` extra. Re-run the suite after `pip install -r requirements.txt` in a fresh venv to prove it resolves. Commit:

```bash
git add pipeline/requirements.txt pipeline/README.md
git commit -m "pipeline: pin direct dependencies to the versions the suite runs on"
```

---

## Final report (the implementer returns this)

Per task: SHA, test counts before and after, deviations. Then `git log --oneline fix/p2-trust-signals..HEAD`, `git diff --stat fix/p2-trust-signals..HEAD`, and a sample JSON log line from a local `python -m bait run --steps ingest --hooklist-file fixtures/hooklist.sample.json --database-url sqlite:///dev.sqlite` (delete `dev.sqlite` afterwards). Do not push.
