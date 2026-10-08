"""Pure functions: routable rule, name normalization, scoring math. No I/O. Tested directly."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta

from .config import DYNAMIC_FEE_FLAG

# DESIGN.md: address not starting 0x91 AND none of these flags AND not dynamicFee.
# Stricter than the published Labs rule, which lists only the two swap deltas; see README.
NON_ROUTABLE_FLAGS = (
    "beforeSwapReturnsDelta",
    "afterSwapReturnsDelta",
    "afterAddLiquidityReturnsDelta",
    "afterRemoveLiquidityReturnsDelta",
)


def routable(hook: dict) -> bool:
    """hook = one hooklist entry: {"hook": {"address": ...}, "flags": {...}, "properties": {...}}."""
    address = (hook.get("hook") or {}).get("address") or hook.get("address") or ""
    flags = hook.get("flags") or {}
    props = hook.get("properties") or {}
    if address.lower().startswith("0x91"):
        return False
    if any(bool(flags.get(f)) for f in NON_ROUTABLE_FLAGS):
        return False
    if bool(props.get("dynamicFee")):
        return False
    return True


def is_dynamic_fee(fee: int | None) -> bool:
    return fee is not None and (int(fee) & DYNAMIC_FEE_FLAG) != 0


_SUFFIXES = ("hooks", "hook")


def normalize_name(name: str | None) -> str:
    """'BunniHook' -> 'bunni', 'Phi ETH Hooks' -> 'phieth', '' -> ''."""
    if not name:
        return ""
    s = re.sub(r"[^a-z0-9]", "", name.lower())
    for suf in _SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    return s


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "project"


def score_trending(vol_7d: float, vol_7d_prev: float) -> float:
    """DESIGN.md: Trending needs vol_7d >= $10k; score = log1p(vol_7d) * clamp(vol_7d / max(prev, 1000), 0.5, 10)."""
    if vol_7d < 10_000 or vol_7d_prev < 1_000:
        return 0.0  # growth needs a real prior week; a zero baseline would max out the ratio
    ratio = vol_7d / max(vol_7d_prev, 1000.0)
    return math.log1p(vol_7d) * min(max(ratio, 0.5), 10.0)


def is_new(first_seen_at: datetime, day: date, max_pool_tvl: float, swaps_7d: int) -> bool:
    """first_seen <= 7d before day AND (max pool TVL > $1k OR swaps_7d >= 10)."""
    cutoff = datetime.combine(day - timedelta(days=7), datetime.min.time(), tzinfo=first_seen_at.tzinfo)
    return first_seen_at >= cutoff and (max_pool_tvl > 1_000 or swaps_7d >= 10)


def is_spam(pool_stats: list[tuple[float, int]]) -> bool:
    """pool_stats = [(tvl_usd, tx_count_cum)]. No pools, or all pools under $100 TVL and < 10 txs lifetime."""
    if not pool_stats:
        return True
    return all((tvl or 0) < 100 and (txs or 0) < 10 for tvl, txs in pool_stats)


def is_alive(last_commit_at: datetime | None, latest_exa_hit: datetime | None, now: datetime) -> bool:
    cutoff = now - timedelta(days=30)
    return bool((last_commit_at and last_commit_at >= cutoff) or (latest_exa_hit and latest_exa_hit >= cutoff))
