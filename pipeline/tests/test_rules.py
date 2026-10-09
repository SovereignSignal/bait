import math
from datetime import date, datetime, timedelta, timezone

import pytest

from bait.rules import eligibility, flags_from_address, is_dynamic_fee, is_new, is_spam, normalize_name, routable, score_trending, slugify


def hook(address="0xabc0000000000000000000000000000000000080", flags=None, dynamic=False):
    return {"hook": {"address": address}, "flags": flags or {}, "properties": {"dynamicFee": dynamic}}


def test_routable_plain_hook():
    assert routable(hook(flags={"beforeSwap": True, "afterSwap": True})) is True


def test_routable_rejects_0x91_prefix_case_insensitive():
    assert routable(hook(address="0x91aa000000000000000000000000000000000000")) is False
    assert routable(hook(address="0X91AA000000000000000000000000000000000000")) is False
    assert routable(hook(address="0x19aa000000000000000000000000000000000000")) is True


@pytest.mark.parametrize("flag,bit", [
    ("beforeSwapReturnsDelta", 3), ("afterSwapReturnsDelta", 2),
    ("afterAddLiquidityReturnsDelta", 1), ("afterRemoveLiquidityReturnsDelta", 0),   # DESIGN.md house rule, stricter than Labs
])
def test_routable_rejects_delta_address_bits(flag, bit):
    base = 0xabc0000000000000000000000000000000000080
    assert routable(hook(address=hex(base | 1 << bit))) is False
    assert routable(hook(address=hex(base), flags={flag: True})) is True     # declared flags are informational; the bits win


def test_routable_rejects_dynamic_fee():
    assert routable(hook(dynamic=True)) is False


def test_routable_fixture_shape(fixture_entries):
    bunni = fixture_entries[0]
    assert bunni["hook"]["name"] == "BunniHook" and bunni["flags"]["beforeSwapReturnsDelta"]
    assert routable(bunni) is False
    assert any(routable(e) for e in fixture_entries)


def test_dynamic_fee_bit():
    assert is_dynamic_fee(0x800000) and is_dynamic_fee(8388608) and not is_dynamic_fee(3000) and not is_dynamic_fee(None)


def test_normalize_name():
    assert normalize_name("BunniHook") == "bunni"
    assert normalize_name("Phi ETH Hooks") == "phieth"
    assert normalize_name("EulerSwap") == "eulerswap"
    assert normalize_name("Hook") == "hook"          # don't strip to empty
    assert normalize_name("") == "" and normalize_name(None) == ""
    assert slugify("Bunni Hook v2!") == "bunni-hook-v2"


def test_score_trending_hand_computed():
    # vol_7d 100k, prev 50k -> ratio 2.0 -> log1p(100000) * 2
    expected = math.log1p(100_000) * 2.0
    assert score_trending(100_000, 50_000) == pytest.approx(expected)
    assert expected == pytest.approx(23.0259, abs=1e-3)


def test_score_trending_clamps_and_floor():
    assert score_trending(9_999, 0) == 0.0                                         # below $10k: not trending
    assert score_trending(10_000, 0) == 0.0                                         # no prior week: growth undefined
    assert score_trending(1_000_000, 10) == 0.0                                     # prior week under $1k: same
    assert score_trending(1_000_000, 1_000) == pytest.approx(math.log1p(1_000_000) * 10.0)   # clamp high
    assert score_trending(20_000, 1_000_000) == pytest.approx(math.log1p(20_000) * 0.5)   # clamp low


def test_is_new():
    day = date(2026, 10, 6)
    fresh = datetime(2026, 10, 3, tzinfo=timezone.utc)
    old = datetime(2026, 9, 20, tzinfo=timezone.utc)
    boundary = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)   # exactly day-7
    assert is_new(fresh, day, max_pool_tvl=5_000, swaps_7d=0) is True
    assert is_new(fresh, day, max_pool_tvl=0, swaps_7d=10) is True
    assert is_new(fresh, day, max_pool_tvl=1_000, swaps_7d=9) is False      # needs > $1k or >= 10
    assert is_new(old, day, max_pool_tvl=1e9, swaps_7d=1e9) is False
    assert is_new(boundary, day, max_pool_tvl=5_000, swaps_7d=0) is True
    assert is_new(boundary - timedelta(seconds=1), day, max_pool_tvl=5_000, swaps_7d=0) is False


def test_is_spam():
    assert is_spam([]) is True
    assert is_spam([(50, 3), (99.9, 9)]) is True
    assert is_spam([(50, 3), (100, 0)]) is False
    assert is_spam([(0, 10)]) is False


def test_flags_from_address_decodes_all_fourteen_bits():
    f = flags_from_address("0x69af000000000000000000000000000000003fff")
    assert all(f.values()) and len(f) == 14
    g = flags_from_address("0x720e000000000000000000000000000000002080")      # beforeInitialize + beforeSwap
    assert {k for k, v in g.items() if v} == {"beforeInitialize", "beforeSwap"}
    assert flags_from_address("0X0000000000000000000000000000000000000000") == {k: False for k in g}


def test_eligibility_unknown_without_pool_fee_or_dynamic_key():
    h = {"hook": {"address": "0x5555000000000000000000000000000000000080"}, "properties": {}}
    assert eligibility(h) == "unknown" and eligibility(h, 3000) == "eligible" and eligibility(h, 0x800000) == "ineligible"


def test_code_hash_is_0x_sha256_of_the_bytes():
    import hashlib
    from bait.rules import code_hash
    want = "0x" + hashlib.sha256(bytes.fromhex("6080604052")).hexdigest()
    assert code_hash("0x6080604052") == code_hash("6080604052") == code_hash("0X6080604052") == want
    assert len(want) == 66
    for empty in (None, "", "0x", "0xzz"):
        assert code_hash(empty) is None


def test_useful_name_and_proxy_bytecode():
    from bait.rules import is_proxy_code, useful_name
    assert useful_name("FoundationHookV2") == "FoundationHookV2"
    assert useful_name("x" * 61) is None and useful_name("Long " + "x" * 70, max_len=None) == "Long " + "x" * 70
    for g in ("ERC1967Proxy", "FeeHook", "Counter", " UniswapV4Hook ", "", None):
        assert useful_name(g, max_len=None) is None
    slot = "360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
    assert is_proxy_code("0x6080604052" + slot.upper() + "5490")                    # EIP-1967 implementation slot
    assert is_proxy_code("0x363d3d373d3d3d363d73" + "ab" * 20 + "5af43d82803e903d91602b57fd5bf3")   # EIP-1167 clone
    assert not is_proxy_code("0x6080604052") and not is_proxy_code(None)


def test_merge_evidence_reads_the_proxy_flag_not_the_name():
    from types import SimpleNamespace
    from bait.rules import merge_evidence

    def h(code=None, dep=None, name=None, cname=None, proxy=False):
        return SimpleNamespace(code_hash=code, deployer=dep, name=name, contract_name=cname, is_proxy=proxy)
    assert merge_evidence([h(cname="OrcaHook")], [h(cname="OrcaHook")]) is None          # a name alone is not evidence
    assert merge_evidence([h(code="0xa")], [h(code="0xa")]) == "code_hash"
    assert merge_evidence([h(code="0xp", proxy=True)], [h(code="0xp", proxy=True)]) is None   # proxy bytecode is shared
    assert merge_evidence([h(code="0xp", cname="OrcaHook", proxy=True)], [h(code="0xp")]) is None  # resolved impl name
    assert merge_evidence([h(code="0xu", proxy=None)], [h(code="0xu", proxy=None)]) is None   # status unknown: no evidence
    assert merge_evidence([h(dep="0xd")], [h(dep="0xd")]) == "deployer"
    create2 = "0x4e59b44847b379578588920ca78fbf26c0b4956c"
    assert merge_evidence([h(dep=create2)], [h(dep=create2)]) is None                     # CREATE2 factory deploys for everyone
    assert merge_evidence([h(name="Bunni Hooks")], [h(name="BunniHook")]) is None        # a shared hooklist name alone neither
    assert merge_evidence([h(name="FeeHook")], [h(name="FeeHook")]) is None                # template name


def test_generic_names_match_after_normalising():
    from bait.rules import useful_name
    for g in ("Limit Order Hook", "LimitOrderHookV2", "Black List Hook", "TWAMM", "FullRange", "GeomeanOracle",
              "Arbitrage Hook", "Example", "My New Hook", "Hook", "Hooks", "Transparent Upgradeable Proxy", "Dynamic-Fee Hook"):
        assert useful_name(g, max_len=None) is None, g
    for ok in ("OrderBookHook", "Arbitrum Hook", "Testudo", "FoundationHookV2", "Limit Order Hook (Arbitrum)", "Bunni",
               "CurveHook", "VaultHook", "OrderHook", "PointsHook", "Base Pair", "ArbHook"):   # bare words: real names too
        assert useful_name(ok, max_len=None) == ok, ok
    assert useful_name("BaseHook") is None and useful_name("BaseTestHooks") is None       # the template class names


def test_proxy_bytecode_needs_the_proxy_shape_not_just_the_constant():
    from types import SimpleNamespace
    from bait.rules import code_evidence, is_proxy_code, proxy_kind
    slot = "360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
    clone = "0x363d3d373d3d3d363d73" + "ab" * 20 + "5af43d82803e903d91602b57fd5bf3"           # real EIP-1167, 45 bytes
    assert len(clone) == 2 + 90 and proxy_kind(clone) == "eip1167" and is_proxy_code(clone)
    clones_user = ("0x6080604052" + "00" * 100 + "3d602d80600a3d3981f3363d3d373d3d3d363d73"   # OZ Clones embeds the
                   + "00" * 300)                                                             # clone code as a constant
    assert not is_proxy_code(clones_user)
    uups_impl = "0x6080604052" + slot + "00" * 3000                                          # UUPS implementation
    assert not is_proxy_code(uups_impl)
    erc1967 = "0x6080604052" + slot + "00" * 200                                             # real ERC1967 proxy
    assert proxy_kind(erc1967) == "eip1967"

    def row(kind):
        return SimpleNamespace(code_hash="0xc", is_proxy=True, proxy_kind=kind)
    assert code_evidence(row("eip1167")) == "0xc"          # same clone hash = same implementation: evidence
    assert code_evidence(row("eip1967")) is None and code_evidence(row(None)) is None
