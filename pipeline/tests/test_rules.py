import math
from datetime import date, datetime, timedelta, timezone

import pytest

from hookfeed.rules import is_dynamic_fee, is_new, is_spam, normalize_name, routable, score_trending, slugify


def hook(address="0xabc0000000000000000000000000000000000001", flags=None, dynamic=False):
    return {"hook": {"address": address}, "flags": flags or {}, "properties": {"dynamicFee": dynamic}}


def test_routable_plain_hook():
    assert routable(hook(flags={"beforeSwap": True, "afterSwap": True})) is True


def test_routable_rejects_0x91_prefix_case_insensitive():
    assert routable(hook(address="0x91aa000000000000000000000000000000000000")) is False
    assert routable(hook(address="0X91AA000000000000000000000000000000000000")) is False
    assert routable(hook(address="0x19aa000000000000000000000000000000000000")) is True


@pytest.mark.parametrize("flag", [
    "beforeSwapReturnsDelta", "afterSwapReturnsDelta",
    "afterAddLiquidityReturnsDelta", "afterRemoveLiquidityReturnsDelta",   # DESIGN.md house rule, stricter than Labs
])
def test_routable_rejects_delta_flags(flag):
    assert routable(hook(flags={flag: True})) is False
    assert routable(hook(flags={flag: False})) is True


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
    assert score_trending(10_000, 0) == pytest.approx(math.log1p(10_000) * 10.0)   # prev floored at 1000 -> ratio 10
    assert score_trending(1_000_000, 10) == pytest.approx(math.log1p(1_000_000) * 10.0)   # clamp high
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
