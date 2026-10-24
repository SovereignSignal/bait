"""Published audit links that are gone. Nightly, from the signals step (free, no key).

The "Audit linked" badge links what the registry (hooks.audit_url) or the team's details (enrichment.parallel.audit_url)
publish. A link can die after it is published: on 2026-10-24 Angstrom's hooklist link, a PDF in spearbit/portfolio, was
a 404 because that repository dropped its pdfs folder. Each night every published link is asked once; a 404 or 410 is a
dead link and goes in the project's `signals` row of kind "audit_links" ({"dead": [url, ...]}), which the web leaves out
of the badge and the count. Only a definite answer moves a verdict: a timeout, a bot wall (403, 429) or a 5xx keeps the
last one, so a flaky host never hides a live audit and a dead link is not revived by an outage.

The fetch reads status lines only (HEAD, or GET without reading the body when a host refuses HEAD) and only from public
hosts: registry entries are third-party input, so an address literal, a private or link-local address, or an internal
name is never requested, and each redirect is checked again before it is followed.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import time
from typing import Callable
from urllib.parse import urljoin, urlsplit

import sqlalchemy as sa

from .links import web_url

log = logging.getLogger("bait")

KIND = "audit_links"
DEAD = frozenset({404, 410})
REDIRECTS = frozenset({301, 302, 303, 307, 308})
MAX_HOPS = 5
_INTERNAL = (".internal", ".local", ".localhost", ".lan", ".home.arpa", ".intranet", ".corp")

Probe = Callable[[str], "int | None"]


def published(value: object) -> str | None:
    """The link exactly as stored when it is a web URL the page would show, else None. The web compares the stored
    string, so the dead list must hold the same string, not a normalized one."""
    return value if isinstance(value, str) and web_url(value) == value else None


def public_url(url: str, resolve: Callable[[str], list[str]] | None = None) -> bool:
    """A web URL on a public host: not an address literal, not localhost or an internal name, and (when `resolve` is
    given) every address the name resolves to is global."""
    if not web_url(url):
        return False
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(_INTERNAL):
        return False
    try:
        ipaddress.ip_address(host)
        return False                       # a published audit is never at a bare address
    except ValueError:
        pass
    if resolve is not None:
        try:
            addrs = resolve(host)
        except OSError:
            return False
        if not addrs or not all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs):
            return False
    return True


def _resolve(host: str) -> list[str]:
    return [ai[4][0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]


def http_probe(timeout: float = 10.0, transport=None, resolve: Callable[[str], list[str]] = _resolve) -> Probe:
    """Final status of a URL after at most MAX_HOPS redirects, each hop on a public host; None when it cannot be asked.
    `transport` and `resolve` are for tests (httpx.MockTransport, a fake DNS)."""
    import httpx

    from .clients.http import USER_AGENT
    client = httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": USER_AGENT}, transport=transport)

    def status(url: str) -> tuple[int, str | None]:
        r = client.head(url)
        if r.status_code in (403, 405, 501):           # some hosts refuse HEAD: ask once with GET, body unread
            with client.stream("GET", url) as g:
                return g.status_code, g.headers.get("location")
        return r.status_code, r.headers.get("location")

    def probe(url: str) -> int | None:
        for _ in range(MAX_HOPS + 1):
            if not public_url(url, resolve):
                return None
            try:
                code, location = status(url)
            except Exception as e:                     # network, TLS, malformed answer: unknown, never dead
                log.info("audit link %s not answered: %s", url, type(e).__name__)
                return None
            if code in REDIRECTS and location:
                url = urljoin(url, location)
                continue
            return code
        return None

    probe.close = client.close                         # type: ignore[attr-defined]
    return probe


def answered(status: int | None) -> bool:
    """A definite answer: the page is there (2xx) or it is gone (404, 410). Anything else is unknown (None, 401, 403,
    429, 5xx, a redirect past the hop limit)."""
    return status is not None and (200 <= status < 300 or status in DEAD)


def check(conn: sa.Connection, now, set_signal, probe: Probe | None = None) -> dict:
    """Probe every published audit link once and write each project's dead list. `set_signal(conn, pid, kind, value,
    now)` is the signals step's upsert (as_of moves only on a change). AUDIT_LINKS=0 turns the check off unless a probe
    is passed (tests); AUDIT_LINKS_MAX caps the links asked per night, AUDIT_LINKS_BUDGET_S the wall clock."""
    from .db import hooks, projects, signals
    out = {"audit_links": 0, "audit_links_dead": 0}
    if probe is None and os.environ.get("AUDIT_LINKS", "1") == "0":
        return out
    by_project: dict[int, set[str]] = {}
    for r in conn.execute(sa.select(hooks.c.project_id, hooks.c.audit_url)
                          .where(hooks.c.project_id.is_not(None), hooks.c.audit_url.is_not(None))):
        if u := published(r.audit_url):
            by_project.setdefault(r.project_id, set()).add(u)
    for r in conn.execute(sa.select(projects.c.id, projects.c.enrichment).where(projects.c.enrichment.is_not(None))):
        par = (r.enrichment or {}).get("parallel") if isinstance(r.enrichment, dict) else None
        if isinstance(par, dict) and (u := published(par.get("audit_url"))):
            by_project.setdefault(r.id, set()).add(u)
    prev = {r.project_id: set((r.value or {}).get("dead") or [])
            for r in conn.execute(sa.select(signals.c.project_id, signals.c.value).where(signals.c.kind == KIND))}
    if not by_project and not any(prev.values()):
        return out
    urls = sorted(set().union(*by_project.values())) if by_project else []
    cap = int(os.environ.get("AUDIT_LINKS_MAX", "300"))
    budget = float(os.environ.get("AUDIT_LINKS_BUDGET_S", "180"))
    if len(urls) > cap:
        log.warning("audit links: %d published, asking %d tonight (AUDIT_LINKS_MAX)", len(urls), cap)
        urls = urls[:cap]
    owned = probe is None
    probe = probe or http_probe()
    status: dict[str, int | None] = {}
    t0 = time.monotonic()
    try:
        for u in urls:
            if time.monotonic() - t0 > budget:
                log.warning("audit links: wall clock budget spent after %d of %d links", len(status), len(urls))
                break
            status[u] = probe(u)
    finally:
        if owned:
            probe.close()                              # type: ignore[attr-defined]
    out["audit_links"] = len(status)
    gone = set()
    for pid in by_project.keys() | prev.keys():
        us = by_project.get(pid, set())
        dead = sorted(u for u in us if status.get(u) in DEAD or (u in prev.get(pid, ()) and not answered(status.get(u))))
        gone.update(dead)
        if dead or prev.get(pid):
            set_signal(conn, pid, KIND, {"dead": dead}, now)
    out["audit_links_dead"] = len(gone)
    if gone:
        log.info("audit links: %d dead (%s)", len(gone), ", ".join(sorted(gone)[:5]))
    return out
