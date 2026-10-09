"""Pure functions: routable rule, name normalization, scoring math. No I/O. Tested directly."""
from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime, timedelta

from .config import DYNAMIC_FEE_FLAG

# DESIGN.md: address not starting 0x91 AND none of these address bits AND not dynamicFee.
# Stricter than the published Labs rule, which lists only the two swap deltas; see README.
NON_ROUTABLE_FLAGS = (
    "beforeSwapReturnsDelta",
    "afterSwapReturnsDelta",
    "afterAddLiquidityReturnsDelta",
    "afterRemoveLiquidityReturnsDelta",
)


PERMISSION_BITS: tuple[tuple[str, int], ...] = (
    ("beforeInitialize", 13), ("afterInitialize", 12),
    ("beforeAddLiquidity", 11), ("afterAddLiquidity", 10),
    ("beforeRemoveLiquidity", 9), ("afterRemoveLiquidity", 8),
    ("beforeSwap", 7), ("afterSwap", 6),
    ("beforeDonate", 5), ("afterDonate", 4),
    ("beforeSwapReturnsDelta", 3), ("afterSwapReturnsDelta", 2),
    ("afterAddLiquidityReturnsDelta", 1), ("afterRemoveLiquidityReturnsDelta", 0),
)


def flags_from_address(address: str) -> dict[str, bool]:
    """Hook permissions live in the low 14 bits of the address (v4 Hooks.sol); the chain enforces them."""
    bits = int(address, 16) & 0x3FFF
    return {name: bool(bits >> bit & 1) for name, bit in PERMISSION_BITS}


def eligibility(hook: dict, pool_fee: int | None = None) -> str:
    """Routing eligibility by Bait's rule: 'eligible' | 'ineligible' | 'unknown'. Permissions come from the address;
    dynamic fee from the pool fee when given, else properties.dynamicFee when present, else unknown."""
    address = (hook.get("hook") or {}).get("address") or hook.get("address") or ""
    props = hook.get("properties") or {}
    if address.lower().startswith("0x91"):
        return "ineligible"
    flags = flags_from_address(address)
    if any(flags[f] for f in NON_ROUTABLE_FLAGS):
        return "ineligible"
    if pool_fee is not None:
        return "ineligible" if is_dynamic_fee(pool_fee) else "eligible"
    if "dynamicFee" in props:
        return "ineligible" if bool(props.get("dynamicFee")) else "eligible"
    return "unknown"


def routable(hook: dict) -> bool:
    """hook = one hooklist entry: {"hook": {"address": ...}, "flags": {...}, "properties": {...}}."""
    return eligibility(hook) == "eligible"


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


# Names that say nothing about the project: proxies and template defaults. Applies to hooklist and verified-source names.
GENERIC = re.compile(
    r"(?i)^(hook|basehook|v4hook|uniswapv4hook|uniswaphook|anyhook|customhook|myhook|testhook|counter|counterhook|"
    r"swapfeehook|feehook|platformhook|.*proxy.*|implementation|main|contract)$")

# Deterministic CREATE2 deployers (Arachnid, ImmutableCreate2Factory, CreateX) deploy for everyone: not team evidence.
SHARED_DEPLOYERS = frozenset({
    "0x4e59b44847b379578588920ca78fbf26c0b4956c",
    "0x0000000000ffe8b47b3e2130213b802212439497",
    "0xba5ed099633d3b313e4d5f7bdc1305d3c28ba5ed",
})


def useful_name(name: str | None, max_len: int | None = 60) -> str | None:
    """The name stripped, or None when it is empty, generic, or (verified-source names) longer than max_len."""
    n = (name or "").strip()
    if not n or (max_len and len(n) > max_len) or GENERIC.match(n):
        return None
    return n


def is_proxy_name(name: str | None) -> bool:
    return "proxy" in (name or "").lower()


def merge_evidence(a, b) -> str | None:
    """Why two sets of hooks are one project, or None. A shared name is only a candidate; evidence is shared non-proxy
    bytecode, a shared deployer that is not a CREATE2 factory, or the same non-generic hooklist name."""
    def hashes(rows):
        return {r.code_hash for r in rows if r.code_hash and not (is_proxy_name(r.name) or is_proxy_name(r.contract_name))}

    def deployers(rows):
        return {r.deployer for r in rows if r.deployer and r.deployer not in SHARED_DEPLOYERS}

    def listed(rows):
        return {normalize_name(useful_name(r.name, max_len=None)) for r in rows} - {""}

    if hashes(a) & hashes(b):
        return "code_hash"
    if deployers(a) & deployers(b):
        return "deployer"
    if listed(a) & listed(b):
        return "hooklist_name"
    return None


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "project"


def code_hash(code_hex: str | None) -> str | None:
    """Canonical bytecode fingerprint: "0x" + sha256 of the deployed bytes, lowercase hex. None for no code or non-hex.
    Every writer (keyless RPC, Blockscout) goes through here so grouping rule 2 compares like with like."""
    s = (code_hex or "").strip().lower().removeprefix("0x")
    if not s:
        return None
    try:
        return "0x" + hashlib.sha256(bytes.fromhex(s)).hexdigest()
    except ValueError:
        return None


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


# 7-day wash from the on-chain indexer (pool_daily_onchain + pool_wallet_day). The 24h rule (keyless.is_wash) only
# sees pools GeckoTerminal tracks; the indexer sees every swap. A pool moving >= $1M in 7 days through <= 10 distinct
# wallets is one desk trading with itself, not usage (10/8: Optimism 0x69af...7fff, $436M from 1 wallet in 8 swaps).
# Private/RFQ hooks with few counterparties also trip this; they are hidden from rankings, not deleted.
WASH_MIN_VOL_7D = 1_000_000
WASH_MAX_WALLETS_7D = 10


def is_wash_7d(vol_7d: float, wallets_7d: int) -> bool:
    return vol_7d >= WASH_MIN_VOL_7D and wallets_7d <= WASH_MAX_WALLETS_7D


def is_alive(last_commit_at: datetime | None, latest_exa_hit: datetime | None, now: datetime) -> bool:
    cutoff = now - timedelta(days=30)
    return bool((last_commit_at and last_commit_at >= cutoff) or (latest_exa_hit and latest_exa_hit >= cutoff))


# --- receipt relevance (mirrors web/src/lib/relevance.ts; shared cases in fixtures/relevance_cases.json) ---
_DOMAIN_WORDS = ("uniswap", "hook", "v4", "liquidity", "swap", "pool", "defi", "amm", "dex", "lp", "mev", "fee")
_BLOCKED_HOSTS = ("linkedin.com", "wikipedia.org", "netlify.app", "facebook.com", "instagram.com", "tiktok.com")


def name_tokens(project_name: str) -> list[str]:
    if re.match(r"(?i)^unlisted hook", project_name or ""):
        return []
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", project_name or "")
    words = [w for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]
    norm = re.sub(r"hooks?$", "", re.sub(r"[^a-z0-9]", "", (project_name or "").lower()))
    first = words[0].lower() if words else ""
    toks: list[str] = []
    if len(norm) >= 3:
        toks.append(norm)
    if len(first) >= 4 and first not in ("uniswap", "hook", "dynamic", "static") and first not in toks:
        toks.append(first)
    return toks


def receipt_relevant(project_name: str, title: str, source: str) -> bool:
    """Keep a link only when its title names the project AND reads as DeFi/Uniswap (Exa matches on names alone)."""
    toks = name_tokens(project_name)
    if not toks:
        return False
    host = (source or "").lower()
    if any(host == b or host.endswith("." + b) for b in _BLOCKED_HOSTS):
        return False
    flat = re.sub(r"[^a-z0-9]", "", (title or "").lower())
    words = (title or "").lower()
    # short tokens must start a word ("Hookr" must not match "hook research"); long ones may sit inside run-together text
    named = any(re.search(rf"(^|[^a-z0-9]){re.escape(t)}", words) or (len(t) >= 8 and t in flat) for t in toks)
    defi = any(re.search(rf"(^|[^a-z0-9]){w}([^a-z0-9]|$)", words) for w in _DOMAIN_WORDS)
    return named and defi
