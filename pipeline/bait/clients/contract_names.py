"""Verified-source contract names for hooks nobody has named (no hooklist entry). All free.

Order per hook: Sourcify (keyless, every chain) -> Blockscout PRO (BLOCKSCOUT_API_KEY, free key, every Blockscout
chain; the per-chain instances for Base/Arbitrum/Polygon/Robinhood sit behind a Cloudflare challenge) -> the
per-chain Blockscout instance -> Routescan (keyless Etherscan-style API; Avalanche only).
Returns None when no source has verified code. Unverified contracts have no name anywhere, so a miss is normal.
"""
from __future__ import annotations

import re

import httpx

from ..config import CHAIN_BY_ID, env
from . import http

ROUTESCAN_CHAINS = {43114}
# Names that say nothing about the project: proxies and template defaults.
GENERIC = re.compile(
    r"(?i)^(hook|basehook|v4hook|uniswapv4hook|uniswaphook|anyhook|customhook|myhook|testhook|counter|counterhook|"
    r"swapfeehook|feehook|platformhook|.*proxy.*|implementation|main|contract)$")


def useful_name(name: str | None) -> str | None:
    n = (name or "").strip()
    if not n or len(n) > 60 or GENERIC.match(n):
        return None
    return n


class ContractNames:
    def __init__(self, blockscout_key: str | None = None, http_client=None):
        self.c = http_client or http.client(timeout=20.0)
        self.key = blockscout_key

    @classmethod
    def from_env(cls, http_client=None):
        return cls(env("BLOCKSCOUT_API_KEY"), http_client)   # always usable; the key only widens coverage

    def lookup(self, chain_id: int, address: str) -> tuple[str, str] | None:
        """(name, source) for verified code, else None. Never raises."""
        for source, fn in (("sourcify", self._sourcify), ("blockscout_pro", self._blockscout_pro),
                           ("blockscout", self._blockscout), ("routescan", self._routescan)):
            try:
                name = fn(chain_id, address)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, AttributeError):
                name = None
            if name:
                return name.strip(), source
        return None

    def _get(self, url: str, params: dict | None = None) -> dict | None:
        r = self.c.get(url, params=params or {})
        if r.status_code != 200:
            return None
        return r.json()

    def _sourcify(self, chain_id: int, address: str) -> str | None:
        d = self._get(f"https://sourcify.dev/server/v2/contract/{chain_id}/{address}", {"fields": "compilation"})
        return ((d or {}).get("compilation") or {}).get("name")

    def _blockscout_pro(self, chain_id: int, address: str) -> str | None:
        if not self.key or not (CHAIN_BY_ID.get(chain_id) and CHAIN_BY_ID[chain_id].blockscout):
            return None
        d = self._get(f"https://api.blockscout.com/{chain_id}/api/v2/smart-contracts/{address}", {"apikey": self.key})
        return (d or {}).get("name") if (d or {}).get("is_verified", True) else None

    def _blockscout(self, chain_id: int, address: str) -> str | None:
        ch = CHAIN_BY_ID.get(chain_id)
        if not ch or not ch.blockscout:
            return None
        d = self._get(f"{ch.blockscout}/api/v2/smart-contracts/{address}")
        return (d or {}).get("name") if (d or {}).get("is_verified", True) else None

    def _routescan(self, chain_id: int, address: str) -> str | None:
        if chain_id not in ROUTESCAN_CHAINS:
            return None
        d = self._get(f"https://api.routescan.io/v2/network/mainnet/evm/{chain_id}/etherscan/api",
                      {"module": "contract", "action": "getsourcecode", "address": address})
        res = (d or {}).get("result")
        return res[0].get("ContractName") if isinstance(res, list) and res else None
