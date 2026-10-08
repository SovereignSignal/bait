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

        def enrich(self, name, hints):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.05)
            with self.lock:
                self.active -= 1
            self.meter.record("parallel", 0.025)
            return {"team": name}

    # budget fits only 4 of 6 tasks: the batch is cut before anything is submitted
    meter = Meter(None, budgets=Budgets(0.10, 10.0, {}))
    par = SlowParallel(meter)
    ctx.clients.update(claude=FakeClaude(), parallel=par, blockscout=None)
    out = enrich.run(conn, ctx)
    assert par.peak > 1                                   # ran side by side
    assert out["parallel"] == 3 and out["budget_stop"] == 1   # 6 -> 3 (halved until 3 x 0.025 <= 0.10)
    assert meter.run_usd <= 0.10 + 1e-9
