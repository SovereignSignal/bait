"""Unlisted hooks named from verified source: lookup caps/rechecks, rename, per-chain merge, curated clash, generic names."""
from datetime import timedelta

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
        self.routes, self.urls = routes, []

    def get(self, url, params=None):
        self.urls.append(url)
        for frag, resp in self.routes.items():
            if frag in url:
                return resp
        return _Resp(404, {})


def test_lookup_order_and_sources():
    h = _Http({"sourcify.dev": _Resp(404, {}), "base.blockscout.com": _Resp(403, "challenge"),
               "api.blockscout.com/8453": _Resp(200, {"name": "ProHook", "is_verified": True})})
    assert ContractNames("k", http_client=h).lookup(8453, A(1)) == ("ProHook", "blockscout_pro")
    h = _Http({"sourcify.dev": _Resp(200, {"compilation": {"name": "SrcHook"}})})
    assert ContractNames(None, http_client=h).lookup(1, A(1)) == ("SrcHook", "sourcify")
    assert len(h.urls) == 1
    h = _Http({"routescan.io": _Resp(200, {"result": [{"ContractName": "SteadyHook"}]})})
    assert ContractNames(None, http_client=h).lookup(43114, A(1)) == ("SteadyHook", "routescan")
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
