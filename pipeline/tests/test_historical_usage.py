"""Historical listing proof must not manufacture recent activity or a complete indexer window."""
from datetime import timedelta

import pytest
import sqlalchemy as sa

from bait import db
from bait.steps import score
from tests.conftest import DAY, NOW, insert_hook, insert_pool, insert_project, insert_snap

END = DAY - timedelta(days=1)
OLD = END - timedelta(days=40)
WEEK = [END - timedelta(days=i) for i in range(7)]


def _row(conn, pid):
    return conn.execute(sa.select(db.project_daily).where(db.project_daily.c.project_id == pid,
                                                         db.project_daily.c.day == DAY)).one()


def _project(conn, *, old_swaps=12, old_volume=120, snapshots=True):
    pid = insert_project(conn, "Historical", first_seen=NOW - timedelta(days=90))
    insert_hook(conn, "0xhistorical", 10, project_id=pid)
    conn.execute(sa.update(db.hooks).values(pools_total=1))
    insert_pool(conn, "0xpool", 10, "0xhistorical", created=NOW - timedelta(days=90))
    if old_swaps is not None:
        conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id="0xpool", day=OLD,
                                                            swaps=old_swaps, volume_usd=old_volume, wallets=6))
    if snapshots:
        for day in WEEK:
            insert_snap(conn, "0xpool", 10, day, 0, 0, 0, vol_day=0, tx_day=0)
    conn.execute(sa.insert(db.indexer_state).values(chain_id=10, backfilled=True, updated_at=NOW, swap_block_time=NOW))
    return pid


def test_history_outside_14_days_keeps_listing_without_changing_recent_metrics(conn, ctx):
    pid = _project(conn)
    # Historical cumulative snapshots are never summed or used to qualify this history; raw UTC usage is retained.
    for i in range(3):
        insert_snap(conn, "0xpool", 10, OLD + timedelta(days=i), 9_000_000, 3_000_000, 100_000)
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.has_historical_usage and not d.is_spam and d.activity_status == "dormant"
    assert (d.vol_24h, d.vol_7d, d.vol_7d_prev, d.swaps_24h, d.swaps_7d) == (0, 0, None, 0, 0)
    assert not d.is_wash and not d.is_new and d.score_trending == 0
    first = dict(d._mapping)
    score.run(conn, ctx)
    assert dict(_row(conn, pid)._mapping) == first


@pytest.mark.parametrize("swaps,volume", [(0, 0), (9, 99), (100, 99), (0, 9_999)])
def test_never_active_or_dust_is_not_historical_legitimacy(conn, ctx, swaps, volume):
    pid = _project(conn, old_swaps=swaps, old_volume=volume)
    conn.execute(sa.update(db.hooks).values(hooklisted=True, verified_source=True, audit_url="https://audit.example/"))
    insert_snap(conn, "0xpool", 10, OLD, 100_000_000, 0, 0, vol_day=0, tx_day=0)
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.is_spam and not d.has_historical_usage and d.activity_status == "unknown"


@pytest.mark.parametrize("problem", ["missing_day", "missing_pool", "other_chain", "stale", "not_backfilled",
                                      "unknown_census", "capped_missing", "owed_lookup"])
def test_incomplete_coverage_retains_history_but_never_calls_it_dormant(conn, ctx, problem):
    pid = _project(conn)
    if problem == "missing_day":
        conn.execute(sa.delete(db.pool_snapshots).where(db.pool_snapshots.c.day == END - timedelta(days=2)))
    elif problem == "missing_pool":
        insert_pool(conn, "0xmissing", 10, "0xhistorical", created=NOW - timedelta(days=90))
        conn.execute(sa.update(db.hooks).values(pools_total=2))
    elif problem == "other_chain":
        insert_hook(conn, "0xother", 8453, project_id=pid)
        insert_pool(conn, "0xotherpool", 8453, "0xother", created=NOW - timedelta(days=90))
    elif problem == "stale":
        conn.execute(sa.update(db.indexer_state).values(updated_at=NOW - timedelta(days=2)))
    elif problem == "not_backfilled":
        conn.execute(sa.update(db.indexer_state).values(backfilled=False))
    elif problem == "unknown_census":
        conn.execute(sa.update(db.hooks).values(pools_total=None))
    elif problem == "capped_missing":
        conn.execute(sa.update(db.hooks).values(pools_total=2001))
    elif problem == "owed_lookup":
        conn.execute(sa.insert(db.pool_lookup_queue).values(chain_id=10, pool_id="0xowed", from_block=1, seen_block=2,
                                                           from_time=NOW - timedelta(days=2)))
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.has_historical_usage and not d.is_spam and d.activity_status == "unknown"
    assert not d.is_new and d.score_trending == 0
    if problem in {"missing_day", "missing_pool", "other_chain"}:
        assert d.swaps_7d is None


def test_no_recent_rows_remain_unknown_not_zero(conn, ctx):
    pid = _project(conn, snapshots=False)
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.has_historical_usage and not d.is_spam and d.activity_status == "unknown"
    assert (d.vol_24h, d.vol_7d, d.vol_7d_prev, d.swaps_24h, d.swaps_7d) == (None, None, None, None, None)


def test_ready_but_short_indexer_history_does_not_prove_a_full_zero_week(conn, ctx):
    pid = _project(conn, old_swaps=None)
    insert_snap(conn, "0xpool", 10, OLD, 0, 1_000_000, None, vol_day=1_000_000)
    conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.day == OLD).values(estimated=False))
    conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id="0xpool", day=END - timedelta(days=2),
                                                        swaps=0, volume_usd=0))
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.has_historical_usage and not d.is_spam and d.activity_status == "unknown"
    assert d.is_wash and d.wash_reason == "unverified" and d.wash_historical


@pytest.mark.parametrize("copied", [True, False])
def test_positive_unpriced_swaps_are_active_even_before_snapshot_copy(conn, ctx, copied):
    pid = _project(conn)
    conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id="0xpool", day=END,
                                                        swaps=3, unpriced_swaps=3, volume_usd=0, wallets=3))
    conn.execute(sa.update(db.pool_snapshots).values(volume_usd_cum=None, volume_usd_day=None))
    if copied:
        conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.day == END).values(tx_count_day=3))
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.has_historical_usage and not d.is_spam and d.activity_status == "active" and d.vol_7d is None
    assert (d.swaps_24h, d.swaps_7d) == ((3, 3) if copied else (None, None))
    assert not d.is_new and d.score_trending == 0


@pytest.mark.parametrize("raw_days_ago, measured_days_ago, measured_volume, expected", [
    (0, 0, 0, (None, None)),
    (3, 0, 0, (0, None)),                 # a stale earlier day does not invalidate the closed day's known zero
    (0, 0, 50, (50, 50)),                # preserve a measured positive total, without adding unpriced USD
    (0, 2, 25, (None, 25)),              # keep the week's observed positive lower bound from another day
])
def test_raw_swaps_ahead_of_zero_snapshots_leave_volume_unknown(conn, ctx, raw_days_ago, measured_days_ago,
                                                               measured_volume, expected):
    pid = _project(conn)
    conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id="0xpool",
                                                        day=END - timedelta(days=raw_days_ago), swaps=3,
                                                        unpriced_swaps=3, volume_usd=0, wallets=3))
    if measured_volume:
        conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.day == END - timedelta(days=measured_days_ago))
                     .values(volume_usd_day=measured_volume))
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.activity_status == "active" and d.swaps_7d is None
    assert (d.vol_24h, d.vol_7d) == expected
    assert d.swaps_24h == (None if raw_days_ago == 0 else 0)


def test_complete_zero_swap_coverage_keeps_measured_zero_volume(conn, ctx):
    pid = _project(conn)
    for day in WEEK:
        conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id="0xpool", day=day, swaps=0, volume_usd=0))
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.activity_status == "dormant"
    assert (d.vol_24h, d.vol_7d, d.swaps_24h, d.swaps_7d) == (0, 0, 0, 0)


def test_missing_history_legacy_cumulatives_and_rolling_estimates_are_not_proof(conn, ctx):
    pid = _project(conn, old_swaps=None)
    for i in range(4):
        day = OLD + timedelta(days=i)
        insert_snap(conn, "0xpool", 10, day, 1_000_000, 10_000_000, 10_000, vol_day=800_000, tx_day=1_000)
        conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.day == day).values(estimated=True))
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.is_spam and not d.has_historical_usage and d.activity_status == "unknown"


def test_overlapping_sources_and_cumulative_snapshots_never_double_count_history(conn, ctx):
    pid = _project(conn, old_swaps=1, old_volume=600_000)
    for i in range(3):
        day = OLD + timedelta(days=i)
        insert_snap(conn, "0xpool", 10, day, 0, 600_000, 1, vol_day=600_000 if i == 0 else 0)
        conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.day == day).values(estimated=False))
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert not d.has_historical_usage and d.is_spam


def test_historical_large_volume_without_exact_swap_proof_keeps_unverified_wash_control(conn, ctx):
    pid = _project(conn, old_swaps=2, old_volume=1_000_000)
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.has_historical_usage and not d.is_spam and d.is_wash and d.wash_reason == "unverified"
    assert d.score_trending == 0 and not d.is_new


def test_prior_wash_is_preserved_when_quiet_but_revival_uses_current_evidence(conn, ctx):
    pid = _project(conn)
    conn.execute(sa.insert(db.project_daily).values(project_id=pid, day=DAY - timedelta(days=1), is_wash=True,
                                                   wash_reason="concentrated", is_spam=False))
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.is_wash and d.wash_historical and d.wash_reason == "concentrated"
    assert d.score_trending == 0 and not d.is_new
    conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.day == END)
                 .values(volume_usd_cum=50, volume_usd_day=50, tx_count_day=2))
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.activity_status == "active" and not d.is_wash and not d.wash_historical


def test_current_wash_rules_still_win_after_revival(conn, ctx):
    pid = _project(conn)
    conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.day == END)
                 .values(volume_usd_cum=2_000_000, volume_usd_day=2_000_000, tx_count_day=1))
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.activity_status == "active" and d.is_wash and not d.wash_historical
    assert d.score_trending == 0 and not d.is_new


def test_unpriced_partial_revival_does_not_clear_historical_wash(conn, ctx):
    pid = _project(conn)
    conn.execute(sa.insert(db.project_daily).values(project_id=pid, day=DAY - timedelta(days=1), is_wash=True,
                                                   wash_reason="concentrated", is_spam=False))
    conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id="0xpool", day=END,
                                                        swaps=1, unpriced_swaps=1, volume_usd=0, wallets=1))
    conn.execute(sa.update(db.pool_snapshots).values(volume_usd_cum=None, volume_usd_day=None))
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.activity_status == "active" and d.is_wash and d.wash_historical and d.wash_reason == "concentrated"
    assert d.swaps_7d is None and d.score_trending == 0 and not d.is_new


def test_a_second_priced_pool_cannot_clear_wash_on_unpriced_revival(conn, ctx):
    pid = _project(conn)
    conn.execute(sa.insert(db.project_daily).values(project_id=pid, day=DAY - timedelta(days=1), is_wash=True,
                                                   wash_reason="concentrated", is_spam=False))
    insert_pool(conn, "0xunpriced", 10, "0xhistorical", created=NOW - timedelta(days=90))
    conn.execute(sa.update(db.hooks).values(pools_total=2))
    for day in WEEK:
        insert_snap(conn, "0xunpriced", 10, day, None, None, None, tx_day=3 if day == END else 0)
    conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id="0xunpriced", day=END,
                                                        swaps=3, unpriced_swaps=3, volume_usd=0, wallets=3))
    # Even some activity in the priced pool is not a complete price picture for the project.
    conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == "0xpool", db.pool_snapshots.c.day == END)
                 .values(volume_usd_cum=10, volume_usd_day=10, tx_count_day=1))
    score.run(conn, ctx)
    d = _row(conn, pid)
    assert d.activity_status == "active" and d.is_wash and d.wash_historical and d.wash_reason == "concentrated"


def test_postgres_migration_updates_existing_repair_backup_and_roundtrips(pg_engine, ctx):
    from bait import repair
    new_columns = ("has_historical_usage", "activity_status", "wash_historical")
    with pg_engine.begin() as conn:
        for table in ("project_daily", "project_daily_p9_backup"):
            for column in new_columns:
                conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        pid = _project(conn)
        conn.execute(sa.text("INSERT INTO project_daily (project_id, day) VALUES (:pid, :day)"), {"pid": pid, "day": DAY})
    added = set(db.add_missing_columns(pg_engine))
    assert {f"{table}.{column}" for table in ("project_daily", "project_daily_p9_backup") for column in new_columns} <= added
    with pg_engine.begin() as conn:
        score.run(conn, ctx)
        expected = dict(_row(conn, pid)._mapping)
        repair.backup_day(conn, DAY)
        conn.execute(sa.update(db.project_daily).values(has_historical_usage=False, activity_status=None))
        repair.restore_day(conn, DAY)
        assert dict(_row(conn, pid)._mapping) == expected
