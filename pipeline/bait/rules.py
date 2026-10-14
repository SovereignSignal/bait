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


# v4 PoolManager emits Swap with the pool's own delta, before hook deltas. A beforeSwapReturnsDelta hook can settle part or
# all of the trade itself (custom curve, NoOp), so the event understates it, often to 0/0. afterSwapReturnsDelta only
# adjusts the unspecified side after the pool swapped: the event's pool leg is real volume, so bit 2 is not included.
CUSTOM_ACCOUNTING_BITS = 1 << 3


def custom_accounting(address: str) -> bool:
    return bool(int(address, 16) & CUSTOM_ACCOUNTING_BITS)


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


# Names that say nothing about the project: proxies, template defaults, and the OpenZeppelin uniswap-hooks and v4 example
# names many unrelated teams deploy unchanged. Matched on the lowercased alphanumerics, with an optional "hook(s)" and
# version suffix ("Limit Order Hook V2" -> "limitorderhookv2"). Applies to hooklist and verified-source names. Bare
# words that real projects use as names (Curve, Vault, Order, Base, Points, Arb) are deliberately not here; their
# template forms (BaseHook, ArbHook, CurveHook, LimitOrder, DynamicFee, ...) are.
GENERIC_STEMS = (
    "basehook", "basetesthooks", "arbhook", "curvehook", "v4", "uniswapv4", "uniswap", "any", "custom", "my", "mynew", "test", "counter", "swapfee", "fee", "platform",
    "limitorder", "dynamicfee", "antisandwich", "liquiditypenalty", "fullrange", "twamm", "geomeanoracle",
    "blacklist", "whitelist", "arbitrage", "grid", "example", "demo", "sample")
GENERIC = re.compile(r"^(?:(?:" + "|".join(GENERIC_STEMS) + r")(?:hooks?)?|hooks?)(?:v?\d+)?$"
                     r"|^.*proxy.*$|^(?:implementation|main|contract)$")

# Deterministic CREATE2 deployers (Arachnid, ImmutableCreate2Factory, CreateX) deploy for everyone: not team evidence.
SHARED_DEPLOYERS = frozenset({
    "0x4e59b44847b379578588920ca78fbf26c0b4956c",
    "0x0000000000ffe8b47b3e2130213b802212439497",
    "0xba5ed099633d3b313e4d5f7bdc1305d3c28ba5ed",
})


def useful_name(name: str | None, max_len: int | None = 60) -> str | None:
    """The name stripped, or None when it is empty, generic, or (verified-source names) longer than max_len."""
    n = (name or "").strip()
    flat = re.sub(r"[^a-z0-9]", "", n.lower())
    if not flat or (max_len and len(n) > max_len) or GENERIC.match(flat):
        return None
    return n


EIP1967_IMPL_SLOT = "360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1167_PREFIX = "363d3d373d3d3d363d73"


ERC1967_MAX_BYTES = 2048
FORWARDER_MAX_BYTES = 1024
DELEGATECALL = "f4"


def proxy_kind(code_hex: str | None) -> str | None:
    """The proxy shape of a runtime, or None. "eip1167": the whole runtime is a 45-byte minimal clone (a hook that only
    embeds the clone code, like OpenZeppelin Clones, is not). "eip1967": reads the implementation slot and is small
    (< 2,048 bytes); a UUPS implementation also carries the slot constant but is a full contract. "unknown": a runtime
    under 1,024 bytes with a DELEGATECALL byte that fits neither shape, likely a hand-rolled forwarder."""
    s = (code_hex or "").lower().removeprefix("0x")
    if s.startswith(EIP1167_PREFIX) and len(s) == 90:
        return "eip1167"
    if EIP1967_IMPL_SLOT in s and len(s) < 2 * ERC1967_MAX_BYTES:
        return "eip1967"
    if len(s) < 2 * FORWARDER_MAX_BYTES and any(s[i:i + 2] == DELEGATECALL for i in range(0, len(s), 2)):
        return "unknown"
    return None


def proxy_flag(kind: str | None) -> bool | None:
    """hooks.is_proxy for a proxy_kind: None ("unknown") keeps its hash out of evidence without calling it a proxy."""
    return None if kind == "unknown" else kind is not None


def is_proxy_code(code_hex: str | None) -> bool:
    return proxy_kind(code_hex) in ("eip1167", "eip1967")


def code_evidence(r) -> str | None:
    """The code_hash when it can join hooks: known not to be a proxy (hooks.is_proxy is False), or an EIP-1167 clone,
    whose bytecode embeds the implementation address (same hash, same implementation). Other proxy bytecode is shared
    by unrelated deployments; unknown status (bytecode not fetched yet) is not evidence either."""
    if not r.code_hash:
        return None
    if getattr(r, "is_proxy", None) is False or getattr(r, "proxy_kind", None) == "eip1167":
        return r.code_hash
    return None


def merge_evidence(a, b) -> str | None:
    """Why two sets of hooks are one project, or None. A shared name, hooklist or verified-source, is only a candidate;
    evidence is shared non-proxy bytecode or a shared deployer that is not a CREATE2 factory."""
    def hashes(rows):
        return {c for r in rows if (c := code_evidence(r))}

    def deployers(rows):
        return {r.deployer for r in rows if r.deployer and r.deployer not in SHARED_DEPLOYERS}

    if hashes(a) & hashes(b):
        return "code_hash"
    if deployers(a) & deployers(b):
        return "deployer"
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


SPAM_EXEMPT_VOL = 1_000_000


def is_spam(pool_stats: list[tuple[float, int]], volume: float = 0.0) -> bool:
    """pool_stats = [(tvl_usd, tx_count_cum)]. No pools, or all pools under $100 TVL and < 10 txs lifetime. A project that
    moved >= $1M (lifetime or 7-day; a lower bound is enough) is never spam: a wash-like whale such as Optimism 0x69af
    ($436M through 8 swaps at $0 TVL) is listed in All with its badge instead of 404ing."""
    if not pool_stats:
        return True
    if volume >= SPAM_EXEMPT_VOL:
        return False
    return all((tvl or 0) < 100 and (txs or 0) < 10 for tvl, txs in pool_stats)


# --- wash rules (DESIGN.md "Wash rules"). A project is wash-like for a reason when the pools that rule flags carry
# >= WASH_SHARE of its volume in that rule's window; reasons are stored in WASH_REASONS order. ------------------------
# "unverified" is not a rule: a project that only escaped the spam rule by moving >= $1M and has no wallet data at all.
WASH_REASONS = ("24h", "7d", "concentrated", "vol_tvl", "no_liquidity", "symmetric", "uniform", "unverified")
WASH_SHARE = 0.5
WASH_SIGNAL_MIN_VOL = 10_000        # below this nothing ranks anywhere (Trending needs $10K in 7 days)


def wallets_lower_bound(distinct: int | None, buyers: int | None, sellers: int | None, traders: int | None) -> int | None:
    """GeckoTerminal counts unique buyers and unique sellers, not unique wallets: buyers + sellers (traders_24h) counts a
    wallet that bought and sold twice, an upper bound. The "at most N wallets" rules need a lower bound: the distinct count
    when one is known, else max(buyers, sellers), else half of buyers + sellers. None = nothing known."""
    if distinct is not None:
        return distinct
    if buyers is not None or sellers is not None:
        return max(buyers or 0, sellers or 0)
    if traders is not None:
        return (traders + 1) // 2
    return None


# 24h (GeckoTerminal pool_stats; on ready chains the onchain step's closed day): big volume from a handful of wallets.
WASH_MIN_VOL_24H = 250_000
WASH_MAX_WALLETS_24H = 25


def is_wash_24h(vol_24h: float | None, wallets: int | None) -> bool:
    return vol_24h is not None and wallets is not None and vol_24h >= WASH_MIN_VOL_24H and wallets <= WASH_MAX_WALLETS_24H


def vol_tvl(vol: float | None, tvl: float | None) -> float | None:
    """Volume over liquidity. None when TVL is unknown or not positive: a ratio over $0 says nothing (see no_liquidity)."""
    if vol is None or tvl is None or tvl <= 0:
        return None
    return vol / tvl


WASH_MIN_VOL_TVL = 50               # 7-day volume over TVL that corroborates a few-wallet pool
NO_LIQUIDITY_MAX_TVL = 1_000        # a KNOWN TVL at or under this, with >= WASH_MIN_VOL_7D in 7 days, is no liquidity


def no_liquidity(vol_7d: float | None, tvl: float | None) -> bool:
    """>= $1M in 7 days through a pool whose known TVL is <= $1,000 (Optimism 0x69af: $436M at $0). Unknown TVL is never
    "no liquidity"."""
    return vol_7d is not None and tvl is not None and vol_7d >= WASH_MIN_VOL_7D and tvl <= NO_LIQUIDITY_MAX_TVL


def corroborated(vol_7d: float | None, tvl: float | None, symmetric: bool, custom: bool = False) -> bool:
    """Few wallets alone also describes private / RFQ hooks (Arrakis private hook) that move real volume against real
    liquidity. A wash flag needs one more sign: 7-day volume > 50x a known TVL, no liquidity at all, or symmetric traders.
    custom: a custom-accounting hook (custom_accounting(address)) keeps its liquidity in the hook, so a $0 pool TVL is not
    "no liquidity" for it."""
    r = vol_tvl(vol_7d, tvl)
    return (r is not None and r > WASH_MIN_VOL_TVL) or (not custom and no_liquidity(vol_7d, tvl)) or symmetric


# 7 days (indexer: pool_daily_onchain + pool_wallet_day, ready chains only). 10/8: Optimism 0x69af...7fff moved $436M
# in 7 days from 1 wallet in 8 swaps.
WASH_MIN_VOL_7D = 1_000_000
WASH_MAX_WALLETS_7D = 10


def is_wash_7d(vol_7d: float, wallets_7d: int | None, tvl: float | None, symmetric: bool, custom: bool = False) -> bool:
    """wallets_7d None (unknown, e.g. pruned pool_wallet_day rows) never matches: no vacuous "<= 10 wallets"."""
    return (wallets_7d is not None and vol_7d >= WASH_MIN_VOL_7D and wallets_7d <= WASH_MAX_WALLETS_7D
            and corroborated(vol_7d, tvl, symmetric, custom))


# One or two wallets moving >= $1M in 7 days through a pool: no TVL corroboration needed, custom-accounting hooks
# included (Optimism 0x69af...7fff: $436M from 1 wallet in 8 swaps). An RFQ / private hook with 5-10 real counterparties
# stays clear. wallets must be a known distinct count (pool_wallet_day, or exact pool_stats days), never a lower bound.
WASH_CONCENTRATED_WALLETS = 2


def is_wash_concentrated(vol_7d: float, wallets: int | None) -> bool:
    return wallets is not None and vol_7d >= WASH_MIN_VOL_7D and wallets <= WASH_CONCENTRATED_WALLETS


def is_wash_7d_by_swaps(vol_7d: float, swaps: int | None) -> bool:
    """The 7-day rule through the swap bound, on any chain: >= $1M in 7 days carried by <= 10 known swaps proves <= 10
    wallets, and that concentration is its own corroboration (no TVL needed)."""
    return swaps is not None and vol_7d >= WASH_MIN_VOL_7D and swaps <= WASH_MAX_WALLETS_7D


# Volume far above liquidity from few wallets. Launchpads churn many times their liquidity too (Base 0x0f48 family:
# 345x) but through thousands of wallets; OUROBOROS/CRCL on Robinhood: 866x through 13 buyers and 13 sellers.
WASH_VOL_TVL = 300
WASH_VOL_TVL_WALLETS = 50           # fewer than this


def is_wash_vol_tvl(vol_7d: float, tvl: float | None, wallets: int | None) -> bool:
    r = vol_tvl(vol_7d, tvl)
    return (r is not None and r > WASH_VOL_TVL and vol_7d >= WASH_SIGNAL_MIN_VOL
            and wallets is not None and wallets < WASH_VOL_TVL_WALLETS)


def is_wash_no_liquidity(vol_7d: float, tvl: float | None, wallets: int | None, custom: bool = False) -> bool:
    """The volume/TVL rule where the ratio is undefined: >= $1M in 7 days, a known TVL <= $1,000, fewer than 50 wallets.
    Never for a custom-accounting hook (its liquidity lives in the hook)."""
    return not custom and no_liquidity(vol_7d, tvl) and wallets is not None and wallets < WASH_VOL_TVL_WALLETS


# Bots trading with themselves: the pool's top wallets that day (activity step, pool_top_wallets) each buy and sell the
# same number of times and carry more than half its volume.
SYMMETRIC_TOP = 10
SYMMETRIC_SHARE = 0.5
SYMMETRIC_MIN_WALLETS = 5           # a pattern needs at least 5 such wallets...
SYMMETRIC_MIN_ROUND_TRIPS = 3       # ...or each of them buying and selling at least 3 times


def is_symmetric(top: list[tuple[float, int, int]], vol_24h: float | None) -> bool:
    """top: (volume_usd, buys, sells) per wallet, any order. The top 10 by volume (at least 2) are judged: at least 5
    wallets, or each with >= 3 round trips, so two people who bought and sold once are not bots. vol_24h is the pool's
    rolling 24h volume, the window the trades come from."""
    if not vol_24h or vol_24h < WASH_SIGNAL_MIN_VOL:
        return False
    t = sorted(top, key=lambda x: -x[0])[:SYMMETRIC_TOP]
    pattern = len(t) >= SYMMETRIC_MIN_WALLETS or all(b >= SYMMETRIC_MIN_ROUND_TRIPS for _, b, _ in t)
    return (len(t) >= 2 and pattern and all(b == s and b > 0 for _, b, s in t)
            and sum(v for v, _, _ in t) > SYMMETRIC_SHARE * vol_24h)


# One trade size across a pool: fresh wallets each making one trade of the same dollar size. 10/13: Hookr MOW/WETH on
# Robinhood, top 10 wallets each one buy of $22,450-$22,515 (0.3% apart) and $17.1M from 809 wallets in 24h ($21.1K each).
# A launch that caps buys gives a uniform top too, but its many smaller buyers pull the pool's per-wallet average well
# under the cap, so the average must sit near the uniform size as well.
UNIFORM_MIN_WALLETS = 8
UNIFORM_MAX_SPREAD = 1.03           # largest / smallest of the top wallets' volumes
UNIFORM_MIN_AVG_SHARE = 0.8         # pool volume per wallet, against the top wallets' median size
UNIFORM_MIN_VOL = 1_000_000


def is_uniform(top: list[tuple[float, int, int]], vol_24h: float | None, wallets_24h: int | None) -> bool:
    """top: (volume_usd, buys, sells) per wallet. wallets_24h: distinct wallets, else buyers + sellers (an upper bound,
    which only lowers the average: the safe side)."""
    if not vol_24h or vol_24h < UNIFORM_MIN_VOL or not wallets_24h:
        return False
    t = sorted(top, key=lambda x: -x[0])[:SYMMETRIC_TOP]
    if len(t) < UNIFORM_MIN_WALLETS or not all(b + s == 1 for _, b, s in t):
        return False
    sizes = sorted(v for v, _, _ in t)
    if sizes[0] <= 0 or sizes[-1] / sizes[0] > UNIFORM_MAX_SPREAD:
        return False
    median = sizes[len(sizes) // 2]
    return vol_24h / wallets_24h >= UNIFORM_MIN_AVG_SHARE * median


def is_alive(last_commit_at: datetime | None, latest_exa_hit: datetime | None, now: datetime) -> bool:
    cutoff = now - timedelta(days=30)
    return bool((last_commit_at and last_commit_at >= cutoff) or (latest_exa_hit and latest_exa_hit >= cutoff))


# --- receipt relevance (mirrors web/src/lib/relevance.ts; shared cases in fixtures/relevance_cases.json) ---
_DOMAIN_WORDS = ("uniswap", "hook", "v4", "liquidity", "swap", "pool", "amm", "dex", "lp", "mev", "fee")
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
