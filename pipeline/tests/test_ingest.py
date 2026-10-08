from datetime import datetime, timezone

import sqlalchemy as sa

from bait import db
from bait.steps import ingest


def test_ingest_fixture_then_idempotent(conn, ctx):
    out = ingest.run(conn, ctx)
    assert out == {"hooks": 300, "new": 300, "updated": 0}
    row = conn.execute(sa.select(db.hooks).where(db.hooks.c.address == "0x0000fe59823933ac763611a69c88f91d45f81888", db.hooks.c.chain_id == 42161)).one()
    assert row.chain_id == 42161 and row.name == "BunniHook" and row.flags["beforeSwapReturnsDelta"] is True
    assert row.verified_source is True and row.properties["dynamicFee"] is False
    assert db.as_utc(row.first_seen_at) == ctx.now

    ctx.now = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)
    out2 = ingest.run(conn, ctx)
    assert out2 == {"hooks": 300, "new": 0, "updated": 300}
    assert conn.execute(sa.select(sa.func.count()).select_from(db.hooks)).scalar_one() == 300
    row2 = conn.execute(sa.select(db.hooks.c.first_seen_at).where(db.hooks.c.address == row.address, db.hooks.c.chain_id == 42161)).scalar_one()
    assert db.as_utc(row2) == datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)   # preserved on update


def test_ingest_lowercases_and_counts_chains(conn, ctx):
    ingest.run(conn, ctx)
    chains = dict(conn.execute(sa.select(db.hooks.c.chain_id, sa.func.count()).group_by(db.hooks.c.chain_id)).all())
    assert chains == {42161: 203, 43114: 69, 8453: 16, 5042: 12}
    assert conn.execute(sa.select(sa.func.count()).select_from(db.hooks).where(db.hooks.c.address != sa.func.lower(db.hooks.c.address))).scalar_one() == 0
