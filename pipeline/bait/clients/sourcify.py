"""Sourcify (keyless, every chain it indexes): who deployed a verified contract.

GET /server/v2/contract/{chainId}/{address}?fields=deployment answers a verified contract with `deployment.deployer`,
the sender of the creation transaction, and `deployment.transactionHash`. The sender is the wallet even when the hook
came out of a CREATE2 proxy or a factory: StablePairHook on Ethereum gives 0xe49a…5b95, where Blockscout's
creator_address_hash is Arachnid's proxy. A contract verified from its runtime alone can come back with every
deployment field null; 404 is "not verified here", 400 an unsupported chain. Recorded responses:
pipeline/fixtures/deployers-2026-10-24.json.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

import httpx

from ..rules import deployer_or_none
from . import http
from .contract_names import PACE_S, RETRY_429_S

BASE = "https://sourcify.dev/server/v2/contract"


class Sourcify:
    def __init__(self, http_client=None, sleep: Callable[[float], None] = time.sleep):
        self.c = http_client or http.client(timeout=20.0)
        self.sleep = sleep
        self.pace_s = PACE_S["sourcify.dev"]       # the names step's measured limit: ~4 calls a second, 429 past a burst
        self._lock = threading.Lock()
        self._next = 0.0

    @classmethod
    def from_env(cls, http_client=None):
        return cls(http_client)                     # keyless: always usable

    def fetch_deployer(self, chain_id: int, address: str) -> tuple[str | None, str]:
        """(deployer, outcome): "found" with the lowercased sender of the creation transaction; "miss" when Sourcify
        answered and has no deployer to give (404 not verified, 400 unsupported chain, a verified contract without a
        deployment record, or a shared CREATE2 deployer, which is never anyone's wallet); "blocked" on 403;
        "transient" on a 429 that outlasted the retries, 408, 5xx, a transport error, or a 200 that is not a JSON
        object (ask again next run)."""
        try:
            r = self._get(f"{BASE}/{chain_id}/{address}", {"fields": "deployment"})
        except httpx.HTTPError:
            return None, "transient"
        if r.status_code in (408, 429) or r.status_code >= 500:
            return None, "transient"
        if r.status_code == 403:
            return None, "blocked"
        if r.status_code != 200:
            return None, "miss"
        try:
            d = r.json()
        except ValueError:
            return None, "transient"
        if not isinstance(d, dict):
            return None, "transient"
        dep = d.get("deployment")
        deployer = deployer_or_none(dep.get("deployer")) if isinstance(dep, dict) else None
        return (deployer, "found") if deployer else (None, "miss")

    def _get(self, url: str, params: dict) -> httpx.Response:
        """Paced GET; a 429 waits and asks again (RETRY_429_S) before the caller sees it."""
        for wait in RETRY_429_S:
            self._pace()
            r = self.c.get(url, params=params)
            if r.status_code != 429:
                return r
            self.sleep(wait)
        self._pace()
        return self.c.get(url, params=params)

    def _pace(self) -> None:
        with self._lock:                            # reserve a slot; workers then sleep outside the lock
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + self.pace_s
        if at > now:
            self.sleep(at - now)
