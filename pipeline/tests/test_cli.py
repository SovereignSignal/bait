import json

import sqlalchemy as sa

from bait import db
from bait.__main__ import main
from tests.conftest import FIXTURE


def test_cli_end_to_end_sqlite_idempotent(tmp_path, monkeypatch):
    for k in ("GRAPH_API_KEY", "ANTHROPIC_API_KEY", "EXA_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NAMES_MAX", "0")              # the keyless lookups (Sourcify, Blockscout) stay off the network
    monkeypatch.setenv("DEPLOYER_LOOKUPS", "0")
    url = f"sqlite:///{tmp_path}/t.sqlite"
    argv = ["run", "--day", "2026-10-06", "--hooklist-file", FIXTURE, "--database-url", url, "--force-digest", "--max-code-fetch", "0"]
    assert main(argv) == 0
    assert main(argv) == 0
    e = db.make_engine(url)
    with e.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(db.hooks)).scalar_one() == 300
        # 78 named projects + 30 template-named hooks (LimitOrderHook, FeeHook, CurveHook, ...) that never merge by name,
        # less one: Bonker's static and dynamic fee hooks share a deployer and an audit URL (rule 4, one team)
        assert c.execute(sa.select(sa.func.count()).select_from(db.projects)).scalar_one() == 107
        assert c.execute(sa.select(sa.func.count()).select_from(db.project_daily)).scalar_one() == 107
        assert c.execute(sa.select(sa.func.count()).select_from(db.digests)).scalar_one() == 1
        receipts = c.execute(sa.select(db.run_receipts)).all()
    assert len(receipts) == 2 and all(r.exit_code == 0 for r in receipts)


def test_cli_rejects_unknown_step_and_chain(tmp_path):
    url = f"sqlite:///{tmp_path}/t.sqlite"
    assert main(["run", "--steps", "nope", "--database-url", url]) == 2
    assert main(["run", "--chains", "mars", "--database-url", url]) == 2


def test_run_exits_3_when_lock_is_held(monkeypatch, tmp_path):
    from bait import db
    monkeypatch.setattr(db, "try_run_lock", lambda engine: None)
    rc = main(["run", "--database-url", f"sqlite:///{tmp_path}/x.sqlite", "--steps", "ingest",
               "--hooklist-file", FIXTURE, "--day", "2026-10-06"])
    assert rc == 3


def test_additive_indexes_are_created_idempotently(tmp_path):
    from bait import db
    e = db.make_engine(f"sqlite:///{tmp_path}/i.sqlite")
    db.create_all(e)
    assert db.add_missing_indexes(e) == [n for n, *_ in db.ADDITIVE_INDEXES]
    assert db.add_missing_indexes(e) == [n for n, *_ in db.ADDITIVE_INDEXES]   # IF NOT EXISTS: same list, no error
    with e.connect() as c:
        names = {r[1] for r in c.exec_driver_sql("PRAGMA index_list('v4_pools')")}
    assert "v4_pools_block_time_idx" in names

def test_run_writes_a_receipt_even_when_a_step_fails(tmp_path, monkeypatch):
    from bait import __main__ as cli
    url = f"sqlite:///{tmp_path}/r.sqlite"
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc1234")
    monkeypatch.setitem(cli.STEPS, "snapshot", lambda conn, ctx: (_ for _ in ()).throw(RuntimeError("fetch https://api.example/v1?key=SECRET failed")))
    rc = cli.main(["run", "--day", "2026-10-06", "--hooklist-file", FIXTURE, "--database-url", url, "--steps", "ingest,snapshot"])
    assert rc == 1
    e = db.make_engine(url)
    with e.connect() as c:
        r = c.execute(sa.select(db.run_receipts)).one()
    assert r.exit_code == 1 and r.code_sha == "abc1234" and r.steps["ingest"]["hooks"] == 300
    assert r.steps["snapshot"]["failed"] == "RuntimeError"
    assert "SECRET" not in json.dumps(r.steps)


def test_soft_errors_do_not_fail_the_run_but_land_in_the_receipt(tmp_path, monkeypatch):
    from bait import __main__ as cli
    url = f"sqlite:///{tmp_path}/s.sqlite"
    monkeypatch.setitem(cli.STEPS, "ingest", lambda conn, ctx: {"hooks": 3, "errors": 2})
    rc = cli.main(["run", "--day", "2026-10-06", "--database-url", url, "--steps", "ingest"])
    assert rc == 0
    e = db.make_engine(url)
    with e.connect() as c:
        r = c.execute(sa.select(db.run_receipts)).one()
    assert r.exit_code == 0 and r.soft_errors == 2 and r.steps["ingest"]["errors"] == 2

def test_run_exits_4_when_a_locked_migration_leaves_a_required_column_missing(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/old.sqlite"
    e = db.make_engine(url)
    with e.begin() as c:   # pool_stats from before 0010: no vol_source
        c.execute(sa.text("CREATE TABLE pool_stats (pool_id text NOT NULL, chain_id integer NOT NULL, day date NOT NULL,"
                          " vol_24h double precision, traders_24h integer, buys_24h integer, sells_24h integer,"
                          " buyers_24h integer, sellers_24h integer, PRIMARY KEY (pool_id, chain_id, day))"))

    def locked(engine, *a, **k):
        raise db.SchemaLocked("pool_stats.vol_source")
    monkeypatch.setattr(db, "add_missing_columns", locked)
    rc = main(["run", "--database-url", url, "--steps", "ingest", "--hooklist-file", FIXTURE, "--day", "2026-10-06"])
    assert rc == 4
    assert db.missing_columns(e) == ["pool_stats.vol_source", "pool_stats.vol_24h_rolling", "pool_stats.wallets_24h",
                                     "pool_stats.window_end"]
    with e.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(db.hooks)).scalar_one() == 0     # no step ran


def test_run_continues_when_a_locked_migration_had_nothing_required_left(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/new.sqlite"

    def locked(engine, *a, **k):
        raise db.SchemaLocked("index")
    monkeypatch.setattr(db, "add_missing_indexes", locked)
    rc = main(["run", "--database-url", url, "--steps", "ingest", "--hooklist-file", FIXTURE, "--day", "2026-10-06"])
    assert rc == 0
