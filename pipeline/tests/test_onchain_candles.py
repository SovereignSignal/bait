"""Bounded UTC-candle repair: recorded Aegis amounts, isolated local database, no live requests."""
import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from bait import db
from bait.rules import custom_accounting
from bait.steps import onchain, score

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "aegis-ink-2026-10-13.json"


class _Candles:
    def __init__(self, response):
        self.response, self.calls = response, []

    def gt(self, path):
        self.calls.append(path)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _replay(conn, ctx, monkeypatch, response="fixture"):
    recorded = json.loads(FIXTURE.read_text())
    pid, cid, hook = recorded["pool_id"], recorded["chain_id"], recorded["hook_address"]
    ctx.day = date.fromisoformat(recorded["day"])
    # Controlled replay clock/readiness, not a claim about production readiness at the original run time.
    ctx.now = datetime.combine(ctx.day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=6)
    ctx.chains = [cid]
    conn.execute(sa.insert(db.projects).values(id=recorded["project_id"], slug="aegis", name="Aegis", first_seen_at=ctx.now))
    conn.execute(sa.insert(db.hooks).values(address=hook, chain_id=cid, project_id=recorded["project_id"], first_seen_at=ctx.now))
    tokens = sorted(recorded["gt_response"]["meta"][side]["address"] for side in ("base", "quote"))
    # Membership and token addresses are captured; no Initialize block/time/fee is fabricated.
    conn.execute(sa.insert(db.pools).values(pool_id=pid, chain_id=cid, hook_address=hook,
                                          token0={"id": tokens[0]}, token1={"id": tokens[1]}))
    conn.execute(sa.insert(db.v4_pools).values(pool_id=pid, chain_id=cid, hook_address=hook,
                                              currency0=tokens[0], currency1=tokens[1]))
    for table, key in ((db.pool_snapshots, "snapshots"), (db.pool_daily_onchain, "raw_daily")):
        rows = [{**r, "day": date.fromisoformat(r["day"])} for r in recorded[key]]
        conn.execute(sa.insert(table), rows)
    conn.execute(sa.insert(db.indexer_state).values(chain_id=cid, backfilled=True, updated_at=ctx.now, swap_block_time=ctx.now))
    conn.execute(sa.insert(db.pool_stats).values(pool_id=pid, chain_id=cid, day=ctx.day,
                                                vol_24h=recorded["snapshots"][-1]["volume_usd_day"]))
    monkeypatch.setenv("KEYLESS", "1")
    http = _Candles(recorded["gt_response"] if response == "fixture" else response)
    ctx.extras["keyless_http"] = http
    return recorded, http


def _snapshot(conn, recorded, day=None):
    return conn.execute(sa.select(db.pool_snapshots).where(
        db.pool_snapshots.c.pool_id == recorded["pool_id"], db.pool_snapshots.c.chain_id == recorded["chain_id"],
        db.pool_snapshots.c.day == (day or date(2026, 10, 13)))).one()


def _score(conn, ctx, recorded):
    score.run(conn, ctx)
    return conn.execute(sa.select(db.project_daily).where(db.project_daily.c.project_id == recorded["project_id"],
                                                         db.project_daily.c.day == ctx.day)).one()


def test_recorded_aegis_candle_replaces_estimate_once_without_using_raw_usd(conn, ctx, monkeypatch):
    recorded, http = _replay(conn, ctx, monkeypatch)
    assert custom_accounting(recorded["hook_address"])
    before = _score(conn, ctx, recorded)
    prior_candle = dict(_snapshot(conn, recorded, date(2026, 10, 12))._mapping)
    out = onchain.run(conn, ctx)
    snap, after = _snapshot(conn, recorded), _score(conn, ctx, recorded)
    candle = 54_040.612305176204
    assert snap.volume_usd_day == candle and snap.estimated is False and snap.tx_count_day == 761
    assert candle != 54_002.80306588893                 # PoolManager custom-accounting USD remains corroboration only
    assert (before.vol_24h, after.vol_24h) == (9_412.7449754386, candle)
    assert after.vol_7d == pytest.approx(before.vol_7d + candle - before.vol_24h)
    assert after.swaps_7d == before.swaps_7d == sum(r["swaps"] for r in recorded["raw_daily"] if r["day"] <= "2026-10-13")
    assert after.tvl_usd == before.tvl_usd
    assert dict(_snapshot(conn, recorded, date(2026, 10, 12))._mapping) == prior_candle
    assert snap.volume_usd_cum == recorded["snapshots"][-1]["volume_usd_cum"]  # legacy counter never added to daily totals
    assert (out["candle_refresh_candidates"], out["candle_refresh_attempts"], out["candle_refresh_rows"]) == (1, 1, 1)
    assert http.calls == [f'/networks/ink/pools/{recorded["pool_id"]}/ohlcv/day?aggregate=1&limit=15&currency=usd']
    stats = conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.pool_id == recorded["pool_id"])).one()
    assert (stats.vol_24h, stats.vol_source) == (candle, "keyless_candle")
    expected = dict(after._mapping)
    again = onchain.run(conn, ctx)
    assert again["candle_refresh_attempts"] == 0 and len(http.calls) == 1
    assert dict(_score(conn, ctx, recorded)._mapping) == expected
    assert conn.execute(sa.select(db.pool_stats.c.vol_source).where(db.pool_stats.c.pool_id == recorded["pool_id"])).scalar_one() == "keyless_candle"


@pytest.mark.parametrize("failure", ["http", "exception", "missing_day", "non_midnight", "invalid_volume", "missing_volume"])
def test_candle_failure_never_replaces_a_custom_estimate_with_raw_or_zero(conn, ctx, monkeypatch, failure):
    recorded = json.loads(FIXTURE.read_text())
    response = copy.deepcopy(recorded["gt_response"])
    candles = response["data"]["attributes"]["ohlcv_list"]
    if failure == "http":
        response = None                                # shared client's exhausted 429/HTTP failure result
    elif failure == "exception":
        response = RuntimeError("429 rate limit")
    elif failure == "missing_day":
        response["data"]["attributes"]["ohlcv_list"] = [r for r in candles if r[0] != 1791849600]
    elif failure == "non_midnight":
        next(r for r in candles if r[0] == 1791849600)[0] += 1
    elif failure == "invalid_volume":
        next(r for r in candles if r[0] == 1791849600)[5] = float("nan")
    elif failure == "missing_volume":
        next(r for r in candles if r[0] == 1791849600)[5] = None
    recorded, http = _replay(conn, ctx, monkeypatch, response)
    before = dict(_snapshot(conn, recorded)._mapping)
    out = onchain.run(conn, ctx)
    assert dict(_snapshot(conn, recorded)._mapping) == before
    assert out["candle_refresh_attempts"] == 1 and out["candle_refresh_rows"] == 0 and out["candle_refresh_misses"] == 1
    assert len(http.calls) == 1


def test_partial_response_keeps_older_estimates_eligible_for_retry(conn, ctx, monkeypatch):
    recorded, http = _replay(conn, ctx, monkeypatch)
    older = date(2026, 10, 12)
    conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.day == older)
                 .values(volume_usd_day=1, estimated=True))
    http.response = copy.deepcopy(recorded["gt_response"])
    http.response["data"]["attributes"]["ohlcv_list"] = [r for r in http.response["data"]["attributes"]["ohlcv_list"]
                                                          if r[0] != 1791763200]
    assert onchain.run(conn, ctx)["candle_refresh_rows"] == 1
    assert _snapshot(conn, recorded, older).estimated is True
    http.response = recorded["gt_response"]
    assert onchain.run(conn, ctx)["candle_refresh_rows"] == 1
    assert _snapshot(conn, recorded, older).volume_usd_day == 292_272.7404112951
    assert _snapshot(conn, recorded, older).estimated is False
    assert len(http.calls) == 2


def test_full_pipeline_rerun_cannot_put_rolling_usd_back_under_closed_day_stats(conn, ctx, monkeypatch):
    recorded, http = _replay(conn, ctx, monkeypatch)
    onchain.run(conn, ctx)
    # snapshot's same-day keyless pass keeps the exact candle but resets stats to its new rolling 24-hour observation.
    conn.execute(sa.update(db.pool_stats).values(vol_24h=123, vol_24h_rolling=123, vol_source=None, window_end=ctx.now))
    out = onchain.run(conn, ctx)
    stats = conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.pool_id == recorded["pool_id"])).one()
    assert (stats.vol_24h, stats.vol_24h_rolling, stats.vol_source) == (54_040.612305176204, 123, "closed_day")
    assert db.as_utc(stats.window_end) == datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert out["candle_refresh_attempts"] == 0 and len(http.calls) == 1


@pytest.mark.parametrize("reason", ["disabled", "not_ready", "no_positive_swaps", "already_known", "explicit_unknown"])
def test_candle_refresh_only_targets_eligible_estimated_days(conn, ctx, monkeypatch, reason):
    recorded, http = _replay(conn, ctx, monkeypatch)
    if reason == "disabled":
        monkeypatch.setenv("ONCHAIN_CANDLE_REFRESH_CAP", "0")
    elif reason == "not_ready":
        conn.execute(sa.update(db.indexer_state).values(backfilled=False))
    elif reason == "no_positive_swaps":
        conn.execute(sa.update(db.pool_daily_onchain).where(db.pool_daily_onchain.c.day == date(2026, 10, 13)).values(swaps=0))
    elif reason == "already_known":
        conn.execute(sa.update(db.pool_snapshots).values(estimated=False))
    elif reason == "explicit_unknown":
        conn.execute(sa.update(db.pool_snapshots).values(volume_usd_cum=None, volume_usd_day=None))
    out = onchain.run(conn, ctx)
    assert out["candle_refresh_attempts"] == 0 and http.calls == []
    if reason == "explicit_unknown":
        assert _snapshot(conn, recorded).volume_usd_day is None


def test_candle_refresh_cap_is_shared_across_pool_candidates(conn, ctx, monkeypatch):
    recorded, http = _replay(conn, ctx, monkeypatch)
    pid, cid, hook = "0x" + "0" * 64, recorded["chain_id"], recorded["hook_address"]
    conn.execute(sa.insert(db.pools).values(pool_id=pid, chain_id=cid, hook_address=hook))
    conn.execute(sa.insert(db.pool_snapshots).values(pool_id=pid, chain_id=cid, day=date(2026, 10, 13),
                                                    volume_usd_cum=1, volume_usd_day=1, tx_count_day=1, estimated=True))
    conn.execute(sa.insert(db.pool_daily_onchain).values(pool_id=pid, chain_id=cid, day=date(2026, 10, 13), swaps=1, volume_usd=0))
    monkeypatch.setenv("ONCHAIN_CANDLE_REFRESH_CAP", "1")
    out = onchain.run(conn, ctx)
    # Same oldest day: Aegis's 761 observed swaps precede the one-swap synthetic candidate, without comparing USD.
    assert (out["candle_refresh_candidates"], out["candle_refresh_attempts"], out["candle_refresh_deferred"]) == (2, 1, 1)
    assert len(http.calls) == 1 and recorded["pool_id"] in http.calls[0]
    assert conn.execute(sa.select(db.pool_snapshots.c.estimated).where(db.pool_snapshots.c.pool_id == pid)).scalar_one() is True
