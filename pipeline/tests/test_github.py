import httpx
import pytest

from bait.clients.github import GitHub


def _gh(handler):
    return GitHub("t", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("status", [403, 404, 451])
def test_private_deleted_or_blocked_repo_is_none_not_an_error(status):
    assert _gh(lambda req: httpx.Response(status, json={"message": "x"}, request=req)).activity("https://github.com/acme/gone") is None


def test_rate_limit_403_still_raises():
    gh = _gh(lambda req: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={}, request=req))
    with pytest.raises(httpx.HTTPStatusError):
        gh.activity("https://github.com/acme/repo")


def test_non_repo_urls_are_rejected_without_a_call():
    calls = []
    gh = _gh(lambda req: calls.append(req) or httpx.Response(200, json={}, request=req))
    for url in ("https://gist.github.com/x/abc", "https://github.com/orgs/acme", "https://evil.example/github.com/a/b",
                "https://github.com.evil.example/a/b", "https://github.com/", "not a url", None):
        assert gh.activity(url) is None
    assert calls == []


def test_repo_url_with_a_deep_path_reads_the_repo():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if req.url.path.endswith("/commits"):
            return httpx.Response(200, json=[], request=req)
        return httpx.Response(200, json={"stargazers_count": 3, "pushed_at": "2026-10-01T00:00:00Z"}, request=req)
    assert _gh(handler).activity("https://github.com/acme/repo/tree/main")["repo"] == "acme/repo"
    assert seen == ["/repos/acme/repo", "/repos/acme/repo/commits"]


def test_rate_limit_403_named_only_in_the_body_still_raises():
    gh = _gh(lambda req: httpx.Response(403, json={"message": "API rate limit exceeded for 1.2.3.4."}, request=req))
    with pytest.raises(httpx.HTTPStatusError):
        gh.activity("https://github.com/acme/repo")
