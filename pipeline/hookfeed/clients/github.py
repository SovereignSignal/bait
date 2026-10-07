"""GitHub REST: last commit + stars for a repo URL."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..config import env
from . import http

_REPO_RE = re.compile(r"github\.com/([\w.-]+)(?:/([\w.-]+))?")


def parse_repo(url: str | None) -> tuple[str, str | None] | None:
    if not url:
        return None
    m = _REPO_RE.search(url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo:
        repo = repo.removesuffix(".git")
    return owner, repo


class GitHub:
    def __init__(self, token: str, http_client=None):
        self.c = http_client or http.client(timeout=30.0)
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    @classmethod
    def from_env(cls, http_client=None):
        tok = env("GITHUB_TOKEN")
        return cls(tok, http_client) if tok else None

    def activity(self, url: str, days: int = 30, now: datetime | None = None) -> dict | None:
        parsed = parse_repo(url)
        if not parsed:
            return None
        owner, repo = parsed
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not repo:  # org URL: pick the most recently pushed public repo
            repos = http.get_json(self.c, f"https://api.github.com/users/{owner}/repos",
                                  params={"sort": "pushed", "per_page": 1}, headers=self.headers)
            if not repos:
                return None
            repo = repos[0]["name"]
        meta = http.get_json(self.c, f"https://api.github.com/repos/{owner}/{repo}", headers=self.headers)
        commits = http.get_json(self.c, f"https://api.github.com/repos/{owner}/{repo}/commits",
                                params={"since": since, "per_page": 1}, headers=self.headers)
        last = None
        if commits:
            last = commits[0].get("commit", {}).get("committer", {}).get("date")
        elif meta.get("pushed_at"):
            last = meta["pushed_at"]
        return {"repo": f"{owner}/{repo}", "stars": meta.get("stargazers_count", 0),
                "last_commit_at": last, "commits_30d": len(commits) > 0}
