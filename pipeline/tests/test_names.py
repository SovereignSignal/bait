"""Unlisted hooks named from verified source: lookup caps/rechecks, rename, per-chain merge, curated clash, generic names."""
from datetime import timedelta

import pytest
import sqlalchemy as sa

from bait import db
from bait.clients.contract_names import ContractNames, useful_name
from bait.steps import names
from tests.conftest import DAY, NOW, insert_hook, insert_project


class FakeNames:
    def __init__(self, table):
        self.table, self.calls = table, []

    def lookup(self, chain_id, address):
        self.calls.append((chain_id, address))
        n = self.table.get(address)
        return (n, "sourcify") if n else None


def _unlisted(conn, addr, chain_id, vol=0.0):
    pid = insert_project(conn, f"Unlisted hook {addr[:6]}…{addr[-4:]}", NOW, slug=f"unlisted-{addr[-6:]}-{chain_id}")
    conn.execute(sa.update(db.projects).where(db.projects.c.id == pid).values(norm_name=None))
    insert_hook(conn, addr, chain_id, project_id=pid)
    conn.execute(sa.insert(db.project_daily).values(project_id=pid, day=DAY, vol_7d=vol, is_new=False, is_spam=False))
    return pid


def A(n):
    return "0x" + f"{n:040x}"


def test_rename_merge_clash_generic_and_miss(conn, ctx):
    curated = insert_project(conn, "LaunchHook", NOW, slug="launchhook")
    conn.execute(sa.update(db.projects).where(db.projects.c.id == curated).values(norm_name="launch"))
    p1 = _unlisted(conn, A(1), 8453, vol=900)      # FoundationHookV2
    p2 = _unlisted(conn, A(2), 8453, vol=800)      # FoundationHookV2, same chain: merges into p1
    p3 = _unlisted(conn, A(3), 1, vol=700)         # FoundationHookV2 on another chain: its own project
    p4 = _unlisted(conn, A(4), 1, vol=600)         # LaunchHook: clashes with the curated project
    p5 = _unlisted(conn, A(5), 1, vol=500)         # generic UniswapV4Hook: stays unlisted
    p6 = _unlisted(conn, A(6), 1, vol=400)         # unverified: miss
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address.in_([A(1), A(2)])).values(code_hash="0x" + "c" * 64, is_proxy=False))
    fake = FakeNames({A(1): "FoundationHookV2", A(2): "FoundationHookV2", A(3): "FoundationHookV2",
                      A(4): "LaunchHook", A(5): "UniswapV4Hook"})
    ctx.clients["names"] = fake
    out = {}
    names.run(conn, ctx, out)

    assert out["names_checked"] == 6 and out["names_found"] == 5
    assert [a for _, a in fake.calls] == [A(1), A(2), A(3), A(4), A(5), A(6)]          # busiest first
    proj = {r.id: r for r in conn.execute(sa.select(db.projects))}
    assert proj[p1].name == "FoundationHookV2" and proj[p1].slug == "foundationhookv2"
    assert p2 not in proj                                                            # merged and deleted
    assert conn.execute(sa.select(db.hooks.c.project_id).where(db.hooks.c.address == A(2))).scalar_one() == p1
    assert conn.execute(sa.select(sa.func.count()).select_from(db.project_daily)
                        .where(db.project_daily.c.project_id == p2)).scalar_one() == 0
    assert proj[p3].name == "FoundationHookV2 (Ethereum)"                             # same name, other chain
    assert proj[p4].name == "LaunchHook (Ethereum)" and curated in proj               # never merged into curated
    assert proj[p5].name.startswith("Unlisted hook")
    assert proj[p6].name.startswith("Unlisted hook")
    miss = conn.execute(sa.select(db.hooks).where(db.hooks.c.address == A(6))).one()
    assert miss.contract_name is None and miss.name_checked_at is not None
    assert conn.execute(sa.select(db.hooks.c.verified_source).where(db.hooks.c.address == A(1))).scalar_one() is True
    assert _aliases(conn)["unlisted-000002-8453"] == (p1, "names:code_hash")


def test_misses_wait_14_days_and_cap_applies(conn, ctx, monkeypatch):
    _unlisted(conn, A(7), 1, vol=10)
    _unlisted(conn, A(8), 1, vol=20)
    fake = FakeNames({})
    ctx.clients["names"] = fake
    monkeypatch.setenv("NAMES_MAX", "1")
    names.run(conn, ctx, {})
    assert [a for _, a in fake.calls] == [A(8)]
    names.run(conn, ctx, {})
    assert [a for _, a in fake.calls] == [A(8), A(7)]
    names.run(conn, ctx, {})                                                          # both checked recently
    assert len(fake.calls) == 2
    ctx.now = NOW + timedelta(days=15)
    names.run(conn, ctx, {})
    assert len(fake.calls) == 3


def test_no_client_still_renames_from_stored_names(conn, ctx):
    pid = _unlisted(conn, A(9), 130)
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == A(9)).values(contract_name="OuroborosHook"))
    names.run(conn, ctx, {})
    assert conn.execute(sa.select(db.projects.c.name).where(db.projects.c.id == pid)).scalar_one() == "OuroborosHook"
    assert _aliases(conn) == {"unlisted-000009-130": (pid, "renamed")}


def test_useful_name():
    assert useful_name("FoundationHookV2") == "FoundationHookV2"
    for g in ("UniswapV4Hook", "TransparentUpgradeableProxy", "ERC1967Proxy", "BaseHook", "", None, "x" * 61):
        assert useful_name(g) is None


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._d = status, data

    def json(self):
        return self._d


class _Http:
    def __init__(self, routes):
        self.routes, self.urls, self.params = routes, [], []

    def get(self, url, params=None):
        self.urls.append(url)
        self.params.append(params)
        for frag, resp in self.routes.items():
            if frag in url:
                return resp
        return _Resp(404, {})


def test_lookup_order_and_sources():
    h = _Http({"sourcify.dev": _Resp(404, {}), "base.blockscout.com": _Resp(403, "challenge"),
               "api.blockscout.com/8453": _Resp(200, {"name": "ProHook", "is_verified": True})})
    assert ContractNames("k", http_client=h).lookup(8453, A(1))[:2] == ("ProHook", "blockscout_pro")
    h = _Http({"sourcify.dev": _Resp(200, {"compilation": {"name": "SrcHook"}})})
    assert ContractNames(None, http_client=h).lookup(1, A(1))[:2] == ("SrcHook", "sourcify")
    assert len(h.urls) == 1
    h = _Http({"routescan.io": _Resp(200, {"result": [{"ContractName": "SteadyHook"}]})})
    assert ContractNames(None, http_client=h).lookup(43114, A(1))[:2] == ("SteadyHook", "routescan")
    h = _Http({})
    assert ContractNames(None, http_client=h).lookup(56, A(1)) is None                 # nothing verified anywhere
    assert not any("api.blockscout.com" in u for u in h.urls)                          # no key: PRO never called


def test_chain_labels_cover_indexed_chains():
    from bait.indexer.chains import CHAINS
    from bait.steps.names import chain_label
    assert chain_label(4326) == "MegaETH" and chain_label(1) == "Ethereum"
    assert all(not chain_label(c.chain_id).isdigit() for c in CHAINS)


def _aliases(conn):
    return {r.old_slug: (r.project_id, r.reason) for r in conn.execute(sa.select(db.project_aliases))}


def test_retire_moves_hooks_and_repoints_aliases(conn, ctx):
    a, b, c = (insert_project(conn, n, NOW, slug=n.lower()) for n in ("A", "B", "C"))
    insert_hook(conn, A(20), 1, project_id=a)
    names.add_alias(conn, "old-a", a, "renamed", NOW)
    names.retire_project(conn, a, into=b, reason="test", now=NOW)
    assert _aliases(conn) == {"old-a": (b, "renamed"), "a": (b, "test")}
    assert conn.execute(sa.select(db.hooks.c.project_id).where(db.hooks.c.address == A(20))).scalar_one() == b
    names.retire_project(conn, c, into=None, reason="split", now=NOW)
    assert {r.id for r in conn.execute(sa.select(db.projects.c.id))} == {b}


def test_backfill_unlisted_aliases_recreates_old_unlisted_slugs(conn, ctx):
    named = insert_project(conn, "OuroborosHook", NOW, slug="ouroboroshook")
    insert_hook(conn, "0x7bc2af6fb9a989505e9629470869a19143532aec", 4663, project_id=named)
    x, y = insert_project(conn, "X", NOW, slug="x"), insert_project(conn, "Y", NOW, slug="y")
    insert_hook(conn, "0x1111" + "0" * 32 + "beef", 1, project_id=x)        # these two would share one old slug:
    insert_hook(conn, "0x1111" + "1" * 32 + "beef", 8453, project_id=y)     # ambiguous, so neither gets it
    conn.execute(sa.update(db.hooks).values(hooklisted=False))
    assert names.backfill_unlisted_aliases(conn, NOW) == 1
    assert names.backfill_unlisted_aliases(conn, NOW) == 0                  # idempotent
    assert _aliases(conn) == {"unlisted-hook-0x7bc2-2aec": (named, "backfill")}


def test_same_name_same_chain_without_evidence_stays_two_projects(conn, ctx):
    p1 = _unlisted(conn, A(10), 8453, vol=9)
    p2 = _unlisted(conn, A(11), 8453, vol=8)
    ctx.clients["names"] = FakeNames({A(10): "OrcaHook", A(11): "OrcaHook"})
    out = {}
    names.run(conn, ctx, out)
    proj = {r.id: r for r in conn.execute(sa.select(db.projects))}
    assert out["projects_merged"] == 0 and out["names_split"] == 1
    assert proj[p1].name == "OrcaHook" and proj[p2].name == "OrcaHook 0x0000…000b"
    assert proj[p2].slug == "orcahook-0x0000-000b" and _aliases(conn)["unlisted-00000b-8453"] == (p2, "renamed")


def test_proxy_resolves_to_its_implementation_name():
    src = {"compilation": {"name": "ERC1967Proxy"},
           "proxyResolution": {"isProxy": True, "implementations": [{"address": A(2), "name": "OrderBookHook"}]}}
    h = _Http({"sourcify.dev": _Resp(200, src)})
    found = ContractNames(None, http_client=h).lookup(4663, A(1))
    assert tuple(found) == ("OrderBookHook", "sourcify", True)                       # isProxy carried through
    assert h.params[0] == {"fields": "compilation.name,proxyResolution"}
    bs = {"name": "ERC1967Proxy", "is_verified": True, "implementations": [{"address_hash": A(2), "name": "OrderBookHook"}]}
    h = _Http({"robinhoodchain.blockscout.com": _Resp(200, bs)})
    assert tuple(ContractNames(None, http_client=h).lookup(4663, A(1))) == ("OrderBookHook", "blockscout", True)


def test_transient_failure_raises_and_every_source_outcome_is_counted():
    from bait.clients.contract_names import LookupTransient
    cn = ContractNames(None, http_client=_Http({"sourcify.dev": _Resp(503, {}),
                                                 "robinhoodchain.blockscout.com": _Resp(403, "challenge")}))
    with pytest.raises(LookupTransient):
        cn.lookup(4663, A(1))
    assert cn.outcomes == {"sourcify:error": 1, "blockscout_pro:skipped": 1, "blockscout:miss": 1, "routescan:miss": 1}
    only_cloudflare = _Http({"robinhoodchain.blockscout.com": _Resp(403, "challenge")})
    assert ContractNames(None, http_client=only_cloudflare).lookup(4663, A(1)) is None    # a real miss


def test_transient_lookups_are_asked_again_and_generic_hooklist_names_are_looked_up(conn, ctx):
    from bait.clients.contract_names import LookupTransient
    _unlisted(conn, A(30), 4663, vol=9)
    p2 = _unlisted(conn, A(31), 8453, vol=8)
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == A(31)).values(name="ERC1967Proxy", hooklisted=True))

    class Flaky(FakeNames):
        def lookup(self, chain_id, address):
            if address == A(30):
                self.calls.append((chain_id, address))
                raise LookupTransient(address)
            return super().lookup(chain_id, address)

    ctx.clients["names"] = flaky = Flaky({A(31): "OrderBookHook"})
    out = {}
    names.run(conn, ctx, out)
    assert out["names_transient"] == 1
    assert conn.execute(sa.select(db.hooks.c.name_checked_at).where(db.hooks.c.address == A(30))).scalar_one() is None
    assert conn.execute(sa.select(db.projects.c.name).where(db.projects.c.id == p2)).scalar_one() == "OrderBookHook"
    names.run(conn, ctx, {})
    assert flaky.calls.count((4663, A(30))) == 2                                       # not parked for 14 days


def test_same_chain_proxies_sharing_bytecode_do_not_merge_on_code_hash(conn, ctx):
    p1 = _unlisted(conn, A(40), 8453, vol=9)
    p2 = _unlisted(conn, A(41), 8453, vol=8)
    conn.execute(sa.update(db.hooks).values(code_hash="0x" + "9" * 64, is_proxy=True))
    ctx.clients["names"] = FakeNames({A(40): "OrcaHook", A(41): "OrcaHook"})
    out = {}
    names.run(conn, ctx, out)
    assert out["projects_merged"] == 0 and out["names_split"] == 1
    assert {r.id for r in conn.execute(sa.select(db.projects.c.id))} == {p1, p2}


def test_sourcify_proxy_flag_is_stored(conn, ctx):
    _unlisted(conn, A(45), 4663)

    class Proxying(FakeNames):
        def lookup(self, chain_id, address):
            return ("OrcaHook", "sourcify", True)
    ctx.clients["names"] = Proxying({})
    names.run(conn, ctx, {})
    assert conn.execute(sa.select(db.hooks.c.is_proxy).where(db.hooks.c.address == A(45))).scalar_one() is True


def test_a_generic_name_does_not_stop_the_lookup_and_stays_the_fallback():
    h = _Http({"sourcify.dev": _Resp(200, {"compilation": {"name": "ERC1967Proxy"}}),
               "robinhoodchain.blockscout.com": _Resp(200, {"name": "ERC1967Proxy", "is_verified": True,
                                                            "implementations": [{"name": "OrcaHook"}]})})
    assert tuple(ContractNames(None, http_client=h).lookup(4663, A(1))) == ("OrcaHook", "blockscout", True)
    only_raw = _Http({"sourcify.dev": _Resp(200, {"compilation": {"name": "ERC1967Proxy"}})})
    cn = ContractNames(None, http_client=only_raw)
    assert cn.lookup(4663, A(1))[:2] == ("ERC1967Proxy", "sourcify")                   # raw kept, like before
    assert cn.outcomes["sourcify:generic"] == 1 and cn.outcomes["blockscout:miss"] == 1


def test_alias_slugs_are_taken_for_new_and_renamed_projects(conn, ctx):
    from bait.steps.group import _create_project
    old = insert_project(conn, "Old", NOW, slug="old")
    names.add_alias(conn, "acme", old, "renamed", NOW)
    other = insert_project(conn, "Other", NOW, slug="other")
    assert names.unique_slug(conn, "Acme", other) == "acme-2"                         # would shadow the redirect
    assert names.unique_slug(conn, "Acme", old) == "acme"                             # its own old slug is fine
    pid = _create_project(conn, ctx, "Acme", "acme", None)
    assert conn.execute(sa.select(db.projects.c.slug).where(db.projects.c.id == pid)).scalar_one() == "acme-2"


def test_retire_keeps_receipts_and_fills_missing_enrichment_on_the_target(conn, ctx):
    a, b = insert_project(conn, "A", NOW, slug="a"), insert_project(conn, "B", NOW, slug="b")
    conn.execute(sa.update(db.projects).where(db.projects.c.id == a).values(
        website="https://a.example", github="https://github.com/a/a", x_handle="a_hooks", enrichment={"team": "A"},
        what_line="Does A things", category="orders"))
    conn.execute(sa.update(db.projects).where(db.projects.c.id == b).values(website="https://b.example", category=None))
    for pid, url in ((a, "https://news.example/1"), (a, "https://news.example/2"), (b, "https://news.example/1")):
        conn.execute(sa.insert(db.receipts).values(project_id=pid, url=url, title="t", source="news.example"))
    names.retire_project(conn, a, into=b, reason="test", now=NOW)
    t = conn.execute(sa.select(db.projects).where(db.projects.c.id == b)).one()
    assert (t.website, t.github, t.x_handle, t.enrichment, t.what_line, t.category) == (
        "https://b.example", "https://github.com/a/a", "a_hooks", {"team": "A"}, "Does A things", "orders")
    urls = sorted(conn.execute(sa.select(db.receipts.c.url).where(db.receipts.c.project_id == b)).scalars())
    assert urls == ["https://news.example/1", "https://news.example/2"]
