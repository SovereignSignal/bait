"""Nightly step "onchain": indexer tables -> hooks / pools / pool_snapshots / pool_stats, overriding keyless usage."""
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from bait import db
from bait.rules import custom_accounting, flags_from_address
from bait.steps import onchain, score
from tests.conftest import DAY, NOW, insert_hook

ZERO = "0x" + "0" * 40
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
HOOK, HOOK2, HOOK3 = "0x" + "a" * 36 + "0080", "0x" + "b" * 36 + "00c0", "0x" + "d" * 36 + "0040"
PA, PC, PE = "0x" + "a" * 64, "0x" + "c" * 64, "0x" + "e" * 64
REF = DAY - timedelta(days=1)          # last complete UTC day at 06:00


def _v4(conn, chain_id, pool_id, hook, block):
    conn.execute(sa.insert(db.v4_pools).values(
        chain_id=chain_id, pool_id=pool_id, hook_address=hook, currency0=ZERO, currency1=USDC, fee=3000, tick_spacing=60,
        sqrt_price_x96="1", tick=0, block_number=block, block_time=datetime(2026, 8, 1, tzinfo=timezone.utc), tx_hash="0x01"))


def _daily(conn, chain_id, pool_id, day, swaps, wallets, vol, unpriced=0):
    conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=chain_id, pool_id=pool_id, day=day, swaps=swaps, wallets=wallets, volume_usd=vol,
                                                         unpriced_swaps=unpriced))


def _setup(conn, coverage_days=20, backfilled=True, updated=NOW - timedelta(hours=1)):
    insert_hook(conn, HOOK, 8453, name="Listed Hook")
    conn.execute(sa.insert(db.pools).values(pool_id=PA, chain_id=8453, hook_address=HOOK, token0={"id": ZERO, "symbol": "ETH"},
                                            token1={"id": USDC, "symbol": "USDC"}, fee=3000, tick_spacing=60, created_at=None))
    _v4(conn, 8453, PA, HOOK, 100)
    _v4(conn, 8453, PC, HOOK2, 200)
    _v4(conn, 143, PE, HOOK3, 300)      # Monad: not in the web chain list, kept anyway
    for i in range(coverage_days, -1, -1):
        d = DAY - timedelta(days=i)
        _daily(conn, 8453, PA, d, 9 if d == REF else 2, 7 if d == REF else 1, 500.0 if d == REF else 100.0)
    _daily(conn, 8453, PC, REF, 4, 2, 50.0)
    conn.execute(sa.insert(db.indexer_state).values(chain_id=8453, initialize_block=10, swap_block=10, head_block=10,
                                                    backfilled=backfilled, updated_at=updated,
                                                    swap_block_time=NOW - timedelta(hours=1)))
    # keyless already wrote PA for today: a 16-day candle series on another scale, TVL from GeckoTerminal, 24h stats
    for i in range(15, -1, -1):
        conn.execute(sa.insert(db.pool_snapshots).values(pool_id=PA, chain_id=8453, day=DAY - timedelta(days=i), tvl_usd=12345.0,
                                                         volume_usd_cum=1_000_000.0 + (15 - i) * 777, tx_count_cum=0))
    conn.execute(sa.insert(db.pool_stats).values(pool_id=PA, chain_id=8453, day=DAY, vol_24h=999.0, traders_24h=3, buys_24h=40, sells_24h=2))


def _snaps(conn, pool_id):
    return {r.day: (r.volume_usd_cum, r.tx_count_cum, r.tvl_usd) for r in conn.execute(
        sa.select(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == pool_id))}


def test_noop_when_indexer_tables_empty(conn, ctx):
    out = onchain.run(conn, ctx)
    assert out["v4_pools"] == 0 and out["hooks_added"] == 0
    assert conn.execute(sa.select(sa.func.count()).select_from(db.pools)).scalar() == 0


def test_onchain_writes_hooks_pools_series_and_overrides_keyless(conn, ctx):
    _setup(conn)
    out = onchain.run(conn, ctx)
    assert out["hooks_added"] == 2 and out["errors"] == 0

    hk = {(r.address, r.chain_id): r for r in conn.execute(sa.select(db.hooks))}
    assert hk[(HOOK2, 8453)].name is None and hk[(HOOK3, 143)].name is None and hk[(HOOK2, 8453)].code_hash is None
    assert hk[(HOOK, 8453)].name == "Listed Hook"
    assert hk[(HOOK2, 8453)].flags == flags_from_address(HOOK2) and hk[(HOOK2, 8453)].flags["beforeSwap"] is True
    assert hk[(HOOK2, 8453)].hooklisted is False and hk[(HOOK3, 143)].hooklisted is False

    pl = {(r.pool_id, r.chain_id): r for r in conn.execute(sa.select(db.pools))}
    assert set(pl) == {(PA, 8453), (PC, 8453), (PE, 143)}
    assert pl[(PA, 8453)].token0["symbol"] == "ETH"                       # keyless symbols survive
    assert pl[(PC, 8453)].token0 == {"id": ZERO, "symbol": None} and pl[(PC, 8453)].hook_address == HOOK2
    assert db.as_utc(pl[(PA, 8453)].created_at) == datetime(2026, 8, 1, tzinfo=timezone.utc)

    s = _snaps(conn, PA)
    start = DAY - timedelta(days=14)
    assert min(s) == start - timedelta(days=1) and s[start - timedelta(days=1)] == (0.0, 0, 12345.0)
    assert s[DAY] == (13 * 100.0 + 500.0 + 100.0, 13 * 2 + 9 + 2, 12345.0)   # day-14..day, TVL kept from GeckoTerminal
    assert all(v[2] == 12345.0 for v in s.values())
    c = _snaps(conn, PC)
    assert c[DAY][0] == 50.0 and c[REF - timedelta(days=1)][0] == 0.0 and c[DAY][2] is None

    st = {r.pool_id: r for r in conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.day == DAY))}
    assert (st[PA].vol_24h, st[PA].traders_24h, st[PA].buys_24h) == (500.0, 7, 40)   # on-chain wins, keyless buys kept
    assert (st[PC].vol_24h, st[PC].traders_24h) == (50.0, 2)

    # score reads the on-chain series: 7d = REF-6..REF, prev = REF-13..REF-7
    for i, h in enumerate([HOOK, HOOK2]):
        p = conn.execute(sa.insert(db.projects).values(slug=f"p{i}", name=f"P{i}", first_seen_at=NOW - timedelta(days=60))).inserted_primary_key[0]
        conn.execute(sa.update(db.hooks).values(project_id=p).where(db.hooks.c.address == h))
    score.run(conn, ctx)
    row = conn.execute(sa.select(db.project_daily).where(db.project_daily.c.project_id == 1)).one()
    assert (row.vol_7d, row.vol_7d_prev, row.swaps_7d) == (6 * 100.0 + 500.0, 700.0, 5 * 2 + 9 + 2)


def test_partial_history_keeps_keyless_series_but_overrides_stats(conn, ctx):
    _setup(conn, coverage_days=3)
    onchain.run(conn, ctx)
    s = _snaps(conn, PA)
    assert s[DAY][0] == 1_000_000.0 + 15 * 777                              # keyless series untouched
    c = _snaps(conn, PC)                                                    # no keyless series: on-chain from coverage start
    assert min(c) == DAY - timedelta(days=4) and c[DAY][0] == 50.0
    st = conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.pool_id == PA)).one()
    assert (st.vol_24h, st.traders_24h) == (500.0, 7)


def test_not_backfilled_or_stale_chain_leaves_usage_alone(conn, ctx):
    _setup(conn, backfilled=False)
    out = onchain.run(conn, ctx)
    assert out["pools"] == 3 and out["pools_usage"] == 0
    assert conn.execute(sa.select(db.pool_stats.c.vol_24h).where(db.pool_stats.c.pool_id == PA)).scalar() == 999.0
    conn.execute(sa.update(db.indexer_state).values(backfilled=True, updated_at=NOW - timedelta(days=2)))
    out = onchain.run(conn, ctx)
    assert out["chains_stale"] == 1 and out["pools_usage"] == 0


def test_new_hook_cap_takes_busiest_first(conn, ctx, monkeypatch):
    monkeypatch.setenv("ONCHAIN_MAX_NEW_HOOKS", "1")
    _setup(conn)
    out = onchain.run(conn, ctx)
    assert out["hooks_added"] == 1 and out["hooks_deferred"] == 1
    assert {r.address for r in conn.execute(sa.select(db.hooks))} == {HOOK, HOOK2}     # HOOK2 has swaps, HOOK3 none
    assert {r.pool_id for r in conn.execute(sa.select(db.pools))} == {PA, PC}           # PE waits for its hook


def test_stuck_swap_cursor_keeps_keyless_even_when_updated_at_is_fresh(conn, ctx):
    _setup(conn)                                   # Initialize commits kept updated_at fresh...
    conn.execute(sa.update(db.indexer_state).values(swap_block_time=datetime(2026, 10, 5, 18, tzinfo=timezone.utc)))
    out = onchain.run(conn, ctx)                   # ...but swaps stopped at 10-05 18:00, inside REF (10-05)
    assert out["chains_stale"] == 1 and out["pools_usage"] == 0
    assert conn.execute(sa.select(db.pool_stats.c.vol_24h).where(db.pool_stats.c.pool_id == PA)).scalar() == 999.0


def test_swaps_still_owed_to_the_indexers_lookup_queue_keep_keyless_usage_for_that_day(conn, ctx):
    """The indexer commits a swap page without the pools it cannot place yet (pool_lookup_queue) and counts their swaps
    when it settles them. A day those swaps fall in is not final until then; owed swaps from today do not hold REF."""
    _setup(conn)
    late_on_ref = datetime(2026, 10, 5, 23, 59, tzinfo=timezone.utc)
    conn.execute(sa.insert(db.pool_lookup_queue).values(chain_id=8453, pool_id=PE, from_block=5, seen_block=9, from_time=late_on_ref))
    conn.execute(sa.insert(db.pool_lookup_queue).values(chain_id=10, pool_id=PE, from_block=5, seen_block=9, from_time=late_on_ref))
    out = onchain.run(conn, ctx)
    assert out["chains_stale"] == 1 and out["pools_usage"] == 0 and onchain.owed_before(conn, 8453, NOW) == 1
    assert conn.execute(sa.select(db.pool_stats.c.vol_24h).where(db.pool_stats.c.pool_id == PA)).scalar() == 999.0
    conn.execute(sa.update(db.pool_lookup_queue).values(from_time=datetime(2026, 10, 6, 0, 1, tzinfo=timezone.utc)))
    out = onchain.run(conn, ctx)
    assert out["chains_stale"] == 0 and out["chains_usage"] == 1
    assert conn.execute(sa.select(db.pool_stats.c.vol_24h).where(db.pool_stats.c.pool_id == PA)).scalar() == 500.0


CUSTOM, PF = "0x" + "f" * 36 + "0088", "0x" + "f" * 64          # beforeSwap + beforeSwapReturnsDelta (bit 3)


def test_custom_accounting_hook_keeps_keyless_volume_and_takes_onchain_wallets(conn, ctx):
    assert custom_accounting(CUSTOM) and not custom_accounting(HOOK2) and not custom_accounting("0x" + "1" * 36 + "0044")
    _setup(conn)
    insert_hook(conn, CUSTOM, 8453, name="Curve Hook")
    _v4(conn, 8453, PF, CUSTOM, 400)
    for i in range(20, -1, -1):
        _daily(conn, 8453, PF, DAY - timedelta(days=i), 5, 3, 0.0)         # the Swap event says 0/0: the hook took the trade
    for i in range(15, -1, -1):
        conn.execute(sa.insert(db.pool_snapshots).values(pool_id=PF, chain_id=8453, day=DAY - timedelta(days=i), tvl_usd=1.0,
                                                         volume_usd_cum=5000.0 - i, tx_count_cum=0))
    conn.execute(sa.insert(db.pool_stats).values(pool_id=PF, chain_id=8453, day=DAY, vol_24h=777.0, traders_24h=1))
    onchain.run(conn, ctx)
    st = {r.pool_id: r for r in conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.day == DAY))}
    assert (st[PF].vol_24h, st[PF].traders_24h, st[PF].vol_source) == (777.0, 3, "hook")
    assert (st[PA].vol_24h, st[PA].vol_source) == (500.0, "onchain")
    assert _snaps(conn, PF)[DAY][0] == 5000.0                               # keyless series untouched


def test_mostly_unpriced_pool_day_does_not_override_and_series_volume_is_null(conn, ctx):
    _setup(conn)
    conn.execute(sa.update(db.pool_daily_onchain).where(db.pool_daily_onchain.c.pool_id == PC).values(unpriced_swaps=3))
    onchain.run(conn, ctx)
    st = conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.pool_id == PC)).one()
    assert (st.vol_24h, st.traders_24h, st.vol_source) == (None, 2, "unpriced")
    assert _snaps(conn, PC)[DAY][:2] == (None, 4)                           # swap count kept, volume unknown


def test_pools_copy_is_incremental_and_catches_up_hooks_known_later(conn, ctx, monkeypatch):
    monkeypatch.setenv("ONCHAIN_MAX_NEW_HOOKS", "1")
    _setup(conn)
    assert onchain.run(conn, ctx)["pools"] == 2                     # PA, PC; HOOK3 deferred, PE now below the watermark
    out = onchain.run(conn, ctx)                                    # HOOK3 known now: the catch-up copies its pool
    assert out["pools"] == 1 and (PE, 143) in {(r.pool_id, r.chain_id) for r in conn.execute(sa.select(db.pools))}
    assert onchain.run(conn, ctx)["pools"] == 0                     # nothing new: nothing read
    _v4(conn, 8453, "0x" + "9" * 64, HOOK, 500)
    assert onchain.run(conn, ctx)["pools"] == 1


def _pool_keys(conn):
    return {(r.pool_id, r.chain_id) for r in conn.execute(sa.select(db.pools))}


def test_pools_inserted_below_the_watermark_after_a_copy_are_copied(conn, ctx):
    _setup(conn)
    onchain.run(conn, ctx)                                          # 8453 watermark = block 200
    late = "0x" + "7" * 64
    _v4(conn, 8453, late, HOOK, 50)                                 # a backfill inserts an older Initialize of a known hook
    onchain.run(conn, ctx)
    assert (late, 8453) in _pool_keys(conn)


def test_deferred_hook_whose_first_pool_came_from_snapshot_still_gets_its_other_pools(conn, ctx, monkeypatch):
    monkeypatch.setenv("ONCHAIN_MAX_NEW_HOOKS", "1")
    _setup(conn)
    onchain.run(conn, ctx)                                          # HOOK3 deferred; PE (block 300) passes the watermark
    insert_hook(conn, HOOK3, 143, name="Snapshotted")               # keyless snapshot adds the hook and one of its pools
    conn.execute(sa.insert(db.pools).values(pool_id="0x" + "6" * 64, chain_id=143, hook_address=HOOK3))
    onchain.run(conn, ctx)
    assert (PE, 143) in _pool_keys(conn)


def test_an_earlier_mostly_unpriced_day_makes_volume_unknown_from_that_day_in_series_and_stats(conn, ctx):
    _setup(conn)
    bad = REF - timedelta(days=3)
    _daily(conn, 8453, PC, bad, 4, 1, 0.0, unpriced=4)              # REF itself is priced (4 swaps, $50)
    onchain.run(conn, ctx)
    s = _snaps(conn, PC)
    assert s[bad - timedelta(days=1)][0] == 0.0 and s[bad][0] is None and s[DAY][:2] == (None, 8)
    st = conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.pool_id == PC)).one()
    assert (st.vol_24h, st.traders_24h, st.vol_source) == (None, 2, "unpriced")   # agrees with the series at REF


def _snapd(conn, pool_id):
    return {r.day: (r.volume_usd_day, r.tx_count_day, r.estimated) for r in conn.execute(
        sa.select(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == pool_id))}


def test_onchain_series_carries_daily_values_and_keeps_older_days(conn, ctx):
    _setup(conn)
    old = DAY - timedelta(days=20)
    conn.execute(sa.insert(db.pool_snapshots).values(pool_id=PA, chain_id=8453, day=old, tvl_usd=1.0, volume_usd_cum=5.0,
                                                     volume_usd_day=5.0, tx_count_day=1))
    conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == PA,
                                                    db.pool_snapshots.c.day == DAY - timedelta(days=15))
                 .values(volume_usd_day=777.0, tx_count_day=3))
    onchain.run(conn, ctx)
    d = _snapd(conn, PA)
    assert d[REF] == (500.0, 9, False) and d[REF - timedelta(days=1)] == (100.0, 2, False)
    assert d[DAY][2] is True                                    # the running day is partial: estimated
    assert d[DAY - timedelta(days=15)] == (777.0, 3, None)      # the seed day resets the cumulative only
    assert d[old] == (5.0, 1, None)                             # days before the series stay, for re-scoring past days
    c = _snapd(conn, PC)
    assert c[REF] == (50.0, 4, False) and c[REF - timedelta(days=1)] == (0.0, 0, False)   # no swaps that day: 0, known


def test_kept_keyless_series_gets_onchain_swap_counts(conn, ctx):
    _setup(conn, coverage_days=3)                               # history too short: PA keeps its keyless series
    onchain.run(conn, ctx)
    d = _snapd(conn, PA)
    assert (d[REF][1], d[REF - timedelta(days=1)][1], d[DAY - timedelta(days=5)][1]) == (9, 2, None)
    assert d[REF][0] is None                                    # volume columns untouched


def test_onchain_pool_stats_carry_distinct_wallets_and_the_closed_day_boundary(conn, ctx):
    _setup(conn)
    onchain.run(conn, ctx)
    st = conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.pool_id == PA)).one()
    assert (st.wallets_24h, st.traders_24h) == (7, 7)
    assert db.as_utc(st.window_end) == datetime(2026, 10, 6, tzinfo=timezone.utc)    # REF (10-05) ends 10-06 00:00


def test_hooks_get_their_true_pool_count_including_pools_past_the_indexer_cap(conn, ctx):
    _setup(conn)
    conn.execute(sa.insert(db.hook_pool_counts).values(chain_id=8453, hook_address=HOOK, pools_total=5000, first_block=150,
                                                       last_block=900))
    out = onchain.run(conn, ctx)
    tot = {(r.chain_id, r.address): r.pools_total for r in conn.execute(sa.select(db.hooks))}
    assert (tot[(8453, HOOK)], tot[(8453, HOOK2)], tot[(143, HOOK3)], out["hook_totals"]) == (5000, 1, 1, 3)
    assert onchain.run(conn, ctx)["hook_totals"] == 0                    # unchanged: nothing rewritten


def test_a_capped_hooks_folded_swaps_reach_its_usage_through_its_aggregate_pool(conn, ctx):
    _setup(conn)
    agg = db.hook_pool_id(HOOK)
    conn.execute(sa.insert(db.hook_pool_counts).values(chain_id=8453, hook_address=HOOK, pools_total=5000, first_block=150,
                                                       last_block=900, swaps_total=4, volume_usd_total=80.0))
    _daily(conn, 8453, agg, REF, 4, 3, 80.0)
    out = onchain.run(conn, ctx)
    p = conn.execute(sa.select(db.pools).where(db.pools.c.pool_id == agg)).one()
    st = conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.pool_id == agg)).one()
    assert (p.hook_address, p.chain_id, out["hook_pools"]) == (HOOK, 8453, 1)
    assert (st.vol_24h, st.wallets_24h, st.vol_source) == (80.0, 3, "hook_total")
    assert _snaps(conn, agg)[REF][1] is not None                        # a daily series like any pool's
    assert onchain.run(conn, ctx)["hook_pools"] == 0
