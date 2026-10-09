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
                return [{"url": None, "title": "Beta hook ships", "source": "x", "published_at": NOW, "highlights": []}]
            return [{"url": f"https://e.com/{query[:5]}", "title": f"{query[1:6]} hook ships", "source": "e.com",
                     "published_at": NOW - timedelta(days=1), "highlights": []}]

    ctx = _ctx()
    ctx.clients["exa"] = Exa()
    with pg_engine.begin() as conn:
        out = signals.run(conn, ctx)
    assert out["errors"] == 1
    with pg_engine.connect() as conn:
        got = {r.project_id for r in conn.execute(sa.select(db.signals.c.project_id).where(db.signals.c.kind == "alive"))}
    assert got == {pids[0], pids[2]}              # Alpha and Gamma persisted; Beta's savepoint rolled back on its own


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


def test_cli_exits_3_without_running_a_step_while_another_run_holds_the_lock(pg_engine):
    import os

    from bait.__main__ import main
    held = db.try_run_lock(pg_engine)
    try:
        rc = main(["run", "--database-url", os.environ["TEST_DATABASE_URL"], "--steps", "ingest",
                   "--hooklist-file", FIXTURE, "--day", "2026-10-06"])
    finally:
        held.close()
    assert rc == 3
    with pg_engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(db.hooks)).scalar_one() == 0   # ingest never ran


def test_indexer_chain_lock_is_exclusive_per_chain_and_released_on_close(pg_engine):
    a = db.try_advisory_lock(pg_engine, "bait-indexer:8453")
    try:
        assert db.try_advisory_lock(pg_engine, "bait-indexer:8453") is None
        other = db.try_advisory_lock(pg_engine, "bait-indexer:1")
        assert other is not None
        other.close()
    finally:
        a.close()
    b = db.try_advisory_lock(pg_engine, "bait-indexer:8453")
    assert b is not None
    b.close()


def test_runner_skips_a_chain_another_indexer_holds(pg_engine, monkeypatch):
    import httpx
    from bait.indexer import core
    from bait.indexer.__main__ import Runner
    from bait.indexer.chains import select_chains
    seen = []
    monkeypatch.setattr("bait.indexer.__main__.sync_hypersync",
                        lambda e, ch, hs, p, s, **kw: seen.append(ch.slug) or core.ChainStats(mode="hypersync"))
    held = db.try_advisory_lock(pg_engine, "bait-indexer:8453")
    try:
        out = Runner(pg_engine, "tok", select_chains("ethereum,base"),
                     httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(500))), core.Settings()).run_once()
    finally:
        held.close()
    assert seen == ["ethereum"] and out["base"].error == "locked"


def test_swap_cursor_cas_loser_waits_on_the_row_lock_then_matches_nothing(pg_engine):
    import threading
    from bait.indexer import store
    with pg_engine.begin() as c:
        store.set_state(c, 8453, initialize_block=100, swap_block=10, head_block=100)
    a = pg_engine.connect()
    a.begin()
    store.advance_swap_cursor(a, 8453, 10, 20, 100)           # first container: row locked, not committed yet
    result = {}

    def second():
        with pg_engine.begin() as b:
            try:
                store.advance_swap_cursor(b, 8453, 10, 20, 100)
                result["b"] = "advanced"
            except store.CursorMoved:
                result["b"] = "moved"
    t = threading.Thread(target=second)
    t.start()
    t.join(timeout=1.0)
    assert t.is_alive()                                        # blocked on A's row lock
    a.commit()
    a.close()
    t.join(timeout=10)
    assert result == {"b": "moved"}                            # READ COMMITTED re-checks swap_block = 10 after A commits
    with pg_engine.connect() as c:
        assert store.get_state(c, 8453).swap_block == 20


def test_onchain_pool_copy_skips_unchanged_rows_and_reports_db_size(pg_engine):
    from datetime import datetime, timezone
    from bait.steps import onchain
    hook = "0x" + "a" * 36 + "0080"
    with pg_engine.begin() as c:
        c.execute(sa.insert(db.hooks).values(address=hook, chain_id=8453, name="H", first_seen_at=NOW, flags={}, properties={}))
        c.execute(sa.insert(db.v4_pools).values(chain_id=8453, pool_id="0x" + "a" * 64, hook_address=hook, currency0="0x0",
                                                currency1="0x1", fee=3000, tick_spacing=60, block_number=7,
                                                block_time=datetime(2026, 8, 1, tzinfo=timezone.utc)))
    with pg_engine.begin() as c:
        out = onchain.run(c, _ctx())
    assert out["pools_written"] == 1 and out["db_gb"] is not None
    with pg_engine.begin() as c:
        c.execute(sa.delete(db.onchain_watermarks))                 # force a re-read of the same row
        out = onchain.run(c, _ctx())
    assert out["pools"] == 1 and out["pools_written"] == 0         # read again, not rewritten
