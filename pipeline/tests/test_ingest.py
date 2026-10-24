from datetime import datetime, timezone

import sqlalchemy as sa

from bait import db
from bait.steps import ingest
from tests.conftest import insert_hook


def test_ingest_fixture_then_idempotent(conn, ctx):
    out = ingest.run(conn, ctx)
    assert out == {"hooks": 300, "new": 300, "updated": 0, "invalid": 0, "errors": 0, "delisted": 0, "regroup": 0}
    row = conn.execute(sa.select(db.hooks).where(db.hooks.c.address == "0x0000fe59823933ac763611a69c88f91d45f81888", db.hooks.c.chain_id == 42161)).one()
    assert row.chain_id == 42161 and row.name == "BunniHook" and row.flags["beforeSwapReturnsDelta"] is True
    assert row.verified_source is True and row.properties["dynamicFee"] is False
    assert db.as_utc(row.first_seen_at) == ctx.now

    ctx.now = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)
    out2 = ingest.run(conn, ctx)
    assert out2 == {"hooks": 300, "new": 0, "updated": 300, "invalid": 0, "errors": 0, "delisted": 0, "regroup": 0}
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


def test_ingest_marks_grouped_hooks_the_registry_just_named(conn, ctx, fixture_entries):
    from tests.conftest import NOW, insert_project
    e = fixture_entries[0]["hook"]
    addr, cid = e["address"].lower(), e["chainId"]
    pid = insert_project(conn, "Unlisted hook x", NOW, slug="unlisted-x")
    insert_hook(conn, addr, cid, project_id=pid)                         # found on-chain before its PR merged
    insert_hook(conn, "0x" + "c" * 40, 8453, project_id=pid)             # never listed: not marked
    conn.execute(sa.update(db.hooks).values(hooklisted=False))
    out = ingest.run(conn, ctx)
    marked = {r.address for r in conn.execute(sa.select(db.hooks.c.address).where(db.hooks.c.regroup.is_(True)))}
    assert out["regroup"] == 1 and marked == {addr}


def test_registry_deployer_wins_a_looked_up_one_survives_the_night_and_a_create2_proxy_is_none(conn, ctx, fixture_entries, tmp_path):
    """Until 2026-10-24 ingest rewrote hooks.deployer from the registry every night, so a deployer the lookup found for
    a listed hook the registry names no deployer for (most of them) was gone the next morning, still stamped."""
    import json
    from tests.conftest import NOW
    entries = [dict(e, hook=dict(e["hook"])) for e in fixture_entries[:3]]
    entries[0]["hook"]["deployer"] = ""                                                    # names none
    entries[1]["hook"]["deployer"] = "0x4e59b44847b379578588920cA78FbF26c0B4956C"           # Arachnid's CREATE2 proxy
    entries[2]["hook"]["deployer"] = "0x" + "AB" * 20
    path = tmp_path / "hooklist.json"
    ctx.hooklist_file = str(path)

    def ingest_with(entries):
        path.write_text(json.dumps(entries))
        ingest.run(conn, ctx)

    def dep(e):
        h = db.hooks.c
        r = conn.execute(sa.select(h.deployer, h.deployer_source, h.deployer_checked_at)
                         .where(h.address == e["hook"]["address"].lower(), h.chain_id == e["hook"]["chainId"])).one()
        return r.deployer, r.deployer_source, r.deployer_checked_at is not None

    ingest_with(entries)
    assert dep(entries[1]) == (None, None, False)                                         # the proxy is nobody's wallet
    assert dep(entries[2]) == ("0x" + "ab" * 20, "hooklist", False)
    found = "0x" + "12" * 20
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == entries[0]["hook"]["address"].lower())
                 .values(deployer=found, deployer_source="sourcify", deployer_checked_at=NOW))   # the lookup's answer
    ingest_with(entries)
    assert dep(entries[0]) == (found, "sourcify", True)                                   # kept overnight
    entries[2]["hook"]["deployer"] = ""
    ingest_with(entries)
    assert dep(entries[2]) == (None, None, False)                                         # dropped: cleared, unstamped
    entries[0]["hook"]["deployer"] = "0x" + "CD" * 20
    ingest_with(entries)
    assert dep(entries[0])[:2] == ("0x" + "cd" * 20, "hooklist")                          # the registry wins
