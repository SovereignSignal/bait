import math
from datetime import timedelta

import pytest
import sqlalchemy as sa

from bait import db
from bait.steps import score
from tests.conftest import DAY, NOW, insert_hook, insert_pool, insert_project, insert_snap

END = DAY - timedelta(days=1)          # window_end: the run on DAY reports closed UTC days through END


def daily(conn, pid):
    return conn.execute(sa.select(db.project_daily).where(db.project_daily.c.project_id == pid)).one()


def series(conn, pool, chain, vols: dict, tvl=0.0, tx: dict | None = None):
    """One row per day: vols = {day: volume}, tx = {day: swaps} (absent = unknown); volume_usd_cum is the running sum."""
    cum = 0.0
    for d in sorted(vols):
        cum += vols[d]
        insert_snap(conn, pool, chain, d, tvl, cum, None, vol_day=vols[d], tx_day=(tx or {}).get(d))


def test_score_hand_computed(conn, ctx):
    # One pool: $5K a day the week before last, $10K a day last week, 100 swaps every day, TVL 5000. The running day (DAY)
    # has a partial row that must never be read.
    p = insert_project(conn, "P", first_seen=NOW - timedelta(days=3))
    insert_hook(conn, "0xp", 8453, project_id=p, name="P")
    insert_pool(conn, "0xpool1", 8453, "0xp", created=NOW - timedelta(days=60))
    vols = {END - timedelta(days=i): (10_000.0 if i < 7 else 5_000.0) for i in range(14)}
    series(conn, "0xpool1", 8453, vols, tvl=5000, tx={d: 100 for d in vols})
    insert_snap(conn, "0xpool1", 8453, DAY, 1, 999_999_999, None, vol_day=1_234_567.0, tx_day=9_999)    # still running
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert (d.window_end, d.vol_24h, d.vol_7d, d.vol_7d_prev) == (END, 10_000, 70_000, 35_000)
    assert (d.swaps_24h, d.swaps_7d, d.tvl_usd, d.pools_n) == (100, 700, 5000, 1)
    assert d.score_trending == pytest.approx(math.log1p(70_000) * 2.0)
    assert d.is_new is False           # pipeline first saw it 3d ago, but its pool is 60d old: not new
    assert d.is_spam is False
    assert out == {"projects": 1, "with_pools": 1, "new": 0, "trending": 1, "spam": 0, "wash": 0, "wash_24h": 0, "wash_7d": 0,
                   "wash_vol_tvl": 0, "wash_no_liquidity": 0, "wash_symmetric": 0}


def test_score_not_new_when_old_or_tiny(conn, ctx):
    old = insert_project(conn, "Old", first_seen=NOW - timedelta(days=30))
    insert_hook(conn, "0xo", 1, project_id=old)
    insert_pool(conn, "0xpo", 1, "0xo", created=NOW - timedelta(days=30))
    insert_snap(conn, "0xpo", 1, END, 5_000_000, 1_000_000, 50, vol_day=0.0, tx_day=0)
    tiny = insert_project(conn, "Tiny", first_seen=NOW - timedelta(days=1), slug="tiny")
    insert_hook(conn, "0xt", 1, project_id=tiny)
    insert_pool(conn, "0xpt", 1, "0xt", created=NOW - timedelta(days=1))          # born on END
    insert_snap(conn, "0xpt", 1, END, 900, 3_000, 9, vol_day=3_000.0, tx_day=9)
    score.run(conn, ctx)
    assert daily(conn, old).is_new is False
    t = daily(conn, tiny)
    assert t.is_new is False and t.is_spam is False     # TVL 900 >= 100 so not spam, but not "new" either
    assert (t.vol_7d, t.swaps_7d, t.score_trending) == (3_000, 9, 0.0)   # days before the pool existed count as 0 swaps


def test_score_cold_start_sums_known_days_never_all_time(conn, ctx):
    p = insert_project(conn, "Cold", first_seen=NOW - timedelta(days=100))
    insert_hook(conn, "0xc", 1, project_id=p)
    insert_pool(conn, "0xpc", 1, "0xc", created=NOW - timedelta(days=100))
    insert_snap(conn, "0xpc", 1, END - timedelta(days=1), 10_000, 9_000_000, 90_000)     # first night: all-time cumulative only
    insert_snap(conn, "0xpc", 1, END, 10_000, 9_020_000, 90_100, vol_day=20_000.0, tx_day=100)
    score.run(conn, ctx)
    d = daily(conn, p)
    assert (d.vol_7d, d.vol_7d_prev, d.vol_24h) == (20_000, 0, 20_000)
    assert d.swaps_7d is None and d.swaps_24h == 100     # six days of the week unknown: no 7-day count
    assert d.score_trending == 0.0                       # no prior week to grow from


def test_score_spam_and_no_pools(conn, ctx):
    p = insert_project(conn, "Ghost", first_seen=NOW)
    insert_hook(conn, "0xg", 1, project_id=p)
    s = insert_project(conn, "Dust", first_seen=NOW, slug="dust")
    insert_hook(conn, "0xd", 1, project_id=s)
    insert_pool(conn, "0xpd", 1, "0xd", created=NOW)
    insert_snap(conn, "0xpd", 1, END, 12, 40, 3)
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
    days = [END - timedelta(days=2), END - timedelta(days=1), END]
    series(conn, "0xlp", 8453, dict(zip(days, (0.0, 0.0, 500_000.0))), tvl=5000, tx=dict(zip(days, (0, 0, 900))))
    score.run(conn, ctx)
    d = daily(conn, p)
    assert d.is_new is True and d.score_trending == 0.0 and d.vol_7d == 500_000 and d.swaps_7d == 900


def test_wash_rule_reads_persisted_pool_stats(conn, ctx):
    """The 24h rule reads pool_stats(day) from the database, so a score-only run judges like the nightly one."""
    p = insert_project(conn, "Washy", first_seen=NOW - timedelta(days=60))
    insert_hook(conn, "0xw", 8453, project_id=p, name="Washy")
    insert_pool(conn, "0xwp", 8453, "0xw", created=NOW - timedelta(days=60))
    insert_snap(conn, "0xwp", 8453, END, 10_000, 9_000_000, 100, vol_day=8_000_000.0)    # 800x its TVL; 100 swaps: not spam
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xwp", chain_id=8453, day=DAY, vol_24h=5_000_000, traders_24h=6))
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert d.is_wash and not d.is_spam and d.wash_reason == "24h,vol_tvl" and d.score_trending == 0.0 and out["wash"] == 1


def _onchain_project(conn, name, pool, wallets, vol_per_day, tvl=50_000):
    p = insert_project(conn, name, first_seen=NOW - timedelta(days=60))
    insert_hook(conn, f"0x{name}", 10, project_id=p, name=name)
    insert_pool(conn, pool, 10, f"0x{name}", created=NOW - timedelta(days=60))
    series(conn, pool, 10, {END - timedelta(days=i): float(vol_per_day) for i in range(14)}, tvl=tvl,
           tx={END - timedelta(days=i): 2 for i in range(14)})
    for i in range(7):
        d = END - timedelta(days=i)
        conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id=pool, day=d, swaps=2, wallets=1, volume_usd=vol_per_day))
        for w in range(wallets):
            conn.execute(sa.insert(db.pool_wallet_day).values(chain_id=10, pool_id=pool, day=d, address=f"0xw{w}", swaps=1))
    return p


def test_wash_7d_from_indexer_one_wallet_hundreds_of_millions(conn, ctx):
    """10/8 case: Optimism 0x69af...7fff moved $436M in 7 days from one wallet. Same wallets every day count once."""
    wash = _onchain_project(conn, "Desk", "0xdp", wallets=1, vol_per_day=60_000_000)
    busy = _onchain_project(conn, "Busy", "0xbp", wallets=11, vol_per_day=60_000_000, tvl=50_000_000)   # 11 wallets: not wash
    small = _onchain_project(conn, "Small", "0xsp", wallets=1, vol_per_day=100_000)       # $700K < $1M: not wash
    _optimism_ready(conn)
    out = score.run(conn, ctx)
    w = daily(conn, wash)
    assert w.is_wash and not w.is_spam and w.wash_reason == "7d,vol_tvl" and w.score_trending == 0.0
    assert not daily(conn, busy).is_wash and not daily(conn, small).is_wash
    assert out["wash_7d"] == 1


def test_is_wash_7d_thresholds():
    from bait.rules import is_wash_7d
    assert is_wash_7d(1_000_000, 10, tvl=1_000, symmetric=False)
    assert not is_wash_7d(999_999, 1, tvl=1, symmetric=False) and not is_wash_7d(5_000_000, 11, tvl=1, symmetric=False)


def _optimism_ready(conn, swap_time=NOW):
    conn.execute(sa.insert(db.indexer_state).values(chain_id=10, initialize_block=1, swap_block=1, head_block=1, backfilled=True,
                                                    updated_at=NOW, swap_block_time=swap_time))


def test_wash_7d_ignores_a_chain_whose_swaps_are_not_caught_up(conn, ctx):
    wash = _onchain_project(conn, "Desk", "0xdp", wallets=1, vol_per_day=60_000_000)
    _optimism_ready(conn, swap_time=NOW - timedelta(days=2))
    out = score.run(conn, ctx)
    assert out["wash_7d"] == 0 and not daily(conn, wash).is_wash


def test_held_back_pool_with_null_volume_is_left_out_of_both_weeks(conn, ctx):
    # Two pools of one project. The normal one did $1,000 in each week. The other's on-chain volume became unknown during
    # this week (onchain wrote NULL from that day on): it must not read as $0 this week next to its $5,000 last week.
    p = insert_project(conn, "Mixed", first_seen=NOW - timedelta(days=60))
    insert_hook(conn, "0xm", 8453, project_id=p, name="Mixed")
    for pool, prev_week, cur in (("0xnormal", 1_000.0, 1_000.0), ("0xheld", 5_000.0, None)):
        insert_pool(conn, pool, 8453, "0xm", created=NOW - timedelta(days=60))
        insert_snap(conn, pool, 8453, END - timedelta(days=7), 0, prev_week, 10, vol_day=prev_week, tx_day=10)
        insert_snap(conn, pool, 8453, END, 0, None if cur is None else prev_week + cur, 20, vol_day=cur, tx_day=10)
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xheld", chain_id=8453, day=DAY, vol_24h=None, traders_24h=3))
    score.run(conn, ctx)
    d = daily(conn, p)
    assert (d.vol_7d, d.vol_7d_prev) == (1_000, 1_000)


def test_swaps_are_null_unless_every_day_of_the_window_is_known(conn, ctx):
    p = insert_project(conn, "Gap", first_seen=NOW - timedelta(days=60), slug="gap")
    insert_hook(conn, "0xg", 8453, project_id=p, name="Gap")
    insert_pool(conn, "0xgp", 8453, "0xg", created=NOW - timedelta(days=60))
    insert_pool(conn, "0xquiet", 8453, "0xg", created=NOW - timedelta(days=60))
    week = {END - timedelta(days=i): 1_000.0 for i in range(7)}
    series(conn, "0xgp", 8453, week, tx={d: 3_000 for d in week if d != END - timedelta(days=3)})       # one day unknown
    insert_snap(conn, "0xquiet", 8453, END - timedelta(days=9), 10, 0.0, 0, vol_day=0.0)               # no volume this week: adds 0
    score.run(conn, ctx)
    d = daily(conn, p)
    assert (d.swaps_7d, d.swaps_24h, d.vol_7d) == (None, 3_000, 7_000)       # never one day presented as seven
    conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == "0xgp",
                                                    db.pool_snapshots.c.day == END - timedelta(days=3)).values(tx_count_day=3_000))
    score.run(conn, ctx)
    assert daily(conn, p).swaps_7d == 21_000


def test_score_for_a_day_reads_stored_rows_only_so_a_rerun_writes_the_same_rows(conn, ctx):
    p = insert_project(conn, "P", first_seen=NOW - timedelta(days=60))
    insert_hook(conn, "0xp", 8453, project_id=p, name="P")
    insert_pool(conn, "0xpool1", 8453, "0xp", created=NOW - timedelta(days=60))
    series(conn, "0xpool1", 8453, {END - timedelta(days=i): 1_000.0 * (i + 1) for i in range(14)}, tvl=9_000,
           tx={END - timedelta(days=i): 5 for i in range(14)})
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xpool1", chain_id=8453, day=DAY, vol_24h=1_000.0, traders_24h=40))
    score.run(conn, ctx)
    first = dict(daily(conn, p)._mapping)
    ctx.now = NOW + timedelta(days=5)                                    # package 9 re-scores days later
    ctx.extras["keyless_pool_stats"] = {("0xpool1", 8453): (9e9, 1)}     # a stale in-run hand-off is never read
    score.run(conn, ctx)
    assert dict(daily(conn, p)._mapping) == first


def test_windows_schema_is_additive_on_an_existing_database():
    e = db.make_engine("sqlite://")
    with e.begin() as c:
        c.execute(sa.text("CREATE TABLE project_daily (project_id integer, day date, tvl_usd float, vol_7d float, vol_7d_prev float, "
                          "swaps_7d bigint, pools_n integer, score_trending float, is_new boolean NOT NULL DEFAULT 0, "
                          "is_spam boolean NOT NULL DEFAULT 0, PRIMARY KEY (project_id, day))"))
        c.execute(sa.text("INSERT INTO project_daily (project_id, day, is_spam) VALUES (1, '2026-10-05', 1)"))
        c.execute(sa.text("CREATE TABLE pool_snapshots (pool_id text, chain_id integer, day date, tvl_usd float, "
                          "volume_usd_cum float, tx_count_cum bigint, PRIMARY KEY (pool_id, chain_id, day))"))
        c.execute(sa.text("CREATE TABLE pool_stats (pool_id text, chain_id integer, day date, vol_24h float, traders_24h integer, "
                          "PRIMARY KEY (pool_id, chain_id, day))"))
    db.create_all(e)
    added = set(db.add_missing_columns(e))
    assert {"project_daily.window_end", "project_daily.vol_24h", "project_daily.swaps_24h", "project_daily.wallets_24h",
            "project_daily.wallets_exact", "project_daily.is_wash", "project_daily.wash_reason",
            "pool_snapshots.volume_usd_day", "pool_snapshots.tx_count_day", "pool_snapshots.estimated",
            "pool_stats.vol_24h_rolling", "pool_stats.wallets_24h", "pool_stats.window_end"} <= added
    with e.connect() as c:
        assert c.execute(sa.text("SELECT is_wash, wash_reason FROM project_daily")).one() == (0, None)


def test_project_wallets_distinct_on_ready_chains_upper_bound_elsewhere(conn, ctx):
    # StablePair-like: six GeckoTerminal pools whose buyers + sellers sum to 1,142 (534+511+45+31+12+9)
    sp = insert_project(conn, "StablePair", first_seen=NOW - timedelta(days=60), slug="stablepair")
    insert_hook(conn, "0xsp", 1, project_id=sp, name="StablePair")
    for i, (b, s) in enumerate([(300, 234), (300, 211), (25, 20), (16, 15), (6, 6), (5, 4)]):
        pool = f"0xsp{i}"
        insert_pool(conn, pool, 1, "0xsp", created=NOW - timedelta(days=60))
        insert_snap(conn, pool, 1, END, 1_000, 10.0, 1, vol_day=10.0, tx_day=1)
        conn.execute(sa.insert(db.pool_stats).values(pool_id=pool, chain_id=1, day=DAY, vol_24h=10.0, traders_24h=b + s,
                                                     buyers_24h=b, sellers_24h=s))
    solo = insert_project(conn, "Solo", first_seen=NOW - timedelta(days=60), slug="solo")
    insert_hook(conn, "0xso", 1, project_id=solo, name="Solo")
    insert_pool(conn, "0xsop", 1, "0xso", created=NOW - timedelta(days=60))
    insert_snap(conn, "0xsop", 1, END, 1_000, 10.0, 1, vol_day=10.0, tx_day=1)
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xsop", chain_id=1, day=DAY, vol_24h=10.0, traders_24h=70, wallets_24h=40))
    desk = _onchain_project(conn, "Desk", "0xdp", wallets=2, vol_per_day=100.0)
    insert_pool(conn, "0xdp2", 10, "0xDesk", created=NOW - timedelta(days=60))
    for w in ("0xw0", "0xw1"):                     # the same two wallets in a second pool count once
        conn.execute(sa.insert(db.pool_wallet_day).values(chain_id=10, pool_id="0xdp2", day=END, address=w, swaps=1))
    _optimism_ready(conn)
    score.run(conn, ctx)
    assert (daily(conn, sp).wallets_24h, daily(conn, sp).wallets_exact) == (1_142, False)
    assert (daily(conn, solo).wallets_24h, daily(conn, solo).wallets_exact) == (40, True)
    assert (daily(conn, desk).wallets_24h, daily(conn, desk).wallets_exact) == (2, True)


def test_24h_wash_uses_a_wallet_lower_bound_not_buyers_plus_sellers(conn, ctx):
    p = insert_project(conn, "Ouro", first_seen=NOW - timedelta(days=60), slug="ouro")
    insert_hook(conn, "0xou", 4663, project_id=p, name="Ouro")
    insert_pool(conn, "0xoup", 4663, "0xou", created=NOW - timedelta(days=60))
    insert_snap(conn, "0xoup", 4663, END, 1_000, 1.0, 1, vol_day=900_000.0, tx_day=500)
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xoup", chain_id=4663, day=DAY, vol_24h=900_000.0, traders_24h=26,
                                                 buyers_24h=13, sellers_24h=13))
    out = score.run(conn, ctx)
    assert out["wash"] == 1                        # 13 buyers + 13 sellers is at least 13 wallets, not 26 > 25


def test_wash_reasons_on_the_live_cases(conn, ctx):
    """10/9 audit cases: OUROBOROS/CRCL on Robinhood is flagged with its reasons; the Base launchpad family 0x0f48 and a
    private RFQ hook are not; dust stays spam and is not called wash."""
    def keyless_project(name, chain, tvl, vol_day, buyers, sellers, rolling=None):
        p = insert_project(conn, name, first_seen=NOW - timedelta(days=60), slug=name.lower())
        insert_hook(conn, f"0x{name.lower()}", chain, project_id=p, name=name)
        pool = f"0x{name.lower()}p"
        insert_pool(conn, pool, chain, f"0x{name.lower()}", created=NOW - timedelta(days=60))
        series(conn, pool, chain, {END - timedelta(days=i): vol_day for i in range(7)}, tvl=tvl)
        conn.execute(sa.insert(db.pool_stats).values(pool_id=pool, chain_id=chain, day=DAY, vol_24h=vol_day, vol_24h_rolling=rolling,
                                                     traders_24h=buyers + sellers, buyers_24h=buyers, sellers_24h=sellers))
        return p, pool
    ouro, ouro_pool = keyless_project("Ouroboros", 4663, tvl=10_000, vol_day=1_237_000.0, buyers=13, sellers=13, rolling=1_237_000.0)
    for w in range(10):       # symmetric 10/10 bots: $1M of $1.237M
        conn.execute(sa.insert(db.pool_top_wallets).values(pool_id=ouro_pool, chain_id=4663, day=DAY, address=f"0xbot{w}",
                                                           volume_usd=100_000.0, trades=10, buys=5, sells=5))
    launch, _ = keyless_project("Launchpad", 8453, tvl=200_000, vol_day=9_857_000.0, buyers=15_000, sellers=15_000)   # 345x
    rfq = _onchain_project(conn, "Rfq", "0xrfq", wallets=4, vol_per_day=714_000, tvl=2_000_000)                       # 2.5x
    _optimism_ready(conn)
    dust = insert_project(conn, "Dust", first_seen=NOW, slug="dust")
    insert_hook(conn, "0xdust", 1, project_id=dust)
    insert_pool(conn, "0xdustp", 1, "0xdust", created=NOW)
    insert_snap(conn, "0xdustp", 1, END, 12, 40, 3)
    out = score.run(conn, ctx)
    o = daily(conn, ouro)
    assert o.is_wash and o.wash_reason == "24h,vol_tvl,symmetric" and not o.is_spam and o.score_trending == 0 and not o.is_new
    assert not daily(conn, launch).is_wash and not daily(conn, rfq).is_wash
    assert daily(conn, dust).is_spam and not daily(conn, dust).is_wash
    assert (out["wash"], out["wash_24h"], out["wash_7d"], out["wash_vol_tvl"], out["wash_no_liquidity"], out["wash_symmetric"]) \
        == (1, 1, 0, 1, 0, 1)


def test_no_liquidity_flags_the_optimism_0x69af_shape(conn, ctx):
    """Optimism 0x69af...7fff: $436M in 7 days through 8 swaps from one wallet, its TVL known to be $0. The volume/TVL
    ratio is undefined at $0, so without "no liquidity" the 7-day rule built for this case missed it."""
    p = insert_project(conn, "Desk69", first_seen=NOW - timedelta(days=60), slug="desk69")
    insert_hook(conn, "0x69af", 10, project_id=p, name="Desk69")
    insert_pool(conn, "0x69afp", 10, "0x69af", created=NOW - timedelta(days=60))
    days = [END - timedelta(days=i) for i in range(4)]                        # 8 swaps: two a day on four days
    series(conn, "0x69afp", 10, {d: 109_000_000.0 for d in days}, tvl=0.0, tx={d: 2 for d in days})
    for d in days:
        conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id="0x69afp", day=d, swaps=2, wallets=1,
                                                             volume_usd=109_000_000.0))
        conn.execute(sa.insert(db.pool_wallet_day).values(chain_id=10, pool_id="0x69afp", day=d, address="0xdesk", swaps=2))
    _optimism_ready(conn)
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert d.is_wash and d.wash_reason == "7d,no_liquidity" and d.score_trending == 0.0 and not d.is_new
    assert (out["wash_7d"], out["wash_no_liquidity"], out["wash_vol_tvl"]) == (1, 1, 0)


def test_24h_rule_spares_a_private_rfq_hook_with_real_liquidity(conn, ctx):
    """Four wallets, $714K a day, $2M TVL: few wallets alone is not wash."""
    rfq = _onchain_project(conn, "Rfq", "0xrfq", wallets=4, vol_per_day=714_000, tvl=2_000_000)
    _optimism_ready(conn)
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xrfq", chain_id=10, day=DAY, vol_24h=714_000.0, traders_24h=4,
                                                 wallets_24h=4, vol_source="onchain"))
    score.run(conn, ctx)
    assert not daily(conn, rfq).is_wash
