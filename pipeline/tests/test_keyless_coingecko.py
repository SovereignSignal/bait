"""COINGECKO_API_KEY: GeckoTerminal calls on CoinGecko's pro /onchain endpoints at the plan's pace; unset, unchanged."""
import io
import json
import logging
import types
import urllib.error
import urllib.request

import pytest

from bait import keyless as K
from bait import repair
from bait.steps.snapshot import keyless_http
from tests.conftest import DAY

KEY = "CG-test-not-a-real-key"
GT = "https://api.geckoterminal.com/api/v2"
PRO = "https://pro-api.coingecko.com/api/v3/onchain"
PATH = "/networks/base/pools/0xabc/ohlcv/day?aggregate=1&limit=15&currency=usd"
KEYLESS_HEADERS = {"Accept": "application/json", "User-agent": "bait/0.1", "Content-type": "application/json"}


@pytest.fixture
def env(monkeypatch):
    for k in ("COINGECKO_API_KEY", "COINGECKO_CALLS_PER_MIN", "KEYLESS_SPACING"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


class Wire:
    """urlopen and keyless's clock: records every request and sleep, answers HTTP codes from a script (then 200)."""

    def __init__(self, monkeypatch, codes=()):
        self.requests: list = []
        self.sleeps: list[float] = []
        self.now = 1_000_000.0
        self.codes = list(codes)
        monkeypatch.setattr(urllib.request, "urlopen", self.urlopen)
        monkeypatch.setattr(K, "time", types.SimpleNamespace(time=lambda: self.now, sleep=self.sleep, monotonic=lambda: self.now))

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s

    def urlopen(self, req, timeout=None):
        self.requests.append((req, timeout))
        code = self.codes.pop(0) if self.codes else 200
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "scripted", {}, io.BytesIO(b"{}"))
        return io.BytesIO(json.dumps({"data": []}).encode())

    @property
    def urls(self) -> list[str]:
        return [r.full_url for r, _ in self.requests]


def test_unset_key_sends_the_keyless_request_unchanged(env):
    w = Wire(env)
    http = K.http_from_env()
    assert (http.api_key, http.gt_spacing) == (None, 10.0)
    assert http.gt(PATH) == {"data": []}
    (req, timeout), = w.requests
    assert req.full_url == GT + PATH and req.data is None and req.get_method() == "GET" and timeout == 40
    assert dict(req.header_items()) == KEYLESS_HEADERS


def test_a_blank_key_is_no_key(env):
    env.setenv("COINGECKO_API_KEY", "   ")
    w = Wire(env)
    http = K.http_from_env()
    http.gt(PATH)
    assert http.api_key is None and w.urls == [GT + PATH] and dict(w.requests[0][0].header_items()) == KEYLESS_HEADERS


def test_a_key_sends_the_same_path_to_the_pro_onchain_root_with_the_key_in_a_header_only(env, caplog):
    env.setenv("COINGECKO_API_KEY", KEY)
    w = Wire(env)
    with caplog.at_level(logging.INFO, logger="bait"):
        http = K.http_from_env()
        assert http.gt(PATH) == {"data": []}
    (req, timeout), = w.requests
    assert req.full_url == PRO + PATH and KEY not in req.full_url and timeout == 40
    assert dict(req.header_items()) == {**KEYLESS_HEADERS, "X-cg-pro-api-key": KEY}
    assert "GeckoTerminal through the CoinGecko API (COINGECKO_API_KEY set): 500 calls/min, 0.12 s apart" in caplog.text
    assert KEY not in caplog.text


@pytest.mark.parametrize("env_vars, spacing", [
    ({}, 10.0),
    ({"KEYLESS_SPACING": "7"}, 7.0),
    ({"COINGECKO_CALLS_PER_MIN": "250"}, 10.0),                                   # no key: the plan's knob is ignored
    ({"COINGECKO_API_KEY": KEY}, 0.12),                                           # Analyst plan: 500 calls/min
    ({"COINGECKO_API_KEY": KEY, "KEYLESS_SPACING": "7"}, 0.12),                   # the keyless knob does not slow a keyed run
    ({"COINGECKO_API_KEY": KEY, "COINGECKO_CALLS_PER_MIN": "250"}, 0.24),
    ({"COINGECKO_API_KEY": KEY, "COINGECKO_CALLS_PER_MIN": "fast"}, 0.12),        # not a number: the default, with a warning
    ({"COINGECKO_API_KEY": KEY, "COINGECKO_CALLS_PER_MIN": "0"}, 0.12),
    ({"COINGECKO_API_KEY": KEY, "COINGECKO_CALLS_PER_MIN": "-5"}, 0.12),
])
def test_calls_are_spaced_at_the_keyless_pace_or_the_plans(env, env_vars, spacing):
    for k, v in env_vars.items():
        env.setenv(k, v)
    w = Wire(env)
    http = K.http_from_env()
    assert http.gt_spacing == pytest.approx(spacing)
    for _ in range(3):
        http.gt(PATH)
    assert len(w.requests) == 3
    assert w.sleeps == pytest.approx([spacing, spacing])          # the first call goes at once, each later one waits the gap


def test_a_keyed_run_starts_at_most_500_calls_a_minute(env):
    env.setenv("COINGECKO_API_KEY", KEY)
    w = Wire(env)
    http = K.http_from_env()
    start = w.now
    for _ in range(501):
        http.gt(PATH)
    assert w.now - start == pytest.approx(60.0)                   # call 501 starts a full minute after call 1


def test_a_bad_plan_rate_is_named_in_a_warning(env, caplog):
    env.setenv("COINGECKO_API_KEY", KEY)
    env.setenv("COINGECKO_CALLS_PER_MIN", "fast")
    with caplog.at_level(logging.WARNING, logger="bait"):
        assert K.keyed_spacing() == pytest.approx(0.12)
    assert "COINGECKO_CALLS_PER_MIN='fast' is not a positive number; using 500" in caplog.text


def test_the_cli_spacing_stays_the_keyless_pace(env):
    env.setenv("KEYLESS_SPACING", "7")                            # the CLI never read it, and still does not
    assert K.http_from_env(keyless=6.5).gt_spacing == 6.5
    env.setenv("COINGECKO_API_KEY", KEY)
    http = K.http_from_env(keyless=6.5)
    assert (http.gt_spacing, http.fallback_spacing) == (pytest.approx(0.12), 6.5)


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_key_turns_the_rest_of_the_run_keyless_instead_of_empty(env, caplog, code):
    env.setenv("COINGECKO_API_KEY", KEY)
    env.setenv("KEYLESS_SPACING", "9")
    w = Wire(env, [code])
    http = K.http_from_env()
    with caplog.at_level(logging.WARNING, logger="bait"):
        assert http.gt(PATH) == {"data": []}
        assert http.gt(PATH) == {"data": []}
    assert w.urls == [PRO + PATH, GT + PATH, GT + PATH]
    assert [dict(r.header_items()) for r, _ in w.requests[1:]] == [KEYLESS_HEADERS, KEYLESS_HEADERS]
    assert w.sleeps == [9.0, 9.0] and (http.api_key, http.gt_spacing) == (None, 9.0)
    assert f"CoinGecko API refused the key (HTTP {code}); GeckoTerminal calls go keyless for the rest of this run, 9 s apart" in caplog.text
    assert KEY not in caplog.text


def test_a_key_still_rate_limited_on_its_third_try_falls_back_to_keyless(env, caplog):
    env.setenv("COINGECKO_API_KEY", KEY)
    w = Wire(env, [429, 429, 429])
    http = K.http_from_env()
    with caplog.at_level(logging.WARNING, logger="bait"):
        assert http.gt(PATH) == {"data": []}
    assert w.urls == [PRO + PATH] * 3 + [GT + PATH]
    assert w.sleeps == [20, 40, 10.0]                             # a minute of backoff, then the keyless pace
    assert "still answered 429 on a third try" in caplog.text and http.api_key is None


def test_one_429_with_a_key_backs_off_and_stays_keyed(env):
    env.setenv("COINGECKO_API_KEY", KEY)
    w = Wire(env, [429])
    http = K.http_from_env()
    assert http.gt(PATH) == {"data": []}
    assert w.urls == [PRO + PATH] * 2 and w.sleeps == [20] and http.api_key == KEY


def test_a_404_with_a_key_is_a_missing_pool_not_a_bad_key(env):
    env.setenv("COINGECKO_API_KEY", KEY)
    w = Wire(env, [404])
    http = K.http_from_env()
    assert http.gt(PATH) is None
    assert w.urls == [PRO + PATH] and http.api_key == KEY


def test_keyless_429s_keep_their_six_tries_and_backoff(env, caplog):
    w = Wire(env, [429] * 6)
    http = K.http_from_env()
    with caplog.at_level(logging.WARNING, logger="bait"):
        assert http.gt(PATH) is None
    assert w.urls == [GT + PATH] * 6 and w.sleeps == [20, 40, 60, 80, 100, 120]
    assert "GT gave up after 6 attempts" in caplog.text and "CoinGecko" not in caplog.text


def test_the_runs_shared_client_follows_the_key(env, ctx):
    assert keyless_http(ctx).api_key is None
    ctx.extras.clear()
    env.setenv("COINGECKO_API_KEY", KEY)
    http = keyless_http(ctx)
    assert http.api_key == KEY and http.gt_spacing == pytest.approx(0.12) and http.fallback_spacing == 10.0
    assert keyless_http(ctx) is http                              # one client, one pace, per run


def test_repair_plans_use_the_runs_pace_and_say_what_calls_cost(env, engine):
    def gt_line():
        with engine.connect() as c:
            return next(x for x in repair.backfill_plan(c, DAY, 14, None, repair._spacing())[0] if "GeckoTerminal calls" in x)
    assert repair._spacing() == 10.0 and gt_line().endswith("10 s apart: about 0 min, free")
    env.setenv("COINGECKO_API_KEY", KEY)
    assert repair._spacing() == pytest.approx(0.12) and gt_line().endswith("0.12 s apart: about 0 min, 0 CoinGecko call credits")
    assert K.gt_cost(15) == "15 CoinGecko call credits"
