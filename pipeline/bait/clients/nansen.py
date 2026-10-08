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
        d = http.post_json(self.c, f"https://api.nansen.ai/api/v1/profiler/address/{self.path}", headers=self.headers,
                           json={"address": address, "chain": ch.slug, "pagination": {"page": 1, "per_page": 100}})
        return parse_labels(d)

    @property
    def path(self) -> str:
        return "premium-labels" if self.premium else "labels"

    @property
    def source(self) -> str:
        return "nansen-" + self.path


def parse_labels(d) -> list[str]:
    """Labels may sit under data or labels (or data.labels); each item is a string or an object with label/name."""
    items = d
    if isinstance(d, dict):
        items = d.get("data")
        if isinstance(items, dict):
            items = items.get("labels") or items.get("data") or []
        if not items:
            items = d.get("labels") or []
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for it in items:
        if isinstance(it, str):
            v = it
        elif isinstance(it, dict):
            v = it.get("label") or it.get("name") or ""
        else:
            continue
        v = str(v).strip()
        if v and v not in out:
            out.append(v)
    return out


def is_smart_money(labels: list[str]) -> bool:
    return any(m in lab.lower() for lab in labels for m in SMART_MONEY_MARKERS)


# ---- credit budget -------------------------------------------------------------------------------------------
# Nansen bills per lookup that returns labels (about 100 credits on /labels, about 500 on /premium-labels; empty
# answers are free). Every lookup is stored in wallet_labels and an address is re-queried at most once per
# NANSEN_RELABEL_DAYS (default 30), so rows with as_of in the last 30 days are an exact ledger of recent spend.
DEFAULT_MAX_ADDRESSES = 10        # per run
DEFAULT_RELABEL_DAYS = 30
DEFAULT_MONTHLY_CREDITS = 10_000  # rolling 30 days, across every step that calls Nansen


def _env_int(name: str, default: int) -> int:
    import os
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def max_addresses() -> int:
    return _env_int("NANSEN_MAX_ADDRESSES", DEFAULT_MAX_ADDRESSES)


def relabel_days() -> int:
    return max(30, _env_int("NANSEN_RELABEL_DAYS", DEFAULT_RELABEL_DAYS))  # >= 30 keeps the ledger exact


def cost_per_hit() -> int:
    import os
    return 500 if os.environ.get("NANSEN_PREMIUM_LABELS") == "1" else 100


def credits_spent_30d(conn, now) -> int:
    """Estimated credits spent in the last 30 days: labeled answers x cost per hit."""
    from datetime import timedelta

    import sqlalchemy as sa

    from ..db import wallet_labels
    rows = conn.execute(sa.select(wallet_labels.c.labels).where(wallet_labels.c.as_of >= now - timedelta(days=30))).all()
    return sum(cost_per_hit() for r in rows if r.labels)


def budget_left(conn, now) -> int:
    return _env_int("NANSEN_MONTHLY_CREDITS", DEFAULT_MONTHLY_CREDITS) - credits_spent_30d(conn, now)
