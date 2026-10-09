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


def _held_row_lock(pg_engine, table):
    """Another session (the web, the nightly step) holding a row lock: RowExclusiveLock on `table`, uncommitted."""
    holder = pg_engine.connect()
    holder.begin()
    holder.execute(sa.text(f'SELECT 1 FROM "{table}" FOR UPDATE'))
    holder.execute(sa.text(f'LOCK TABLE "{table}" IN ROW EXCLUSIVE MODE'))
    return holder


def _within(fn, seconds):
    """Run fn in a thread; return (finished, result-or-exception)."""
    import threading
    box = {}

    def go():
        try:
            box["r"] = fn()
        except Exception as e:  # surfaced to the test
            box["r"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout=seconds)
    return not t.is_alive(), box.get("r"), t


def test_additive_migration_issues_no_ddl_when_nothing_is_missing(pg_engine):
    db.add_missing_indexes(pg_engine)                  # the fixture adds columns, not indexes: make the schema current
    holder = _held_row_lock(pg_engine, "pool_stats")
    other = _held_row_lock(pg_engine, "v4_pools")
    try:
        done, cols, t1 = _within(lambda: db.add_missing_columns(pg_engine), 3)
        assert done and cols == []                    # no ALTER: an existing column must not wait for ACCESS EXCLUSIVE
        done, idx, t2 = _within(lambda: db.add_missing_indexes(pg_engine), 3)
        assert done and not isinstance(idx, Exception) # no CREATE INDEX: an existing index must not wait for SHARE
    finally:
        holder.rollback(); holder.close(); other.rollback(); other.close()


def test_additive_migration_gives_up_on_a_lock_instead_of_blocking(pg_engine):
    with pg_engine.begin() as c:
        c.execute(sa.text('ALTER TABLE pool_stats DROP COLUMN vol_source'))
    holder = _held_row_lock(pg_engine, "pool_stats")
    try:
        done, r, _ = _within(lambda: db.add_missing_columns(pg_engine, lock_timeout="300ms"), 5)
        assert done and isinstance(r, db.SchemaLocked)
    finally:
        holder.rollback(); holder.close()
    assert "pool_stats.vol_source" in db.add_missing_columns(pg_engine)     # retried once the lock is gone


def test_two_threads_committing_the_same_page_count_it_once(pg_engine):
    import threading
    from datetime import date
    from bait.indexer import core, store
    from bait.indexer.chains import CHAIN_BY_ID
    base = CHAIN_BY_ID[8453]
    with pg_engine.begin() as c:
        store.set_state(c, 8453, initialize_block=100, swap_block=10, head_block=100)
    holder = pg_engine.connect()                                # park both writers on the indexer_state row lock
    holder.begin()
    holder.execute(sa.update(db.indexer_state).where(db.indexer_state.c.chain_id == 8453).values(head_block=100))
    results, start = [], threading.Barrier(2)

    def commit_page():
        agg = core.SwapAgg()
        agg.add("0xpool", date(2026, 10, 6), "0xw1", 100.0)
        agg.add("0xpool", date(2026, 10, 6), "0xw2", 50.0)
        start.wait()
        try:
            core._commit_swaps(pg_engine, base, agg, 10, 20, 100, core.ChainStats())
            results.append("won")
        except store.CursorMoved:
            results.append("moved")
    threads = [threading.Thread(target=commit_page) for _ in range(2)]
    for t in threads:
        t.start()
    import time
    time.sleep(0.5)
    holder.rollback()
    holder.close()
    for t in threads:
        t.join(timeout=10)
    assert sorted(results) == ["moved", "won"]
    with pg_engine.connect() as c:
        r = c.execute(sa.select(db.pool_daily_onchain)).one()
        assert (r.swaps, r.wallets, r.volume_usd) == (2, 2, 150.0)   # the loser added nothing
        assert c.execute(sa.select(sa.func.count()).select_from(db.pool_wallet_day)).scalar_one() == 2
        assert store.get_state(c, 8453).swap_block == 20
