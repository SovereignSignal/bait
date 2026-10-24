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


def test_fallback_category_reads_words_then_factory_pattern_then_dynamic_fee():
    from bait.rules import fallback_category
    assert fallback_category(["LimitOrderHook", None], [{}], [], False, 1) == "orders"
    assert fallback_category(["Euler Lending Hook"], [{}], [], False, 1) == "lending"
    assert fallback_category(["Angstrom"], [{}], [], False, 3) == "mev-lvr"
    assert fallback_category(["KYC gated pool"], [{}], [], False, 1) == "compliance"
    assert fallback_category(["Unlisted hook 0x1234…abcd"], [{}], [], True, 5) == "launchpad"
    assert fallback_category(["Unlisted hook 0x1234…abcd"], [{}], [3000], False, 2_500) == "launchpad"
    assert fallback_category(["Fee Thing"], [{"dynamicFee": True}], [], False, 1) == "dynamic-fee"
    assert fallback_category(["Something"], [{}], [8388608], False, 1) == "dynamic-fee"
    assert fallback_category(["Aegis"], [{}], [3000], False, 2) is None          # nothing says: stays unguessed
    assert fallback_category([], [], [], False, None) is None


# Real hooks and pools from the 2026-10-23 feed (public addresses), all "other" before the 2026-10-24 rules.
ETH = "0x0000000000000000000000000000000000000000"
INK_WETH = "0x4200000000000000000000000000000000000006"
RH_WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
RH_SPCX = "0x4a0e65a3eccec6dbe60ae065f2e7bb85fae35eea"


def _guess(names=(), texts=None, flags=(), pools=()):
    from bait.rules import fallback_category
    return fallback_category(list(names) if texts is None else texts, [{}], [f for _, _, f in pools if f is not None], False,
                             len(pools), names=names, flags=flags, pools=pools)


def test_launch_in_a_hooks_own_name_is_a_launchpad_but_not_in_a_description():
    from bait.rules import flags_from_address
    # LaunchHook (verified contract name) on Robinhood, 0x048f…20cc: its 22 pools pair new coins with several bases, so
    # only the name says it; before this, "Launch Hook" matched no word rule
    hook = flags_from_address("0x048f2dc8a71a0321b5e119cd577c56d0190920cc")
    pools = [(RH_SPCX, "0x5452fcb54894027801cf8852e6acbca2890cb5f7", 0), ("0x7c0495053f47ec52eb4b678c4ad8f572df745bfe", "0x84cab63bc87912e71ad199ff14a0ba45de68fef8", 0),
             ("0x4a3e1c523efbfbda311d4724487e21aadeb8f7ed", "0xad25ac6c84d497db898fa1e8387bf6af3532a1c4", 0), (RH_SPCX, "0x9353ac4dcf13630169e22188894deb20abf40e3a", 0),
             ("0xad25ac6c84d497db898fa1e8387bf6af3532a1c4", "0xf36834a6f6453baf1a1be347a00b30071947f2d5", 0)]
    assert _guess(names=["LaunchHook"], flags=[hook], pools=pools) == "launchpad"
    assert _guess(names=[None], texts=[None], flags=[hook], pools=pools) is None          # unnamed, the pools alone do not say
    # OHMVAULT VaultHook's registry description (Ethereum): "launch" there is a fee schedule, not a launchpad
    desc = "Charges a time-decaying fee (80% at launch, decaying to 5% over 60 seconds) on both swap directions"
    assert _guess(names=[], texts=[desc]) is None
    # PairV5LaunchV2NativeFeeHook: an earlier word rule still wins ("TWAP oracle" in its description)
    assert _guess(names=["PairV5LaunchV2NativeFeeHook"], texts=["PairV5LaunchV2NativeFeeHook", "records tick observations to maintain a 30-minute TWAP oracle"]) == "oracle"


def test_launch_pattern_a_pool_creation_gate_over_one_pool_per_coin():
    from bait.rules import flags_from_address, launch_pattern
    # ClankInitializationGuardHook, Robinhood: two hooks whose only permission is beforeInitialize; ETH against a new
    # coin in 6 of its 7 pools
    gate = [flags_from_address("0x18bff70216af1b93f3552016dec742c5d78ee000"), flags_from_address("0xd405e0dff2d0cc7eaa48b3aec2e3f2456c072000")]
    assert all(set(k for k, v in f.items() if v) == {"beforeInitialize"} for f in gate)
    coins = ["0xfa31d04462746aaa24bc7e5626403e09e4b106ec", "0xb90ad88c8ecd9f22a05cd8cb365542614f279ca9", "0xcf5b720f33febdc1c9d4880b8317203b63fdf368",
             "0x03e6a6881d0813bf6157ac4ee44d3cc73d5465a3", "0x16bfec50d5fe807945d593e56f38c6fb5953bb4e", "0x360462b4d2b48335f86003167a249247edc7061e"]
    pools = [(ETH, c, 3000) for c in coins] + [("0x0fe97b2d477014d78a61829a3c41866912d469ea", RH_SPCX, 3000)]
    assert _guess(texts=[None], flags=gate, pools=pools) == "launchpad"
    assert launch_pattern(gate, pools[:4]) is False                                  # 4 pools is not a platform yet
    # SettlePermissionHook's pools (the same gate, labelled compliance): no shared base, so no launch pattern
    settle = [("0x162a789d397193f3c5509b311533fb271aa42dbd", "0x1b0e319c6a659f002271b69db8a7df2f911c153e", 3000),
              ("0x162a789d397193f3c5509b311533fb271aa42dbd", "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec", 3000),
              (RH_WETH, "0x63d7fa99022794f594f724e7c38ff0be3f9e284a", 3000), (RH_WETH, "0x162a789d397193f3c5509b311533fb271aa42dbd", 3000),
              ("0x162a789d397193f3c5509b311533fb271aa42dbd", RH_SPCX, 3000), (RH_WETH, "0x9fb8ae7eb5889e02802ca403100ba288fb2b9eb2", 3000),
              ("0x020bfc650a365f8bb26819deaabf3e21291018b4", "0x88f714282bf14723ba670cbca811b4718aa8d333", 3000)]
    assert launch_pattern(gate, settle) is False


def test_launch_pattern_fee_taking_hook_on_zero_fee_launch_pools():
    from bait.rules import flags_from_address, launch_pattern
    # SynthHookV2, Robinhood (0x9808…6aec): beforeInitialize, swap deltas, LP fee 0, ETH against a new coin per pool
    synth = [flags_from_address("0x980879a75e09662d923c99ff74bd119ac29c6aec")]
    coins = ["0x0baf47946d97e9f35cbf21d76e41af155f0b7e00", "0x83846775da402622940310aadfe7fce8312c2b5c", "0x6d71d2de7a2292eafa882c1875c31613c6527d10",
             "0x609dc9e984328483d06672e5159d2ec486cc5d83", "0x571672d25bd8647833293c5ed441ff0e8837b20d", "0xd1f3fe2d84abd2832d4dac6fcab2227873a9a6a2"]
    pools = [(ETH, c, 0) for c in coins]
    assert _guess(texts=[None], flags=synth, pools=pools) == "launchpad"
    assert launch_pattern(synth, pools[:-1] + [(ETH, coins[-1], 3000)]) is False    # an LP fee: the hook is not the fee
    # ChimeraHook404 on Ink (0x2eeb…2dcc): exactly 5 pools, WETH in each
    chimera = [flags_from_address("0x2eebd90bf253a27961624762f73eee1409c52dcc")]
    ink = [(INK_WETH, "0xc7d264d906b488ef6660005af5450c979f58006c", 0), (INK_WETH, "0xbff795017ea6f7fbb2cc30cba4a22d123901ef79", 0),
           (INK_WETH, "0x6e1e9bf1f64522ea3fb25ed4d707b2f865f01678", 0), ("0x00e63bb9534551d2e7eed8cce4320b8ef5368634", INK_WETH, 0),
           ("0x19bb33e16f9b01524dcbc30557bc13f2c0309505", INK_WETH, 0)]
    assert launch_pattern(chimera, ink) is True
    # ERC20THook on Ethereum (0xed9a…e544): its platform token FWA is the base in 6 of 7 pools
    fwa = "0xa0df17b5ac76ababa36e1450e2cbcd18a620c845"
    erc20t = [flags_from_address("0xed9a04fa6693e754661a22502524df9b1857e544")]
    eth_pools = [(fwa, "0xbbe9d1ecbae3d811642f3dffdf8bc5ae8ea4514f", 0), ("0x4dec9296365ec243c2638365273393cff82f37e0", fwa, 0),
                 (fwa, "0xb0707bc69b3c7a5e8965a95655b17c8c5816e8f0", 0), (fwa, "0xa639fa428738787388a8290bac439b8ffbf9599a", 0),
                 (fwa, "0xa22aaa38a2f645d6adffe88feb95e147ec6d63f7", 0), (fwa, "0xf86d547990ffb08c4c425eced8e50afe1d270fc3", 0),
                 (ETH, "0x0802671f2478fb6938535f5a25466cb391e88313", 0)]
    assert launch_pattern(erc20t, eth_pools) is True


def test_launch_pattern_needs_the_creation_gate_and_no_after_initialize():
    from bait.rules import flags_from_address, launch_pattern
    usdg = "0xe343167631d89b6ffc58b88d6b7fb0228795491d"
    # tokenized-stock hooks on Ink (0x26b8…8044): the same pool shape (USDG against one stock each, LP fee 0, a swap
    # delta) without beforeInitialize: not a launchpad
    stocks = [flags_from_address("0x26b86c03bd6576b8661d53a48d70faf8c85b8044")]
    xs = [(usdg, "0xe7e553cd128f0011777323a0b44a7b96ea1cb540", 0), ("0x30987adf0b11dc698438a99ba04ec3a1ab2c7eab", usdg, 0),
          ("0xc3fdbe3a68ee5de461d30415a8165cf9aefe1171", usdg, 0), ("0x7d87fd6a379714194a797c0bbb8b40c30d250856", usdg, 0),
          ("0x943bf64d566c32a2bcd41ac92fb63c111cc9de8f", usdg, 0), ("0x4a2df09536f62341c9f946427d16414c04e21342", usdg, 0)]
    assert launch_pattern(stocks, xs) is False
    # 0x5afd…90c4 on Robinhood: AI against a new coin, LP fee 0, swap delta, but afterInitialize and no gate
    ai = "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18"
    memes = [(ai, c, 0) for c in ("0x812486eaea648819853f8e372dc9f1516c7868bd", "0xd9db30bb0d2b8d2eae3826a1372117e058791e18",
                                  "0xd6fde6a3fc6ab2d83b2be58383944ca1bade1e18", "0x7dbf38976f6d3b9c529e7d9484a71898b409ee6a",
                                  "0xaa07a0e9209e16ac99708c3ec70159c6ef3128a3")]
    assert launch_pattern([flags_from_address("0x5afd41ecfab151bf37107eb68a0dec646dff90c4")], memes) is False
    # BackGeoOracle on OP Mainnet (0x7923…fac4), an oracle: gate, deltas and zero-fee ETH pools, but afterInitialize
    geo = [flags_from_address("0x79234983ded8eaa571873fffe94e437e11c7fac4")]
    assert geo[0]["afterInitialize"] and geo[0]["beforeInitialize"]
    op = [(ETH, c, 0) for c in ("0x0b2c639c533813f4aa9d7837caf62653d097ff85", "0x4200000000000000000000000000000000000042",
                                "0x0555e30da8f98308edb960aa94c0db47230d2b9c", "0x03c2868c6d7fd27575426f395ee081498b1120dd",
                                "0x8f187aa05619a017077f5308904739877ce9ea21")]
    assert launch_pattern(geo, op) is False
    # 0x3493…2ac0 on Robinhood (the DualPool permission set): gate plus liquidity and swap marks, LP fees set: not a launchpad
    dual = [flags_from_address("0x3493b97fbf6917c91cab8e2ba9c871d31aa82ac0")]
    assert launch_pattern(dual, [(RH_WETH, c, 1000) for c in ("0x98096d17e191b3da1d5f99a6d7b3584351b11e18", RH_SPCX,
                                                              "0x385f4f8ae47651ce5f58f5265395a669f8281e18", "0x020bfc650a365f8bb26819deaabf3e21291018b4",
                                                              "0x01e6301bc1118be90cc6bc2d1b5c0c44bb883111")]) is False
    assert launch_pattern([], []) is False


def test_wash_roundtrip_thresholds():
    from bait.rules import is_wash_roundtrip
    assert is_wash_roundtrip(1_390_000_000.0, 1_380_000_000.0)      # the Robinhood USDG family
    assert is_wash_roundtrip(1_000_000.0, 500_000.0)                 # exactly half, at the $1M floor
    assert not is_wash_roundtrip(1_000_000.0, 499_999.0)
    assert not is_wash_roundtrip(999_999.0, 999_999.0)               # under $1M nothing ranks anyway
    assert not is_wash_roundtrip(5_000_000.0, None)                  # no indexer row: unknown, never vacuous
    assert not is_wash_roundtrip(0.0, 0.0)


def test_lookalike_pool_catches_address_poisoning_and_same_symbol_pairs():
    from bait.rules import is_lookalike_pool
    usdc = {"id": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "symbol": "USDC"}
    spoof = {"id": "0x8335cb97db9ccdd95325c2da5622eebdd6ca2913", "symbol": "USDC"}
    weth = {"id": "0x4200000000000000000000000000000000000006", "symbol": "WETH"}
    meme = {"id": "0x1234567890abcdef1234567890abcdef12345678", "symbol": "MEME"}
    assert is_lookalike_pool(8453, usdc, spoof)
    assert is_lookalike_pool(8453, weth, {**spoof, "symbol": "USD Coin"})        # the address alone gives it away
    assert is_lookalike_pool(8453, usdc, {"id": meme["id"], "symbol": "usdc"})   # USDC/USDC with one side not canonical
    assert not is_lookalike_pool(8453, usdc, weth)
    assert not is_lookalike_pool(8453, meme, weth)
    assert not is_lookalike_pool(8453, {"id": "0x" + "0" * 40, "symbol": "ETH"}, usdc)
    assert is_lookalike_pool(999_999, usdc, spoof)                                # same symbol, two addresses: no canonical list needed
    assert not is_lookalike_pool(999_999, usdc, {"id": spoof["id"], "symbol": "USDC.e"})   # a bridged variant has its own symbol
    assert not is_lookalike_pool(8453, None, None)
    # Robinhood 2026-10-17: the real USDG and two spoofs sharing its first and last four hex digits (DESIGN.md "Round-trip wash")
    usdg = {"id": "0x5fc5360d0400a0fd4f2af552add042d716f1d168", "symbol": "USDG"}
    spoof1 = {"id": "0x5fc5910f80a6c1275fcfde8fe95056d7d536d168", "symbol": "USDG"}
    spoof2 = {"id": "0x5fc5a1f152a98ae93eb71cccf76ac0dc5f1ad168", "symbol": "USDG"}
    pons = {"id": "0xf9dbea39c0f7caf5028adb723573b2648acc4571", "symbol": "PONS"}
    assert is_lookalike_pool(4663, usdg, spoof1) and is_lookalike_pool(4663, usdg, spoof2)
    assert is_lookalike_pool(4663, spoof1, pons) and is_lookalike_pool(4663, pons, spoof2)   # the spoof's address alone gives it away
    assert not is_lookalike_pool(4663, usdg, pons)


def test_idle_tvl_needs_a_full_measured_week():
    from datetime import date
    from bait.rules import is_idle_tvl
    end = date(2026, 10, 15)
    assert is_idle_tvl(155_863_042.0, 0.12, 7, date(2026, 9, 1), end)
    assert not is_idle_tvl(155_863_042.0, 0.12, 6, date(2026, 9, 1), end)       # a day unmeasured
    assert not is_idle_tvl(155_863_042.0, 0.12, 7, date(2026, 10, 12), end)      # younger than a week
    assert not is_idle_tvl(900_000.0, 0.0, 7, None, end)                         # under $1M
    assert not is_idle_tvl(5_000_000.0, 6_000.0, 7, None, end)                   # traded 0.12% of it
    assert not is_idle_tvl(5_000_000.0, None, 7, None, end)


def test_self_priced_supply_needs_half_the_fdv_and_almost_no_trading():
    from bait.rules import is_self_priced_supply
    assert is_self_priced_supply(160_115_034.0, 222_804_287.0, 0.0)          # KATIE/WETH
    assert is_self_priced_supply(10_336_205.0, 10_300_000.0, None)            # HOOD/WETH, no volume
    assert not is_self_priced_supply(14_095_343.0, 19_821_224.0, 1_188_555.0) # trades: keeps its TVL
    assert not is_self_priced_supply(1_351_643_510.0, 4_391_052_633.0, 0.0)   # 31% of FDV (the spoof: lookalike rule)
    assert not is_self_priced_supply(900_000.0, 900_000.0, 0.0)               # under $1M
    assert not is_self_priced_supply(5_000_000.0, None, 0.0)                  # no FDV, no judgement
