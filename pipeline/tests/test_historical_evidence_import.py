"""Reviewed historical input: atomic import/replay/rollback, never recent metrics or coverage."""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from bait import db, historical_evidence as H
from bait.__main__ import main
from bait.steps import RunContext, score

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/bunni-historical-import-v1.json"
SHA = "42e7edad6476cceed690ae430cef14fbca96a7be9a6c7a95a4e7648ba991bf6a"
NOW = datetime(2026, 10, 15, 6, tzinfo=timezone.utc)
DAY = date(2026, 10, 14)


def bundle():
    return H.load_bundle(FIXTURE, SHA, now=NOW)


def seed(conn):
    """Captured one-pool identity; additional incomplete chain is an explicit controlled coverage scenario."""
    event = bundle()["rows"][0]["evidence"]["event"]
    value = event["valuation"]
    conn.execute(sa.insert(db.projects).values(id=1505, slug="bunnihook", name="BunniHook", first_seen_at=NOW))
    conn.execute(sa.insert(db.hooks).values(chain_id=1, address=event["hook_address"], project_id=1505,
                                           pools_total=1, first_seen_at=NOW))
    conn.execute(sa.insert(db.pools).values(chain_id=1, pool_id=event["pool_id"], hook_address=event["hook_address"],
        token0={"id": value["currency0"]}, token1={"id": value["currency1"]}, fee=0, tick_spacing=1,
        created_at=datetime(2025, 7, 22, 18, 57, 11, tzinfo=timezone.utc)))
    conn.execute(sa.insert(db.hooks).values(chain_id=8453, address="0x"+"a"*40, project_id=1505,
                                           pools_total=None, first_seen_at=NOW))
    conn.execute(sa.insert(db.indexer_state).values(chain_id=8453, backfilled=False, updated_at=NOW))


def scored(conn):
    score.run(conn, RunContext(day=DAY, now=NOW))
    return conn.execute(sa.select(db.project_daily).where(db.project_daily.c.project_id == 1505,
                                                         db.project_daily.c.day == DAY)).one()


def protected(conn):
    return {table.name: [dict(r._mapping) for r in conn.execute(sa.select(table))] for table in db.metadata.sorted_tables
            if table.name != "historical_swap_evidence"}


def write(tmp_path, data, name="bundle.json"):
    path = tmp_path/name
    path.write_text(json.dumps(data, indent=2, allow_nan=False))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, sha


def load_changed(tmp_path, change):
    data = json.loads(FIXTURE.read_text())
    change(data)
    path, sha = write(tmp_path, data)
    return H.load_bundle(path, sha, now=NOW)


def test_verified_fixture_decodes_19_positive_transactions_one_lower_bound_and_zero_alias():
    rows = bundle()["rows"]
    assert len(rows) == 20 and sum(r["positive"] for r in rows) == 19
    assert len({r["transaction_hash"] for r in rows}) == 19
    priced = [r for r in rows if r["usd_lower_bound"] is not None]
    assert len(priced) == 1 and priced[0]["usd_lower_bound"] == Decimal("262.4545875")
    alias = next(r for r in rows if r["event_kind"] == "pool_manager_swap")
    assert alias["transaction_hash"] == priced[0]["transaction_hash"] and alias["log_index"] == 167
    assert not alias["positive"] and alias["usd_lower_bound"] is None


@pytest.fixture(params=["engine", "pg_engine"])
def import_engine(request):
    return request.getfixturevalue(request.param)


def test_import_replay_classification_and_rollback_on_both_databases(import_engine, tmp_path):
    e = import_engine
    with e.begin() as conn:
        seed(conn)
        before = scored(conn)
        assert before.is_spam and not before.has_historical_usage
        unchanged = protected(conn)
        preview = H.import_bundle(conn, bundle())
        assert preview["would_insert"] == 20 and preview["project_ids"] == [1505]
        assert preview["historical_total_usd"] is None and not preview["scored"]
        assert len(preview["pools"]) == 1
        assert preview["pools"][0]["positive_transactions"] == 19
        assert Decimal(preview["pools"][0]["known_usd_contribution"]) == Decimal("262.4545875")
        assert preview["pools"][0]["historical_total_usd"] is None
        assert H.import_bundle(conn, bundle(), apply=True)["inserted"] == 20
        assert protected(conn) == unchanged                   # import does not score or alter any other table
        assert H.import_bundle(conn, bundle(), apply=True)["unchanged"] == 20
        reformatted = json.loads(FIXTURE.read_text())
        path, other_sha = write(tmp_path, reformatted)
        replay = H.load_bundle(path, other_sha, now=NOW)
        assert H.import_bundle(conn, replay, apply=True)["inserted"] == 0
        assert H.rollback_bundle(conn, other_sha)["would_delete"] == 0  # replay never transfers ownership
        evidence = conn.execute(sa.select(db.historical_swap_evidence)).all()
        assert len(evidence) == 20 and sum(r.positive for r in evidence) == 19
        observed = sum(r.usd_lower_bound or Decimal(0) for r in evidence)
        if conn.dialect.name == "postgresql":
            assert observed == Decimal("262.4545875")
        else:
            assert float(observed) == pytest.approx(262.4545875)  # SQLite NUMERIC uses floating storage; PG is exact.
        after = scored(conn)
        assert after.has_historical_usage and not after.is_spam and after.activity_status == "unknown"
        assert not after.is_new and after.score_trending == 0
        fields = ("vol_24h", "vol_7d", "vol_7d_prev", "swaps_24h", "swaps_7d")
        assert tuple(getattr(after, k) for k in fields) == tuple(getattr(before, k) for k in fields) == (None,)*5
        assert conn.execute(sa.select(sa.func.count()).select_from(db.pool_daily_onchain)).scalar_one() == 0
        assert conn.execute(sa.select(sa.func.count()).select_from(db.pool_snapshots)).scalar_one() == 0
        assert H.rollback_bundle(conn, SHA)["would_delete"] == 20
        with pytest.raises(ValueError):
            H.rollback_bundle(conn, SHA, apply=True, expected_rows=19)
        assert H.rollback_bundle(conn, SHA, apply=True, expected_rows=20)["deleted"] == 20
        assert scored(conn).is_spam
        assert H.import_bundle(conn, bundle(), apply=True)["inserted"] == 20  # explicit reimport after rollback


def test_changed_replay_rejects_entire_batch_before_new_rows(import_engine, tmp_path):
    with import_engine.begin() as conn:
        seed(conn)
        H.import_bundle(conn, bundle(), apply=True)
    data = json.loads(FIXTURE.read_text())
    new = copy.deepcopy(data["events"][1])
    new["transaction_hash"] = new["raw_log"]["transaction_hash"] = "0x"+"f"*64
    new["log_index"] = new["raw_log"]["log_index"] = 10000
    new["source"]["sha256"] = H.digest(new["raw_log"])
    data["events"] = [new, data["events"][0]]
    data["events"][1]["source"]["url"] += "?changed_provenance=1"
    path, sha = write(tmp_path, data)
    changed = H.load_bundle(path, sha, now=NOW)
    with pytest.raises(ValueError, match="changed replay"):
        with import_engine.begin() as conn:
            H.import_bundle(conn, changed, apply=True)
    with import_engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(db.historical_swap_evidence)).scalar_one() == 20


def test_insert_transaction_rollback_leaves_no_evidence(import_engine):
    with import_engine.begin() as conn:
        seed(conn)
    with pytest.raises(RuntimeError):
        with import_engine.begin() as conn:
            H.import_bundle(conn, bundle(), apply=True)
            raise RuntimeError("simulate later transaction failure")
    with import_engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(db.historical_swap_evidence)).scalar_one() == 0


def test_cross_bundle_conflicting_block_is_rejected(conn, tmp_path):
    seed(conn)
    H.import_bundle(conn, bundle(), apply=True)
    def change(data):
        data["events"] = [data["events"][1]]
        event = data["events"][0]
        event["transaction_hash"] = event["raw_log"]["transaction_hash"] = "0x"+"f"*64
        event["block_hash"] = event["raw_log"]["block_hash"] = "0x"+"e"*64
        event["source"]["sha256"] = H.digest(event["raw_log"])
    with pytest.raises(ValueError, match="stored block identity"):
        H.import_bundle(conn, load_changed(tmp_path, change), apply=True)


@pytest.mark.parametrize("across_batches", [True, False])
@pytest.mark.parametrize("problem", ["block_hash", "block_log_position"])
def test_reverse_block_identity_and_physical_log_cannot_be_rebound(conn, tmp_path, across_batches, problem):
    seed(conn)
    if across_batches:
        H.import_bundle(conn, bundle(), apply=True)
    def change(data):
        event = copy.deepcopy(data["events"][1])
        event["transaction_hash"] = event["raw_log"]["transaction_hash"] = "0x"+"f"*64
        if problem == "block_hash":
            event["block_number"] = event["raw_log"]["block_number"] = 23200000
        event["source"]["sha256"] = H.digest(event["raw_log"])
        data["events"] = [event] if across_batches else data["events"]+[event]
    with pytest.raises(ValueError, match="block"):
        H.import_bundle(conn, load_changed(tmp_path, change), apply=True)


@pytest.mark.parametrize("direction", ["accept_excluded", "exclude_accepted"])
def test_cross_bundle_exclusion_conflicts_require_explicit_review_and_rollback(conn, tmp_path, direction):
    seed(conn)
    H.import_bundle(conn, bundle(), apply=True)
    def change(data):
        if direction == "accept_excluded":
            identity = data["exclusions"].pop()["event_id"].split(":")
            data["events"] = [data["events"][1]]
            event = data["events"][0]
            event["transaction_hash"] = event["raw_log"]["transaction_hash"] = identity[1]
            event["log_index"] = event["raw_log"]["log_index"] = int(identity[2])
            event["source"]["sha256"] = H.digest(event["raw_log"])
        else:
            excluded = data["events"].pop(1)
            data["events"] = [data["events"][0]]
            data["exclusions"].append({"event_id": f"1:{excluded['transaction_hash']}:{excluded['log_index']}",
                                        "reason": "new review contradiction"})
    with pytest.raises(ValueError, match="accepted/excluded"):
        H.import_bundle(conn, load_changed(tmp_path, change), apply=True)
    assert conn.execute(sa.select(sa.func.count()).select_from(db.historical_swap_evidence)).scalar_one() == 20


@pytest.mark.parametrize("kind,index,value", [
    ("pool_manager_swap", 0, 2**128), ("pool_manager_swap", 1, 2**255),
    ("pool_manager_swap", 2, 2**160), ("pool_manager_swap", 3, 2**128),
    ("pool_manager_swap", 4, 2**24), ("pool_manager_swap", 5, 2**24),
    ("custom_input_output_swap", 4, 2**160), ("custom_input_output_swap", 5, 2**24),
    ("custom_input_output_swap", 6, 2**24),
])
def test_noncanonical_abi_words_cannot_create_usage(tmp_path, kind, index, value):
    def change(data):
        event = next(e for e in data["events"] if e["event_kind"] == kind)
        words = [event["raw_log"]["data"][i:i+64] for i in range(2, len(event["raw_log"]["data"]), 64)]
        words[index] = f"{value:064x}"
        event["raw_log"]["data"] = "0x"+"".join(words)
        event["source"]["sha256"] = H.digest(event["raw_log"])
    with pytest.raises(ValueError, match="ABI integer"):
        load_changed(tmp_path, change)


@pytest.mark.parametrize("problem", ["missing", "extra", "noncanonical_sender"])
def test_malformed_indexed_topics_are_rejected(tmp_path, problem):
    def change(data):
        event = data["events"][0]
        topics = event["raw_log"]["topics"]
        if problem == "missing":
            event["raw_log"]["topics"] = topics[:2]
        elif problem == "extra":
            topics[-1] = "0x"+"0"*64
        else:
            topics[2] = "0x"+"f"*64
        event["source"]["sha256"] = H.digest(event["raw_log"])
    with pytest.raises(ValueError, match="sender topics"):
        load_changed(tmp_path, change)


@pytest.mark.parametrize("change", [
    lambda b: b.update(historical_total_usd="262.4545875"),
    lambda b: b.update(coverage="complete"),
    lambda b: b["events"].append(copy.deepcopy(b["events"][0])),
    lambda b: b["exclusions"].append({"event_id": "1:"+b["events"][0]["transaction_hash"]+":166", "reason": "excluded"}),
    lambda b: b["events"][0].update(event_kind="unknown"),
    lambda b: b["events"][0].update(pool_id="hook:"+b["events"][0]["hook_address"]),
    lambda b: b["events"][0].update(block_timestamp="2026-10-14T00:00:00Z"),
    lambda b: b["events"][0].update(block_timestamp="2025-09-01T20:49:11"),
    lambda b: b["events"][0]["raw_log"].update(log_index=999),
    lambda b: b["events"][0]["source"].update(sha256="0"*64),
    lambda b: b["events"][0]["source"].update(url="http://example.com"),
    lambda b: b["events"][0]["valuation"].update(usd_lower_bound="1000000"),
    lambda b: b["events"][0]["valuation"].update(price0="1"),
    lambda b: b["events"][0]["valuation"].update(quote_response_sha256="0"*64),
])
def test_invalid_reviewed_manifest_is_rejected(tmp_path, change):
    with pytest.raises(ValueError):
        load_changed(tmp_path, change)


def test_external_review_digest_must_match():
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        H.load_bundle(FIXTURE, "0"*64)


@pytest.mark.parametrize("problem", ["missing_pool", "wrong_hook", "wrong_currency", "unassigned_project"])
def test_import_checks_current_identity_without_creating_it(conn, problem):
    seed(conn)
    if problem == "missing_pool":
        conn.execute(sa.delete(db.pools))
    elif problem == "wrong_hook":
        conn.execute(sa.update(db.pools).values(hook_address="0x"+"b"*40))
    elif problem == "wrong_currency":
        conn.execute(sa.update(db.pools).values(token0={"id": "0x"+"0"*40}))
    else:
        conn.execute(sa.update(db.hooks).values(project_id=None))
    with pytest.raises(ValueError):
        H.import_bundle(conn, bundle(), apply=True)
    assert conn.execute(sa.select(sa.func.count()).select_from(db.historical_swap_evidence)).scalar_one() == 0


def test_targeted_score_changes_only_preexisting_requested_project_day(conn):
    seed(conn)
    scored(conn)
    conn.execute(sa.insert(db.projects).values(id=2222, slug="untouched", name="Unrelated", first_seen_at=NOW))
    conn.execute(sa.insert(db.project_daily).values(project_id=2222, day=DAY, tvl_usd=12345, vol_7d=999,
        is_spam=False, activity_status="active", score_trending=17))
    unrelated = dict(conn.execute(sa.select(db.project_daily).where(db.project_daily.c.project_id == 2222)).one()._mapping)
    H.import_bundle(conn, bundle(), apply=True)
    out = score.run(conn, RunContext(day=DAY, now=NOW), project_ids={1505})
    assert out["projects"] == 1
    assert dict(conn.execute(sa.select(db.project_daily).where(db.project_daily.c.project_id == 2222)).one()._mapping) == unrelated
    target = conn.execute(sa.select(db.project_daily).where(db.project_daily.c.project_id == 1505)).one()
    assert target.has_historical_usage and not target.is_spam and target.activity_status == "unknown"


def test_targeted_score_refuses_missing_or_empty_scope(conn):
    seed(conn)
    for scope in ({1505}, set(), {True}):
        with pytest.raises(ValueError):
            score.run(conn, RunContext(day=DAY, now=NOW), project_ids=scope)
    assert conn.execute(sa.select(sa.func.count()).select_from(db.project_daily)).scalar_one() == 0


def test_preview_without_schema_does_not_bootstrap_any_tables(tmp_path):
    url = "sqlite:///"+str(tmp_path/"empty.db")
    assert main(["historical-evidence", "import", str(FIXTURE), "--sha256", SHA, "--database-url", url]) == 4
    e = db.make_engine(url)
    assert sa.inspect(e).get_table_names() == []
    e.dispose()


def test_pg_cli_preview_is_read_only_and_apply_obeys_pipeline_lock(pg_engine, monkeypatch):
    from bait import __main__ as cli
    with pg_engine.begin() as conn:
        seed(conn)
    monkeypatch.setattr(cli.db, "make_engine", lambda _: pg_engine)
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    sa.event.listen(pg_engine, "before_cursor_execute", record)
    args = ["historical-evidence", "import", str(FIXTURE), "--sha256", SHA]
    try:
        assert main(args) == 0
    finally:
        sa.event.remove(pg_engine, "before_cursor_execute", record)
    assert "SET TRANSACTION READ ONLY" in statements
    assert not any(s.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"} for s in statements)
    held = db.try_run_lock(pg_engine)
    try:
        assert main(args+["--apply"]) == 3
    finally:
        held.close()
    assert main(args+["--apply"]) == 0
    assert main(args+["--apply"]) == 0
    assert main(["historical-evidence", "rollback", "--sha256", SHA, "--apply", "--expected-rows", "20"]) == 0


def test_pg_migration_reapply_transactional_rollback_and_down_migration(pg_engine):
    root = Path(__file__).resolve().parents[2]
    up = (root/"db/migrations/0017_historical_evidence.sql").read_text()
    down = (root/"db/rollback/0017_historical_evidence.sql").read_text()
    with pg_engine.begin() as conn:
        conn.exec_driver_sql(down)
        assert not sa.inspect(conn).has_table("historical_swap_evidence")
    with pg_engine.connect() as conn:
        tx = conn.begin()
        conn.exec_driver_sql(up)
        assert sa.inspect(conn).has_table("historical_swap_evidence")
        tx.rollback()
        assert not sa.inspect(conn).has_table("historical_swap_evidence")
    with pg_engine.begin() as conn:
        conn.exec_driver_sql(up)
        conn.exec_driver_sql(up)
        seed(conn)
        H.import_bundle(conn, bundle(), apply=True)
        conn.exec_driver_sql(up)
        assert conn.execute(sa.select(sa.func.count()).select_from(db.historical_swap_evidence)).scalar_one() == 20
        conn.exec_driver_sql(down)
        assert not sa.inspect(conn).has_table("historical_swap_evidence")
        assert conn.execute(sa.select(sa.func.count()).select_from(db.projects)).scalar_one() == 1


def test_pg_actual_migration_runner_reapplies_after_evidence_rollback(pg_engine):
    root = Path(__file__).resolve().parents[2]
    psql = shutil.which("psql") or "/opt/homebrew/opt/postgresql@17/bin/psql"
    if not Path(psql).is_file():
        pytest.skip("psql unavailable for actual migration runner")
    schema = "evidence_migration_"+uuid.uuid4().hex
    url = pg_engine.url.render_as_string(hide_password=False).replace("postgresql+psycopg:", "postgresql:")
    env = {**os.environ, "DATABASE_URL": url, "PGOPTIONS": "-csearch_path="+schema,
           "PATH": str(Path(psql).parent)+os.pathsep+os.environ["PATH"]}
    with pg_engine.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    try:
        first = subprocess.run(["bash", "scripts/apply-migrations.sh"], cwd=root, env=env, capture_output=True, text=True)
        assert first.returncode == 0, first.stderr
        with pg_engine.begin() as conn:
            conn.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
            seed(conn)
            H.import_bundle(conn, bundle(), apply=True)
        undo = subprocess.run([psql, url, "-X", "-v", "ON_ERROR_STOP=1", "-f",
                               "db/rollback/0017_historical_evidence.sql"], cwd=root, env=env, capture_output=True, text=True)
        assert undo.returncode == 0, undo.stderr
        with pg_engine.begin() as conn:
            conn.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
            assert not sa.inspect(conn).has_table("historical_swap_evidence")
            assert conn.execute(sa.text("SELECT count(*) FROM schema_migrations WHERE filename='0017_historical_evidence.sql'")).scalar_one() == 0
            assert conn.execute(sa.select(sa.func.count()).select_from(db.projects)).scalar_one() == 1
        again = subprocess.run(["bash", "scripts/apply-migrations.sh"], cwd=root, env=env, capture_output=True, text=True)
        assert again.returncode == 0, again.stderr
        assert "apply  0017_historical_evidence.sql" in again.stdout and "applied=1 " in again.stdout
        with pg_engine.begin() as conn:
            conn.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
            assert H.import_bundle(conn, bundle(), apply=True)["inserted"] == 20
            assert H.import_bundle(conn, bundle(), apply=True)["unchanged"] == 20
    finally:
        with pg_engine.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))


def test_apply_refuses_inside_the_nightly_blackout_before_touching_the_database(tmp_path):
    """The same window one-off repairs refuse: a write there races the 06:00 UTC nightly's lock, or replaces the
    nightly outright when run from a start-command override that was not cleared."""
    from datetime import datetime, timezone
    from bait import historical_evidence
    from bait.__main__ import parse_args
    url = f"sqlite:///{tmp_path / 'blackout.sqlite'}"
    args = parse_args(["historical-evidence", "import", str(FIXTURE), "--sha256", SHA, "--database-url", url, "--apply"])
    engine = db.make_engine(url)
    try:
        for hhmm in ((5, 30), (5, 59), (6, 29)):
            assert historical_evidence.main(args, engine, now=datetime(2026, 10, 6, *hhmm, tzinfo=timezone.utc)) == 5
        assert sa.inspect(engine).get_table_names() == []   # refused before any lock, receipt or schema touch
        # outside the window the command proceeds to its normal checks (no ledger here: exit 4)
        assert historical_evidence.main(args, engine, now=datetime(2026, 10, 6, 6, 31, tzinfo=timezone.utc)) == 4
        preview = parse_args(["historical-evidence", "import", str(FIXTURE), "--sha256", SHA, "--database-url", url])
        assert historical_evidence.main(preview, engine, now=datetime(2026, 10, 6, 5, 45, tzinfo=timezone.utc)) == 4  # preview is never refused
    finally:
        engine.dispose()
