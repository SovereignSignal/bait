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

# Deterministic CREATE2 deployers deploy for everyone: not team evidence, and never stored as a hook's deployer (such a
# contract creator says nothing about who deployed; the creation transaction's sender does). The same address on
# every chain. Keep SHARED_DEPLOYERS in web/src/lib/present.ts in step.
SHARED_DEPLOYERS = frozenset({
    "0x4e59b44847b379578588920ca78fbf26c0b4956c",   # Arachnid's deterministic deployment proxy (Foundry's default)
    "0x0000000000ffe8b47b3e2130213b802212439497",   # ImmutableCreate2Factory
    "0xba5ed099633d3b313e4d5f7bdc1305d3c28ba5ed",   # CreateX
    "0x914d7fec6aac8cd542e72bca78b30650d45643d7",   # Safe singleton factory
    "0x7a0d94f55792c434d74a40883c6ed8545e406d12",   # Zoltu's deterministic deployment proxy
    "0x13b0d85ccb8bf860b6b79af3029fca081ae9bef2",   # Create2Deployer
    "0x9fbb3df7c40da2e5a0de984ffe2ccb7c47cd0abf",   # CREATE3Factory
})


_ADDRESS = re.compile(r"0x[0-9a-f]{40}")


def deployer_or_none(v) -> str | None:
    """v as a hook's deployer: a lowercased 20-byte hex address that is not a shared CREATE2 deployer, else None."""
    s = v.strip().lower() if isinstance(v, str) else ""
    return s if _ADDRESS.fullmatch(s) and s not in SHARED_DEPLOYERS else None


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


# --- same-team stems (group rule 4, DESIGN.md "Same-team merge") ---
# Words that name one deployment of a team's hook, not the team: chains, "L2" / "mainnet", versions, the static or
# dynamic fee mode, and "hook". A chain word is never the first word (Zora and Base are also names). "ETH" is an asset,
# not a chain: FablesRampETH keeps its own stem.
STEM_CHAINS = frozenset({
    "ethereum", "base", "arbitrum", "optimism", "unichain", "polygon", "avalanche", "avax", "bnb", "bsc", "ink",
    "robinhood", "monad", "worldchain", "soneium", "megaeth", "celo", "blast", "linea", "xlayer", "arc", "tempo", "zora"})
STEM_CHAIN_PAIRS = (("x", "layer"), ("world", "chain"), ("mega", "eth"))
STEM_DEPLOY = frozenset({"l2", "mainnet"})
_FEE_MODES = frozenset({"static", "dynamic"})
_VERSION_WORD = re.compile(r"^v?\d+(?:\.\d+)*$")                     # "v2", "V6", "v2.3.0", "1.1.2"
# names.py tells same-named contracts apart with "Name 0x1234…abcd": two teams by design, never a stem.
ADDRESS_SUFFIX = re.compile(r"\s0x[0-9a-fA-F]{4}…[0-9a-fA-F]{4}$")
KEEP, CHAIN, VERSION, FEE = "keep", "chain", "version", "fee"


def _stem_words(name: str | None) -> list[tuple[str, str]]:
    """(word as written, kind) for each word of `name`, camelCase and digits split ("ZoraV4CoinHook" -> Zora V4 Coin
    Hook, "AngstromL2" -> Angstrom L2). kind is CHAIN (chains, "L2", "mainnet"), VERSION, FEE (static / dynamic fee)
    or KEEP; "hook" is KEEP here (a display name keeps it) and dropped from the stem by _stem_parts."""
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", name or "")
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    s = re.sub(r"(\d)([A-Z])", r"\1 \2", s)
    words = re.findall(r"(?i)v?\d+(?:\.\d+)+|[A-Za-z0-9]+", s)
    low = [w.lower() for w in words]
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(words):
        w, first = low[i], not any(k == KEEP for _, k in out)
        pair = next((p for p in STEM_CHAIN_PAIRS if tuple(low[i:i + len(p)]) == p), None)
        if w in STEM_CHAINS and low[i + 1:i + 2] == ["chain"]:
            pair = (w, "chain")                              # "(Robinhood Chain)", "(BNB Chain)"
        if w in _FEE_MODES and low[i + 1:i + 2] == ["fee"]:
            out += [(words[i], FEE), (words[i + 1], FEE)]
            i += 2
        elif pair and not first:
            out += [(x, CHAIN) for x in words[i:i + len(pair)]]
            i += len(pair)
        else:
            kind = (VERSION if _VERSION_WORD.match(w) else FEE if w in ("staticfee", "dynamicfee")
                    else CHAIN if not first and (w in STEM_CHAINS or w in STEM_DEPLOY) else KEEP)
            out.append((words[i], kind))
            i += 1
    return out


def _stem_parts(name: str) -> list[str]:
    parts = []
    for w, kind in _stem_words(name):
        w = w.lower()
        if kind != KEEP or w in ("hook", "hooks"):
            continue
        w = re.sub(r"(?<=[a-z]{3})v\d+$", "", w)          # "lendinghookv4": a version glued on in lowercase
        for suf in _SUFFIXES:
            if w.endswith(suf) and len(w) > len(suf):
                w = w[: -len(suf)]
                break
        parts.append(w)
    return parts


def name_stem(name: str | None) -> str | None:
    """The team part of a name: 'Clanker Static Fee Hook v2 (Base)', 'ClankerHookDynamicFee' -> 'clanker'; 'Aegis v3
    Hook (Unichain)' -> 'aegis'; 'AngstromL2' -> 'angstrom'; 'Zora Post Hook v2.3.0 (Base)' -> 'zorapost'. None for
    unlisted, generic and address-suffixed names, or when under 3 characters are left. A stem is a candidate only: a
    merge also needs evidence (group._merge_same_team)."""
    n = (name or "").strip()
    if not n or n.startswith("Unlisted hook") or ADDRESS_SUFFIX.search(n) or useful_name(n, max_len=None) is None:
        return None
    stem = "".join(_stem_parts(n))
    return stem if len(stem) >= 3 and not GENERIC.match(stem) else None


def stem_root(name: str | None) -> str | None:
    """The stem's first word, address suffix ignored: who a name says it belongs to ('Zora Post Hook' -> 'zora'). None
    when the name has no stem. A signal that names with two roots share (an OpenZeppelin template, an auditor's
    portfolio page) is nobody's team signal."""
    n = ADDRESS_SUFFIX.sub("", (name or "").strip())
    return _stem_parts(n)[0] if name_stem(n) else None


def deployment_words(name: str | None) -> dict[str, frozenset[str]]:
    """{kind: lowercased words} for the CHAIN, VERSION and FEE words of a name."""
    out: dict[str, set[str]] = {CHAIN: set(), VERSION: set(), FEE: set()}
    for w, kind in _stem_words(name):
        if kind != KEEP:
            out[kind].add(w.lower())
    return {k: frozenset(v) for k, v in out.items()}


def team_name(name: str, drop: set[str] | frozenset[str] = frozenset({CHAIN, VERSION, FEE})) -> str:
    """The name without the `drop` kinds of deployment words, as written; a kept chain goes in parentheses at the end.
    'Clanker Static Fee Hook v2 (Base)' -> 'Clanker Hook'; with drop={CHAIN}, 'Aegis v3 Hook (Unichain)' -> 'Aegis v3
    Hook'; with drop={FEE}, 'Bonker Static Fee Hook (Arc)' -> 'Bonker Hook (Arc)'."""
    words = _stem_words(name)
    body = " ".join(w for w, k in words if k not in drop and k != CHAIN)
    chain = " ".join(w for w, k in words if k == CHAIN) if CHAIN not in drop else ""
    return (f"{body} ({chain})" if chain else body) or name


# Hosts, GitHub owners and X handles many unrelated projects point at: never one team's link.
SHARED_HOSTS = frozenset({
    "github.com", "x.com", "twitter.com", "linktr.ee", "medium.com", "mirror.xyz", "paragraph.xyz", "substack.com",
    "t.me", "discord.gg", "discord.com", "docs.google.com", "notion.so", "notion.site", "gitbook.io", "youtube.com",
    "uniswap.org", "app.uniswap.org", "docs.uniswap.org", "blog.uniswap.org", "uniswapfoundation.org", "warpcast.com",
    "farcaster.xyz", "dexscreener.com", "geckoterminal.com", "coingecko.com", "etherscan.io", "basescan.org"})
SHARED_GITHUB = frozenset({"uniswap", "uniswapfoundation", "openzeppelin", "hooklist", "v4-template"})
SHARED_X = frozenset({"uniswap", "unichain", "uniswapfnd", "uniswapfoundation", "base", "buildonbase", "openzeppelin"})


def team_links(website: str | None, github: str | None, x_handle: str | None) -> dict[str, str]:
    """Enrichment links reduced to what identifies a team: website host (no www), GitHub owner, X handle, lowercased.
    Shared hosts and platform accounts are dropped."""
    out = {}
    host = re.sub(r"^(?:https?://)?(?:www\.)?", "", (website or "").strip().lower()).split("/")[0].split("?")[0]
    if host and "." in host and host not in SHARED_HOSTS:
        out["website"] = host
    m = re.search(r"github\.com/([A-Za-z0-9_.-]+)", github or "")
    if m and m.group(1).lower() not in SHARED_GITHUB:
        out["github"] = m.group(1).lower()
    x = re.sub(r"^(?:https?://)?(?:www\.)?(?:x|twitter)\.com/", "", (x_handle or "").strip()).lstrip("@").split("/")[0].lower()
    if x and x not in SHARED_X:
        out["x"] = x
    return out


def audit_key(url: str | None) -> str | None:
    """A hooklist audit URL compared as written, minus scheme, query, fragment and trailing slash."""
    u = re.sub(r"^https?://(?:www\.)?", "", (url or "").strip()).split("#")[0].split("?")[0].rstrip("/")
    return u.lower() or None


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


def code_shape(code_hex: str | None) -> str | None:
    """Bytecode template fingerprint: "0x" + sha256 of the runtime with the trailing CBOR metadata removed and every PUSH32
    operand zeroed. Solidity writes immutables as PUSH32, so one launchpad's per-token hooks (same source, different
    constructor arguments, one metadata hash per generated file) share a shape while their code_hash differs. Function
    selectors (PUSH4) and constant addresses (PUSH20) stay, so different code never shares one. None for no code."""
    s = (code_hex or "").strip().lower().removeprefix("0x")
    try:
        code = bytes.fromhex(s)
    except ValueError:
        return None
    if not code:
        return None
    if len(code) >= 2:                                   # solc appends CBOR metadata, then its length in 2 bytes
        n = int.from_bytes(code[-2:], "big")
        if 0 < n + 2 <= len(code) and code[-n - 2] in (0xA1, 0xA2, 0xA3):
            code = code[:-n - 2]
    out, i = bytearray(), 0
    while i < len(code):
        op = code[i]
        out.append(op)
        i += 1
        if 0x60 <= op <= 0x7F:                           # PUSH1..PUSH32
            n = op - 0x5F
            arg = code[i:i + n]
            out += bytes(len(arg)) if n == 32 else arg
            i += n
    return "0x" + hashlib.sha256(bytes(out)).hexdigest()


def shape_evidence(r) -> str | None:
    """(chain, code_shape) when the shape can join hooks: the same conditions as code_evidence (proxy bytecode is shared
    by unrelated deployments), and only on one chain, where a template's immutables are the only thing that differs."""
    if not getattr(r, "code_shape", None) or code_evidence(r) is None:
        return None
    return f"{r.chain_id}:{r.code_shape}"


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


# --- TVL trust (score.py). GeckoTerminal's reserve_in_usd values every side of a pool at the pool's own price, so a
# token that copies USDC, or a launch's whole supply priced by its own pool, reads as hundreds of millions of TVL. Both
# cases make the pool's TVL unknown; a lookalike pool is also wash-like ("lookalike"). 2026-10-16 audit: a spoof-USDC pool
# on Base read $1.307B TVL (81% of everything listed) and $221M of one wallet's round trips; a Clanker launch pool read
# $155.9M TVL on $0.12 of volume in a week. ---------------------------------------------------------------------------
IDLE_TVL_MIN = 1_000_000.0
IDLE_TURNOVER = 0.001               # traded under 0.1% of its TVL in a full measured week: the TVL is not liquidity
_ZERO_ADDRESS = "0x" + "0" * 40


def canonical_tokens(chain_id: int) -> frozenset[str]:
    """Wrapped native coin and major stablecoins Bait prices without DefiLlama (indexer/prices.py)."""
    from .indexer.prices import STABLES, WRAPPED_NATIVE
    out = set(STABLES.get(chain_id, {}))
    if chain_id in WRAPPED_NATIVE:
        out.add(WRAPPED_NATIVE[chain_id])
    return frozenset(a.lower() for a in out)


def _token(t) -> tuple[str, str]:
    if not isinstance(t, dict):
        return "", ""
    return str(t.get("id") or t.get("address") or "").lower(), str(t.get("symbol") or "").strip().lower()


def is_lookalike_pool(chain_id: int, token0, token1) -> bool:
    """A pool with a token posing as another: an address that copies a canonical token's first and last four hex digits
    (address poisoning; a chance collision is about 1 in 4 billion), or a same-symbol pair such as USDC/USDC with two
    different addresses. The symbol rule needs no canonical list: a real token has one address on a chain, so two
    "USDG" in one pool means one of them is not USDG (Robinhood 2026-10-17, before USDG was canonical there). Bridged
    variants carry their own symbols (USDC.e, USDbC), so they do not collide."""
    canon = canonical_tokens(chain_id)
    toks = [_token(token0), _token(token1)]
    for addr, _ in toks:
        if len(addr) != 42 or addr in canon or addr == _ZERO_ADDRESS:
            continue
        if any(c != _ZERO_ADDRESS and addr[2:6] == c[2:6] and addr[-4:] == c[-4:] for c in canon):
            return True
    (a0, s0), (a1, s1) = toks
    return bool(s0) and s0 == s1 and len(a0) == 42 and len(a1) == 42 and a0 != a1


SELF_PRICED_SHARE = 0.5            # the pool holds at least half the token's supply (GeckoTerminal fdv_usd of its base)


def is_self_priced_supply(tvl: float | None, fdv: float | None, vol_24h: float | None) -> bool:
    """$1M+ of TVL that is mostly one token's own supply valued at the pool's price, in a pool that barely trades: at least
    half the base token's fully diluted value sits in the pool and its rolling 24h volume is under 0.1% of the TVL."""
    if tvl is None or tvl < IDLE_TVL_MIN or not fdv or fdv <= 0:
        return False
    return tvl >= SELF_PRICED_SHARE * fdv and (vol_24h or 0.0) < tvl * IDLE_TURNOVER


def is_idle_tvl(tvl: float | None, vol_7d: float | None, measured_days: int, created: date | None, end: date) -> bool:
    """$1M+ of TVL that traded under 0.1% of it over a fully measured week, in a pool at least a week old."""
    if tvl is None or tvl < IDLE_TVL_MIN or vol_7d is None or measured_days < 7:
        return False
    if created is not None and created > end - timedelta(days=6):
        return False
    return vol_7d < tvl * IDLE_TURNOVER


# --- wash rules (DESIGN.md "Wash rules"). A project is wash-like for a reason when the pools that rule flags carry
# >= WASH_SHARE of its volume in that rule's window; reasons are stored in WASH_REASONS order. ------------------------
# "unverified" is not a rule: a project that only escaped the spam rule by moving >= $1M and has no wallet data at all.
# "lookalike": the volume runs through a pool with a token posing as USDC, USDT or the wrapped native coin.
# "roundtrip": the volume came and went inside single transactions (indexer core.roundtrip_usd), nothing was bought or sold.
WASH_REASONS = ("24h", "7d", "concentrated", "vol_tvl", "no_liquidity", "symmetric", "uniform", "unverified", "lookalike",
                "roundtrip")
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


# Round trips inside one transaction (indexer core.roundtrip_usd: a transaction's swaps in one pool that net to under 1%
# of their gross on both tokens). 10/16-17: an unlisted family of eleven zero-fee hooks on Robinhood moved $1.39B in 388
# swaps and 9 transactions, one of them 80 swaps of 5M USDG back and forth against a USDG lookalike; GeckoTerminal valued
# the lookalike side at $210M of TVL, so no few-wallet rule was corroborated. Nothing was bought or sold, so the round
# trips are their own corroboration: a pool whose week is at least half such volume is wash-like.
WASH_ROUNDTRIP_SHARE = 0.5


def is_wash_roundtrip(vol_7d: float, roundtrip_7d: float | None) -> bool:
    """roundtrip_7d None (no indexer row) never matches."""
    return (roundtrip_7d is not None and vol_7d >= WASH_MIN_VOL_7D and roundtrip_7d > 0
            and roundtrip_7d >= WASH_ROUNDTRIP_SHARE * vol_7d)


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


def is_alive(last_commit_at: datetime | None, now: datetime) -> bool:
    """A commit in the last 30 days. GitHub only since 2026-10-24, when the Exa receipts search was removed."""
    return bool(last_commit_at and last_commit_at >= now - timedelta(days=30))


# --- receipt relevance (mirrors web/src/lib/relevance.ts; shared cases in fixtures/relevance_cases.json) ---
# No step writes receipts since 2026-10-24 (the Exa receipts search is gone); the web still filters the stored ones with
# its copy, and this one keeps the shared cases pinned on both sides.
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


# ---------- category fallback (2026-10-15) ----------
# Categories come from enrichment (Claude reads the sources) and that reaches a few dozen projects a night, so most of
# the feed sat in "other". For a project with no generated category, a deterministic guess from what the chain and
# registry already say: a dynamic fee is a property or a pool fee flag, a launchpad is a factory or a hook with many
# pools, the rest are words in the hook's own name or description. Replaced the night enrichment writes a category.
CATEGORIES = ("dynamic-fee", "launchpad", "orders", "lp-management", "compliance", "oracle", "mev-lvr", "lending", "gaming-nft", "other")
LAUNCHPAD_MIN_POOLS = 100           # one hook initializing this many pools is a launchpad pattern, factory or not
_CATEGORY_WORDS = (
    ("orders", re.compile(r"\b(limit[ -]?orders?|twamm|dca|order ?book|stop[ -]?loss)\b", re.I)),
    ("lending", re.compile(r"\b(lend(ing)?|borrow(ing)?|collateral|money market|euler)\b", re.I)),
    ("mev-lvr", re.compile(r"\b(mev|lvr|auction|arbitrage|sandwich|angstrom)\b", re.I)),
    ("oracle", re.compile(r"\b(oracle|twap|price feed)\b", re.I)),
    ("compliance", re.compile(r"\b(kyc|whitelist|allow[ -]?list|permission(ed|s)?|compliance|gated|sanction(s|ed)?)\b", re.I)),
    ("lp-management", re.compile(r"\b(rebalanc(e|ing)|vault|range order|liquidity manag(er|ement)|lp manag(er|ement)|alm|just[ -]in[ -]time|jit)\b", re.I)),
    ("gaming-nft", re.compile(r"\b(nft|game|gaming|lottery|raffle|casino)\b", re.I)),
    ("launchpad", re.compile(r"\b(launch ?pad|launcher|token factory|bonding curve|meme ?coin|fair launch|pump)\b", re.I)),
    ("dynamic-fee", re.compile(r"\b(dynamic[ -]?fee|fee hook|surge fee|volatility fee)\b", re.I)),
)


def fallback_category(texts, properties, pool_fees, is_factory: bool, pools_total_max: int | None) -> str | None:
    """A category for a project the enrichment has not described, or None when nothing says. Word matches on the hooks'
    names and descriptions win, then the launchpad pattern, then a dynamic fee; "other" is never guessed."""
    # registry and contract names are CamelCase ("LimitOrderHook"): split them so word boundaries exist
    blob = " ".join(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", t) for t in texts if t)
    for cat, rx in _CATEGORY_WORDS:
        if blob and rx.search(blob):
            return cat
    if is_factory or (pools_total_max or 0) >= LAUNCHPAD_MIN_POOLS:
        return "launchpad"
    if any((p or {}).get("dynamicFee") for p in properties) or any(is_dynamic_fee(f) for f in pool_fees):
        return "dynamic-fee"
    return None


# --- pool labels (steps/snapshot.py). Symbols Bait can vouch for by address: the native coin (currency 0x0), the wrapped
# native coin and major stablecoins. Used to put back labels written in GeckoTerminal's base/quote order instead of
# currency0/currency1 (2026-10-16 audit: about 10% of pools). --------------------------------------------------------
_NATIVE_SYMBOL = {56: {"BNB"}, 137: {"POL", "MATIC"}, 43114: {"AVAX"}, 42220: {"CELO"}}
_WRAPPED_SYMBOL = {56: {"WBNB"}, 137: {"WPOL", "WMATIC"}, 43114: {"WAVAX"}, 42220: {"CELO"}}
_KNOWN_SYMBOLS: dict[int, dict[str, set[str]]] = {
    1: {"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": {"USDC"}, "0xdac17f958d2ee523a2206206994597c13d831ec7": {"USDT"},
        "0x6b175474e89094c44da98b954eedeac495271d0f": {"DAI"}},
    10: {"0x0b2c639c533813f4aa9d7837caf62653d097ff85": {"USDC"}},
    56: {"0x55d398326f99059ff775485246999027b3197955": {"USDT", "BSC-USD"}, "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": {"USDC"}},
    130: {"0x078d782b760474a361dda0af3839290b0ef57ad6": {"USDC"}},
    8453: {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": {"USDC"}},
    42161: {"0xaf88d065e77c8cc2239327c5edb3a432268e5831": {"USDC"}},
    43114: {"0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e": {"USDC"}, "0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7": {"USDT"}},
}


def known_symbols(chain_id: int, address: str) -> set[str]:
    from .indexer.prices import WRAPPED_NATIVE
    a = (address or "").lower()
    if a == "0x" + "0" * 40:
        return _NATIVE_SYMBOL.get(chain_id, {"ETH"})
    if WRAPPED_NATIVE.get(chain_id) == a:
        return _WRAPPED_SYMBOL.get(chain_id, {"WETH"})
    return _KNOWN_SYMBOLS.get(chain_id, {}).get(a, set())


def symbols_swapped(chain_id: int, token0, token1) -> bool:
    """True when a pool's two symbols sit on the wrong addresses: a side Bait knows by address carries the other side's
    symbol, and the known side's own symbol is not on it (USDC at the USDG address and USDG at USDC's)."""
    def tok(t) -> tuple[str, str]:
        return (str(t.get("id") or "").lower(), str(t.get("symbol") or "").strip().lower()) if isinstance(t, dict) else ("", "")
    (a0, s0), (a1, s1) = tok(token0), tok(token1)
    if not (s0 and s1) or s0 == s1:
        return False
    e0 = {x.lower() for x in known_symbols(chain_id, a0)}
    e1 = {x.lower() for x in known_symbols(chain_id, a1)}
    return (bool(e1) and s0 in e1 and s1 not in e1 and s0 not in e0) or (bool(e0) and s1 in e0 and s0 not in e0 and s1 not in e1)
