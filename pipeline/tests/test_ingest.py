from datetime import datetime, timezone

import sqlalchemy as sa

from bait import db
from bait.steps import ingest
from tests.conftest import insert_hook


def test_ingest_fixture_then_idempotent(conn, ctx):
    out = ingest.run(conn, ctx)
    assert out == {"hooks": 300, "new": 300, "updated": 0, "invalid": 0, "errors": 0, "delisted": 0}
    row = conn.execute(sa.select(db.hooks).where(db.hooks.c.address == "0x0000fe59823933ac763611a69c88f91d45f81888", db.hooks.c.chain_id == 42161)).one()
    assert row.chain_id == 42161 and row.name == "BunniHook" and row.flags["beforeSwapReturnsDelta"] is True
    assert row.verified_source is True and row.properties["dynamicFee"] is False
    assert db.as_utc(row.first_seen_at) == ctx.now

    ctx.now = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)
    out2 = ingest.run(conn, ctx)
    assert out2 == {"hooks": 300, "new": 0, "updated": 300, "invalid": 0, "errors": 0, "delisted": 0}
    assert conn.execute(sa.select(sa.func.count()).select_from(db.hooks)).scalar_one() == 300
    row2 = conn.execute(sa.select(db.hooks.c.first_seen_at).where(db.hooks.c.address == row.address, db.hooks.c.chain_id == 42161)).scalar_one()
    assert db.as_utc(row2) == datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)   # preserved on update


def test_ingest_lowercases_and_counts_chains(conn, ctx):
    ingest.run(conn, ctx)
    chains = dict(conn.execute(sa.select(db.hooks.c.chain_id, sa.func.count()).group_by(db.hooks.c.chain_id)).all())
    assert chains == {42161: 203, 43114: 69, 8453: 16, 5042: 12}
    assert conn.execute(sa.select(sa.func.count()).select_from(db.hooks).where(db.hooks.c.address != sa.func.lower(db.hooks.c.address))).scalar_one() == 0


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


def test_ingest_marks_listed_hooks_and_demotes_the_rest(conn, ctx):
    insert_hook(conn, "0x" + "c" * 40, 8453, name=None)            # found on-chain, never listed
    out = ingest.run(conn, ctx)
    assert out["delisted"] == 1
    rows = {(r.address, r.chain_id): r.hooklisted for r in conn.execute(sa.select(db.hooks.c.address, db.hooks.c.chain_id, db.hooks.c.hooklisted))}
    assert rows[("0x" + "c" * 40, 8453)] is False
    listed = [v for k, v in rows.items() if k != ("0x" + "c" * 40, 8453)]
    assert listed and all(v is True for v in listed)
