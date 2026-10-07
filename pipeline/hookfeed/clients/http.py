"""Shared httpx client + retry policy."""
from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

USER_AGENT = "hookfeed-pipeline/0.1 (+https://github.com/SovereignSignal)"


def client(timeout: float = 30.0, **kw) -> httpx.Client:
    headers = {"User-Agent": USER_AGENT, **kw.pop("headers", {})}
    return httpx.Client(timeout=timeout, headers=headers, follow_redirects=True, **kw)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (exc.response.status_code == 429 or exc.response.status_code >= 500)


retrying = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_retryable),
)


@retrying
def get_json(c: httpx.Client, url: str, **kw):
    r = c.get(url, **kw)
    r.raise_for_status()
    return r.json()


@retrying
def post_json(c: httpx.Client, url: str, **kw):
    r = c.post(url, **kw)
    r.raise_for_status()
    return r.json()
