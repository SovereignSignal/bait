import sqlalchemy as sa

from bait import db
from bait.clients.blockscout import CodeInfo
from bait.steps import group, ingest
from tests.conftest import insert_hook


def test_group_by_name_and_factories(conn, ctx):
    ingest.run(conn, ctx)
    out = group.run(conn, ctx)
    assert out["by_name"] == 300 and out["created"] == 91 and out["unassigned"] == 0
    assert out["factories"] == 3   # EulerSwap 51, OrketHook 47, StrategyFeeHook 34 (> 20)
    rows = conn.execute(sa.select(db.projects.c.name, db.projects.c.is_factory, db.projects.c.slug, db.projects.c.what_line)).all()
    by = {r.name: r for r in rows}
    assert by["EulerSwap"].is_factory and not by["BunniHook"].is_factory
    assert by["BunniHook"].slug == "bunnihook" and by["BunniHook"].what_line  # placeholder from description
    n = conn.execute(sa.select(sa.func.count()).select_from(db.hooks).join(db.projects).where(db.projects.c.name == "EulerSwap")).scalar_one()
    assert n == 51
    # idempotent
    out2 = group.run(conn, ctx)
    assert out2["created"] == 0 and out2["by_name"] == 0 and out2["factories"] == 3


def test_group_same_normalized_name_merges(conn, ctx):
    insert_hook(conn, "0x01", 1, name="BunniHook")
    insert_hook(conn, "0x02", 8453, name="Bunni Hooks")
    insert_hook(conn, "0x03", 8453, name="bunni")
    out = group.run(conn, ctx)
    assert out["created"] == 1 and out["by_name"] == 3
    assert conn.execute(sa.select(sa.func.count(sa.distinct(db.hooks.c.project_id)))).scalar_one() == 1


class FakeBlockscout:
    def __init__(self, table):
        self.table, self.calls = table, []

    def fetch_code(self, chain_id, address):
        self.calls.append(address)
        h = self.table.get(address)
        return CodeInfo(code_hash=h) if h else None


def test_group_by_code_hash_joins_unnamed_to_existing(conn, ctx):
    insert_hook(conn, "0xaa", 1, name="Saturn")
    insert_hook(conn, "0xbb", 1, name=None)   # same bytecode as Saturn, no name
    insert_hook(conn, "0xcc", 1, name=None)   # different bytecode, nothing to join -> long tail
    ctx.clients["blockscout"] = FakeBlockscout({"0xaa": "0xhash1", "0xbb": "0xhash1", "0xcc": "0xhash2"})
    ctx.max_code_fetch = 10
    out = group.run(conn, ctx)
    assert out["by_name"] == 1 and out["by_code"] == 1 and out["code_fetched"] == 3
    pid = {r.address: r.project_id for r in conn.execute(sa.select(db.hooks))}
    assert pid["0xaa"] == pid["0xbb"]
    # no Claude key: the leftover becomes its own address-named project
    assert pid["0xcc"] not in (None, pid["0xaa"]) and out["created"] == 2 and out["unassigned"] == 0


class FakeClaude:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def classify_hook(self, name, description, address, known_projects):
        self.calls.append((name, address, list(known_projects)))
        return self.answers[address]


def test_group_long_tail_with_claude(conn, ctx):
    insert_hook(conn, "0xaa", 1, name="Saturn")
    insert_hook(conn, "0xdd", 1, name=None)
    insert_hook(conn, "0xee", 1, name=None)
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == "0xdd").values(description="Saturn omnichain hook, second deployment"))
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == "0xee").values(description="A brand new launchpad hook"))
    ctx.clients["claude"] = FakeClaude({
        "0xdd": {"project": "Saturn", "category": "orders", "reason": "same team"},
        "0xee": {"project": "Fresh Thing", "category": "launchpad", "reason": "new"},
    })
    out = group.run(conn, ctx)
    assert out["by_claude"] == 2 and out["created"] == 2
    rows = {r.name: r for r in conn.execute(sa.select(db.projects))}
    assert rows["Fresh Thing"].category == "launchpad" and rows["Fresh Thing"].slug == "fresh-thing"
    pid = {r.address: r.project_id for r in conn.execute(sa.select(db.hooks))}
    assert pid["0xdd"] == pid["0xaa"] == rows["Saturn"].id and pid["0xee"] == rows["Fresh Thing"].id
    assert "Saturn" in ctx.clients["claude"].calls[0][2]


def test_unlisted_bytecode_family_becomes_one_project(conn, ctx):
    from tests.conftest import insert_hook
    ctx.clients.update(claude=None, blockscout=None)
    for i in range(3):
        insert_hook(conn, f"0xfa0{i}", 4663, name=None, code_hash="same")
    insert_hook(conn, "0x10e0", 4663, name=None, code_hash="other")
    group.run(conn, ctx)
    rows = conn.execute(sa.select(db.hooks.c.address, db.hooks.c.project_id)).all()
    fam = {r.project_id for r in rows if r.address.startswith("0xfa0")}
    lone = next(r.project_id for r in rows if r.address == "0x10e0")
    assert len(fam) == 1 and lone not in fam
    name = conn.execute(sa.select(db.projects.c.name).where(db.projects.c.id == fam.pop())).scalar_one()
    assert name.startswith("Unlisted hook family")



def test_bare_address_hooks_never_reach_claude(conn, ctx):
    insert_hook(conn, "0xb1", 8453, name=None)
    insert_hook(conn, "0xb2", 8453, name=None)
    fc = FakeClaude({})
    ctx.clients["claude"] = fc
    out = group.run(conn, ctx)
    assert fc.calls == [] and out["by_claude"] == 0 and out["unlisted_named"] == 2
    names = sorted(r.name for r in conn.execute(sa.select(db.projects)))
    assert all(n.startswith("Unlisted hook 0x") for n in names)


def test_group_backfills_address_flags_for_empty_metadata_hooks(conn, ctx):
    insert_hook(conn, "0x4444000000000000000000000000000000000008", 8453)   # flags={} from conftest
    group.run(conn, ctx)
    f = conn.execute(sa.select(db.hooks.c.flags).where(db.hooks.c.address == "0x4444000000000000000000000000000000000008")).scalar_one()
    assert f["beforeSwapReturnsDelta"] is True and f["beforeSwap"] is False
