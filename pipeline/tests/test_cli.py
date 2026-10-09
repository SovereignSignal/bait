import json

import sqlalchemy as sa

from bait import db
from bait.__main__ import main
from tests.conftest import FIXTURE


def test_cli_end_to_end_sqlite_idempotent(tmp_path, monkeypatch):
    for k in ("GRAPH_API_KEY", "ANTHROPIC_API_KEY", "EXA_API_KEY", "PARALLEL_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    url = f"sqlite:///{tmp_path}/t.sqlite"
    argv = ["run", "--day", "2026-10-06", "--hooklist-file", FIXTURE, "--database-url", url, "--force-digest", "--max-code-fetch", "0"]
    assert main(argv) == 0
    assert main(argv) == 0
    e = db.make_engine(url)
    with e.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(db.hooks)).scalar_one() == 300
        # 78 named projects + 30 template-named hooks (LimitOrderHook, FeeHook, ...) that never merge by name
        assert c.execute(sa.select(sa.func.count()).select_from(db.projects)).scalar_one() == 108
        assert c.execute(sa.select(sa.func.count()).select_from(db.project_daily)).scalar_one() == 108
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
