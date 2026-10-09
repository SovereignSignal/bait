from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from bait import db, keyless as K
from bait.steps import score, snapshot


def _pool(addr, hook, vol24, buyers, created, daily):
    return {"net": "base", "attrs": {"address": addr, "name": "ETH / USDC 0.3%", "pool_created_at": created,
                                     "reserve_in_usd": "2000000", "volume_usd": {"h24": str(vol24)},
                                     "transactions": {"h24": {"buys": 300, "sells": 200, "buyers": buyers, "sellers": 0}}},
            "key": {"currency0": "0x" + "0" * 40, "currency1": "0x" + "1" * 40, "fee": 3000, "tickSpacing": 60, "hooks": hook},
            "daily": daily}


def _t(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _run_keyless(conn, ctx, monkeypatch, found):
    monkeypatch.setenv("KEYLESS", "1")
    monkeypatch.setattr(K, "collect", lambda *a, **k: {("base", p["attrs"]["address"]): p for p in found})
    for name in ("resolve_hooks", "resolve_via_logs", "fetch_ohlcv"):
        monkeypatch.setattr(K, name, lambda *a, **k: None)
    monkeypatch.setattr(K, "code_hashes", lambda *a, **k: {})
    return snapshot.run(conn, ctx)


def _rows(conn, pool_id):
    return {r.day: r for r in conn.execute(sa.select(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == pool_id))}


def test_keyless_rows_feed_scoring_and_wash_is_excluded(conn, ctx, monkeypatch):
    day, end = ctx.day, ctx.day - timedelta(days=1)
    # candles day-14 .. day: $10K a day for the closed week day-7 .. day-1 (and the running day), $5K the week before
    daily = [(_t(day - timedelta(days=i)), 10_000.0 if i <= 7 else 5_000.0) for i in range(14, -1, -1)]
    real = _pool("0x" + "a" * 64, "0x" + "b" * 40, 10_000, 400, "2026-01-01T00:00:00Z", daily)
    wash = _pool("0x" + "c" * 64, "0x" + "d" * 40, 5_000_000, 5, "2026-01-01T00:00:00Z", daily)
    out = _run_keyless(conn, ctx, monkeypatch, [real, wash])
    assert out["keyless_pools"] == 2 and out["errors"] == 0 and out["estimated_pools"] == 0
    hooks = {r.address: r for r in conn.execute(sa.select(db.hooks))}
    assert hooks["0x" + "b" * 40].name is None          # unlisted: grouping names it (bytecode family or address)
    p = conn.execute(sa.select(db.pools).where(db.pools.c.pool_id == real["attrs"]["address"])).one()
    assert p.token0["symbol"] == "ETH" and p.token1["symbol"] == "USDC" and p.fee == 3000
    st = conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.pool_id == real["attrs"]["address"])).one()
    assert (st.buys_24h, st.sells_24h, st.buyers_24h, st.sellers_24h, st.traders_24h) == (300, 200, 400, 0, 400)
    assert (st.vol_24h, st.vol_24h_rolling, st.wallets_24h, db.as_utc(st.window_end)) == (10_000, 10_000, None, ctx.now)
    e = _rows(conn, real["attrs"]["address"])[end]
    assert (e.volume_usd_day, e.tx_count_day, e.estimated) == (10_000.0, 500, False)

    # one project per hook, then score
    for i, addr in enumerate(["0x" + "b" * 40, "0x" + "d" * 40]):
        pid = conn.execute(sa.insert(db.projects).values(slug=f"p{i}", name=f"P{i}", first_seen_at=ctx.now - timedelta(days=60))).inserted_primary_key[0]
        conn.execute(sa.update(db.hooks).values(project_id=pid).where(db.hooks.c.address == addr))
    res = score.run(conn, ctx)
    rows = {r.project_id: r for r in conn.execute(sa.select(db.project_daily).where(db.project_daily.c.day == day))}
    real_row, wash_row = rows[1], rows[2]
    assert (real_row.vol_7d, real_row.vol_7d_prev, real_row.vol_24h) == (70_000, 35_000, 10_000)
    assert (real_row.swaps_24h, real_row.swaps_7d) == (500, None)      # one known day of swaps is not a 7-day count
    assert real_row.score_trending > 0 and not real_row.is_spam
    assert wash_row.is_spam and wash_row.score_trending == 0 and res["wash"] == 1


def test_keyless_rerun_clears_a_stale_onchain_volume_label(conn, ctx, monkeypatch):
    real = _pool("0x" + "a" * 64, "0x" + "b" * 40, 10_000, 400, "2026-01-01T00:00:00Z", [(_t(ctx.day), 10_000.0)])
    # the onchain step ran earlier today and labelled the row; a same-day snapshot rerun rewrites vol_24h from keyless
    conn.execute(sa.insert(db.pool_stats).values(pool_id=real["attrs"]["address"], chain_id=8453, day=ctx.day, vol_24h=1.0,
                                                 traders_24h=1, vol_source="onchain"))
    _run_keyless(conn, ctx, monkeypatch, [real])
    st = conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.pool_id == real["attrs"]["address"])).one()
    assert (st.vol_24h, st.vol_source) == (10_000.0, None)


def test_pool_past_the_ohlcv_cap_writes_only_the_closed_day_as_an_estimate(conn, ctx, monkeypatch):
    end, pid = ctx.day - timedelta(days=1), "0x" + "a" * 64
    for i in range(1, 4):           # three earlier days from nights when it was inside the cap
        conn.execute(sa.insert(db.pool_snapshots).values(pool_id=pid, chain_id=8453, day=end - timedelta(days=i), tvl_usd=1.0,
                                                         volume_usd_cum=1000.0 * (4 - i), volume_usd_day=1000.0,
                                                         tx_count_cum=30 * (4 - i), tx_count_day=30, estimated=False))
    out = _run_keyless(conn, ctx, monkeypatch, [_pool(pid, "0x" + "b" * 40, 7_000, 400, "2026-01-01T00:00:00Z", None)])
    s = _rows(conn, pid)
    assert sorted(s) == [end - timedelta(days=3), end - timedelta(days=2), end - timedelta(days=1), end]   # no running-day row
    assert all(s[end - timedelta(days=i)].volume_usd_day == 1000.0 for i in range(1, 4))                   # history untouched
    assert (s[end].volume_usd_day, s[end].volume_usd_cum, s[end].estimated) == (7_000.0, 10_000.0, True)
    assert (s[end].tx_count_day, s[end].tx_count_cum) == (500, 590)          # 300 buys + 200 sells on top of 90 known
    assert out["estimated_pools"] == 1


def test_a_candle_row_for_the_closed_day_is_never_replaced_by_an_estimate(conn, ctx, monkeypatch):
    end, pid = ctx.day - timedelta(days=1), "0x" + "a" * 64
    conn.execute(sa.insert(db.pool_snapshots).values(pool_id=pid, chain_id=8453, day=end, tvl_usd=1.0, volume_usd_cum=5.0,
                                                     volume_usd_day=5.0, estimated=False))
    capped = _pool(pid, "0x" + "b" * 40, 7_000, 400, "2026-01-01T00:00:00Z", None)
    assert _run_keyless(conn, ctx, monkeypatch, [capped])["estimated_pools"] == 0
    _run_keyless(conn, ctx, monkeypatch, [capped])                           # same-day rerun
    r = conn.execute(sa.select(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == pid)).one()
    assert (r.volume_usd_day, r.estimated, r.tx_count_day, r.tx_count_cum) == (5.0, False, 500, 500)


def test_candles_fill_days_without_trades_with_zero_and_never_touch_earlier_days(conn, ctx, monkeypatch):
    end, pid = ctx.day - timedelta(days=1), "0x" + "a" * 64
    conn.execute(sa.insert(db.pool_snapshots).values(pool_id=pid, chain_id=8453, day=end - timedelta(days=5), tvl_usd=1.0,
                                                     volume_usd_cum=9.0, volume_usd_day=9.0, estimated=False))
    candles = [(_t(end - timedelta(days=2)), 300.0), (_t(end), 200.0), (_t(ctx.day), 50.0)]
    _run_keyless(conn, ctx, monkeypatch, [_pool(pid, "0x" + "b" * 40, 4_000, 400, "2026-01-01T00:00:00Z", candles)])
    s = {d: (r.volume_usd_day, r.estimated) for d, r in _rows(conn, pid).items()}
    assert s == {end - timedelta(days=5): (9.0, False), end - timedelta(days=2): (300.0, False),
                 end - timedelta(days=1): (0.0, False), end: (200.0, False), ctx.day: (50.0, False)}
