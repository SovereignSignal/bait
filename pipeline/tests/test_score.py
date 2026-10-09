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
    assert out == {"projects": 1, "with_pools": 1, "new": 0, "trending": 1, "spam": 0, "wash": 0, "wash_7d": 0}


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
    assert d.score_trending == 0.0     # only two nights of history: no prior week to grow from


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



def test_wash_rule_reads_persisted_pool_stats(conn, ctx):
    """A run without the snapshot step still excludes wash projects, from pool_stats."""
    from bait.db import pool_stats
    p = insert_project(conn, "Washy", first_seen=NOW - timedelta(days=60))
    insert_hook(conn, "0xw", 8453, project_id=p, name="Washy")
    insert_pool(conn, "0xwp", 8453, "0xw", created=NOW - timedelta(days=60))
    insert_snap(conn, "0xwp", 8453, DAY - timedelta(days=14), 0, 0, 0)
    insert_snap(conn, "0xwp", 8453, DAY - timedelta(days=7), 0, 1_000_000, 0)
    insert_snap(conn, "0xwp", 8453, DAY, 0, 9_000_000, 0)
    conn.execute(sa.insert(pool_stats).values(pool_id="0xwp", chain_id=8453, day=DAY, vol_24h=5_000_000, traders_24h=6))
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert d.is_spam and d.score_trending == 0.0 and out["wash"] == 1


def _onchain_project(conn, name, pool, wallets, vol_per_day):
    p = insert_project(conn, name, first_seen=NOW - timedelta(days=60))
    insert_hook(conn, f"0x{name}", 10, project_id=p, name=name)
    insert_pool(conn, pool, 10, f"0x{name}", created=NOW - timedelta(days=60))
    insert_snap(conn, pool, 10, DAY - timedelta(days=14), 50_000, 0, 100)
    insert_snap(conn, pool, 10, DAY - timedelta(days=7), 50_000, 1_000_000, 200)
    insert_snap(conn, pool, 10, DAY, 50_000, 1_000_000 + 7 * vol_per_day, 300)
    for i in range(7):
        d = DAY - timedelta(days=i)
        conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id=pool, day=d, swaps=2, wallets=1, volume_usd=vol_per_day))
        for w in range(wallets):
            conn.execute(sa.insert(db.pool_wallet_day).values(chain_id=10, pool_id=pool, day=d, address=f"0xw{w}", swaps=1))
    return p


def test_wash_7d_from_indexer_one_wallet_hundreds_of_millions(conn, ctx):
    """10/8 case: Optimism 0x69af...7fff moved $436M in 7 days from one wallet. Same wallets every day count once."""
    wash = _onchain_project(conn, "Desk", "0xdp", wallets=1, vol_per_day=60_000_000)
    busy = _onchain_project(conn, "Busy", "0xbp", wallets=11, vol_per_day=60_000_000)     # 11 distinct wallets: not wash
    small = _onchain_project(conn, "Small", "0xsp", wallets=1, vol_per_day=100_000)       # $700K < $1M: not wash
    _optimism_ready(conn)
    out = score.run(conn, ctx)
    assert daily(conn, wash).is_spam and daily(conn, wash).score_trending == 0.0
    assert not daily(conn, busy).is_spam and not daily(conn, small).is_spam
    assert out["wash_7d"] == 1


def test_is_wash_7d_thresholds():
    from bait.rules import is_wash_7d
    assert is_wash_7d(1_000_000, 10) and not is_wash_7d(999_999, 1) and not is_wash_7d(5_000_000, 11)


def _optimism_ready(conn, swap_time=NOW):
    conn.execute(sa.insert(db.indexer_state).values(chain_id=10, initialize_block=1, swap_block=1, head_block=1, backfilled=True,
                                                    updated_at=NOW, swap_block_time=swap_time))


def test_wash_7d_ignores_a_chain_whose_swaps_are_not_caught_up(conn, ctx):
    wash = _onchain_project(conn, "Desk", "0xdp", wallets=1, vol_per_day=60_000_000)
    _optimism_ready(conn, swap_time=NOW - timedelta(days=2))
    out = score.run(conn, ctx)
    assert out["wash_7d"] == 0 and not daily(conn, wash).is_spam


def test_held_back_pool_with_null_volume_is_left_out_of_both_weeks(conn, ctx):
    # Two pools of one project. The normal one did $1,000 in each week. The other's on-chain volume became unknown during
    # this week (onchain wrote NULL cumulative volume from that day on): it must not read as $0 this week next to its
    # $5,000 last week, which would fake a crash in the trending ratio.
    p = insert_project(conn, "Mixed", first_seen=NOW - timedelta(days=60))
    insert_hook(conn, "0xm", 8453, project_id=p, name="Mixed")
    for pool, prev_week, cur in (("0xnormal", 1_000, 2_000), ("0xheld", 5_000, None)):
        insert_pool(conn, pool, 8453, "0xm", created=NOW - timedelta(days=60))
        insert_snap(conn, pool, 8453, DAY - timedelta(days=14), 0, 0, 0)
        insert_snap(conn, pool, 8453, DAY - timedelta(days=7), 0, prev_week, 10)
        insert_snap(conn, pool, 8453, DAY, 0, cur, 20)
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xheld", chain_id=8453, day=DAY, vol_24h=None, traders_24h=3))
    score.run(conn, ctx)
    d = daily(conn, p)
    assert (d.vol_7d, d.vol_7d_prev) == (1_000, 1_000)
