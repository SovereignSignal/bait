"""Blockscout: deployed bytecode (for code_hash), verified source, and a contract's deployer. Chain-conditional; some
instances 403 bots."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

from ..config import CHAIN_BY_ID, env
from ..rules import code_hash as canonical_code_hash, deployer_or_none, proxy_flag, proxy_kind
from . import http


@dataclass
class CodeInfo:
    code_hash: str | None
    verified: bool = False
    name: str | None = None
    compiler: str | None = None
    source_snippet: str | None = None
    raw: dict = field(default_factory=dict)
    is_proxy: bool | None = None       # Blockscout's own verdict when it gives one, else the bytecode shape
    proxy_kind: str | None = None      # eip1167 | eip1967 | explorer | unknown


class Blockscout:
    def __init__(self, api_key: str | None = None, http_client=None):
        self.c = http_client or http.client(timeout=30.0)
        self.params = {"apikey": api_key} if api_key else {}

    @classmethod
    def from_env(cls, http_client=None):
        return cls(env("BLOCKSCOUT_API_KEY"), http_client)  # key optional; client always usable

    def base_url(self, chain_id: int) -> str | None:
        ch = CHAIN_BY_ID.get(chain_id)
        return ch.blockscout if ch else None

    def fetch_code(self, chain_id: int, address: str, snippet_chars: int = 4000) -> CodeInfo | None:
        """Returns None when the chain has no Blockscout or the request fails."""
        base = self.base_url(chain_id)
        if not base:
            return None
        try:
            d = http.get_json(self.c, f"{base}/api/v2/smart-contracts/{address}", params=self.params)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return self._from_eth_get_code(base, address)
            return None
        except httpx.HTTPError:
            return None
        code = d.get("deployed_bytecode") or ""
        if not code or code == "0x":
            return self._from_eth_get_code(base, address)
        src = d.get("source_code") or ""
        return CodeInfo(
            code_hash=canonical_code_hash(code),
            verified=bool(d.get("is_verified")),
            name=d.get("name"),
            compiler=d.get("compiler_version"),
            source_snippet=src[:snippet_chars] if src else None,
            raw={"language": d.get("language"), "is_proxy": bool(d.get("proxy_type"))},
            **_proxy_verdict(d, code),
        )

    def fetch_creator(self, chain_id: int, address: str) -> tuple[str | None, str]:
        """The wallet that deployed a contract: the sender of its creation transaction (what Sourcify reports as the
        deployer, and the tx.from the indexer counts swaps by), from the address record's creation_transaction_hash
        and that transaction's `from`, lowercased. When the transaction cannot be read, the record's
        creator_address_hash. A shared CREATE2 deployer (rules.SHARED_DEPLOYERS) is never returned: it is the creator
        Blockscout gives for StablePairHook on Ethereum (Arachnid's proxy) and for anyone else's CREATE2 deploys.
        Returns (deployer, outcome): "found"; "miss" (404, or a record without a usable deployer: an EOA, an unindexed
        chain, no instance, only a shared CREATE2 deployer); "blocked" (403: the instance challenges bots, as Base and
        Robinhood do from this server, so asking again tonight is pointless); "transient" (429, 5xx, a transport error
        or a non-JSON reply: ask next run)."""
        base = self.base_url(chain_id)
        if not base:
            return None, "miss"
        d, outcome = self._record(f"{base}/api/v2/addresses/{address}")
        if outcome:
            return None, outcome
        tx = d.get("creation_transaction_hash") or d.get("creation_tx_hash")
        if isinstance(tx, str) and _TX_HASH.fullmatch(tx):
            t, outcome = self._record(f"{base}/api/v2/transactions/{tx}")
            if outcome in ("blocked", "transient"):
                return None, outcome
            if sender := deployer_or_none(_hash_of((t or {}).get("from"))):
                return sender, "found"
        if creator := deployer_or_none(_hash_of(d.get("creator_address_hash"))):
            return creator, "found"
        return None, "miss"

    def _record(self, url: str) -> tuple[dict | None, str | None]:
        """A v2 JSON object, or (None, outcome) with fetch_creator's outcomes; a reply that is not an object is a miss."""
        try:
            d = http.get_json(self.c, url, params=self.params)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            return None, "miss" if code == 404 else "blocked" if code == 403 else "transient"
        except (httpx.HTTPError, ValueError):
            return None, "transient"
        return (d, None) if isinstance(d, dict) else (None, "miss")

    def _from_eth_get_code(self, base: str, address: str) -> CodeInfo | None:
        try:
            d = http.get_json(self.c, f"{base}/api", params={
                "module": "proxy", "action": "eth_getCode", "address": address, "tag": "latest", **self.params})
        except httpx.HTTPError:
            return None
        code = d.get("result") or ""
        if not isinstance(code, str) or not code.startswith("0x") or code == "0x":
            return None
        kind = proxy_kind(code)
        return CodeInfo(code_hash=canonical_code_hash(code), is_proxy=proxy_flag(kind), proxy_kind=kind)


_TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}")


def _hash_of(v) -> str | None:
    """An address field: Blockscout v2 gives {"hash": ...} objects for transaction parties, a bare string elsewhere."""
    return v.get("hash") if isinstance(v, dict) else v if isinstance(v, str) else None


def _proxy_verdict(d: dict, code: str) -> dict:
    """The bytecode shape decides; the explorer may only upgrade it. Blockscout returns proxy_type null and no
    implementations for a proxy it could not resolve, so its "not a proxy" never overrides an eip1967 runtime."""
    kind = proxy_kind(code)
    if d.get("proxy_type") or d.get("implementations"):
        return {"is_proxy": True, "proxy_kind": kind if kind in ("eip1167", "eip1967") else "explorer"}
    return {"is_proxy": proxy_flag(kind), "proxy_kind": kind}
