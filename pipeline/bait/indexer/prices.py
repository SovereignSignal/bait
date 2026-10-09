"""Token USD prices from DefiLlama's keyless current-price API, cached ~30 minutes.

GET https://coins.llama.fi/prices/current/{chain}:{addr},{chain}:{addr},...
-> {"coins": {"base:0x83..": {"decimals": 6, "symbol": "USDC", "price": 0.9996, "timestamp": ..., "confidence": 0.99}}}
Native currency (0x0) maps to the chain's coingecko id (no decimals returned; native is 18).

Volume is approximate by design: every swap is valued at the token's CURRENT price, not the price at swap time.
"""
from __future__ import annotations

import logging
import math
import time

import httpx

from .chains import IndexedChain
from .decode import ZERO

log = logging.getLogger("bait.indexer")

LLAMA = "https://coins.llama.fi/prices/current/"
TTL = 30 * 60
BATCH = 80
MIN_CONFIDENCE = 0.5
MAX_PRICE = 10_000_000.0        # no token is worth more than $10M; above that the quote is broken
MAX_SWAP_USD = 500_000_000.0    # a single swap above $500M is a broken quote: the swap counts as unpriced
MISMATCH = 20.0                 # sides disagree by more than 20x: trust the smaller one

# Fallback when DefiLlama has no reliable quote (new chain keys, low confidence): the wrapped native coin (priced as the
# chain's native coin id) and major stablecoins ($1). OP-stack chains share the WETH predeploy. Unichain, World, Soneium,
# Ink, Celo, Robinhood and MegaETH entries were checked on each chain's Blockscout token page 2026-10-09; the rest are the
# canonical deployments. Monad, Zora and Robinhood/MegaETH stables stay out until checked the same way.
OP_WETH = "0x4200000000000000000000000000000000000006"
WRAPPED_NATIVE: dict[int, str] = {
    1: "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", 10: OP_WETH, 56: "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    130: OP_WETH, 137: "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270", 480: OP_WETH, 1868: OP_WETH, 4326: OP_WETH,
    4663: "0x0bd7d308f8e1639fab988df18a8011f41eacad73", 8453: OP_WETH, 42161: "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
    42220: "0x471ece3750da237f93b8e339c536989b8978a438", 43114: "0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7",
    57073: OP_WETH, 7777777: OP_WETH,
}
STABLES: dict[int, dict[str, int]] = {
    1: {"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6, "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,
        "0x6b175474e89094c44da98b954eedeac495271d0f": 18},
    10: {"0x0b2c639c533813f4aa9d7837caf62653d097ff85": 6, "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58": 6},
    56: {"0x55d398326f99059ff775485246999027b3197955": 18, "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": 18},
    130: {"0x078d782b760474a361dda0af3839290b0ef57ad6": 6},
    137: {"0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": 6, "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": 6,
          "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": 6},
    480: {"0x79a02482a880bce3f13e09da970dc34db4cd24d1": 6},
    1868: {"0xba9986d2381edf1da03b0b9c1f8b00dc4aacc369": 6},
    8453: {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6},
    42161: {"0xaf88d065e77c8cc2239327c5edb3a432268e5831": 6, "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": 6},
    42220: {"0xceba9300f2b948710d2653dd7b07f33a8b32118c": 6},
    43114: {"0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e": 6, "0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7": 6},
    57073: {"0x0200c29006150606b650577bbe7b6248f58470c1": 6},
}


_REJECTED = object()            # _fetch: DefiLlama answered 4xx (not 429) for the batch


class PricesUnavailable(RuntimeError):
    """DefiLlama did not answer for a batch. The caller must not commit the page: a swap stored at $0 is never revalued."""


class PriceBook:
    def __init__(self, client: httpx.Client, ttl: float = TTL, clock=time.time):
        self.client = client
        self.ttl = ttl
        self.clock = clock
        self._cache: dict[str, tuple[float, tuple[float, int] | None]] = {}
        self.calls = 0

    def coin_id(self, chain: IndexedChain, addr: str) -> str:
        return chain.native if addr.lower() == ZERO else f"{chain.llama}:{addr.lower()}"

    def get(self, chain: IndexedChain, addrs) -> dict[str, tuple[float, int]]:
        """-> {address: (usd price, decimals)} for addresses with a reliable price. Misses are cached too."""
        now = self.clock()
        want = {a.lower() for a in addrs}
        wrapped = WRAPPED_NATIVE.get(chain.chain_id)
        ids = {a: self.coin_id(chain, a) for a in want | ({ZERO} if wrapped in want else set())}
        missing = sorted({cid for cid in ids.values() if cid not in self._cache or now - self._cache[cid][0] > self.ttl})
        failed = 0
        for i in range(0, len(missing), BATCH):
            chunk = missing[i:i + BATCH]
            got = self._fetch_split(chunk)
            if got is None:  # transient failure: nothing cached, the caller retries the page
                failed += len(chunk)
                continue
            for cid in chunk:
                self._cache[cid] = (now, got.get(cid))
        if failed:
            raise PricesUnavailable(f"{chain.slug}: DefiLlama unavailable for {failed} coins")
        found = {a: hit[1] for a, cid in ids.items() if (hit := self._cache.get(cid)) and hit[1] is not None}
        stables = STABLES.get(chain.chain_id, {})
        out = {}
        for a in want:
            if a in found:
                out[a] = found[a]
            elif a == wrapped and ZERO in found:
                out[a] = (found[ZERO][0], 18)
            elif a in stables:
                out[a] = (1.0, stables[a])
        return out

    def _fetch_split(self, ids: list[str]) -> dict[str, tuple[float, int]] | None:
        """A 4xx (not 429) rejects the whole batch, usually because of one id: split and retry the halves, so only an id
        DefiLlama rejects on its own is treated as unpriced. None = a transient failure in some part."""
        got = self._fetch(ids)
        if got is not _REJECTED:
            return got
        if len(ids) == 1:
            log.warning("defillama rejected %s; treated as unpriced", ids[0])
            return {}
        mid = len(ids) // 2
        left, right = self._fetch_split(ids[:mid]), self._fetch_split(ids[mid:])
        return None if left is None or right is None else left | right

    def _fetch(self, ids: list[str]):
        """-> {coin id: (price, decimals)}, None on a transient failure, or _REJECTED on a non-429 4xx."""
        self.calls += 1
        try:
            r = self.client.get(LLAMA + ",".join(ids), headers={"User-Agent": "bait-indexer/0.1"}, timeout=30)
        except httpx.HTTPError as e:
            log.warning("defillama error %r", e)
            return None
        if r.status_code in (401, 403, 429) or r.status_code >= 500:  # auth/rate/server: transient, never a bad batch
            log.warning("defillama HTTP %s for %d coins; page retried next loop", r.status_code, len(ids))
            return None
        if r.status_code != 200:
            return _REJECTED
        try:
            coins = (r.json() or {}).get("coins") or {}
        except ValueError as e:
            log.warning("defillama bad JSON %r", e)
            return None
        out = {}
        for cid, c in coins.items():
            p = reliable(c, native=cid.startswith("coingecko:"))
            if p:
                out[cid] = p
        return out


def reliable(c: dict, native: bool = False) -> tuple[float, int] | None:
    try:
        price = float(c.get("price"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0 or price > MAX_PRICE:
        return None
    conf = c.get("confidence")
    if conf is not None and float(conf) < MIN_CONFIDENCE:
        return None
    dec = c.get("decimals", 18 if native else None)
    if dec is None:
        return None
    dec = int(dec)
    if not 0 <= dec <= 36:
        return None
    return price, dec


def swap_usd(amount0: int, amount1: int, p0: tuple[float, int] | None, p1: tuple[float, int] | None) -> float | None:
    """USD value of one swap from whichever side has a price. Both priced: the larger, unless they disagree by more
    than MISMATCH (one quote is wrong), then the smaller. None (unpriced) when neither side is priced or the value is
    above MAX_SWAP_USD."""
    sides = []
    for amt, p in ((amount0, p0), (amount1, p1)):
        if p is not None:
            sides.append(abs(amt) / 10 ** p[1] * p[0])
    if not sides:
        return None
    if len(sides) == 2:
        lo, hi = min(sides), max(sides)
        v = lo if lo > 0 and hi / lo > MISMATCH else hi
    else:
        v = sides[0]
    return None if v > MAX_SWAP_USD else v          # a broken quote is unpriced (counted), not $0
