"""Synthetic retained-proof regressions; no external evidence is imported by these tests."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa

from bait import db
from bait.steps import score
from tests.conftest import DAY, NOW, insert_hook, insert_pool, insert_project, insert_snap

CHAIN = 10
POOL = "0x" + "1" * 64
HOOK = "0x" + "a" * 36 + "0088"
OLD = NOW - timedelta(days=40)
END = DAY - timedelta(days=1)
BEFORE = DAY - timedelta(days=7)


def _project(conn):
    pid = insert_project(conn, "Ledger", first_seen=NOW - timedelta(days=90))
    insert_hook(conn, HOOK, CHAIN, project_id=pid)
    conn.execute(sa.update(db.hooks).values(pools_total=1))
    insert_pool(conn, POOL, CHAIN, HOOK, created=NOW - timedelta(days=90))
    return pid


def _log(conn, tx, log_index=0, usd="10", **overrides):
    row = {
        "chain_id": CHAIN, "transaction_hash": f"0x{tx:064x}", "log_index": log_index,
        "pool_id": POOL, "hook_address": HOOK, "block_number": 100 + tx,
        "block_hash": f"0x{100 + tx:064x}", "block_timestamp": OLD,
        "event_kind": "custom_swap", "positive": True,
        "usd_lower_bound": Decimal(usd) if usd is not None else None,
        "evidence": {"source": "synthetic unit-test evidence"}, "evidence_sha256": "a" * 64,
        "bundle_sha256": "b" * 64, "imported_at": NOW,
    }
    row.update(overrides)
    conn.execute(sa.insert(db.historical_swap_evidence).values(**row))


def _row(conn, ctx, pid):
    score.run(conn, ctx)
    return conn.execute(sa.select(db.project_daily).where(db.project_daily.c.project_id == pid,
                                                         db.project_daily.c.day == DAY)).one()


def test_distinct_positive_transactions_and_known_lower_bound_retain_unknown_project(conn, ctx):
    pid = _project(conn)
    for tx in range(10):
        _log(conn, tx, usd="100" if tx == 0 else None)
    d = _row(conn, ctx, pid)
    assert d.has_historical_usage and not d.is_spam and d.activity_status == "unknown"
    assert (d.vol_24h, d.vol_7d, d.vol_7d_prev, d.swaps_24h, d.swaps_7d) == (None, None, None, None, None)
    assert not d.is_new and not d.is_wash and d.score_trending == 0


def test_standard_and_custom_logs_count_once_per_transaction(conn, ctx):
    pid = _project(conn)
    for tx in range(5):
        _log(conn, tx, event_kind="poolmanager_swap")
        _log(conn, tx, log_index=1, event_kind="custom_swap")
    d = _row(conn, ctx, pid)
    assert not d.has_historical_usage and d.is_spam  # ten logs are only five transactions
    for tx in range(5, 10):
        _log(conn, tx, event_kind="poolmanager_swap")
        _log(conn, tx, log_index=1, event_kind="custom_swap")
    assert _row(conn, ctx, pid).has_historical_usage


def test_multiple_logs_use_max_usd_per_transaction_never_sum(conn, ctx):
    pid = _project(conn)
    for tx in range(10):
        _log(conn, tx, usd="6")
        _log(conn, tx, log_index=1, usd="6", event_kind="poolmanager_swap")
        _log(conn, tx, log_index=2, usd=None)
    assert not _row(conn, ctx, pid).has_historical_usage  # $60 lower bound, not $120
    conn.execute(sa.update(db.historical_swap_evidence).where(db.historical_swap_evidence.c.log_index == 1)
                 .values(usd_lower_bound=Decimal("10")))
    assert _row(conn, ctx, pid).has_historical_usage


@pytest.mark.parametrize("raw_swaps,raw_usd,ledger_swaps,ledger_usd", [(5, 50, 5, "10"), (10, 50, 10, "5")])
def test_ledger_and_other_sources_cannot_combine_below_threshold_evidence(conn, ctx, raw_swaps, raw_usd,
                                                                        ledger_swaps, ledger_usd):
    pid = _project(conn)
    conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=CHAIN, pool_id=POOL, day=OLD.date(),
                                                        swaps=raw_swaps, volume_usd=raw_usd))
    insert_snap(conn, POOL, CHAIN, OLD.date(), 0, raw_usd, None, vol_day=raw_usd)
    conn.execute(sa.update(db.pool_snapshots).values(estimated=False))
    for tx in range(ledger_swaps):
        _log(conn, tx, usd=ledger_usd)
    assert not _row(conn, ctx, pid).has_historical_usage


def test_ledger_has_no_standalone_large_volume_exception(conn, ctx):
    pid = _project(conn)
    _log(conn, 1, usd="1000000")
    assert not _row(conn, ctx, pid).has_historical_usage


def test_history_is_strictly_before_the_current_seven_day_boundary(conn, ctx):
    pid = _project(conn)
    for tx in range(9):
        _log(conn, tx)
    boundary = datetime.combine(BEFORE, datetime.min.time(), tzinfo=timezone.utc)
    _log(conn, 9, block_timestamp=boundary)
    assert not _row(conn, ctx, pid).has_historical_usage
    conn.execute(sa.update(db.historical_swap_evidence).where(db.historical_swap_evidence.c.transaction_hash == f"0x{9:064x}")
                 .values(block_timestamp=boundary - timedelta(microseconds=1)))
    assert _row(conn, ctx, pid).has_historical_usage


@pytest.mark.parametrize("problem", ["hook_mismatch", "unmapped_pool", "folded_pool", "nonpositive", "unpriced"])
def test_nonqualifying_identity_or_event_evidence_cannot_preserve_listing(conn, ctx, problem):
    pid = _project(conn)
    extra = {}
    usd = "10"
    if problem == "hook_mismatch":
        extra["hook_address"] = "0x" + "b" * 40
    elif problem == "unmapped_pool":
        extra["pool_id"] = "0x" + "2" * 64
    elif problem == "folded_pool":
        extra["pool_id"] = db.hook_pool_id(HOOK)
        insert_pool(conn, extra["pool_id"], CHAIN, HOOK, created=None)
    elif problem == "nonpositive":
        extra["positive"], usd = False, None
    elif problem == "unpriced":
        usd = None
    for tx in range(10):
        _log(conn, tx, usd=usd, **extra)
    assert not _row(conn, ctx, pid).has_historical_usage


def test_positive_custom_events_can_qualify_without_counting_the_zero_poolmanager_pair(conn, ctx):
    pid = _project(conn)
    for tx in range(10):
        _log(conn, tx, event_kind="poolmanager_swap", positive=False, usd=None)
        _log(conn, tx, log_index=1, event_kind="custom_swap")
    assert _row(conn, ctx, pid).has_historical_usage


@pytest.mark.parametrize("other_chain", [False, True])
def test_threshold_cannot_combine_different_pools_or_chains(conn, ctx, other_chain):
    pid = _project(conn)
    cid, pool = (1, POOL) if other_chain else (CHAIN, "0x" + "2" * 64)
    if other_chain:
        insert_hook(conn, HOOK, cid, project_id=pid)
    insert_pool(conn, pool, cid, HOOK, created=None)
    for tx in range(5):
        _log(conn, tx)
        _log(conn, tx, log_index=1, chain_id=cid, pool_id=pool)
    assert not _row(conn, ctx, pid).has_historical_usage


def test_historical_wash_remains_independent_of_ledger_qualification(conn, ctx):
    pid = _project(conn)
    for tx in range(10):
        _log(conn, tx)
    conn.execute(sa.insert(db.project_daily).values(project_id=pid, day=DAY - timedelta(days=1), is_wash=True,
                                                   wash_reason="concentrated", is_spam=False))
    d = _row(conn, ctx, pid)
    assert not d.is_spam and d.is_wash and d.wash_historical and d.wash_reason == "concentrated"
    assert d.activity_status == "unknown" and d.vol_7d is None and d.swaps_7d is None
    assert not d.is_new and d.score_trending == 0


def test_ledger_does_not_prove_recent_coverage_or_add_to_complete_recent_totals(conn, ctx):
    pid = _project(conn)
    for tx in range(10):
        _log(conn, tx)
    assert _row(conn, ctx, pid).activity_status == "unknown"
    conn.execute(sa.insert(db.indexer_state).values(chain_id=CHAIN, backfilled=True, updated_at=NOW, swap_block_time=NOW))
    for i in range(7):
        day = END - timedelta(days=i)
        insert_snap(conn, POOL, CHAIN, day, 0, 0, 0, vol_day=0, tx_day=0)
        conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=CHAIN, pool_id=POOL, day=day, swaps=0, volume_usd=0))
    d = _row(conn, ctx, pid)
    assert d.activity_status == "dormant" and not d.is_spam
    assert (d.vol_24h, d.vol_7d, d.swaps_24h, d.swaps_7d) == (0, 0, 0, 0)
    conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.day == END)
                 .values(volume_usd_cum=20, volume_usd_day=20, tx_count_day=3))
    conn.execute(sa.update(db.pool_daily_onchain).where(db.pool_daily_onchain.c.day == END).values(swaps=3, volume_usd=20))
    d = _row(conn, ctx, pid)
    assert d.activity_status == "active" and (d.vol_24h, d.vol_7d, d.swaps_24h, d.swaps_7d) == (20, 20, 3, 3)


def test_evidence_survives_pool_pruning_but_cannot_score_without_current_identity(conn, ctx):
    pid = _project(conn)
    for tx in range(10):
        _log(conn, tx)
    assert _row(conn, ctx, pid).has_historical_usage
    conn.execute(sa.delete(db.pools))
    assert conn.execute(sa.select(sa.func.count()).select_from(db.historical_swap_evidence)).scalar_one() == 10
    assert not _row(conn, ctx, pid).has_historical_usage


@pytest.mark.parametrize("invalid", [
    {"chain_id": 0}, {"log_index": -1}, {"block_number": -1}, {"usd_lower_bound": Decimal("-1")},
    {"positive": False, "usd_lower_bound": Decimal("1")},
])
def test_schema_rejects_invalid_evidence_ranges(conn, invalid):
    with pytest.raises(sa.exc.IntegrityError):
        _log(conn, 1, **invalid)


def test_postgres_ledger_threshold_keeps_decimal_precision(pg_engine, ctx):
    with pg_engine.begin() as conn:
        pid = _project(conn)
        for tx in range(10):
            _log(conn, tx, usd="9.999999999999999999")
        assert not _row(conn, ctx, pid).has_historical_usage
        conn.execute(sa.update(db.historical_swap_evidence).values(usd_lower_bound=Decimal("10.000000000000000000")))
        assert _row(conn, ctx, pid).has_historical_usage
