import httpx
import pytest

from bait.clients import http


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    for fn in ("get_json", "post_json", "post_json_paid"):
        if hasattr(http, fn):
            monkeypatch.setattr(getattr(http, fn).retry, "sleep", lambda s: None)


def _seq(*steps):
    calls = []

    def handler(req):
        step = steps[min(len(calls), len(steps) - 1)]
        calls.append(req)
        if isinstance(step, type):
            raise step("x", request=req)
        return httpx.Response(step, json={"ok": True}, request=req)
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


@pytest.mark.parametrize("first", [httpx.ReadTimeout, 500, 502, 503])
def test_paid_post_is_sent_once_when_the_provider_may_have_started(first):
    c, calls = _seq(first, 200)
    with pytest.raises((httpx.ReadTimeout, httpx.HTTPStatusError)):
        http.post_json_paid(c, "https://paid.test/run", json={})
    assert len(calls) == 1


@pytest.mark.parametrize("first", [httpx.ConnectError, httpx.ConnectTimeout, 429])
def test_paid_post_retries_when_the_request_never_landed(first):
    c, calls = _seq(first, 200)
    assert http.post_json_paid(c, "https://paid.test/run", json={}) == {"ok": True} and len(calls) == 2


def test_gets_keep_retrying_5xx_and_read_timeouts():
    c, calls = _seq(503, httpx.ReadTimeout, 200)
    assert http.get_json(c, "https://free.test/x") == {"ok": True} and len(calls) == 3


def test_parallel_and_exa_use_the_paid_post(monkeypatch):
    from bait.clients.exa import Exa
    from bait.clients.parallel import Parallel
    sent = []
    monkeypatch.setattr(http, "post_json", lambda *a, **k: pytest.fail("paid call through the retrying POST"))
    monkeypatch.setattr(http, "post_json_paid", lambda c, url, **k: sent.append(url) or {"run_id": "r1", "results": []})
    assert Parallel("k", http_client=object()).submit("P", "hints") == "r1"
    assert Exa("k", http_client=object()).search_recent("q") == []
    assert sent == ["https://api.parallel.ai/v1/tasks/runs", "https://api.exa.ai/search"]
