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
