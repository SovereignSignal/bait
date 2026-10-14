import math
from datetime import date, datetime, timedelta, timezone

import pytest

from bait.rules import (corroborated, is_symmetric, is_uniform, is_wash_24h, is_wash_7d, is_wash_no_liquidity, is_wash_vol_tvl, vol_tvl,
                        wallets_lower_bound)
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


def test_wallets_lower_bound_never_uses_buyers_plus_sellers():
    assert wallets_lower_bound(7, 534, 511, 1045) == 7            # a distinct count wins
    assert wallets_lower_bound(None, 13, 13, 26) == 13            # OUROBOROS/CRCL: at least 13 wallets, not 26
    assert wallets_lower_bound(None, 5, None, None) == 5
    assert wallets_lower_bound(None, None, None, 27) == 14        # rows with buyers + sellers only: at least half
    assert wallets_lower_bound(None, None, None, None) is None


def test_wash_24h_counts_a_wallet_that_bought_and_sold_once():
    assert is_wash_24h(300_000, wallets_lower_bound(None, 13, 13, 26))      # 26 "traders" used to pass the <= 25 test
    assert not is_wash_24h(249_999, 1) and not is_wash_24h(300_000, 26) and not is_wash_24h(300_000, None)
    assert not is_wash_24h(None, 1)


def test_wash_7d_needs_corroboration_not_few_wallets_alone():
    assert vol_tvl(100.0, 2.0) == 50.0 and vol_tvl(1.0, 0.0) is None and vol_tvl(1.0, None) is None
    assert is_wash_7d(436_000_000, 1, tvl=50_000, symmetric=False)            # 8,720x its TVL
    assert not is_wash_7d(5_000_000, 4, tvl=2_000_000, symmetric=False)       # private / RFQ hook: real liquidity
    assert is_wash_7d(5_000_000, 4, tvl=2_000_000, symmetric=True)
    assert not is_wash_7d(5_000_000, 4, tvl=None, symmetric=False)            # TVL unknown: no ratio
    assert not is_wash_7d(999_999, 1, tvl=1, symmetric=True) and not is_wash_7d(5_000_000, 11, tvl=1, symmetric=True)
    assert corroborated(5_100, 100, False) and not corroborated(5_000, 100, False) and corroborated(None, None, True)


def test_no_liquidity_corroborates_and_flags_the_0x69af_shape():
    """Optimism 0x69af: $436M through 8 swaps from one wallet at a known $0 TVL. The ratio is undefined at $0."""
    assert corroborated(436_000_000, 0.0, False) and corroborated(1_000_000, 1_000.0, False)
    assert not corroborated(999_999, 0.0, False) and not corroborated(436_000_000, None, False)   # unknown TVL never
    assert is_wash_7d(436_000_000, 1, tvl=0.0, symmetric=False)
    assert is_wash_no_liquidity(436_000_000, 0.0, 1)
    assert not is_wash_no_liquidity(436_000_000, None, 1) and not is_wash_no_liquidity(436_000_000, 1_001.0, 1)
    assert not is_wash_no_liquidity(999_999, 0.0, 1) and not is_wash_no_liquidity(436_000_000, 0.0, 50)
    assert not is_wash_no_liquidity(436_000_000, 0.0, None)


def test_vol_tvl_flags_robinhood_bots_not_base_launchpads():
    assert is_wash_vol_tvl(866 * 10_000, 10_000, wallets_lower_bound(None, 13, 13, 26))     # OUROBOROS/CRCL
    assert not is_wash_vol_tvl(345 * 200_000, 200_000, 30_000)                                  # Base 0x0f48 family
    assert not is_wash_vol_tvl(301 * 10, 10, 3)                                                 # under $10K: ranks nowhere
    assert not is_wash_vol_tvl(1e9, 0.0, 1) and not is_wash_vol_tvl(1e9, None, 1) and not is_wash_vol_tvl(1e9, 1, None)
    assert not is_wash_vol_tvl(1e9, 1, 50)                                                      # "fewer than 50"


def test_symmetric_traders():
    bots = [(50_000.0, 5, 5)] * 10
    assert is_symmetric(bots, 900_000)                       # $500K of $900K from 10 wallets, each 5 buys / 5 sells
    assert not is_symmetric(bots, 1_000_000)                 # exactly half is not more than half
    assert not is_symmetric(bots[:9] + [(50_000.0, 6, 5)], 900_000)
    assert not is_symmetric([(50_000.0, 5, 5)], 60_000)      # one wallet is not a pattern
    assert not is_symmetric(bots, None) and not is_symmetric([(5_000.0, 1, 1)] * 2, 9_000)    # under $10K
    assert is_symmetric(bots + [(1.0, 1, 0)], 900_000)       # only the top 10 by volume are judged


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
               "Curve", "VaultHook", "OrderHook", "PointsHook", "Base Pair", "Arb Labs"):     # bare words: real names too
        assert useful_name(ok, max_len=None) == ok, ok
    assert useful_name("BaseHook") is None and useful_name("BaseTestHooks") is None       # the template class names
    assert useful_name("ArbHook") is None and useful_name("Curve Hook") is None             # template example hooks


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


def test_a_small_delegatecalling_runtime_of_no_known_shape_is_unknown():
    from types import SimpleNamespace
    from bait.rules import code_evidence, is_proxy_code, proxy_kind
    small = "0x363d3d37" + "73" + "ab" * 20 + "5af4" + "3d82803e" + "00" * 50     # forwards with DELEGATECALL (f4)
    assert proxy_kind(small) == "unknown" and not is_proxy_code(small)
    assert proxy_kind(small + "00" * 1100) is None                                  # a full contract may delegatecall
    assert proxy_kind("0x6080604052") is None
    assert code_evidence(SimpleNamespace(code_hash="0xd", is_proxy=None, proxy_kind="unknown")) is None


def test_a_project_that_moved_a_million_is_never_spam():
    assert is_spam([(0, 8)]) is True                                  # $0 TVL, 8 swaps lifetime
    assert is_spam([(0, 8)], volume=436_000_000) is False             # ...but $436M moved (Optimism 0x69af)
    assert is_spam([(0, 8)], volume=999_999) is True
    assert is_spam([], volume=5_000_000) is True                      # no pools is still spam


def test_symmetric_needs_repeated_round_trips_or_several_wallets():
    assert not is_symmetric([(50_000.0, 1, 1)] * 2, 90_000)          # two wallets, one round trip each: could be anyone
    assert not is_symmetric([(50_000.0, 2, 2)] * 4, 190_000)         # four wallets, two round trips each
    assert is_symmetric([(50_000.0, 3, 3)] * 2, 90_000)              # three round trips each
    assert is_symmetric([(20_000.0, 1, 1)] * 5, 190_000)             # five wallets


def test_uniform_flags_one_size_fresh_wallets():
    # Hookr MOW/WETH 10/13: top 10 each one buy of $22,450-$22,515; $17.1M from 809 wallets in 24h
    top = [(22_515 - i * 7, 1, 0) for i in range(10)]
    assert is_uniform(top, 17_100_000, 809)
    # sells of the same size count too
    assert is_uniform([(22_500 - i * 5, i % 2, 1 - i % 2) for i in range(10)], 17_100_000, 809)


def test_uniform_spares_a_buy_cap_launch_and_real_trading():
    capped = [(5_000, 1, 0)] * 10
    assert not is_uniform(capped, 2_000_000, 2_000)          # $1K per wallet: the cap is the top, not the pool
    assert not is_uniform([(22_500 * (1 + i / 10), 1, 0) for i in range(10)], 17_100_000, 809)   # sizes spread out
    assert not is_uniform([(22_500, 2, 1)] * 10, 17_100_000, 809)                                 # repeat traders
    assert not is_uniform([(22_500, 1, 0)] * 7, 17_100_000, 809)                                  # too few to be a pattern
    assert not is_uniform([(22_500, 1, 0)] * 10, 900_000, 40)                                     # under $1M
    assert not is_uniform([(22_500, 1, 0)] * 10, 17_100_000, None)                                # wallets unknown
