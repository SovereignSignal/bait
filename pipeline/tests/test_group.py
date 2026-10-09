import copy
import json

import sqlalchemy as sa

from bait import db
from bait.clients.blockscout import CodeInfo
from bait.steps import group, ingest
from tests.conftest import NOW, insert_hook, insert_project


def test_group_by_name_and_factories(conn, ctx):
    ingest.run(conn, ctx)
    out = group.run(conn, ctx)
    # 30 template-named hooks (LimitOrderHook x7, FeeHook x3, CustomHook, DynamicFeeHook, BlackListHook, Counter,
    # ArbitrageHook, MyNewHook, ArbHook, GridHook, CurveHook x2 each, TestHook, SwapFeeHook) get a card each
    assert out["by_name"] == 270 and out["created"] == 108 and out["unassigned"] == 0
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
        return CodeInfo(code_hash=h, is_proxy=False) if h else None


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
    conn.execute(sa.update(db.hooks).values(is_proxy=False))          # bytecode fetched: not a proxy
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


def test_regroup_column_is_added_to_an_existing_hooks_table(tmp_path):
    e = db.make_engine(f"sqlite:///{tmp_path}/old.sqlite")
    with e.begin() as c:
        c.execute(sa.text("CREATE TABLE hooks (address text NOT NULL, chain_id integer NOT NULL, name text,"
                          " first_seen_at timestamp NOT NULL, PRIMARY KEY (address, chain_id))"))
        c.execute(sa.text("INSERT INTO hooks VALUES ('0xa', 1, NULL, '2026-10-06 06:00:00')"))
    db.create_all(e)
    assert "hooks.regroup" in db.add_missing_columns(e)
    with e.begin() as c:
        assert c.execute(sa.text("SELECT regroup FROM hooks")).scalar_one() == 0
        assert sa.inspect(c).has_table("project_aliases")


def A(n):
    return "0x" + f"{n:040x}"


def _unlisted_project(conn, addr, chain_id, **hook):
    pid = insert_project(conn, f"Unlisted hook {addr[:6]}…{addr[-4:]}", NOW, slug=f"unlisted-hook-{addr[:6]}-{addr[-4:]}")
    conn.execute(sa.update(db.projects).where(db.projects.c.id == pid).values(norm_name=None))
    insert_hook(conn, addr, chain_id, project_id=pid, **hook)
    return pid


def _hooklist(tmp_path, fixture_entries, *hooks):
    out = []
    for addr, chain_id, name in hooks:
        e = copy.deepcopy(fixture_entries[0])
        e["hook"].update(address=addr, chainId=chain_id, name=name)
        out.append(e)
    path = tmp_path / "hooklist.json"
    path.write_text(json.dumps(out))
    return str(path)


def _alias(conn, slug):
    return conn.execute(sa.select(db.project_aliases.c.project_id, db.project_aliases.c.reason)
                        .where(db.project_aliases.c.old_slug == slug)).first()


def test_hooklisted_later_renames_its_contract_project_in_place(conn, ctx, tmp_path, fixture_entries):
    pid = insert_project(conn, "OrcaHook (Base)", NOW, slug="orcahook-base")
    conn.execute(sa.update(db.projects).where(db.projects.c.id == pid).values(norm_name="contract:8453:orca"))
    insert_hook(conn, A(1), 8453, project_id=pid)
    conn.execute(sa.update(db.hooks).values(hooklisted=False, contract_name="OrcaHook"))
    ctx.hooklist_file = _hooklist(tmp_path, fixture_entries, (A(1), 8453, "OrcaHook"))
    ingest.run(conn, ctx)
    out = group.run(conn, ctx)
    assert out["promoted"] == 1 and out["created"] == 0
    assert [(p.id, p.name, p.norm_name, p.slug) for p in conn.execute(sa.select(db.projects))] == [(pid, "OrcaHook", "orca", "orcahook")]
    assert tuple(_alias(conn, "orcahook-base")) == (pid, "renamed")
    assert conn.execute(sa.select(db.hooks.c.regroup)).scalar_one() is False


def test_hooklisted_later_joins_the_curated_project_and_old_link_redirects(conn, ctx, tmp_path, fixture_entries):
    bunni = insert_project(conn, "Bunni", NOW, slug="bunni")
    lone = _unlisted_project(conn, A(2), 8453)
    conn.execute(sa.update(db.hooks).values(hooklisted=False))
    ctx.hooklist_file = _hooklist(tmp_path, fixture_entries, (A(2), 8453, "BunniHook"))
    ingest.run(conn, ctx)
    out = group.run(conn, ctx)
    assert out["moved"] == 1 and out["retired"] == 1
    assert conn.execute(sa.select(db.hooks.c.project_id)).scalar_one() == bunni
    assert lone not in {r.id for r in conn.execute(sa.select(db.projects.c.id))}
    assert tuple(_alias(conn, "unlisted-hook-0x0000-0002")) == (bunni, "regroup:name")


def test_legacy_sha1_hash_is_rehashed_and_the_clone_joins_the_registered_project(conn, ctx, monkeypatch):
    canon = "0x" + "c" * 64
    saturn = insert_project(conn, "Saturn", NOW, slug="saturn")
    insert_hook(conn, A(3), 8453, project_id=saturn, name="Saturn", code_hash=canon)   # Blockscout wrote this one
    _unlisted_project(conn, A(4), 8453, code_hash="f" * 40)                            # keyless sha1: never matched
    other = _unlisted_project(conn, A(5), 1, code_hash="e" * 40)                      # RPC did not answer: kept
    asked = []
    facts = {(8453, A(3)): (canon, False), (8453, A(4)): (canon, False)}
    monkeypatch.setattr(group, "_rpc_code_facts", lambda ctx, rows, deadline: asked.extend(rows) or facts)
    out = group.run(conn, ctx)
    pid = {r.address: (r.project_id, r.code_hash) for r in conn.execute(sa.select(db.hooks))}
    assert out["code_refreshed"] == 2 and len(asked) == 3
    assert pid[A(4)] == (saturn, canon) and pid[A(5)] == (other, "e" * 40)
    assert tuple(_alias(conn, "unlisted-hook-0x0000-0004")) == (saturn, "regroup:code_hash")


def test_rule1_adopts_a_same_chain_contract_project_only_with_evidence(conn, ctx):
    order = insert_project(conn, "OrcaHook (Base)", NOW, slug="orcahook-base")
    pool = insert_project(conn, "PoolHook", NOW, slug="poolhook")
    conn.execute(sa.update(db.projects).where(db.projects.c.id == order).values(norm_name="contract:8453:orca"))
    conn.execute(sa.update(db.projects).where(db.projects.c.id == pool).values(norm_name="contract:8453:pool"))
    insert_hook(conn, A(6), 8453, project_id=order)
    insert_hook(conn, A(7), 8453, project_id=pool)
    insert_hook(conn, A(8), 8453, name="OrcaHook")
    insert_hook(conn, A(9), 8453, name="PoolHook")
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address.in_([A(6), A(8)])).values(deployer="0x" + "d" * 40))
    out = group.run(conn, ctx)
    assert out["adopted"] == 1 and out["created"] == 1 and out["by_name"] == 2
    p = {r.id: r for r in conn.execute(sa.select(db.projects))}
    assert (p[order].name, p[order].norm_name, p[order].slug) == ("OrcaHook", "orca", "orcahook")
    pid = {r.address: r.project_id for r in conn.execute(sa.select(db.hooks))}
    assert pid[A(8)] == order and pid[A(9)] not in (pool, None)                       # a name alone does not merge


def test_proxy_named_project_is_split_and_proxies_never_join_by_bytecode(conn, ctx):
    proxy = insert_project(conn, "ERC1967Proxy", NOW, slug="erc1967proxy")
    insert_hook(conn, A(11), 8453, project_id=proxy, name="ERC1967Proxy", code_hash="0xp")
    insert_hook(conn, A(12), 56, project_id=proxy, name="ERC1967Proxy", code_hash="0xp")
    insert_hook(conn, A(13), 8453, code_hash="0xp")                                    # on-chain proxy clone
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == A(13)).values(contract_name="ERC1967Proxy"))
    out = group.run(conn, ctx)
    assert out["split"] == 1 and out["by_code"] == 0 and out["by_name"] == 0
    rows = conn.execute(sa.select(db.projects.c.name, db.projects.c.slug).join(db.hooks)).all()
    assert len({r.slug for r in rows}) == 3 and all(r.name.startswith("Unlisted hook 0x") for r in rows)
    assert _alias(conn, "erc1967proxy").project_id in {r.project_id for r in conn.execute(sa.select(db.hooks))}


def test_template_named_hooks_group_by_deployer_never_by_name(conn, ctx):
    acme = insert_project(conn, "Acme", NOW, slug="acme")
    other = insert_project(conn, "Other", NOW, slug="other")
    insert_hook(conn, A(21), 8453, project_id=acme, name="Acme")
    insert_hook(conn, A(22), 8453, project_id=other, name="Other")
    for n in (23, 24, 25):
        insert_hook(conn, A(n), 8453, name="FeeHook")
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address.in_([A(21), A(23)])).values(deployer="0x" + "d" * 40))
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address.in_([A(22), A(25)]))
                 .values(deployer="0x4e59b44847b379578588920ca78fbf26c0b4956c"))
    out = group.run(conn, ctx)
    pid = {r.address: r.project_id for r in conn.execute(sa.select(db.hooks))}
    assert out["by_deployer"] == 1 and out["by_name"] == 0
    assert pid[A(23)] == acme and pid[A(25)] != other and pid[A(24)] != pid[A(25)]
    assert "FeeHook" not in {r.name for r in conn.execute(sa.select(db.projects.c.name))}


PROXY = "0x" + "9" * 64       # one ERC1967Proxy runtime, deployed by unrelated teams


class _Names:
    def __init__(self, table):
        self.table = table

    def lookup(self, chain_id, address):
        n = self.table.get(address)
        return (n, "sourcify") if n else None


def _all_proxies(monkeypatch):
    monkeypatch.setattr(group, "_rpc_code_facts", lambda ctx, rows, deadline: {(r.chain_id, r.address): (PROXY, True) for r in rows})


def test_unverified_proxies_sharing_bytecode_never_become_a_family(conn, ctx, monkeypatch):
    _all_proxies(monkeypatch)
    for n in (41, 42, 43):
        insert_hook(conn, A(n), 8453, code_hash=PROXY)               # from the on-chain step: proxy status unknown
    ctx.clients["names"] = _Names({A(41): "OrcaHook", A(42): "KestrelVault", A(43): "OrcaHook"})
    out = group.run(conn, ctx)
    rows = conn.execute(sa.select(db.hooks.c.address, db.hooks.c.is_proxy, db.projects.c.name).join(db.projects)).all()
    assert out["by_code"] == 0 and all(r.is_proxy is True for r in rows)
    assert len({r.name for r in rows}) == 3 and "KestrelVault" in {r.name for r in rows}
    assert conn.execute(sa.select(sa.func.count(sa.distinct(db.hooks.c.project_id)))).scalar_one() == 3


def test_first_night_rehash_never_pulls_proxies_into_a_curated_project(conn, ctx, monkeypatch):
    _all_proxies(monkeypatch)
    arbi = insert_project(conn, "Arbi", NOW, slug="arbi")
    insert_hook(conn, A(51), 42161, project_id=arbi, name="Arbi", code_hash=PROXY)    # Blockscout hash, status unknown
    lone = [_unlisted_project(conn, A(n), c, code_hash=ch * 40) for n, c, ch in ((52, 1, "a"), (53, 8453, "b"), (54, 8453, "d"))]
    out = group.run(conn, ctx)
    pid = {r.address: r.project_id for r in conn.execute(sa.select(db.hooks))}
    assert out["code_refreshed"] == 4 and out["moved"] == 0 and out["retired"] == 0
    assert [pid[A(n)] for n in (52, 53, 54)] == lone and pid[A(51)] == arbi


def test_proxy_bytecode_family_is_split_and_generic_contract_names_are_looked_up_again(conn, ctx, monkeypatch):
    _all_proxies(monkeypatch)
    fam = insert_project(conn, "Unlisted hook family 0x0000…", NOW, slug="unlisted-hook-family-0x0000")
    conn.execute(sa.update(db.projects).where(db.projects.c.id == fam).values(norm_name=None))
    for n in (61, 62):
        insert_hook(conn, A(n), 8453, project_id=fam, code_hash=PROXY)
    conn.execute(sa.update(db.hooks).values(contract_name="ERC1967Proxy", name_checked_at=NOW))
    out = group.run(conn, ctx)
    rows = conn.execute(sa.select(db.hooks)).all()
    slugs = set(conn.execute(sa.select(db.projects.c.slug)).scalars())
    assert out["split"] == 1 and len({r.project_id for r in rows}) == 2 and "unlisted-hook-family-0x0000" not in slugs
    assert all(r.contract_name is None and r.name_checked_at is None and r.is_proxy is True for r in rows)


def test_sample_template_names_never_merge_by_name(conn, ctx):
    ingest.run(conn, ctx)
    group.run(conn, ctx)
    rows = conn.execute(sa.select(db.hooks.c.name, db.hooks.c.chain_id, db.hooks.c.project_id, db.projects.c.name.label("pname"))
                        .join(db.projects)).all()
    for name, n in (("LimitOrderHook", 7), ("BlackListHook", 2), ("CurveHook", 2)):
        mine = [r for r in rows if r.name == name]
        assert len(mine) == n and len({r.project_id for r in mine}) == n and not any(r.pname == name for r in mine)
    assert {r.chain_id for r in rows if r.name == "CurveHook"} == {42161, 5042}


def _deploy(conn, n, chain_id, name, dep):
    insert_hook(conn, A(n), chain_id, name=name)
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == A(n)).values(deployer=dep))


def test_a_hooklist_name_shared_by_unrelated_deployers_on_one_chain_is_generic(conn, ctx):
    for n, d in ((71, "1"), (72, "2"), (70, "4")):                                # three teams, one chain, one name
        _deploy(conn, n, 8453, "Zephyr", "0x" + d * 40)
    for n in (73, 74):                                                             # one team, two deployments
        _deploy(conn, n, 8453, "Mistral", "0x" + "3" * 40)
    group.run(conn, ctx)
    pid = {r.address: r.project_id for r in conn.execute(sa.select(db.hooks))}
    names_ = set(conn.execute(sa.select(db.projects.c.name)).scalars())
    assert len({pid[A(70)], pid[A(71)], pid[A(72)]}) == 3 and "Zephyr" not in names_
    assert pid[A(73)] == pid[A(74)] and "Mistral" in names_


def test_one_team_with_a_deployer_per_chain_stays_one_project(conn, ctx):
    for n, cid, d in ((75, 1, "5"), (76, 8453, "6"), (77, 42161, "7")):          # per-chain bytecode differs too
        _deploy(conn, n, cid, "BunniHook", "0x" + d * 40)
    group.run(conn, ctx)
    first = {r.address: r.project_id for r in conn.execute(sa.select(db.hooks))}
    group.run(conn, ctx)
    again = {r.address: r.project_id for r in conn.execute(sa.select(db.hooks))}
    assert len(set(first.values())) == 1 and first == again
    assert set(conn.execute(sa.select(db.projects.c.slug)).scalars()) == {"bunnihook"}


def test_an_enriched_or_large_generic_project_is_logged_not_split(conn, ctx, caplog):
    import logging
    fee = insert_project(conn, "FeeHook", NOW, slug="feehook")
    counter = insert_project(conn, "Counter", NOW, slug="counter")
    conn.execute(sa.update(db.projects).where(db.projects.c.id == fee).values(norm_name="fee", website="https://fee.example"))
    for n in (111, 112):
        insert_hook(conn, A(n), 8453, project_id=fee, name="FeeHook")
    for n in (113, 114, 115):
        insert_hook(conn, A(n), 8453, project_id=counter, name="Counter")
    with caplog.at_level(logging.INFO, logger="bait"):
        out = group.run(conn, ctx)
    pid = {r.address: r.project_id for r in conn.execute(sa.select(db.hooks))}
    assert out["split"] == 0 and {pid[A(111)], pid[A(112)]} == {fee} and {pid[A(n)] for n in (113, 114, 115)} == {counter}
    assert "group: would split feehook (generic name, 2 hooks)" in caplog.text
    assert "group: would split counter (generic name, 3 hooks)" in caplog.text


def test_a_split_carries_its_slug_receipts_and_enrichment_to_where_most_hooks_land(conn, ctx):
    old = insert_project(conn, "TestHook", NOW, slug="testhook")
    conn.execute(sa.update(db.projects).where(db.projects.c.id == old).values(norm_name="test", x_handle="th_hooks"))
    conn.execute(sa.insert(db.receipts).values(project_id=old, url="https://news.example/th", title="t", source="news.example"))
    from bait.steps import names
    names.add_alias(conn, "testhook-older", old, "renamed", NOW)
    for n in (121, 122):
        insert_hook(conn, A(n), 8453, project_id=old, name="TestHook")
    out = group.run(conn, ctx)
    holder = conn.execute(sa.select(db.projects.c.id).where(db.projects.c.x_handle == "th_hooks")).scalars().all()
    assert out["split"] == 1 and len(holder) == 1
    assert {tuple(_alias(conn, s)) for s in ("testhook", "testhook-older")} == {(holder[0], "split:generic")}
    assert conn.execute(sa.select(db.receipts.c.project_id)).scalar_one() == holder[0]
    assert holder[0] in {r.project_id for r in conn.execute(sa.select(db.hooks))}


def test_a_generic_claude_answer_is_not_created_split_and_recreated(conn, ctx):
    insert_hook(conn, A(81), 8453, name=None)
    conn.execute(sa.update(db.hooks).values(description="Counts swaps per pool, a template deployment"))
    fc = FakeClaude({A(81): {"project": "Counter", "category": "other", "reason": "template"}})
    ctx.clients["claude"] = fc
    pids = []
    for _ in range(3):
        group.run(conn, ctx)
        pids.append(conn.execute(sa.select(db.hooks.c.project_id).where(db.hooks.c.address == A(81))).scalar_one())
    p = conn.execute(sa.select(db.projects).where(db.projects.c.id == pids[0])).one()
    assert len(fc.calls) == 1 and len(set(pids)) == 1
    assert p.name.startswith("Unlisted hook 0x") and p.norm_name is None and p.category == "other"


def test_code_refresh_nulls_a_definitive_miss_within_the_budget(conn, ctx, monkeypatch):
    import time
    _unlisted_project(conn, A(91), 8453, code_hash="a" * 40)
    budgets = []

    def facts(ctx, rows, deadline):
        budgets.append(deadline - time.monotonic())
        return {(8453, A(91)): None}                                # the chain answered: no code at this address
    monkeypatch.setattr(group, "_rpc_code_facts", facts)
    monkeypatch.setenv("REHASH_BUDGET_S", "7")
    out = group.run(conn, ctx)
    assert out["code_missing"] == 1 and 6 < budgets[0] <= 7
    assert conn.execute(sa.select(db.hooks.c.code_hash).where(db.hooks.c.address == A(91))).scalar_one() is None


def test_code_refresh_window_rotates_past_unreachable_rows(conn, ctx, monkeypatch):
    from datetime import timedelta
    pid = _unlisted_project(conn, A(92), 8453, code_hash="b" * 40)
    for n in (93, 94):
        insert_hook(conn, A(n), 8453, project_id=pid, code_hash="b" * 40)
    seen = []
    monkeypatch.setattr(group, "_rpc_code_facts", lambda ctx, rows, deadline: seen.extend(r.address for r in rows) or {})
    monkeypatch.setattr(group, "CODE_FACTS_BATCH", 1)
    for _ in range(3):                                               # never answered: each night asks a different row
        group.run(conn, ctx)
        ctx.day += timedelta(days=1)
    assert sorted(seen) == [A(92), A(93), A(94)]


def test_a_hook_marked_for_name_and_code_regroups_on_both(conn, ctx):
    code = "0x" + "e" * 64
    bunni = insert_project(conn, "Bunni", NOW, slug="bunni")
    _unlisted_project(conn, A(101), 8453, code_hash=code)
    _unlisted_project(conn, A(102), 8453, code_hash=code)                         # a clone nobody marked
    conn.execute(sa.update(db.hooks).values(is_proxy=False))
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == A(101)).values(name="BunniHook", regroup=True))
    group.run(conn, ctx)
    pid = {r.address: r.project_id for r in conn.execute(sa.select(db.hooks))}
    assert pid[A(101)] == bunni and pid[A(102)] == bunni
