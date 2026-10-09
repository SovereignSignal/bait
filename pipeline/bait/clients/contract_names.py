"""Verified-source contract names for hooks nobody has named (no hooklist entry). All free.

Order per hook: Sourcify (keyless, every chain) -> Blockscout PRO (BLOCKSCOUT_API_KEY, free key, every Blockscout
chain; the per-chain instances for Base/Arbitrum/Polygon/Robinhood sit behind a Cloudflare challenge) -> the
per-chain Blockscout instance -> Routescan (keyless Etherscan-style API; Avalanche only).
Returns None when no source has verified code. Unverified contracts have no name anywhere, so a miss is normal.
"""
from __future__ import annotations

import threading
from collections import Counter
from typing import NamedTuple

import httpx

from ..config import CHAIN_BY_ID, env
from ..rules import GENERIC, useful_name  # noqa: F401  re-exported; the list lives in rules
from . import http

ROUTESCAN_CHAINS = {43114}


class Found(NamedTuple):
    name: str
    source: str
    is_proxy: bool | None = None      # True when a source reported a proxy (Sourcify proxyResolution, Blockscout)


class LookupTransient(Exception):
    """No source had a name and at least one failed in a way that may clear (network, 429, 5xx): ask again next run."""


class _Transient(Exception):
    pass


class _Skip(Exception):
    pass


def _impl_name(impls) -> str | None:
    """First useful implementation name of a proxy (Sourcify proxyResolution, Blockscout implementations)."""
    for i in impls or []:
        if n := useful_name((i or {}).get("name")):
            return n
    return None


def _blockscout_name(d: dict | None) -> tuple[str | None, bool | None]:
    d = d or {}
    proxy = bool(d.get("proxy_type") or d.get("implementations")) or None
    return _impl_name(d.get("implementations")) or (d.get("name") if d.get("is_verified", True) else None), proxy


class ContractNames:
    def __init__(self, blockscout_key: str | None = None, http_client=None):
        self.c = http_client or http.client(timeout=20.0)
        self.key = blockscout_key
        self.outcomes: Counter = Counter()        # "source:hit|miss|error|skipped|bad_payload", logged per run
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, http_client=None):
        return cls(env("BLOCKSCOUT_API_KEY"), http_client)   # always usable; the key only widens coverage

    def lookup(self, chain_id: int, address: str) -> Found | None:
        """Found(name, source, is_proxy) for verified code (a proxy's implementation name when known), else None. The
        first useful name wins; a generic one ("ERC1967Proxy") is kept only as the fallback while later sources may
        resolve the implementation. Raises LookupTransient when no useful name was found and a source failed transiently."""
        transient, proxy, fallback = False, None, None
        for source, fn in (("sourcify", self._sourcify), ("blockscout_pro", self._blockscout_pro),
                           ("blockscout", self._blockscout), ("routescan", self._routescan)):
            outcome, name = None, None
            try:
                name, flag = fn(chain_id, address) or (None, None)
                proxy = proxy or flag
            except _Skip:
                outcome = "skipped"
            except (httpx.HTTPError, _Transient):
                outcome, transient = "error", True
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                outcome = "bad_payload"
            useful = useful_name(name)
            if name and not useful:
                outcome = "generic"
                fallback = fallback or (name.strip(), source)
            with self._lock:
                self.outcomes[f"{source}:{outcome or ('hit' if name else 'miss')}"] += 1
            if useful:
                return Found(useful, source, proxy)
        if transient:
            raise LookupTransient(f"{chain_id}:{address}")
        return Found(*fallback, proxy) if fallback else None

    def _get(self, url: str, params: dict | None = None) -> dict | None:
        r = self.c.get(url, params=params or {})
        if r.status_code == 429 or r.status_code >= 500:
            raise _Transient(r.status_code)
        if r.status_code != 200:
            return None       # 404 unverified; 403 Cloudflare challenge is persistent, so it is a miss, not a retry
        return r.json()

    def _sourcify(self, chain_id: int, address: str) -> tuple[str | None, bool | None]:
        d = self._get(f"https://sourcify.dev/server/v2/contract/{chain_id}/{address}",
                      {"fields": "compilation.name,proxyResolution"}) or {}
        pr = d.get("proxyResolution") or {}
        return _impl_name(pr.get("implementations")) or (d.get("compilation") or {}).get("name"), pr.get("isProxy") or None

    def _blockscout_pro(self, chain_id: int, address: str) -> tuple[str | None, bool | None] | None:
        if not (CHAIN_BY_ID.get(chain_id) and CHAIN_BY_ID[chain_id].blockscout):
            return None
        if not self.key:
            raise _Skip
        return _blockscout_name(self._get(f"https://api.blockscout.com/{chain_id}/api/v2/smart-contracts/{address}",
                                          {"apikey": self.key}))

    def _blockscout(self, chain_id: int, address: str) -> tuple[str | None, bool | None] | None:
        ch = CHAIN_BY_ID.get(chain_id)
        if not ch or not ch.blockscout:
            return None
        return _blockscout_name(self._get(f"{ch.blockscout}/api/v2/smart-contracts/{address}"))

    def _routescan(self, chain_id: int, address: str) -> tuple[str | None, None] | None:
        if chain_id not in ROUTESCAN_CHAINS:
            return None
        d = self._get(f"https://api.routescan.io/v2/network/mainnet/evm/{chain_id}/etherscan/api",
                      {"module": "contract", "action": "getsourcecode", "address": address})
        res = (d or {}).get("result")
        return (res[0].get("ContractName") if isinstance(res, list) and res else None), None
