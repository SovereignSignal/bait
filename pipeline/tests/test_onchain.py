"""Nightly step "onchain": indexer tables -> hooks / pools / pool_snapshots / pool_stats, overriding keyless usage."""
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from bait import db
from bait.rules import flags_from_address
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


def _daily(conn, chain_id, pool_id, day, swaps, wallets, vol):
    conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=chain_id, pool_id=pool_id, day=day, swaps=swaps, wallets=wallets, volume_usd=vol))


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
                                                    backfilled=backfilled, updated_at=updated))
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
    ctx.extras["keyless_pool_stats"] = {(PA, 8453): (999.0, 3)}
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
    assert ctx.extras["keyless_pool_stats"][(PA, 8453)] == (500.0, 7)

    # score reads the on-chain series: 7d = day-6..day, prev = day-13..day-7
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
