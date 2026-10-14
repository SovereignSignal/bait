"""Same-team merge (group rule 4): projects whose names share a stem merge only with a shared signal that one name root
owns. Fixtures are the live 10/14 families: hooklist names, and per-hook bytecode templates (code_shape) and deployers
as read from public RPC and the hooklist that day."""
from datetime import timedelta

import pytest
import sqlalchemy as sa

from bait import db
from bait.rules import audit_key, name_stem, normalize_name, slugify, stem_root, team_links
from bait.steps import group
from tests.conftest import NOW


def _proj(conn, name, *, contract=None, first_seen=NOW, website=None, github=None, x=None, enriched=False):
    """A curated project keyed by its hooklist name, or with contract=<chain> a verified-source one."""
    norm = f"contract:{contract}:{normalize_name(name)}" if contract else normalize_name(name)
    r = conn.execute(sa.insert(db.projects).values(
        slug=slugify(name), name=name, norm_name=norm, first_seen_at=first_seen, website=website, github=github,
        x_handle=x, enriched_at=NOW if enriched else None))
    return r.inserted_primary_key[0]


_n = iter(range(1, 10_000))


def _hook(conn, pid, chain, shape=None, deployer=None, audit=None, proxy=False, name=None):
    addr = "0x%040x" % next(_n)
    conn.execute(sa.insert(db.hooks).values(
        address=addr, chain_id=chain, project_id=pid, name=name, code_hash="0xh" + addr, is_proxy=proxy,
        code_shape=shape, deployer=deployer, audit_url=audit, first_seen_at=NOW, flags={}, properties={}))
    return addr


def _of(conn, pid):
    return conn.execute(sa.select(db.projects).where(db.projects.c.id == pid)).one_or_none()


def _home(conn, addr):
    return conn.execute(sa.select(db.hooks.c.project_id).where(db.hooks.c.address == addr)).scalar_one()


def _aliases(conn):
    return dict(conn.execute(sa.select(db.project_aliases.c.old_slug, db.project_aliases.c.project_id)).all())


@pytest.mark.parametrize("name,stem", [
    ("Aegis", "aegis"), ("AegisHook", "aegis"), ("Aegis v2 Hook (Unichain)", "aegis"), ("Aegis v3 Hook (Base)", "aegis"),
    ("Aegis DFM", "aegisdfm"),
    ("Clanker Static Fee Hook (Arbitrum)", "clanker"), ("ClankerHookDynamicFeeV2", "clanker"),
    ("Clanker Dynamic Fee Hook (V2)", "clanker"), ("ClankerCompatibleHook", "clankercompatible"),
    ("SUKOClankerHook", "sukoclanker"),
    ("Zora Hook", "zora"), ("Zora Post Hook v2.3.0 (Base)", "zorapost"), ("ZoraV4CoinHook", "zoracoin"),
    ("Angstrom", "angstrom"), ("AngstromL2", "angstrom"), ("AngstromL2 (Base)", "angstrom"),
    ("Arrakis Private Hook (Ethereum)", "arrakisprivate"), ("ArrakisPrivateHook", "arrakisprivate"),
    ("FablesRampETH", "fablesrampeth"),                    # ETH is an asset, not a chain
    ("LendingHookV4 (Ethereum)", "lending"), ("Foo Hook (X Layer)", "foo"),
    ("LimitOrderHook", None), ("Dynamic Fee Hook", None), ("Unlisted hook 0x1234…abcd", None),
    ("FoundationHookV2 0x022b…20cc", None),                # names.py's two-teams suffix
])
def test_name_stem(name, stem):
    assert name_stem(name) == stem


def test_stem_root_and_link_keys():
    assert stem_root("Zora Post Hook v2.3.0 (Base)") == stem_root("ZoraV4CoinHook") == "zora"
    assert stem_root("FoundationHookV2 0x022b…20cc") == "foundation"
    assert team_links("https://www.Aegis.markets/app", "https://github.com/labs-solo/AEGIS_DFM", "@AegisMarkets") == {
        "website": "aegis.markets", "github": "labs-solo", "x": "aegismarkets"}
    assert team_links("https://github.com/foo", "https://github.com/Uniswap/v4-core", "uniswap") == {}
    assert audit_key("https://github.com/a/b/audit.pdf?raw=1") == audit_key("http://github.com/a/b/audit.pdf/")


def test_aegis_links_merge_v2_and_chain_twins_merge_by_template(conn, ctx):
    links = dict(website="https://aegis.markets/", github="https://github.com/labs-solo/AEGIS_DFM", x="AegisMarkets")
    main = _proj(conn, "Aegis", enriched=True, first_seen=NOW - timedelta(days=3), **links)
    for chain in (1, 8453, 57073, 4663):
        _hook(conn, main, chain, shape="0xfcd4")
    v2 = _proj(conn, "Aegis v2 Hook (Unichain)", enriched=True, first_seen=NOW - timedelta(days=5),
               website="https://aegis.markets", github=links["github"], x=links["x"])
    _hook(conn, v2, 130, shape="0x737d")
    v3u = _proj(conn, "Aegis v3 Hook (Unichain)")
    v3u_hook = _hook(conn, v3u, 130, shape="0xef66")
    v3b = _proj(conn, "Aegis v3 Hook (Base)")
    v3b_hook = _hook(conn, v3b, 8453, shape="0xef66")         # same template on another chain
    v1 = _proj(conn, "Aegis v1 Hook (Unichain)")
    _hook(conn, v1, 130, shape="0x7f27")                       # nothing shared: stays its own card
    out = group.run(conn, ctx)
    assert out["team_merged"] == 2
    p = _of(conn, main)
    assert (p.name, p.slug, db.as_utc(p.first_seen_at)) == ("Aegis", "aegis", NOW - timedelta(days=5))   # the oldest
    assert _of(conn, v2) is None and _aliases(conn)["aegis-v2-hook-unichain"] == main
    keep = _home(conn, v3u_hook)
    assert keep == _home(conn, v3b_hook) != main
    assert _of(conn, keep).name == "Aegis v3 Hook"                  # spans chains: the chain word goes
    assert _aliases(conn)["aegis-v3-hook-unichain"] == _aliases(conn)["aegis-v3-hook-base"] == keep
    assert _of(conn, v1).name == "Aegis v1 Hook (Unichain)"
    assert group.run(conn, ctx)["team_merged"] == 0                 # idempotent


def test_clanker_variants_become_one_project_by_deployer_and_template(conn, ctx):
    dep = "0x3b5aedd9ab921e592d2fccaa6e33cf893e7eeb70"
    fam = {
        "ClankerHookStaticFee": [(8453, "0x4d46", dep), (56, "0x4d46", None), (1, "0x716f", None)],
        "ClankerHookStaticFeeV2": [(8453, "0x30ad", dep), (4663, "0xd3bf", None)],
        "ClankerHookDynamicFee": [(8453, "0xc1ba", dep)],
        "ClankerHookDynamicFeeV2": [(8453, "0x4b08", dep)],
        "Clanker Static Fee Hook (Arbitrum)": [(42161, "0x4d46", None)],
        "Clanker Static Fee Hook (Base)": [(8453, "0x4d46", None)],
        "Clanker Static Fee Hook v2 (Base)": [(8453, "0x30ad", None)],
        "Clanker Static Fee Hook (V2)": [(4663, "0x30ad", None)],
        "Clanker Dynamic Fee Hook (Arbitrum)": [(42161, "0xc1ba", None)],
        "Clanker Dynamic Fee Hook v2 (Base)": [(8453, "0x4b08", None)],
    }
    pids = {}
    for name, hs in fam.items():
        enr = dict(website="https://clanker.world/", github="https://github.com/clanker-devco/v4-contracts",
                   x="clanker_world", enriched=True) if name == "Clanker Static Fee Hook v2 (Base)" else {}
        pids[name] = _proj(conn, name, **enr)
        for chain, shape, d in hs:
            _hook(conn, pids[name], chain, shape=shape, deployer=d)
    lone = _proj(conn, "ClankerHookV2")
    _hook(conn, lone, 8453, shape="0x4955")                    # same stem, nothing shared
    compat = _proj(conn, "ClankerCompatibleHook")
    _hook(conn, compat, 8453, shape="0xb05b", deployer=dep)    # shares the deployer, but another stem
    out = group.run(conn, ctx)
    assert out["team_merged"] == len(fam) - 1
    homes = {_home(conn, a) for (a,) in conn.execute(sa.select(db.hooks.c.address).where(
        db.hooks.c.project_id.not_in([lone, compat])))}
    assert len(homes) == 1
    keep = _of(conn, homes.pop())
    assert keep.id == pids["Clanker Static Fee Hook v2 (Base)"]     # curated and the richest enrichment
    assert (keep.name, keep.slug, keep.website) == ("Clanker Hook", "clanker-hook", "https://clanker.world/")
    aliases = _aliases(conn)
    assert aliases["clanker-static-fee-hook-v2-base"] == aliases["clankerhookstaticfeev2"] == keep.id
    assert aliases["clanker-dynamic-fee-hook-arbitrum"] == keep.id
    assert _of(conn, lone) is not None and _of(conn, compat) is not None
    assert group.run(conn, ctx)["team_merged"] == 0


def test_arrakis_and_angstrom(conn, ctx):
    arrakis = _proj(conn, "ArrakisPrivateHook", website="https://www.arrakis.finance", enriched=True)
    for chain in (42161, 8453, 1):
        _hook(conn, arrakis, chain, shape="0x75f7")
    _hook(conn, arrakis, 1, shape="0x04ad")
    eth = _proj(conn, "Arrakis Private Hook (Ethereum)")
    _hook(conn, eth, 1, shape="0x75f7")
    base = _proj(conn, "Arrakis Private Hook (Base)")
    _hook(conn, base, 8453, shape="0x75f7")

    angstrom = _proj(conn, "Angstrom", website="https://angstrom.xyz/", enriched=True)
    _hook(conn, angstrom, 1, shape="0xaafe")
    l2 = _proj(conn, "AngstromL2")
    for _ in range(3):
        _hook(conn, l2, 8453, shape="0x4b15")
    l2_found = _proj(conn, "AngstromL2 (Base)", contract=8453)     # names.py's verified-source twin of a curated name
    l2_hook = _hook(conn, l2_found, 8453, shape="0x4b15")
    out = group.run(conn, ctx)
    assert out["team_merged"] == 3
    assert _of(conn, eth) is None and _of(conn, base) is None and _of(conn, arrakis).name == "ArrakisPrivateHook"
    assert _aliases(conn)["arrakis-private-hook-ethereum"] == arrakis
    assert _home(conn, l2_hook) == l2 and _of(conn, l2).name == "AngstromL2"   # "Angstrom" is taken: no rename
    assert _of(conn, angstrom).name == "Angstrom"                               # mainnet Angstrom shares nothing


def test_zora_stems_differ_and_versions_without_a_signal_stay_apart(conn, ctx):
    hook = _proj(conn, "Zora Hook")
    _hook(conn, hook, 8453, shape="0x91b2")
    coin = _proj(conn, "ZoraV4CoinHook")
    _hook(conn, coin, 8453, shape="0x91b2")                    # same template, but stem zoracoin != zora
    p23 = _proj(conn, "Zora Post Hook v2.3.0 (Base)")
    _hook(conn, p23, 8453, shape="0x11fe")
    p24 = _proj(conn, "Zora Post Hook v2.4.0 (Base)")
    _hook(conn, p24, 8453, shape="0x833c")                     # same stem, different code, no other signal
    assert group.run(conn, ctx)["team_merged"] == 0
    assert all(_of(conn, p) is not None for p in (hook, coin, p23, p24))


def test_two_unrelated_launch_hooks_never_merge(conn, ctx):
    # Name alone: two verified-source "LaunchHook"s on two chains, own deployers, own bytecode.
    a = _proj(conn, "LaunchHook", contract=8453)
    _hook(conn, a, 8453, shape="0xaaaa", deployer="0x" + "1" * 40)
    b = _proj(conn, "LaunchHook (Robinhood)", contract=4663)
    _hook(conn, b, 4663, shape="0xbbbb", deployer="0x" + "2" * 40)
    # A shared CREATE2 factory is no team.
    c = _proj(conn, "Launch Hook V2", contract=1)
    _hook(conn, c, 1, shape="0xcccc", deployer="0x4e59b44847b379578588920ca78fbf26c0b4956c")
    d = _proj(conn, "Launch Hook (Base)")
    _hook(conn, d, 8453, shape="0xdddd", deployer="0x4e59b44847b379578588920ca78fbf26c0b4956c")
    # An OpenZeppelin-style template that another name also deploys belongs to nobody.
    e = _proj(conn, "LaunchHook (Ethereum)", contract=1)
    _hook(conn, e, 1, shape="0xeeee")
    f = _proj(conn, "Launch Hook (Unichain)")
    _hook(conn, f, 130, shape="0xeeee")
    g = _proj(conn, "MemeRocketHook")
    _hook(conn, g, 8453, shape="0xeeee")
    # One shared link is the name repeated by a web search; two links that disagree on the third are two teams.
    h = _proj(conn, "Launch Hook (Arbitrum)", website="https://launch.fun", enriched=True)
    _hook(conn, h, 42161, shape="0x1111")
    i = _proj(conn, "Launch Hook (Ink)", website="https://launch.fun", enriched=True)
    _hook(conn, i, 57073, shape="0x2222")
    j = _proj(conn, "Launch Hook (Optimism)", website="https://launch.fun", github="https://github.com/one", x="a",
              enriched=True)
    _hook(conn, j, 10, shape="0x3333")
    k = _proj(conn, "Launch Hook (Monad)", website="https://launch.fun", github="https://github.com/one", x="b",
              enriched=True)
    _hook(conn, k, 143, shape="0x4444")
    # Proxy bytecode is shared by unrelated deployments.
    m = _proj(conn, "Launch Hook (Polygon)")
    _hook(conn, m, 137, shape="0x5555", proxy=True)
    n = _proj(conn, "Launch Hook (Avalanche)")
    _hook(conn, n, 43114, shape="0x5555", proxy=True)
    assert group.run(conn, ctx)["team_merged"] == 0
    assert conn.execute(sa.select(sa.func.count()).select_from(db.projects)).scalar_one() == 13


def test_generic_and_unlisted_projects_never_take_part(conn, ctx):
    a = _proj(conn, "LimitOrderHook")
    _hook(conn, a, 1, shape="0xf00d", deployer="0x" + "3" * 40)
    b = _proj(conn, "LimitOrderHook (Base)", contract=8453)
    _hook(conn, b, 8453, shape="0xf00d", deployer="0x" + "3" * 40)
    u1 = _proj(conn, "Unlisted hook 0x0000…0001")
    _hook(conn, u1, 1, deployer="0x" + "4" * 40)
    u2 = _proj(conn, "Unlisted hook 0x0000…0002")
    _hook(conn, u2, 8453, deployer="0x" + "4" * 40)
    assert group.run(conn, ctx)["team_merged"] == 0


def test_audit_url_merges_and_receipts_and_enrichment_follow(conn, ctx):
    audit = "https://github.com/bonker-wtf/contracts/blob/main/audit/CLAWDIT-Bonker-Audit.pdf"
    s = _proj(conn, "Bonker Static Fee Hook (Arc)")
    _hook(conn, s, 5042, audit=audit)
    d = _proj(conn, "Bonker Dynamic Fee Hook (Arc)", website="https://bonker.wtf", github="https://github.com/bonker-wtf",
              enriched=True)
    _hook(conn, d, 5042, audit=audit)
    conn.execute(sa.insert(db.receipts).values(project_id=s, url="https://news.example/bonker", title="Bonker launches"))
    out = group.run(conn, ctx)
    assert out["team_merged"] == 1
    keep = _of(conn, d)                                         # the enriched one is kept
    assert (keep.name, keep.website) == ("Bonker Hook (Arc)", "https://bonker.wtf")   # one chain: it stays in the name
    assert conn.execute(sa.select(db.receipts.c.project_id)).scalar_one() == d
    assert _aliases(conn) == {"bonker-static-fee-hook-arc": d, "bonker-dynamic-fee-hook-arc": d}


def test_new_hooklist_name_folds_in_the_same_night(conn, ctx):
    """Rule 4 runs after rule 1, so a hook under a new per-chain name never shows as its own card for a night."""
    dep = "0x" + "5" * 40
    main = _proj(conn, "Aegis")
    _hook(conn, main, 8453, deployer=dep)
    new = _hook(conn, None, 57073, deployer=dep, name="Aegis v4 Hook (Ink)")
    out = group.run(conn, ctx)
    assert out["created"] == 1 and out["team_merged"] == 1 and _home(conn, new) == main


def test_shapes_fill_rule4_candidates_before_newer_hooks(conn, ctx, monkeypatch):
    """A merge that waits on a bytecode template should not wait behind thousands of newer launchpad hooks."""
    a = _proj(conn, "Clanker Static Fee Hook (Base)")
    ha = _hook(conn, a, 8453)
    b = _proj(conn, "Clanker Static Fee Hook (Arbitrum)")
    hb = _hook(conn, b, 42161)
    other = _proj(conn, "MemeRocketHook")
    newer = _hook(conn, other, 8453)
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == newer).values(first_seen_at=NOW + timedelta(days=1)))
    asked = []

    def fake(ctx, rows, deadline):
        asked.extend(r.address for r in rows)
        return {(r.chain_id, r.address): "0xS" for r in rows}
    monkeypatch.setattr(group, "_rpc_shapes", fake)
    monkeypatch.setenv("SHAPE_MAX", "2")
    out = group.run(conn, ctx)
    assert sorted(asked) == sorted([ha, hb]) and out["team_merged"] == 1      # shaped and merged the same night
    group.run(conn, ctx)
    assert asked[2:] == [newer]


def test_shapes_fill_query_has_no_constant_order_term_without_candidates(conn, ctx, monkeypatch):
    """With no rule 4 candidates the shape query must not carry a bare `true` in ORDER BY: Postgres 15+ rejects it as a
    non-integer constant, and SQLite (which these tests run on) does not, so compile it for Postgres here."""
    from sqlalchemy.dialects import postgresql
    _hook(conn, _proj(conn, "MemeRocketHook"), 8453)
    seen = []

    @sa.event.listens_for(conn, "before_execute")
    def grab(c, stmt, *_a):
        if isinstance(stmt, sa.Select) and "code_shape IS NULL" in str(stmt):
            seen.append(str(stmt.compile(dialect=postgresql.dialect())))
    monkeypatch.setattr(group, "_rpc_shapes", lambda ctx, rows, deadline: {})
    group._fill_shapes(conn, ctx, {"shapes_filled": 0}, set())
    assert len(seen) == 1
    order = seen[0].split("ORDER BY", 1)[1]
    assert "true" not in order.lower() and "NOT IN" not in order
