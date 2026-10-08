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


def test_keyless_rows_feed_scoring_and_wash_is_excluded(conn, ctx, monkeypatch):
    monkeypatch.setenv("KEYLESS", "1")
    day = ctx.day
    t = lambda d: int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    daily = [(t(day - timedelta(days=i)), 10_000.0 if i < 7 else 5_000.0) for i in range(13, -1, -1)]
    real = _pool("0x" + "a" * 64, "0x" + "b" * 40, 10_000, 400, "2026-01-01T00:00:00Z", daily)
    wash = _pool("0x" + "c" * 64, "0x" + "d" * 40, 5_000_000, 5, "2026-01-01T00:00:00Z", daily)
    found = {("base", real["attrs"]["address"]): real, ("base", wash["attrs"]["address"]): wash}
    monkeypatch.setattr(K, "collect", lambda *a, **k: found)
    monkeypatch.setattr(K, "resolve_hooks", lambda *a, **k: None)
    monkeypatch.setattr(K, "resolve_via_logs", lambda *a, **k: None)
    monkeypatch.setattr(K, "fetch_ohlcv", lambda *a, **k: None)

    out = snapshot.run(conn, ctx)
    assert out["keyless_pools"] == 2 and out["errors"] == 0
    hooks = {r.address: r for r in conn.execute(sa.select(db.hooks))}
    assert hooks["0x" + "b" * 40].name.startswith("Unlisted hook")
    p = conn.execute(sa.select(db.pools).where(db.pools.c.pool_id == real["attrs"]["address"])).one()
    assert p.token0["symbol"] == "ETH" and p.token1["symbol"] == "USDC" and p.fee == 3000

    # one project per hook, then score
    for i, addr in enumerate(["0x" + "b" * 40, "0x" + "d" * 40]):
        pid = conn.execute(sa.insert(db.projects).values(slug=f"p{i}", name=f"P{i}", first_seen_at=ctx.now - timedelta(days=60))).inserted_primary_key[0]
        conn.execute(sa.update(db.hooks).values(project_id=pid).where(db.hooks.c.address == addr))
    res = score.run(conn, ctx)
    rows = {r.project_id: r for r in conn.execute(sa.select(db.project_daily).where(db.project_daily.c.day == day))}
    real_row, wash_row = rows[1], rows[2]
    assert round(real_row.vol_7d) == 70_000 and round(real_row.vol_7d_prev) == 35_000
    assert real_row.score_trending > 0 and not real_row.is_spam
    assert wash_row.is_spam and wash_row.score_trending == 0 and res["wash"] == 1
