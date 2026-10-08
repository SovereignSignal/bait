import math
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from bait import db
from bait.steps import score
from tests.conftest import DAY, NOW, insert_hook, insert_pool, insert_project, insert_snap


def daily(conn, pid):
    return conn.execute(sa.select(db.project_daily).where(db.project_daily.c.project_id == pid)).one()


def test_score_hand_computed(conn, ctx):
    # Project P, one pool. cum volume 100k @ day-14, 150k @ day-7, 250k @ day; txs 1000/1200/1500; TVL 5000 today.
    p = insert_project(conn, "P", first_seen=NOW - timedelta(days=3))
    insert_hook(conn, "0xp", 8453, project_id=p, name="P")
    insert_pool(conn, "0xpool1", 8453, "0xp", created=NOW - timedelta(days=60))
    insert_snap(conn, "0xpool1", 8453, DAY - timedelta(days=14), 4000, 100_000, 1000)
    insert_snap(conn, "0xpool1", 8453, DAY - timedelta(days=7), 4500, 150_000, 1200)
    insert_snap(conn, "0xpool1", 8453, DAY, 5000, 250_000, 1500)
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert d.vol_7d == 100_000 and d.vol_7d_prev == 50_000 and d.swaps_7d == 300
    assert d.tvl_usd == 5000 and d.pools_n == 1
    assert d.score_trending == pytest.approx(math.log1p(100_000) * 2.0)
    assert d.is_new is False           # pipeline first saw it 3d ago, but its pool is 60d old: not new
    assert d.is_spam is False
    assert out == {"projects": 1, "with_pools": 1, "new": 0, "trending": 1, "spam": 0, "wash": 0}


def test_score_not_new_when_old_or_tiny(conn, ctx):
    old = insert_project(conn, "Old", first_seen=NOW - timedelta(days=30))
    insert_hook(conn, "0xo", 1, project_id=old)
    insert_pool(conn, "0xpo", 1, "0xo", created=NOW - timedelta(days=30))
    insert_snap(conn, "0xpo", 1, DAY, 5_000_000, 1_000_000, 50)
    tiny = insert_project(conn, "Tiny", first_seen=NOW - timedelta(days=1), slug="tiny")
    insert_hook(conn, "0xt", 1, project_id=tiny)
    insert_pool(conn, "0xpt", 1, "0xt", created=NOW - timedelta(days=1))
    insert_snap(conn, "0xpt", 1, DAY, 900, 3_000, 9)
    score.run(conn, ctx)
    assert daily(conn, old).is_new is False
    t = daily(conn, tiny)
    assert t.is_new is False and t.is_spam is False     # TVL 900 >= 100 so not spam, but not "new" either
    assert t.vol_7d == 3_000 and t.score_trending == 0.0  # pool born inside the window: baseline 0; under $10k -> no score


def test_score_cold_start_uses_earliest_snapshot_not_all_time(conn, ctx):
    p = insert_project(conn, "Cold", first_seen=NOW - timedelta(days=100))
    insert_hook(conn, "0xc", 1, project_id=p)
    insert_pool(conn, "0xpc", 1, "0xc", created=NOW - timedelta(days=100))
    insert_snap(conn, "0xpc", 1, DAY - timedelta(days=2), 10_000, 9_000_000, 90_000)   # first night we ran
    insert_snap(conn, "0xpc", 1, DAY, 10_000, 9_020_000, 90_100)
    score.run(conn, ctx)
    d = daily(conn, p)
    assert d.vol_7d == 20_000 and d.vol_7d_prev == 0 and d.swaps_7d == 100
    assert d.score_trending == pytest.approx(math.log1p(20_000) * 10.0)


def test_score_spam_and_no_pools(conn, ctx):
    p = insert_project(conn, "Ghost", first_seen=NOW)
    insert_hook(conn, "0xg", 1, project_id=p)
    s = insert_project(conn, "Dust", first_seen=NOW, slug="dust")
    insert_hook(conn, "0xd", 1, project_id=s)
    insert_pool(conn, "0xpd", 1, "0xd", created=NOW)
    insert_snap(conn, "0xpd", 1, DAY, 12, 40, 3)
    out = score.run(conn, ctx)
    assert daily(conn, p).is_spam is True and daily(conn, p).pools_n == 0
    assert daily(conn, s).is_spam is True
    assert out["spam"] == 2
    # idempotent: re-run replaces, no duplicate rows
    score.run(conn, ctx)
    assert conn.execute(sa.select(sa.func.count()).select_from(db.project_daily)).scalar_one() == 2



def test_young_launch_is_new_not_trending(conn, ctx):
    p = insert_project(conn, "L", first_seen=NOW - timedelta(days=1))
    insert_hook(conn, "0xl", 8453, project_id=p, name="L")
    insert_pool(conn, "0xlp", 8453, "0xl", created=NOW - timedelta(days=3))
    insert_snap(conn, "0xlp", 8453, DAY - timedelta(days=1), 2000, 0, 0)
    insert_snap(conn, "0xlp", 8453, DAY, 5000, 500_000, 900)
    score.run(conn, ctx)
    d = daily(conn, p)
    assert d.is_new is True and d.score_trending == 0.0 and d.vol_7d == 500_000
