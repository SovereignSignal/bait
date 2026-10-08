"""Nansen address labels. Chain-conditional (no Unichain, no Ink). Labels cost credits only when non-empty."""
from __future__ import annotations

from ..config import CHAIN_BY_ID, env
from . import http

SMART_MONEY_MARKERS = ("smart money", "fund", "smart trader", "smart lp", "smart dex")


class Nansen:
    def __init__(self, api_key: str, premium: bool = False, http_client=None):
        self.c = http_client or http.client(timeout=60.0)
        self.headers = {"apikey": api_key}
        self.premium = premium

    @classmethod
    def from_env(cls, http_client=None):
        key = env("NANSEN_API_KEY")
        if not key:
            return None
        return cls(key, premium=env("NANSEN_PREMIUM_LABELS") == "1", http_client=http_client)

    @staticmethod
    def supports(chain_id: int) -> bool:
        ch = CHAIN_BY_ID.get(chain_id)
        return bool(ch and ch.nansen)

    def labels(self, address: str, chain_id: int) -> list[str]:
        ch = CHAIN_BY_ID[chain_id]
        path = "premium-labels" if self.premium else "labels"
        d = http.post_json(self.c, f"https://api.nansen.ai/api/v1/profiler/address/{path}", headers=self.headers,
                           json={"address": address, "chain": ch.slug, "pagination": {"page": 1, "per_page": 50}})
        items = d.get("data") or d.get("labels") or []
        out = []
        for it in items:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict):
                out.append(str(it.get("label") or it.get("name") or ""))
        return [x for x in out if x]


def is_smart_money(labels: list[str]) -> bool:
    return any(m in lab.lower() for lab in labels for m in SMART_MONEY_MARKERS)
