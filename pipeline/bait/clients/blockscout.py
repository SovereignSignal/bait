"""Blockscout: deployed bytecode (for code_hash) and verified source. Chain-conditional; some instances 403 bots."""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from ..config import CHAIN_BY_ID, env
from ..rules import code_hash as canonical_code_hash, is_proxy_code
from . import http


@dataclass
class CodeInfo:
    code_hash: str | None
    verified: bool = False
    name: str | None = None
    compiler: str | None = None
    source_snippet: str | None = None
    raw: dict = field(default_factory=dict)
    is_proxy: bool | None = None       # from the bytecode, or Blockscout's proxy_type / implementations


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
            is_proxy=is_proxy_code(code) or bool(d.get("proxy_type")) or bool(d.get("implementations")),
        )

    def _from_eth_get_code(self, base: str, address: str) -> CodeInfo | None:
        try:
            d = http.get_json(self.c, f"{base}/api", params={
                "module": "proxy", "action": "eth_getCode", "address": address, "tag": "latest", **self.params})
        except httpx.HTTPError:
            return None
        code = d.get("result") or ""
        if not isinstance(code, str) or not code.startswith("0x") or code == "0x":
            return None
        return CodeInfo(code_hash=canonical_code_hash(code), is_proxy=is_proxy_code(code))

