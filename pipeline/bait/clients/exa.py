"""Exa search, last 30 days, for receipts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from ..config import env
from . import http


class Exa:
    def __init__(self, api_key: str, http_client=None):
        self.c = http_client or http.client(timeout=60.0)
        self.headers = {"x-api-key": api_key}

    @classmethod
    def from_env(cls, http_client=None):
        key = env("EXA_API_KEY")
        return cls(key, http_client) if key else None

    def search_recent(self, query: str, days: int = 30, n: int = 10, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        body = {
            "query": query, "type": "auto", "numResults": n,
            "startPublishedDate": (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "contents": {"highlights": True},
        }
        d = http.post_json(self.c, "https://api.exa.ai/search", json=body, headers=self.headers)
        out = []
        for r in d.get("results", []):
            url = r.get("url")
            if not url:
                continue
            out.append({
                "url": url,
                "title": (r.get("title") or "")[:300],
                "source": urlparse(url).netloc.removeprefix("www."),
                "published_at": _parse_dt(r.get("publishedDate")),
                "highlights": (r.get("highlights") or [])[:2],
            })
        return out


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
