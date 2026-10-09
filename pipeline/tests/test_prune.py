"""prune-pools: the one-time trim of v4_pools for hooks past the per-hook cap."""
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from bait import db
from bait.indexer import prune, store
from bait.indexer.__main__ import main
from tests.conftest import DAY

PAD, OTHER = "0x" + "a" * 36 + "0080", "0x" + "b" * 36 + "00c0"


def _pool(n):
    return "0x" + format(n, "064x")


def _v4(conn, n, hook, block):
    conn.execute(sa.insert(db.v4_pools).values(chain_id=8453, pool_id=_pool(n), hook_address=hook, currency0="0x0", currency1="0x1",
                                               block_number=block, block_time=datetime(2026, 9, 1, tzinfo=timezone.utc)))


def _seed(conn):
    """PAD: ten pools (blocks 101..110), each copied into pools; 2 swapped, 3 has a snapshot. OTHER: two pools."""
    for i in range(1, 11):
        _v4(conn, i, PAD, 100 + i)
        conn.execute(sa.insert(db.pools).values(pool_id=_pool(i), chain_id=8453, hook_address=PAD))
    for i in (1000, 1001):
        _v4(conn, i, OTHER, 50)
    conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=8453, pool_id=_pool(2), day=DAY, swaps=1))
    conn.execute(sa.insert(db.pool_snapshots).values(pool_id=_pool(3), chain_id=8453, day=DAY, tvl_usd=1.0))


def _ids(conn, table=db.v4_pools, hook=PAD):
    return {r[0] for r in conn.execute(sa.select(table.c.pool_id).where(table.c.hook_address == hook))}


def test_dry_run_reports_each_hook_over_the_cap_and_changes_nothing(engine, caplog):
    with engine.begin() as c:
        _seed(c)
    with caplog.at_level("INFO", logger="bait.indexer"):
        out = prune.prune_pools(engine, keep=4, apply=False, chain_ids=[8453])
    assert (out["hooks"], out["rows"], out["pools_rows"], out["est_bytes"]) == (1, 4, 4, 8 * prune.ROW_BYTES)
    assert any(f"hook={PAD}" in m and "delete=4" in m for m in caplog.messages)
    with engine.connect() as c:
        assert len(_ids(c)) == 10 and c.execute(sa.select(sa.func.count()).select_from(db.hook_pool_counts)).scalar() == 0


def test_prune_keeps_the_newest_and_every_active_pool_and_records_the_true_total(engine):
    with engine.begin() as c:
        _seed(c)
    out = prune.prune_pools(engine, keep=4, apply=True, chain_ids=[8453], batch=3)
    kept = {_pool(i) for i in (2, 3, 7, 8, 9, 10)}
    with engine.connect() as c:
        assert _ids(c) == kept and _ids(c, db.pools) == kept and len(_ids(c, hook=OTHER)) == 2
        k = c.execute(sa.select(db.hook_pool_counts)).one()
    assert (out["rows"], out["pools_rows"], out["batches"]) == (4, 4, 2)
    assert (k.hook_address, k.pools_total, k.first_block, k.last_block) == (PAD, 10, 101, 110)
    row = {"pool_id": _pool(11), "chain_id": 8453, "hook_address": PAD, "currency0": "0x0", "currency1": "0x1", "fee": 0,
           "tick_spacing": 1, "sqrt_price_x96": "1", "tick": 0, "block_number": 200, "block_time": None, "tx_hash": None}
    with engine.begin() as c:                        # the indexer then counts the hook's new pools without storing them
        assert store.store_pools(c, 8453, [row], cap=4) == ([], [row])
    with engine.connect() as c:
        assert c.execute(sa.select(db.hook_pool_counts.c.pools_total)).scalar_one() == 11
    assert prune.prune_pools(engine, keep=4, apply=True, chain_ids=[8453])["rows"] == 0


def test_cli_needs_a_mode_and_a_dry_run_changes_nothing(tmp_path):
    url = f"sqlite:///{tmp_path / 'bait.db'}"
    e = db.make_engine(url)
    db.create_all(e)
    with e.begin() as c:
        _seed(c)
    with pytest.raises(SystemExit):
        main(["prune-pools", "--database-url", url])
    assert main(["prune-pools", "--dry-run", "--keep", "4", "--chains", "base", "--database-url", url]) == 0
    with e.connect() as c:
        assert len(_ids(c)) == 10


def test_a_stop_between_batches_says_stopped_early_and_the_cli_exits_4(engine, caplog, monkeypatch, tmp_path):
    with engine.begin() as c:
        _seed(c)
    asked = []

    def stop_after_first_batch():          # asked before the hook, before batch 1, then before batch 2
        asked.append(1)
        return len(asked) > 2
    with caplog.at_level("INFO", logger="bait.indexer"):
        out = prune.prune_pools(engine, keep=4, apply=True, chain_ids=[8453], batch=1, should_stop=stop_after_first_batch)
    assert out["stopped"] and out["rows"] == 1 and any("stopped early" in m for m in caplog.messages)
    assert any(m.startswith("prune-pools stopped:") for m in caplog.messages)
    assert not any(m.startswith("prune-pools done:") for m in caplog.messages)
    monkeypatch.setattr(prune, "prune_pools", lambda *a, **k: {"locked": [8453], "stopped": True})
    url = f"sqlite:///{tmp_path / 'bait.db'}"
    assert main(["prune-pools", "--yes", "--database-url", url]) == 4
