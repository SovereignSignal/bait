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
MAX_SWAP_USD = 500_000_000.0    # a single swap above $500M is a broken quote, not a trade
MISMATCH = 20.0                 # sides disagree by more than 20x: trust the smaller one


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
        ids = {a.lower(): self.coin_id(chain, a) for a in addrs}
        missing = sorted({cid for cid in ids.values() if cid not in self._cache or now - self._cache[cid][0] > self.ttl})
        failed = 0
        for i in range(0, len(missing), BATCH):
            chunk = missing[i:i + BATCH]
            got = self._fetch(chunk)
            if got is None:  # transient failure: nothing cached, the caller retries the page
                failed += len(chunk)
                continue
            for cid in chunk:
                self._cache[cid] = (now, got.get(cid))
        if failed:
            raise PricesUnavailable(f"{chain.slug}: DefiLlama unavailable for {failed} coins")
        out = {}
        for a, cid in ids.items():
            hit = self._cache.get(cid)
            if hit and hit[1] is not None:
                out[a] = hit[1]
        return out

    def _fetch(self, ids: list[str]) -> dict[str, tuple[float, int]] | None:
        self.calls += 1
        try:
            r = self.client.get(LLAMA + ",".join(ids), headers={"User-Agent": "bait-indexer/0.1"}, timeout=30)
        except httpx.HTTPError as e:
            log.warning("defillama error %r", e)
            return None
        if r.status_code == 429 or r.status_code >= 500:
            log.warning("defillama HTTP %s for %d coins; page retried next loop", r.status_code, len(ids))
            return None
        if r.status_code != 200:
            log.warning("defillama HTTP %s for %d coins; treated as unpriced", r.status_code, len(ids))
            return {}
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
    than MISMATCH (one quote is wrong), then the smaller. None when neither side is priced."""
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
    return 0.0 if v > MAX_SWAP_USD else v
