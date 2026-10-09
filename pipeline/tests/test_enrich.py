import pytest
from datetime import timedelta

import sqlalchemy as sa

from bait import db
from bait.steps import enrich
from tests.conftest import DAY, NOW, insert_hook, insert_pool, insert_project


class FakeClaude:
    def __init__(self):
        self.calls = []

    def what_line(self, ctxd):
        self.calls.append(ctxd["name"])
        return {"what_line": f"{ctxd['name']} does a thing.", "category": "dynamic-fee"}


def test_enrich_only_projects_with_pools_busiest_first_and_capped(conn, ctx, monkeypatch):
    monkeypatch.setenv("ENRICH_MAX", "2")
    for name, vol in (("Quiet", 10.0), ("Busy", 5_000_000.0), ("Mid", 50_000.0)):
        pid = insert_project(conn, name, first_seen=NOW - timedelta(days=30))
        addr = "0x" + name.lower().ljust(40, "0")[:40]
        insert_hook(conn, addr, 8453, project_id=pid, name=name)
        insert_pool(conn, "0xpool" + name, 8453, addr, NOW - timedelta(days=30))
        conn.execute(sa.insert(db.pool_stats).values(pool_id="0xpool" + name, chain_id=8453, day=DAY, vol_24h=vol, traders_24h=50))
    ghost = insert_project(conn, "Ghost", first_seen=NOW, slug="ghost")       # registry hook, no pools: never paid for
    insert_hook(conn, "0x" + "9" * 40, 8453, project_id=ghost, name="Ghost")
    claude = FakeClaude()
    ctx.clients.update(claude=claude, parallel=None, blockscout=None)
    out = enrich.run(conn, ctx)
    assert claude.calls == ["Busy", "Mid"] and out["candidates"] == 2


def test_parallel_runs_side_by_side_and_respects_budget(conn, ctx, monkeypatch, tmp_path):
    import threading
    import time

    from bait.costs import Budgets, Meter
    monkeypatch.setenv("ENRICH_MAX", "6")
    monkeypatch.setenv("PARALLEL_CONCURRENCY", "6")
    for i in range(6):
        pid = insert_project(conn, f"P{i}", first_seen=NOW - timedelta(days=30), slug=f"p{i}")
        addr = f"0x{i + 1:040x}"
        insert_hook(conn, addr, 8453, project_id=pid, name=f"P{i}")
        insert_pool(conn, f"0xpool{i}", 8453, addr, NOW - timedelta(days=30))
        conn.execute(sa.insert(db.pool_stats).values(pool_id=f"0xpool{i}", chain_id=8453, day=DAY, vol_24h=1000.0 * (i + 1), traders_24h=50))

    class SlowParallel:
        processor = "core"

        def __init__(self, meter):
            self.meter, self.active, self.peak, self.lock = meter, 0, 0, threading.Lock()

        def submit(self, name, hints):
            self.meter.record("parallel", 0.025)
            return f"run-{name}"

        def result(self, run_id, max_wait_s=None):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.05)
            with self.lock:
                self.active -= 1
            return {"team": run_id}

    # budget fits only 4 of 6 tasks: the batch is cut before anything is submitted
    meter = Meter(None, budgets=Budgets(0.10, 10.0, {}))
    par = SlowParallel(meter)
    ctx.clients.update(claude=FakeClaude(), parallel=par, blockscout=None)
    out = enrich.run(conn, ctx)
    assert par.peak > 1                                   # ran side by side
    assert out["parallel"] == 3 and out["budget_stop"] == 1   # 6 -> 3 (halved until 3 x 0.025 <= 0.10)
    assert meter.run_usd <= 0.10 + 1e-9


def test_parallel_timeouts_are_not_failures_and_paid_runs_are_collected_not_repaid(conn, ctx, monkeypatch):
    import httpx

    from bait.clients.parallel import Parallel
    from bait.costs import Budgets, Meter
    monkeypatch.setenv("ENRICH_MAX", "1")
    pid = insert_project(conn, "Slow", first_seen=NOW - timedelta(days=30), slug="slow")
    insert_hook(conn, "0x" + "5" * 40, 8453, project_id=pid, name="Slow")
    insert_pool(conn, "0xpslow", 8453, "0x" + "5" * 40, NOW - timedelta(days=30))
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xpslow", chain_id=8453, day=DAY, vol_24h=5000.0, traders_24h=50))

    class Resp:
        def __init__(self, status, data=None):
            self.status_code, self._d = status, data or {}

        def json(self):
            return self._d

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

    class Http:
        def __init__(self, ready_after):
            self.posts, self.gets, self.ready_after = 0, 0, ready_after

        def post(self, url, **kw):
            self.posts += 1
            return Resp(200, {"run_id": "trun_1"})

        def get(self, url, **kw):
            self.gets += 1
            if self.gets == 1:
                raise httpx.ReadTimeout("still running")      # the failure mode seen in production
            if self.gets < self.ready_after:
                return Resp(408)                               # server-side "still running"
            return Resp(200, {"output": {"content": {"team": "Slow Labs"}}})

    meter = Meter(None, budgets=Budgets(5.0, 50.0, {}))
    # run 1: never ready within the wait -> paid once, run_id kept, project NOT marked enriched
    h1 = Http(ready_after=10**9)
    par = Parallel("k", http_client=h1, poll_s=0, max_wait_s=0.05); par.meter = meter
    claude = FakeClaude()
    ctx.clients.update(claude=claude, parallel=par, blockscout=None)
    out = enrich.run(conn, ctx)
    row = conn.execute(sa.select(db.projects).where(db.projects.c.id == pid)).one()
    assert h1.posts == 1 and out["parallel_pending"] == 1 and out["errors"] == 0
    assert row.enriched_at is None and row.enrichment["parallel_run_id"] == "trun_1" and claude.calls == []
    # run 2: same run_id collected for free (no new submission), then what_line written once
    h2 = Http(ready_after=2)
    par2 = Parallel("k", http_client=h2, poll_s=0, max_wait_s=5); par2.meter = meter
    ctx.clients.update(parallel=par2)
    out2 = enrich.run(conn, ctx)
    row = conn.execute(sa.select(db.projects).where(db.projects.c.id == pid)).one()
    assert h2.posts == 0 and out2["parallel"] == 1 and row.enriched_at is not None
    assert row.enrichment["parallel"]["team"] == "Slow Labs" and len(claude.calls) == 1
    assert meter.run_usd == pytest.approx(0.025)               # paid exactly once across both runs


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
