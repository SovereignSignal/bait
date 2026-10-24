from datetime import timedelta

import httpx
import sqlalchemy as sa

from bait import audit_links, db
from bait.steps import signals
from tests.conftest import NOW, insert_hook, insert_project

ANGSTROM = "https://github.com/spearbit/portfolio/blob/master/pdfs/Sorella-Spearbit-Security-Review-October-2024.pdf"
CANTINA = "https://cantina.xyz/portfolio/c2fe4e46-66a3-416e-ab26-40dd4b437ff6"
UPEG = "https://github.com/ChainCreators/Upeg/tree/main/upegs_hook"
FLAKY = "https://docs.example.org/audits"


def _audited(conn, name, url, parallel=None):
    pid = insert_project(conn, name, NOW - timedelta(days=30), slug=name.lower())
    insert_hook(conn, f"0x{name.lower()}", 1, project_id=pid)
    conn.execute(sa.update(db.hooks).values(audit_url=url).where(db.hooks.c.project_id == pid))
    if parallel:
        conn.execute(sa.update(db.projects).values(enrichment={"parallel": {"team": "Sorella", "audit_url": parallel}})
                     .where(db.projects.c.id == pid))
    return pid


def _dead(conn, pid):
    v = conn.execute(sa.select(db.signals.c.value).where(db.signals.c.project_id == pid, db.signals.c.kind == "audit_links")).scalar()
    return None if v is None else v["dead"]


def test_a_404_audit_link_is_dead_and_only_a_definite_answer_moves_the_verdict(conn):
    angstrom = _audited(conn, "Angstrom", ANGSTROM, parallel=CANTINA)
    upeg = _audited(conn, "Upeg", UPEG)
    flaky = _audited(conn, "Flaky", FLAKY)
    answers = {ANGSTROM: 404, CANTINA: 200, UPEG: 200, FLAKY: 503}
    out = audit_links.check(conn, NOW, signals._set_signal, answers.get)
    assert out == {"audit_links": 4, "audit_links_dead": 1}
    assert _dead(conn, angstrom) == [ANGSTROM]               # the registry's PDF is gone; the Cantina page is not
    assert _dead(conn, upeg) is None and _dead(conn, flaky) is None   # alive, or no verdict yet: no row at all

    answers.update({ANGSTROM: 429, FLAKY: 404})              # a rate limit does not revive a dead link
    audit_links.check(conn, NOW + timedelta(days=1), signals._set_signal, answers.get)
    assert _dead(conn, angstrom) == [ANGSTROM] and _dead(conn, flaky) == [FLAKY]

    answers.update({ANGSTROM: 200, FLAKY: None})             # fixed upstream: live again; a timeout keeps "dead"
    audit_links.check(conn, NOW + timedelta(days=2), signals._set_signal, answers.get)
    assert _dead(conn, angstrom) == [] and _dead(conn, flaky) == [FLAKY]


def test_audit_links_are_asked_once_each_and_kept_verbatim(conn):
    a = _audited(conn, "Alpha", ANGSTROM)
    b = _audited(conn, "Beta", ANGSTROM)                      # two projects publishing one link: one request
    _audited(conn, "Gamma", " https://x.org/audit.pdf")        # not a link the page would show: never asked
    asked = []

    def probe(u):
        asked.append(u)
        return 410
    audit_links.check(conn, NOW, signals._set_signal, probe)
    assert asked == [ANGSTROM]
    assert _dead(conn, a) == _dead(conn, b) == [ANGSTROM]


def test_signals_step_checks_every_project_and_counts_a_failure_as_a_soft_error(conn, ctx):
    pid = _audited(conn, "Angstrom", ANGSTROM)                # not a New or Trending target tonight
    ctx.extras["audit_probe"] = {ANGSTROM: 404}.get
    out = signals.run(conn, ctx)
    assert out["projects"] == 0 and out["audit_links"] == 1 and out["audit_links_dead"] == 1 and out["errors"] == 0
    assert _dead(conn, pid) == [ANGSTROM]

    def broken(u):
        raise RuntimeError("boom")
    ctx.extras["audit_probe"] = broken
    out = signals.run(conn, ctx)
    assert out["errors"] == 1 and _dead(conn, pid) == [ANGSTROM]   # the earlier verdict stands


def test_switched_off_without_a_probe(conn, monkeypatch):
    _audited(conn, "Angstrom", ANGSTROM)
    monkeypatch.setenv("AUDIT_LINKS", "0")
    assert audit_links.check(conn, NOW, signals._set_signal) == {"audit_links": 0, "audit_links_dead": 0}


def test_only_public_hosts_are_asked():
    public = lambda host: ["140.82.112.3"]                     # noqa: E731
    assert audit_links.public_url("https://github.com/a/b", public)
    for bad in ("http://127.0.0.1/audit.pdf", "https://10.0.0.8/a", "https://[::1]/a", "https://localhost/a",
                "https://db.example.internal/a", "https://printer.local/a", "javascript:alert(1)", "ftp://x.org/a",
                "https://user:pw@x.org/a"):
        assert not audit_links.public_url(bad, public), bad
    assert not audit_links.public_url("https://rebind.example.com/a", lambda h: ["169.254.169.254"])
    assert not audit_links.public_url("https://mixed.example.com/a", lambda h: ["140.82.112.3", "10.1.2.3"])

    def nxdomain(h):
        raise OSError("no such host")
    assert not audit_links.public_url("https://gone.example.com/a", nxdomain)


def test_http_probe_follows_public_redirects_only_and_falls_back_to_get():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, str(req.url)))
        u = str(req.url)
        if u == "https://old.example.com/report.pdf":
            return httpx.Response(301, headers={"location": "https://new.example.com/report.pdf"})
        if u == "https://new.example.com/report.pdf":
            return httpx.Response(404)
        if u == "https://trap.example.com/a":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
        if u == "https://nohead.example.com/a":
            return httpx.Response(405 if req.method == "HEAD" else 200)
        if u == "https://loop.example.com/a":
            return httpx.Response(302, headers={"location": "/a"})
        return httpx.Response(200)
    probe = audit_links.http_probe(transport=httpx.MockTransport(handler), resolve=lambda h: ["93.184.216.34"])
    assert probe("https://old.example.com/report.pdf") == 404
    assert probe("https://trap.example.com/a") is None                       # the metadata address is never asked
    assert not any("169.254" in u for _, u in seen)
    assert probe("https://nohead.example.com/a") == 200
    assert ("GET", "https://nohead.example.com/a") in seen
    assert probe("https://loop.example.com/a") is None                        # past the hop limit: unknown
