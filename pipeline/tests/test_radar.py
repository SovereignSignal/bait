from datetime import timedelta

import pytest
import sqlalchemy as sa

from bait import db
from bait.steps import radar
from tests.conftest import NOW, insert_hook, insert_project

ISSUE_BODY = """### Chain

robinhood

### Hook Address

0x3cc31DBb63ad4575Ea86cDab57bCc5c06fE22044

### Hook Name

WorkLaunchHook3

### Description

Immutable fee hook for the single WORK/IMD pool.

### Deployer Address

_No response_

### Audit URL

_No response_
"""


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def issue(n, title, body="", state="open", days_ago=1, pr=False):
    """pr=False: an issue; pr=None: an unmerged PR; pr=<iso>: a PR merged then."""
    d = {"number": n, "title": title, "body": body, "state": state, "created_at": iso(NOW - timedelta(days=days_ago)),
         "html_url": f"https://github.com/Uniswap/hooklist/{'pull' if pr is not False else 'issues'}/{n}"}
    if pr is not False:
        d["pull_request"] = {"merged_at": pr}
    return d


def repo(full, stars=3, days_ago=2, fork=False, desc="A v4 hook"):
    owner, name = full.split("/")
    return {"html_url": f"https://github.com/{full}", "full_name": full, "name": name, "owner": {"login": owner},
            "stargazers_count": stars, "pushed_at": iso(NOW - timedelta(days=days_ago)), "fork": fork, "description": desc}


class FakeGitHub:
    def __init__(self, pages=None, repos=None, code=None, authenticated=True):
        self.pages = pages or [[]]
        self.repos = repos or {}
        self.code = code or []
        self.authenticated = authenticated
        self.issue_calls = self.search_calls = self.code_calls = 0
        self.queries = []

    def issues(self, owner, repo, page=1, per_page=100):
        self.issue_calls += 1
        return self.pages[page - 1] if page <= len(self.pages) else []

    def search_repos(self, q, per_page=30):
        self.search_calls += 1
        self.queries.append(q)
        for key, items in self.repos.items():
            if key in q:
                return items
        return []

    def search_code(self, q, per_page=30):
        self.code_calls += 1
        return self.code


class FakeExa:
    def __init__(self, results):
        self.results, self.calls = results, 0

    def search_recent(self, query, days=30, n=10, now=None):
        self.calls += 1
        assert days == 60
        return self.results(query) if callable(self.results) else self.results


class FakeClaude:
    def __init__(self, score=0.9, kind="audit"):
        self.calls, self.score, self.kind = 0, score, kind

    def classify_radar(self, item):
        self.calls += 1
        s = self.score(item) if callable(self.score) else self.score
        return {"kind": self.kind, "summary": f"About {item['title']}.", "score": s}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(radar, "SLEEP", lambda s: None)


def rows(conn):
    return {r.url: r for r in conn.execute(sa.select(db.radar_items))}


def hit(url, title="t", days_ago=3):
    return {"url": url, "title": title, "source": "example.com", "published_at": NOW - timedelta(days=days_ago), "highlights": []}


# ---------- parsing ----------

def test_parse_issue_form_and_titles():
    sub = radar.parse_submission(issue(1, "hook: WorkLaunchHook3 (robinhood)", ISSUE_BODY))
    assert sub["chain_id"] == 4663 and sub["hook_address"] == "0x3cc31dbb63ad4575ea86cdab57bcc5c06fe22044"
    assert sub["name"] == "WorkLaunchHook3" and sub["description"].startswith("Immutable fee hook")

    title_only = radar.parse_submission(issue(2, "hook: 0x6cd60dddaca4a967f186b5d98d87bdbd4dd700cc on robinhood"))
    assert title_only["chain_id"] == 4663 and title_only["hook_address"] == "0x6cd60dddaca4a967f186b5d98d87bdbd4dd700cc"

    pr = radar.parse_submission(issue(3, "Add StablePairHook on Arbitrum",
                                      "Adds `hooks/arbitrum/0x630e9cac0fff557bd3a01daa111ed253c771bcc0.json`", pr=None))
    assert pr["chain_id"] == 42161 and pr["hook_address"] == "0x630e9cac0fff557bd3a01daa111ed253c771bcc0"
    assert pr["name"] == "StablePairHook"
    assert radar.chain_id_of("World Chain") == 480 and radar.chain_id_of("zora") == 7777777 and radar.chain_id_of("mars") is None


def test_registry_folds_bot_pr_into_issue_and_links_project(conn, ctx):
    pid = insert_project(conn, "Work", NOW - timedelta(days=5), slug="work")
    insert_hook(conn, "0x3cc31dbb63ad4575ea86cdab57bcc5c06fe22044", 4663, project_id=pid)
    page = [
        issue(11, "Add WorkLaunchHook3 hook on robinhood", "## Summary\nA fee hook.\n\nCloses #10", state="closed",
              pr="2026-10-05T00:00:00Z"),
        issue(10, "hook: WorkLaunchHook3 (robinhood)", ISSUE_BODY, state="closed"),
        issue(9, "Add OtherHook hook on base", "## Summary\nAdds a hook.\n", state="open", pr=None),   # standalone PR
        issue(8, "hook: Old (base)", "", days_ago=45),                                                # outside 30 days
    ]
    gh = FakeGitHub(pages=[page, [issue(7, "never fetched", "")]])
    ctx.clients["github"] = gh
    out = radar.run(conn, ctx)
    assert gh.issue_calls == 1                      # page 1 crossed the 30-day cutoff, so no page 2
    assert out["registry"] == 2 and out["linked"] == 1 and out["source_errors"] == 0
    got = rows(conn)
    folded = got["https://github.com/Uniswap/hooklist/issues/10"]
    assert folded.kind == "registry_submission" and folded.summary.startswith("Merged. Immutable fee hook")
    assert folded.chain_id == 4663 and folded.project_slug == "work" and folded.title == "WorkLaunchHook3"
    assert "https://github.com/Uniswap/hooklist/pull/11" not in got
    other = got["https://github.com/Uniswap/hooklist/pull/9"]
    assert other.summary.startswith("Open. Adds a hook") and other.chain_id == 8453 and other.project_slug is None


def test_registry_page_cap(conn, ctx, monkeypatch):
    monkeypatch.setenv("RADAR_REGISTRY_PAGES", "2")
    full = [issue(1000 - i, f"hook: H{i} (base)", "") for i in range(100)]
    gh = FakeGitHub(pages=[full, [issue(500 + i, f"hook: G{i} (base)", "") for i in range(100)], full])
    ctx.clients["github"] = gh
    out = radar.run(conn, ctx)
    assert gh.issue_calls == 2 and out["registry"] == 200


# ---------- repos ----------

def test_repo_filters():
    assert radar.keep_repo(repo("dev/fee-hook"), NOW)
    assert not radar.keep_repo(repo("dev/fee-hook", fork=True), NOW)
    assert not radar.keep_repo(repo("Uniswap/v4-periphery", stars=900), NOW)
    assert not radar.keep_repo(repo("dev/v4-hook-template", stars=5), NOW)
    assert radar.keep_repo(repo("dev/v4-hook-template", stars=21), NOW)          # starred template passes
    assert not radar.keep_repo(repo("dev/uhi-workshop", stars=0, days_ago=1), NOW)
    assert radar.keep_repo(repo("dev/quiet", stars=0, days_ago=5), NOW)           # fresh push, no stars
    assert not radar.keep_repo(repo("dev/quiet", stars=0, days_ago=20), NOW)      # no stars, not fresh
    assert not radar.keep_repo(repo("dev/stale", stars=50, days_ago=40), NOW)     # pushed outside 30 days


def test_per_owner_cap_keeps_most_starred():
    burst = [repo(f"launches/launch-{i}", stars=i % 3, days_ago=1 + i % 5) for i in range(8)] + [repo("dev/one")]
    kept = radar.per_owner(burst)
    assert len([r for r in kept if r["full_name"].startswith("launches/")]) == 3
    assert [r["stargazers_count"] for r in kept if r["full_name"].startswith("launches/")] == [2, 2, 1]
    assert any(r["full_name"] == "dev/one" for r in kept)


def test_repos_dedupe_cap_and_code_search(conn, ctx, monkeypatch):
    monkeypatch.setenv("RADAR_GITHUB_QUERIES", "5")
    shared = repo("dev/fee-hook", stars=4)
    gh = FakeGitHub(
        repos={'"uniswap v4"': [shared, repo("dev/v4-template", stars=2), repo("dev/forked", fork=True)],
               "topic:uniswap-v4-hooks": [shared, repo("lab/limit-orders", stars=12)],
               "repo:": [repo("code/only", stars=1)]},
        code=[{"repository": {"full_name": "code/only"}}, {"repository": {"full_name": "dev/fee-hook"}}])
    ctx.clients["github"] = gh
    out = radar.run(conn, ctx)
    # 4 repo queries + 1 code search = 5 = cap; the batched repo: resolve call would be the 6th, so it is not made
    assert gh.search_calls == 4 and gh.code_calls == 1 and out["github_calls"] == 5 + gh.issue_calls
    got = rows(conn)
    assert set(u for u, r in got.items() if r.kind == "repo") == {"https://github.com/dev/fee-hook", "https://github.com/lab/limit-orders"}
    r = got["https://github.com/dev/fee-hook"]
    assert r.stars == 4 and r.repo == "dev/fee-hook" and r.summary == "A v4 hook" and r.published_at is not None

    monkeypatch.setenv("RADAR_GITHUB_QUERIES", "8")
    gh2 = FakeGitHub(repos=gh.repos, code=gh.code)
    ctx.clients["github"] = gh2
    radar.run(conn, ctx)
    assert gh2.code_calls == 2 and any(q.startswith("repo:code/only") for q in gh2.queries)
    assert "https://github.com/code/only" in rows(conn)


def test_anonymous_github_skips_code_search_and_links_repo_to_project(conn, ctx):
    pid = insert_project(conn, "Limit", NOW - timedelta(days=50), slug="limit")
    conn.execute(sa.update(db.projects).values(github="https://github.com/Lab/Limit-Orders").where(db.projects.c.id == pid))
    gh = FakeGitHub(repos={"topic:uniswap-v4-hooks": [repo("lab/limit-orders", stars=12)]}, authenticated=False)
    ctx.clients["github"] = gh
    out = radar.run(conn, ctx)
    assert gh.code_calls == 0 and gh.search_calls == 4
    assert rows(conn)["https://github.com/lab/limit-orders"].project_slug == "limit" and out["linked"] == 1


# ---------- Exa + Claude ----------

def test_exa_dedupe_caps_existing_and_drop(conn, ctx, monkeypatch):
    monkeypatch.setenv("RADAR_EXA_QUERIES", "3")
    monkeypatch.setenv("RADAR_CLASSIFY_MAX", "3")
    # every query returns the same five URLs (one with a fragment) -> five candidates, not fifteen
    results = [hit("https://a.example/audit"), hit("https://a.example/audit#top"), hit("https://b.example/x", title="junk"),
               hit("https://c.example/y"), hit("https://d.example/z"), hit("https://e.example/old")]
    conn.execute(sa.insert(db.radar_items).values(url="https://e.example/old", kind="launch_post", title="old",
                                                  first_seen=NOW - timedelta(days=10), last_seen=NOW - timedelta(days=10), score=0.8))
    exa = FakeExa(results)
    claude = FakeClaude(score=lambda item: 0.2 if item["title"] == "junk" else 0.9)
    ctx.clients.update(exa=exa, claude=claude)
    out = radar.run(conn, ctx)
    assert exa.calls == 3 and claude.calls == 3 and out["classified"] == 3
    assert out["bumped"] == 1 and out["dropped"] == 1 and out["exa_new"] == 2
    got = rows(conn)
    assert db.as_utc(got["https://e.example/old"].last_seen) == NOW and db.as_utc(got["https://e.example/old"].first_seen) < NOW
    assert got["https://a.example/audit"].kind == "audit" and got["https://a.example/audit"].summary == "About t."
    assert got["https://b.example/x"].score == 0.2           # kept as a do-not-reclassify marker; the web hides < 0.5
    assert "https://d.example/z" not in got                  # over the classify cap, picked up next run

    # next run: stored URLs are not re-classified, the leftover one is
    claude2 = FakeClaude()
    ctx.clients["claude"] = claude2
    out2 = radar.run(conn, ctx)
    assert claude2.calls == 1 and out2["bumped"] == 4 and "https://d.example/z" in rows(conn)


def test_exa_needs_claude(conn, ctx):
    exa = FakeExa([hit("https://a.example/x")])
    ctx.clients["exa"] = exa
    out = radar.run(conn, ctx)
    assert exa.calls == 0 and out["classified"] == 0


# ---------- skips, failures, retention, idempotency ----------

def test_no_keys_no_network_and_retention(conn, ctx):
    conn.execute(sa.insert(db.radar_items).values(url="https://old.example", kind="repo", first_seen=NOW - timedelta(days=200),
                                                  last_seen=NOW - timedelta(days=91)))
    conn.execute(sa.insert(db.radar_items).values(url="https://keep.example", kind="repo", first_seen=NOW - timedelta(days=200),
                                                  last_seen=NOW - timedelta(days=89)))
    out = radar.run(conn, ctx)          # KEYLESS=0 (conftest) + github None -> no anonymous client either
    assert out["registry"] == out["repos"] == out["classified"] == out["github_calls"] == out["exa_calls"] == 0
    assert out["pruned"] == 1 and set(rows(conn)) == {"https://keep.example"}


def test_failing_source_does_not_stop_the_others(conn, ctx):
    class Broken(FakeGitHub):
        def issues(self, *a, **k):
            raise RuntimeError("403 rate limited")

    ctx.clients["github"] = Broken(repos={"topic:uniswap-v4-hooks": [repo("lab/limit-orders")]})
    out = radar.run(conn, ctx)
    assert out["source_errors"] == 1 and out["repos"] == 1 and "errors" not in out


def test_rerun_keeps_first_seen_and_refreshes_state(conn, ctx):
    ctx.clients["github"] = FakeGitHub(pages=[[issue(10, "hook: WorkLaunchHook3 (robinhood)", ISSUE_BODY)]])
    radar.run(conn, ctx)
    first = rows(conn)["https://github.com/Uniswap/hooklist/issues/10"]
    assert first.summary.startswith("Open.")
    ctx.now = NOW + timedelta(days=1)
    ctx.clients["github"] = FakeGitHub(pages=[[issue(10, "hook: WorkLaunchHook3 (robinhood)", ISSUE_BODY, state="closed")]])
    radar.run(conn, ctx)
    again = rows(conn)["https://github.com/Uniswap/hooklist/issues/10"]
    assert db.as_utc(again.first_seen) == NOW and db.as_utc(again.last_seen) == NOW + timedelta(days=1)
    assert again.summary.startswith("Closed.")
