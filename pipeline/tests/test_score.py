import math
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from bait import db
from bait.steps import activity, onchain, score
from tests.conftest import DAY, NOW, insert_hook, insert_pool, insert_project, insert_snap

END = DAY - timedelta(days=1)          # window_end: the run on DAY reports closed UTC days through END


class _FakeGT:
    def __init__(self, responses):
        self.responses = responses

    def gt(self, path):
        return self.responses.get(path)


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
    insert_snap(conn, "0xpool1", 8453, DAY, 5000, 999_999_999, None, vol_day=1_234_567.0, tx_day=9_999)  # running: TVL only
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert (d.window_end, d.vol_24h, d.vol_7d, d.vol_7d_prev) == (END, 10_000, 70_000, 35_000)
    assert (d.swaps_24h, d.swaps_7d, d.tvl_usd, d.pools_n) == (100, 700, 5000, 1)
    assert d.score_trending == pytest.approx(math.log1p(70_000) * 2.0)
    assert d.is_new is False           # pipeline first saw it 3d ago, but its pool is 60d old: not new
    assert d.is_spam is False
    assert out == {"projects": 1, "with_pools": 1, "new": 0, "trending": 1, "spam": 0, "wash": 0, "wash_24h": 0, "wash_7d": 0,
                   "wash_vol_tvl": 0, "wash_no_liquidity": 0, "wash_symmetric": 0, "wash_uniform": 0, "wash_unverified": 0,
                   "wash_concentrated": 0, "wash_lookalike": 0, "wash_roundtrip": 0, "tvl_untrusted": 0}


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
    assert (d.vol_7d, d.vol_7d_prev, d.vol_24h) == (20_000, None, 20_000)  # no prior-week daily evidence is unknown
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


def _onchain_project(conn, name, pool, wallets, vol_per_day, tvl=50_000, roundtrip_per_day=0.0):
    p = insert_project(conn, name, first_seen=NOW - timedelta(days=60))
    insert_hook(conn, f"0x{name}", 10, project_id=p, name=name)
    insert_pool(conn, pool, 10, f"0x{name}", created=NOW - timedelta(days=60))
    series(conn, pool, 10, {END - timedelta(days=i): float(vol_per_day) for i in range(14)}, tvl=tvl,
           tx={END - timedelta(days=i): 2 for i in range(14)})
    for i in range(7):
        d = END - timedelta(days=i)
        conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id=pool, day=d, swaps=2, wallets=1, volume_usd=vol_per_day,
                                                            roundtrip_usd=roundtrip_per_day))
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
    assert w.is_wash and not w.is_spam and w.wash_reason == "7d,concentrated,vol_tvl" and w.score_trending == 0.0
    assert not daily(conn, busy).is_wash and not daily(conn, small).is_wash
    assert out["wash_7d"] == 1


def test_wash_roundtrip_from_indexer_needs_no_tvl_corroboration(conn, ctx):
    """10/16-17 case: eleven zero-fee hooks on Robinhood, $1.39B in 388 swaps and 9 transactions, one of them 80 swaps of
    5M USDG back and forth; a USDG lookalike read as $210M of TVL, so no few-wallet rule was corroborated. The week's
    volume being round trips is the finding; a desk whose transactions buy or sell is untouched."""
    churn = _onchain_project(conn, "Churn", "0xchurn", wallets=6, vol_per_day=70_000_000, tvl=210_000_000,
                             roundtrip_per_day=69_000_000)
    desk = _onchain_project(conn, "Desk2", "0xdesk2", wallets=6, vol_per_day=70_000_000, tvl=210_000_000,
                            roundtrip_per_day=20_000_000)       # 29% round trips: an occasional arb, not the pool's week
    tiny = _onchain_project(conn, "TinyRT", "0xtinyrt", wallets=1, vol_per_day=100_000, roundtrip_per_day=100_000)   # < $1M
    _optimism_ready(conn)
    out = score.run(conn, ctx)
    c = daily(conn, churn)
    assert c.is_wash and c.wash_reason == "roundtrip" and c.score_trending == 0.0 and not c.is_spam
    assert not daily(conn, desk).is_wash and not daily(conn, tiny).is_wash
    assert out["wash_roundtrip"] == 1


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
    # a chain that is not caught up has no exact counts either: neither the 7-day rule nor the swap bound judges it
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
    insert_snap(conn, "0xquiet", 8453, END - timedelta(days=9), 10, 0.0, 0, vol_day=0.0)               # missing week: unknown
    score.run(conn, ctx)
    d = daily(conn, p)
    assert (d.swaps_7d, d.swaps_24h, d.vol_7d) == (None, None, 7_000)        # zero priced volume is not zero swaps
    conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == "0xgp",
                                                    db.pool_snapshots.c.day == END - timedelta(days=3)).values(tx_count_day=3_000))
    score.run(conn, ctx)
    assert daily(conn, p).swaps_7d is None                                # the other pool still has missing coverage
    series(conn, "0xquiet", 8453, {d: 0.0 for d in week}, tx={d: 0 for d in week})
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
            "project_daily.has_historical_usage", "project_daily.activity_status", "project_daily.wash_historical",
            "pool_snapshots.volume_usd_day", "pool_snapshots.tx_count_day", "pool_snapshots.estimated",
            "pool_stats.vol_24h_rolling", "pool_stats.wallets_24h", "pool_stats.window_end"} <= added
    with e.connect() as c:
        assert c.execute(sa.text("SELECT is_wash, wash_reason FROM project_daily")).one() == (0, None)
        assert c.execute(sa.text("SELECT has_historical_usage, activity_status, wash_historical FROM project_daily")).one() == (0, None, 0)


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
    launch, _ = keyless_project("Launchpad", 8453, tvl=200_000, vol_day=9_857_000.0, buyers=15_000, sellers=15_000)   # 345x
    rfq = _onchain_project(conn, "Rfq", "0xrfq", wallets=4, vol_per_day=714_000, tvl=2_000_000)                       # 2.5x
    _optimism_ready(conn)
    dust = insert_project(conn, "Dust", first_seen=NOW, slug="dust")
    insert_hook(conn, "0xdust", 1, project_id=dust)
    insert_pool(conn, "0xdustp", 1, "0xdust", created=NOW)
    insert_snap(conn, "0xdustp", 1, END, 12, 40, 3)
    # symmetric 10/10 bots, $1M of $1.237M, seen through the activity step (a 24h-wash candidate is fetched too)
    bots = [{"attributes": {"tx_from_address": f"0x{w:040x}", "kind": kind, "volume_in_usd": "10000",
                            "block_timestamp": "2026-10-06T01:00:00Z", "tx_hash": "0x" + "f" * 64}}
            for w in range(10) for kind in ("buy", "sell") * 5]
    ctx.extras["keyless_http"] = _FakeGT({f"/networks/robinhood/pools/{ouro_pool}/trades": {"data": bots}})
    assert activity.run(conn, ctx)["wash_pools"] == 1
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
    insert_hook(conn, "0x69a0", 10, project_id=p, name="Desk69")    # low bits clear: not a custom-accounting hook
    insert_pool(conn, "0x69afp", 10, "0x69a0", created=NOW - timedelta(days=60))
    days = [END - timedelta(days=i) for i in range(4)]                        # 8 swaps: two a day on four days
    series(conn, "0x69afp", 10, {d: 109_000_000.0 for d in days}, tvl=0.0, tx={d: 2 for d in days})
    for d in days:
        conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id="0x69afp", day=d, swaps=2, wallets=1,
                                                             volume_usd=109_000_000.0))
        conn.execute(sa.insert(db.pool_wallet_day).values(chain_id=10, pool_id="0x69afp", day=d, address="0xdesk", swaps=2))
    _optimism_ready(conn)
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert d.is_wash and {"7d", "no_liquidity"} <= set(d.wash_reason.split(",")) and d.score_trending == 0.0 and not d.is_new
    assert not d.is_spam               # $0 TVL and 8 swaps, but $436M moved: listed in All with the badge, never a 404
    assert (out["wash_7d"], out["wash_no_liquidity"], out["wash_vol_tvl"], out["spam"]) == (1, 1, 0, 0)


def test_24h_rule_spares_a_private_rfq_hook_with_real_liquidity(conn, ctx):
    """Four wallets, $714K a day, $2M TVL: few wallets alone is not wash."""
    rfq = _onchain_project(conn, "Rfq", "0xrfq", wallets=4, vol_per_day=714_000, tvl=2_000_000)
    _optimism_ready(conn)
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xrfq", chain_id=10, day=DAY, vol_24h=714_000.0, traders_24h=4,
                                                 wallets_24h=4, vol_source="onchain"))
    score.run(conn, ctx)
    assert not daily(conn, rfq).is_wash


def test_a_project_whose_only_pool_has_unknown_volume_stores_null_volume_not_zero(conn, ctx):
    """Package 5 held this pool's volume back (NULL cumulative): the project's volume is unknown, never $0."""
    p = insert_project(conn, "Held", first_seen=NOW - timedelta(days=60), slug="held")
    insert_hook(conn, "0xh", 8453, project_id=p, name="Held")
    insert_pool(conn, "0xhp", 8453, "0xh", created=NOW - timedelta(days=60))
    insert_snap(conn, "0xhp", 8453, END - timedelta(days=7), 5_000, 100.0, 10, vol_day=100.0, tx_day=10)
    insert_snap(conn, "0xhp", 8453, END, 5_000, None, 20, vol_day=None, tx_day=10)
    score.run(conn, ctx)
    d = daily(conn, p)
    assert (d.vol_24h, d.vol_7d, d.vol_7d_prev) == (None, None, None)
    assert d.score_trending == 0.0 and d.pools_n == 1 and d.tvl_usd == 5_000


def test_a_quiet_day_is_not_a_seven_day_wallet_count(conn, ctx):
    """A launchpad pool did $2M through thousands of wallets on the first day of the week and has been quiet since; its
    TVL is $500 now. Yesterday's 0 buyers / 0 sellers must not read as "fewer than 50 wallets" for the 7-day rules."""
    p = insert_project(conn, "Launch", first_seen=NOW - timedelta(days=60), slug="launch")
    insert_hook(conn, "0xla", 8453, project_id=p, name="Launch")
    insert_pool(conn, "0xlap", 8453, "0xla", created=NOW - timedelta(days=60))
    series(conn, "0xlap", 8453, {END - timedelta(days=i): (2_000_000.0 if i == 6 else 0.0) for i in range(7)}, tvl=500.0,
           tx={END - timedelta(days=i): (6_000 if i == 6 else 0) for i in range(7)})
    for i in range(7):                       # pool_stats(day) covers the day before: day-6 holds the busy day END-6
        busy = i == 6
        conn.execute(sa.insert(db.pool_stats).values(pool_id="0xlap", chain_id=8453, day=DAY - timedelta(days=i),
                                                     vol_24h=2_000_000.0 if busy else 0.0, traders_24h=6_000 if busy else 0,
                                                     buyers_24h=3_000 if busy else 0, sellers_24h=3_000 if busy else 0))
    score.run(conn, ctx)
    assert not daily(conn, p).is_wash


def test_a_whale_off_ready_chains_with_only_rolling_swap_counts_is_unverified_not_a_free_pass(conn, ctx):
    """$436M over 4 days through a pool whose TVL is known to be $0, 8 swaps, no pool_stats row, on a chain the indexer
    does not cover. Keyless swap counts are rolling 06:00-to-06:00 estimates filed under the day before, so they prove
    nothing; with no other evidence the project is unverified wash-like volume, never ranked."""
    p = insert_project(conn, "Whale", first_seen=NOW - timedelta(days=60), slug="whale")
    insert_hook(conn, "0xwh", 4663, project_id=p, name="Whale")
    insert_pool(conn, "0xwhp", 4663, "0xwh", created=NOW - timedelta(days=60))
    days = [END - timedelta(days=i) for i in range(4)]
    series(conn, "0xwhp", 4663, {d: 109_000_000.0 for d in days}, tvl=0.0, tx={d: 2 for d in days})
    insert_snap(conn, "0xwhp", 4663, END - timedelta(days=13), 0.0, 0.0, 0, vol_day=0.0)     # last week: nothing
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert d.is_wash and d.wash_reason == "unverified" and d.score_trending == 0.0 and not d.is_spam
    assert out["trending"] == 0


def test_a_spam_shaped_project_that_moved_a_million_with_nothing_to_check_is_unverified(conn, ctx):
    """$5M in a week through a $50 pool, swap counts and wallets unknown: not spam (it moved $1M), not provably wash, so
    it is listed as unverified wash-like volume, never ranked."""
    p = insert_project(conn, "Ghost", first_seen=NOW - timedelta(days=60), slug="ghostly")
    insert_hook(conn, "0xgh", 4663, project_id=p, name="Ghost")
    insert_pool(conn, "0xghp", 4663, "0xgh", created=NOW - timedelta(days=60))
    series(conn, "0xghp", 4663, {END - timedelta(days=i): 714_286.0 for i in range(7)}, tvl=50.0)
    for i in range(7, 14):
        insert_snap(conn, "0xghp", 4663, END - timedelta(days=i), 50.0, 0.0, 0, vol_day=100_000.0)
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert d.is_wash and d.wash_reason == "unverified" and not d.is_spam and d.score_trending == 0.0
    assert out["wash_unverified"] == 1


def test_no_liquidity_never_corroborates_a_custom_accounting_hook(conn, ctx):
    """An RFQ / custom-accounting hook (beforeSwapReturnsDelta, address bit 3) keeps its liquidity in the hook, so a $0
    pool TVL says nothing: four wallets moving $1M a week through it are not wash."""
    rfq = insert_project(conn, "Rfq", first_seen=NOW - timedelta(days=60), slug="rfq")
    hook = "0x" + "f" * 36 + "0088"
    insert_hook(conn, hook, 10, project_id=rfq, name="Rfq")
    insert_pool(conn, "0xrfqp", 10, hook, created=NOW - timedelta(days=60))
    series(conn, "0xrfqp", 10, {END - timedelta(days=i): 143_000.0 for i in range(7)}, tvl=0.0,
           tx={END - timedelta(days=i): 20 for i in range(7)})
    for i in range(7):
        d = END - timedelta(days=i)
        conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id="0xrfqp", day=d, swaps=20, wallets=4, volume_usd=143_000.0))
        for w in range(4):
            conn.execute(sa.insert(db.pool_wallet_day).values(chain_id=10, pool_id="0xrfqp", day=d, address=f"0xw{w}", swaps=5))
    _optimism_ready(conn)
    score.run(conn, ctx)
    assert not daily(conn, rfq).is_wash


def test_a_pool_born_this_morning_makes_its_project_new_not_spam(conn, ctx):
    """Created at 03:00 UTC on the run day with $500K TVL: its only row is the running day's, which carries TVL ("now");
    volume windows still end yesterday."""
    p = insert_project(conn, "Fresh", first_seen=NOW - timedelta(hours=2), slug="fresh")
    insert_hook(conn, "0xfr", 8453, project_id=p, name="Fresh")
    insert_pool(conn, "0xfrp", 8453, "0xfr", created=datetime(2026, 10, 6, 3, tzinfo=timezone.utc))
    insert_snap(conn, "0xfrp", 8453, DAY, 500_000, 20_000.0, None, vol_day=20_000.0)
    score.run(conn, ctx)
    d = daily(conn, p)
    assert d.is_new and not d.is_spam and (d.pools_n, d.tvl_usd, d.vol_7d) == (1, 500_000, 0)


def test_rescoring_a_past_day_does_not_depend_on_indexer_freshness_now(conn, ctx):
    """Package 9 re-scores past days. The indexer's updated_at five days later says nothing about whether that day's
    swaps were complete; swap_block_time past the day's end does."""
    wash = _onchain_project(conn, "Desk", "0xdp", wallets=1, vol_per_day=60_000_000)
    _optimism_ready(conn)
    score.run(conn, ctx)
    first = daily(conn, wash).wash_reason
    ctx.now = NOW + timedelta(days=5)            # updated_at (NOW) is now days stale
    score.run(conn, ctx)
    assert first == "7d,concentrated,vol_tvl" and daily(conn, wash).wash_reason == first


def test_rescoring_a_past_day_waits_for_swaps_the_indexer_still_owes_for_it(conn, ctx):
    _optimism_ready(conn)
    ctx.now = NOW + timedelta(days=5)            # a re-score: the past-day path, not ready_chains
    end = DAY - timedelta(days=1)
    owed_from = datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
    conn.execute(sa.insert(db.pool_lookup_queue).values(chain_id=10, pool_id="0x" + "9" * 64, from_block=1, seen_block=2,
                                                        from_time=owed_from))
    assert 10 not in score._ready(conn, ctx, end)
    conn.execute(sa.delete(db.pool_lookup_queue))
    assert 10 in score._ready(conn, ctx, end)


def test_pruned_wallet_rows_are_unknown_not_zero_wallets(conn, ctx):
    """pool_wallet_day is kept 30 days; volume without wallet rows must not read as "<= 10 wallets"."""
    wash = _onchain_project(conn, "Desk", "0xdp", wallets=0, vol_per_day=60_000_000)
    _optimism_ready(conn)
    out = score.run(conn, ctx)
    assert out["wash_7d"] == 0 and "7d" not in (daily(conn, wash).wash_reason or "").split(",")


def test_one_wallet_moving_a_million_through_its_own_custom_hook_is_concentrated(conn, ctx):
    """The real Optimism 0x69af...7fff: every permission bit set (bit 3 too, so no_liquidity does not judge it), one
    wallet, 8 swaps, $436M, TVL 0. One wallet moving $1M+ through its own pool needs no TVL corroboration."""
    p = insert_project(conn, "Desk7fff", first_seen=NOW - timedelta(days=60), slug="desk7fff")
    hook = "0x69af" + "0" * 32 + "7fff"
    insert_hook(conn, hook, 10, project_id=p, name="Desk7fff")
    insert_pool(conn, "0x69affp", 10, hook, created=NOW - timedelta(days=60))
    days = [END - timedelta(days=i) for i in range(4)]
    series(conn, "0x69affp", 10, {d: 109_000_000.0 for d in days}, tvl=0.0, tx={d: 2 for d in days})
    for d in days:
        conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id="0x69affp", day=d, swaps=2, wallets=1,
                                                             volume_usd=109_000_000.0))
        conn.execute(sa.insert(db.pool_wallet_day).values(chain_id=10, pool_id="0x69affp", day=d, address="0xdesk", swaps=2))
    _optimism_ready(conn)
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert d.is_wash and "concentrated" in d.wash_reason.split(",") and d.score_trending == 0.0 and not d.is_spam
    assert out["wash_concentrated"] == 1


def test_an_rfq_hook_with_several_real_counterparties_is_not_concentrated(conn, ctx):
    rfq = _onchain_project(conn, "Rfq5", "0xrfq5", wallets=5, vol_per_day=500_000, tvl=5_000_000)
    _optimism_ready(conn)
    score.run(conn, ctx)
    assert not daily(conn, rfq).is_wash


def test_a_whale_with_unknown_tvl_and_eight_swaps_is_judged_by_its_swaps(conn, ctx):
    """TVL unknown, 8 exact swaps (on-chain UTC days, a ready chain) carrying $436M, no pool_stats row and no wallet rows.
    No TVL rule can judge it, but 8 swaps prove at most 8 wallets: the 7-day rule applies through the swap bound."""
    p = insert_project(conn, "Blind", first_seen=NOW - timedelta(days=60), slug="blind")
    insert_hook(conn, "0xbl", 10, project_id=p, name="Blind")
    insert_pool(conn, "0xblp", 10, "0xbl", created=NOW - timedelta(days=60))
    days = [END - timedelta(days=i) for i in range(4)]
    series(conn, "0xblp", 10, {d: 109_000_000.0 for d in days}, tvl=None, tx={d: 2 for d in days})
    insert_snap(conn, "0xblp", 10, END - timedelta(days=13), None, 0.0, 0, vol_day=1_000_000.0)    # a prior week
    _optimism_ready(conn)
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert d.is_wash and "7d" in d.wash_reason.split(",") and d.score_trending == 0.0 and not d.is_spam
    assert out["trending"] == 0


def test_two_exact_swaps_carrying_a_million_prove_concentration(conn, ctx):
    p = insert_project(conn, "Pair", first_seen=NOW - timedelta(days=60), slug="pair")
    insert_hook(conn, "0xpa", 10, project_id=p, name="Pair")
    insert_pool(conn, "0xpap", 10, "0xpa", created=NOW - timedelta(days=60))
    series(conn, "0xpap", 10, {END: 3_000_000.0}, tvl=None, tx={END: 2})
    _optimism_ready(conn)
    score.run(conn, ctx)
    assert "concentrated" in daily(conn, p).wash_reason.split(",")


def _week_off_ready(conn, name, per_day):
    """A $1.4M week on a chain the indexer does not cover, TVL $10M, swaps unknown. per_day[i] for day END-i:
    (exact wallets or None, buyers, sellers, top-wallet addresses); its pool_stats row is day END-i+1."""
    p = insert_project(conn, name, first_seen=NOW - timedelta(days=60), slug=name.lower())
    hook, pool = f"0x{name.lower()}", f"0x{name.lower()}p"
    insert_hook(conn, hook, 4663, project_id=p, name=name)
    insert_pool(conn, pool, 4663, hook, created=NOW - timedelta(days=60))
    series(conn, pool, 4663, {END - timedelta(days=i): 200_000.0 for i in range(7)}, tvl=10_000_000.0)
    for i, (exact, b, s, addrs) in enumerate(per_day):
        sd = DAY - timedelta(days=i)
        conn.execute(sa.insert(db.pool_stats).values(pool_id=pool, chain_id=4663, day=sd, vol_24h=200_000.0, traders_24h=b + s,
                                                     buyers_24h=b, sellers_24h=s, wallets_24h=exact))
        for a in addrs:
            conn.execute(sa.insert(db.pool_top_wallets).values(pool_id=pool, chain_id=4663, day=sd, address=a, volume_usd=100_000.0,
                                                               trades=2, buys=1, sells=1))
    return p


def test_concentrated_off_ready_chains_needs_the_same_few_wallets_every_day(conn, ctx):
    rotating = _week_off_ready(conn, "Rotating", [(2, 1, 1, [f"0xr{i}a", f"0xr{i}b"]) for i in range(7)])   # 14 wallets
    one_exact = _week_off_ready(conn, "OneExact", [(2, 1, 1, ["0xa", "0xb"])] + [(None, 2, 2, [])] * 6)
    same = _week_off_ready(conn, "Same", [(2, 1, 1, ["0xa", "0xb"])] * 7)
    score.run(conn, ctx)
    assert "concentrated" not in (daily(conn, rotating).wash_reason or "")
    assert "concentrated" not in (daily(conn, one_exact).wash_reason or "")
    assert "concentrated" in (daily(conn, same).wash_reason or "")


def test_a_quiet_days_zero_wallets_is_unknown_for_a_pool_with_volume(conn, ctx):
    """A launch pool did $2M early in the week; its only pool_stats row is a quiet day (0 buyers, 0 sellers). Zero is
    not a wallet count for $2M: neither vol_tvl nor no_liquidity may fire on that basis alone."""
    p = insert_project(conn, "Quiet", first_seen=NOW - timedelta(days=60), slug="quiet")
    insert_hook(conn, "0xqu", 8453, project_id=p, name="Quiet")
    insert_pool(conn, "0xqup", 8453, "0xqu", created=NOW - timedelta(days=60))
    series(conn, "0xqup", 8453, {END - timedelta(days=5): 2_000_000.0, END: 0.0}, tvl=500.0)
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xqup", chain_id=8453, day=DAY, vol_24h=0.0, traders_24h=0,
                                                 buyers_24h=0, sellers_24h=0))
    score.run(conn, ctx)
    reasons = (daily(conn, p).wash_reason or "").split(",")
    assert "vol_tvl" not in reasons and "no_liquidity" not in reasons


def test_a_burst_launch_is_not_judged_on_the_next_nights_rolling_swap_count(conn, ctx):
    """Born 01:00 UTC yesterday, $1.2M before 06:00 through hundreds of wallets (yesterday's run: 400 buyers / 350
    sellers), quiet since. Tonight's rolling count (4 swaps, filed under yesterday) is not that day's count, and a known
    lower bound of 400 wallets already rules out "at most 10" or "at most 2"."""
    p = insert_project(conn, "Burst", first_seen=NOW - timedelta(days=1), slug="burst")
    insert_hook(conn, "0xbu", 8453, project_id=p, name="Burst")
    insert_pool(conn, "0xbup", 8453, "0xbu", created=datetime(2026, 10, 5, 1, tzinfo=timezone.utc))
    insert_snap(conn, "0xbup", 8453, END, 300_000, 1_200_000.0, 4, vol_day=1_200_000.0, tx_day=4)
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xbup", chain_id=8453, day=END, vol_24h=1_200_000.0, traders_24h=750,
                                                 buyers_24h=400, sellers_24h=350))
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xbup", chain_id=8453, day=DAY, vol_24h=900.0, traders_24h=4,
                                                 buyers_24h=2, sellers_24h=2))
    score.run(conn, ctx)
    assert not daily(conn, p).is_wash


def test_a_known_wallet_bound_overrides_the_swap_bound_on_ready_chains_too(conn, ctx):
    p = insert_project(conn, "Many", first_seen=NOW - timedelta(days=60), slug="many")
    insert_hook(conn, "0xma", 10, project_id=p, name="Many")
    insert_pool(conn, "0xmap", 10, "0xma", created=NOW - timedelta(days=60))
    series(conn, "0xmap", 10, {END: 3_000_000.0}, tvl=None, tx={END: 2})
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xmap", chain_id=10, day=DAY, vol_24h=3_000_000.0, traders_24h=60,
                                                 buyers_24h=30, sellers_24h=30))
    _optimism_ready(conn)
    score.run(conn, ctx)
    assert not daily(conn, p).is_wash


def test_pools_n_counts_every_pool_of_a_launchpad_past_the_indexer_cap(conn, ctx):
    pad = insert_project(conn, "Pad", first_seen=NOW - timedelta(days=30), slug="pad")
    insert_hook(conn, "0xpad", 8453, project_id=pad, name="Pad")
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == "0xpad").values(pools_total=104_000))
    other = insert_project(conn, "Other", first_seen=NOW - timedelta(days=30), slug="other")
    insert_hook(conn, "0xother", 8453, project_id=other, name="Other")
    for p, h in (("0xpp1", "0xpad"), ("0xop1", "0xother"), ("0xop2", "0xother")):
        insert_pool(conn, p, 8453, h, created=NOW - timedelta(days=30))
        series(conn, p, 8453, {END: 1000.0}, tvl=100.0, tx={END: 5})
    score.run(conn, ctx)
    assert (daily(conn, pad).pools_n, daily(conn, other).pools_n) == (104_000, 2)


def test_a_hooks_aggregate_pool_adds_volume_but_is_not_a_pool(conn, ctx):
    pad = insert_project(conn, "Pad", first_seen=NOW - timedelta(days=30), slug="pad")
    insert_hook(conn, "0xpad", 8453, project_id=pad, name="Pad")
    for p in ("0xpp1", db.hook_pool_id("0xpad")):
        insert_pool(conn, p, 8453, "0xpad", created=NOW - timedelta(days=30) if p == "0xpp1" else None)
    series(conn, "0xpp1", 8453, {END: 1000.0}, tvl=100.0, tx={END: 5})
    series(conn, db.hook_pool_id("0xpad"), 8453, {END: 500.0}, tx={END: 40})
    score.run(conn, ctx)
    d = daily(conn, pad)
    assert (d.vol_24h, d.swaps_24h, d.pools_n) == (1500.0, 45, 1)


def test_a_launchpad_whose_only_usage_is_an_unpriced_aggregate_has_unknown_volume_not_zero(conn, ctx):
    pad = insert_project(conn, "Pad", first_seen=NOW - timedelta(days=30), slug="pad")
    insert_hook(conn, "0xpad", 8453, project_id=pad, name="Pad")
    insert_pool(conn, "0xidle", 8453, "0xpad", created=NOW - timedelta(days=30))       # stored, never snapshotted
    agg = db.hook_pool_id("0xpad")
    insert_pool(conn, agg, 8453, "0xpad", created=None)
    insert_snap(conn, agg, 8453, END, None, None, None, vol_day=None, tx_day=3)        # folded swaps, all unpriced
    score.run(conn, ctx)
    d = daily(conn, pad)
    assert (d.vol_24h, d.vol_7d, d.pools_n) == (None, None, 0)


def test_folded_usage_shows_in_the_totals_but_never_ranks_trending(conn, ctx):
    pad = insert_project(conn, "Pad", first_seen=NOW - timedelta(days=30), slug="pad")
    insert_hook(conn, "0xpad", 8453, project_id=pad, name="Pad")
    insert_pool(conn, "0xpp1", 8453, "0xpad", created=NOW - timedelta(days=60))
    agg = db.hook_pool_id("0xpad")
    insert_pool(conn, agg, 8453, "0xpad", created=None)
    days = [END - timedelta(days=i) for i in range(14)]
    series(conn, "0xpp1", 8453, {d: 1_000.0 for d in days}, tvl=50_000.0, tx={d: 10 for d in days})
    series(conn, agg, 8453, {d: (1_000_000.0 if d > END - timedelta(days=7) else 0.0) for d in days}, tx={d: 10 for d in days})
    score.run(conn, ctx)
    d = daily(conn, pad)
    assert d.vol_7d == 7_000.0 + 7_000_000.0                                            # displayed totals include it
    assert d.score_trending == pytest.approx(score.score_trending(7_000.0, 7_000.0))     # ranking does not


def test_wash_shares_use_unfolded_volume_so_folded_usage_cannot_dilute_a_wash_pool(conn, ctx):
    """A capped project: its sampled wash pool carries 60% of the unfolded volume; the hook's aggregate pool (folded swaps,
    which no rule can judge) equals the unfolded total. Counted in the denominator it would cut the share to 30%."""
    pad = _onchain_project(conn, "Pad", "0xwash", wallets=1, vol_per_day=60_000_000)            # $420M, one wallet
    insert_pool(conn, "0xclean", 10, "0xPad", created=NOW - timedelta(days=60))
    series(conn, "0xclean", 10, {END - timedelta(days=i): 40_000_000.0 for i in range(14)}, tvl=50_000_000,
           tx={END - timedelta(days=i): 500 for i in range(14)})
    for i in range(7):
        d = END - timedelta(days=i)
        conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id="0xclean", day=d, swaps=500, wallets=60,
                                                             volume_usd=40_000_000))
        for w in range(60):
            conn.execute(sa.insert(db.pool_wallet_day).values(chain_id=10, pool_id="0xclean", day=d, address=f"0xc{w}", swaps=1))
    agg = db.hook_pool_id("0xPad")
    insert_pool(conn, agg, 10, "0xPad", created=None)
    series(conn, agg, 10, {END - timedelta(days=i): 100_000_000.0 for i in range(14)}, tvl=None,
           tx={END - timedelta(days=i): 5_000 for i in range(14)})
    _optimism_ready(conn)
    score.run(conn, ctx)
    d = daily(conn, pad)
    assert d.is_wash and "7d" in d.wash_reason.split(",") and d.score_trending == 0.0


def test_wallets_7d_and_top_wallet_share_from_exact_swap_counts(conn, ctx):
    """The small-hook signal: distinct wallets over the week and the busiest wallet's share of swaps, with no threshold."""
    even = _onchain_project(conn, "Even", "0xep", wallets=3, vol_per_day=1_000)             # 3 wallets, 1 swap each a day
    lopsided = _onchain_project(conn, "Lop", "0xlp", wallets=2, vol_per_day=1_000)
    conn.execute(sa.update(db.pool_wallet_day).where(db.pool_wallet_day.c.pool_id == "0xlp", db.pool_wallet_day.c.address == "0xw0")
                 .values(swaps=8))                                                           # one wallet does 8 of 9 swaps a day
    _optimism_ready(conn)
    score.run(conn, ctx)
    e, l = daily(conn, even), daily(conn, lopsided)
    assert (e.wallets_7d, e.top_wallet_share_7d) == (3, 0.3333)
    assert (l.wallets_7d, l.top_wallet_share_7d) == (2, round(8 / 9, 4))
    assert not l.is_wash                     # $7K a week is far under every wash threshold; the figure still shows


def test_wallets_7d_unknown_unless_every_chain_is_ready_and_covers_the_window(conn, ctx):
    pid = _onchain_project(conn, "Mixed", "0xmp", wallets=3, vol_per_day=1_000)
    score.run(conn, ctx)                     # chain 10 not ready
    d = daily(conn, pid)
    assert d.wallets_7d is None and d.top_wallet_share_7d is None
    _optimism_ready(conn)
    insert_hook(conn, "0xMixedBase", 8453, project_id=pid, name="Mixed")   # a second chain with no indexer state
    score.run(conn, ctx)
    d = daily(conn, pid)
    assert d.wallets_7d is None and d.top_wallet_share_7d is None
    conn.execute(sa.delete(db.hooks).where(db.hooks.c.chain_id == 8453))
    conn.execute(sa.delete(db.pool_daily_onchain).where(db.pool_daily_onchain.c.day == END - timedelta(days=6)))  # history starts inside the window
    score.run(conn, ctx)
    assert daily(conn, pid).wallets_7d is None


def test_wallets_7d_is_zero_for_a_covered_silent_project(conn, ctx):
    pid = _onchain_project(conn, "Quiet", "0xqp", wallets=0, vol_per_day=0)
    _optimism_ready(conn)
    score.run(conn, ctx)
    d = daily(conn, pid)
    assert d.wallets_7d == 0 and d.top_wallet_share_7d is None


def _chain_state(conn, chain_id, backfilled=True, swap_time=NOW):
    conn.execute(sa.insert(db.indexer_state).values(chain_id=chain_id, initialize_block=1, swap_block=1, head_block=1,
                                                    backfilled=backfilled, updated_at=NOW, swap_block_time=swap_time))


def _indexed_week(conn, chain_id, hook, pool, swaps_by_wallet: dict):
    """A pool of `hook` whose seven closed days the indexer counted: each wallet makes its swaps every day."""
    insert_pool(conn, pool, chain_id, hook, created=NOW - timedelta(days=60))
    for i in range(7):
        d = END - timedelta(days=i)
        conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=chain_id, pool_id=pool, day=d, swaps=sum(swaps_by_wallet.values()),
                                                             wallets=len(swaps_by_wallet), volume_usd=1_000.0))
        for address, swaps in swaps_by_wallet.items():
            conn.execute(sa.insert(db.pool_wallet_day).values(chain_id=chain_id, pool_id=pool, day=d, address=address, swaps=swaps))


def test_wallets_7d_ignores_a_registry_hook_on_a_chain_bait_does_not_index(conn, ctx):
    """2026-10-24: StablePair (Ethereum + Arbitrum, both ready, 19K swaps a week) read "Wallets 7d -" because the registry
    also lists its hook on X Layer (196), a chain the indexer does not cover and so can never be ready. The site shows no
    pool, swap or volume there, so the figure covers the chains it does show."""
    pid = insert_project(conn, "Stable", first_seen=NOW - timedelta(days=60))
    insert_hook(conn, "0xstable1", 1, project_id=pid, name="Stable")
    insert_hook(conn, "0xstable42161", 42161, project_id=pid, name="Stable")
    insert_hook(conn, "0xstable196", 196, project_id=pid, name="Stable")             # registry entry only
    _indexed_week(conn, 1, "0xstable1", "0xsp1", {"0xa": 3, "0xb": 1})
    _indexed_week(conn, 42161, "0xstable42161", "0xsp2", {"0xa": 1, "0xc": 1})          # 0xa on both chains: one wallet
    _chain_state(conn, 1)
    _chain_state(conn, 42161)
    score.run(conn, ctx)
    d = daily(conn, pid)
    assert (d.wallets_7d, d.top_wallet_share_7d) == (3, round(28 / 42, 4))
    # a pool Bait stored on such a chain would have swaps nothing counts: then the figure is unknown again
    insert_pool(conn, "0xsp3", 196, "0xstable196", created=NOW - timedelta(days=60))
    score.run(conn, ctx)
    assert daily(conn, pid).wallets_7d is None


def test_wallets_7d_counts_a_hook_under_the_cap_on_a_chain_still_settling_its_lookup_queue(conn, ctx):
    """Base is not "ready" while launchpad pools past the per-hook cap owe swaps to pool_lookup_queue. Every pool of a hook
    under the cap is a v4_pools row from its Initialize on, so none of its swaps can be owed: once the swap cursor is past
    the window, the indexer has its whole week. 2026-10-24: Angstrom (Ethereum + Base) and eleven others read "-"."""
    pid = insert_project(conn, "Ang", first_seen=NOW - timedelta(days=60))
    insert_hook(conn, "0xang1", 1, project_id=pid, name="Ang")
    insert_hook(conn, "0xang8453", 8453, project_id=pid, name="Ang")
    _indexed_week(conn, 1, "0xang1", "0xap1", {"0xa": 2, "0xb": 1})
    _indexed_week(conn, 8453, "0xang8453", "0xap2", {"0xc": 1})
    _chain_state(conn, 1)
    _chain_state(conn, 8453, backfilled=False)
    conn.execute(sa.insert(db.pool_lookup_queue).values(chain_id=8453, pool_id="0x" + "9" * 64, from_block=1, seen_block=2,
                                                        from_time=NOW - timedelta(days=3)))
    assert onchain.ready_chains(conn, ctx) == [1]
    score.run(conn, ctx)
    d = daily(conn, pid)
    assert (d.wallets_7d, d.top_wallet_share_7d) == (3, 0.5)
    # past the cap, some of the hook's pools are not rows and their swaps may still be owed to the queue: unknown
    conn.execute(sa.insert(db.hook_pool_counts).values(chain_id=8453, hook_address="0xang8453", pools_total=2_500))
    score.run(conn, ctx)
    assert daily(conn, pid).wallets_7d is None
    # under the cap again, but the swap cursor stopped short of the window's end: unknown
    conn.execute(sa.delete(db.hook_pool_counts))
    conn.execute(sa.update(db.indexer_state).where(db.indexer_state.c.chain_id == 8453).values(swap_block_time=NOW - timedelta(hours=7)))
    score.run(conn, ctx)
    assert daily(conn, pid).wallets_7d is None


def test_a_week_counted_on_a_settling_chain_never_reads_an_unread_last_day_as_zero_wallets(conn, ctx):
    """The 24h figure reads the indexer only on ready chains, so a week counted on Base proves no 24h zero; a covered week
    with no wallet at all still does."""
    busy = insert_project(conn, "Fam", first_seen=NOW - timedelta(days=60))
    insert_hook(conn, "0xfam", 8453, project_id=busy, name="Fam")
    _indexed_week(conn, 8453, "0xfam", "0xfp", {"0xa": 1, "0xb": 1})
    quiet = insert_project(conn, "Still", first_seen=NOW - timedelta(days=60), slug="still")
    insert_hook(conn, "0xstill", 8453, project_id=quiet, name="Still")
    _indexed_week(conn, 8453, "0xstill", "0xstp", {})
    _chain_state(conn, 8453, backfilled=False)
    score.run(conn, ctx)
    b, q = daily(conn, busy), daily(conn, quiet)
    assert (b.wallets_7d, b.wallets_24h, b.wallets_exact) == (2, None, None)
    assert (q.wallets_7d, q.wallets_24h, q.wallets_exact) == (0, 0, True)


def test_deployer_share_of_swaps_from_known_deployer_wallets(conn, ctx):
    """The grant-review question: how much of a hook's activity is its own team. Factories never count as a deployer."""
    lop = _onchain_project(conn, "Lop", "0xlp", wallets=2, vol_per_day=1_000)
    conn.execute(sa.update(db.pool_wallet_day).where(db.pool_wallet_day.c.pool_id == "0xlp", db.pool_wallet_day.c.address == "0xw0").values(swaps=8))
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == "0xLop").values(deployer="0xw0", deployer_source="explorer"))
    quiet = _onchain_project(conn, "Team", "0xtp", wallets=3, vol_per_day=1_000)             # deployer known, never traded
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == "0xTeam").values(deployer="0xteamwallet"))
    factory = _onchain_project(conn, "Fac", "0xfp", wallets=3, vol_per_day=1_000)
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == "0xFac")                        # registry-supplied proxy
                 .values(deployer="0x4e59b44847b379578588920ca78fbf26c0b4956c", deployer_source="hooklist"))
    unknown = _onchain_project(conn, "Anon", "0xap", wallets=3, vol_per_day=1_000)
    _optimism_ready(conn)
    score.run(conn, ctx)
    assert daily(conn, lop).deployer_swap_share_7d == round(8 / 9, 4)
    assert daily(conn, quiet).deployer_swap_share_7d == 0.0
    assert daily(conn, factory).deployer_swap_share_7d is None       # a CREATE2 factory is nobody's wallet: unknown, not 0
    assert daily(conn, unknown).deployer_swap_share_7d is None


def test_project_tvl_is_unknown_not_zero_when_no_pool_tvl_is_known(conn, ctx):
    """10/16 UX pass: 377 trading projects read "TVL $0" because unknown pool TVLs summed to zero."""
    p = insert_project(conn, "Unpriced", first_seen=NOW - timedelta(days=30))
    insert_hook(conn, "0xunpriced", 8453, project_id=p, name="Unpriced")
    insert_pool(conn, "0xup", 8453, "0xunpriced", created=NOW - timedelta(days=30))
    for i in range(7):
        insert_snap(conn, "0xup", 8453, END - timedelta(days=i), None, 1000.0 * (i + 1), 10 * (i + 1), vol_day=1000.0, tx_day=10)
    q = insert_project(conn, "Priced", first_seen=NOW - timedelta(days=30))
    insert_hook(conn, "0xpriced", 8453, project_id=q, name="Priced")
    insert_pool(conn, "0xpp", 8453, "0xpriced", created=NOW - timedelta(days=30))
    insert_pool(conn, "0xpq", 8453, "0xpriced", created=NOW - timedelta(days=30))
    for i in range(7):
        insert_snap(conn, "0xpp", 8453, END - timedelta(days=i), 0.0, 1000.0 * (i + 1), 10 * (i + 1), vol_day=1000.0, tx_day=10)
        insert_snap(conn, "0xpq", 8453, END - timedelta(days=i), None, 1000.0 * (i + 1), 10 * (i + 1), vol_day=1000.0, tx_day=10)
    score.run(conn, ctx)
    assert daily(conn, p).tvl_usd is None
    assert daily(conn, q).tvl_usd == 0.0                     # one known pool at $0 is a measured zero


def test_covered_quiet_day_reads_zero_wallets_not_unknown(conn, ctx):
    pid = _onchain_project(conn, "Quiet2", "0xq2", wallets=0, vol_per_day=0)
    _optimism_ready(conn)
    score.run(conn, ctx)
    d = daily(conn, pid)
    assert (d.wallets_24h, d.wallets_exact, d.wallets_7d) == (0, True, 0)


USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
SPOOF_USDC_BASE = "0x8335cb97db9ccdd95325c2da5622eebdd6ca2913"   # live 2026-10-15: same first and last four hex digits


def _set_tokens(conn, pool_id, chain_id, t0, t1):
    conn.execute(db.pools.update().where(db.pools.c.pool_id == pool_id, db.pools.c.chain_id == chain_id)
                 .values(token0=t0, token1=t1))


def _snap_tvl(conn, pool_id, day):
    return conn.execute(sa.select(db.pool_snapshots.c.tvl_usd).where(db.pool_snapshots.c.pool_id == pool_id,
                                                                     db.pool_snapshots.c.day == day)).scalar_one()


def test_a_spoof_usdc_pool_is_wash_like_and_its_tvl_unknown(conn, ctx):
    """2026-10-16 audit: "USDC/USDC hook" on Base, real USDC against a vanity copy, read $1.307B TVL and $221M volume (one
    wallet's round trips) and topped Leaders. No wallet data on Base, so only the token itself can flag it."""
    p = insert_project(conn, "USDC/USDC hook", first_seen=NOW - timedelta(days=1), slug="spoof")
    insert_hook(conn, "0xspoofhook", 8453, project_id=p)
    insert_pool(conn, "0xspoof", 8453, "0xspoofhook", created=NOW - timedelta(days=1))
    _set_tokens(conn, "0xspoof", 8453, {"id": USDC_BASE, "symbol": "USDC"}, {"id": SPOOF_USDC_BASE, "symbol": "USDC"})
    insert_snap(conn, "0xspoof", 8453, END, 1_307_100_542.97, 221_123_342.0, 498, vol_day=221_123_342.0, tx_day=498)
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert d.is_wash and d.wash_reason == "lookalike" and not d.is_new and d.score_trending == 0.0
    assert d.tvl_usd is None and _snap_tvl(conn, "0xspoof", END) is None    # the page's pool row reads unknown too
    assert d.vol_7d == pytest.approx(221_123_342.0)                         # volume stays shown, behind the badge
    assert (out["wash_lookalike"], out["tvl_untrusted"]) == (1, 1)


def test_a_launch_supply_priced_by_its_own_pool_is_not_tvl(conn, ctx):
    """Clanker v2 KATIE/WETH on Base: $155.9M TVL on $0.12 of volume in 7 days. Its TVL is unknown; the project's real
    pool still counts, and the project is not wash-like for it."""
    p = insert_project(conn, "Launchpad", first_seen=NOW - timedelta(days=60))
    insert_hook(conn, "0xlaunch", 8453, project_id=p)
    insert_pool(conn, "0xidle", 8453, "0xlaunch", created=NOW - timedelta(days=30))
    insert_pool(conn, "0xlive", 8453, "0xlaunch", created=NOW - timedelta(days=30))
    series(conn, "0xidle", 8453, {END - timedelta(days=i): (0.12 if i == 3 else 0.0) for i in range(7)}, tvl=155_863_042.0)
    series(conn, "0xlive", 8453, {END - timedelta(days=i): 60_000.0 for i in range(7)}, tvl=327_211.0)
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert d.tvl_usd == pytest.approx(327_211.0) and not d.is_wash
    assert _snap_tvl(conn, "0xidle", END) is None and _snap_tvl(conn, "0xlive", END) == pytest.approx(327_211.0)
    assert out["tvl_untrusted"] == 1


def test_a_deep_pool_younger_than_a_week_or_partly_measured_keeps_its_tvl(conn, ctx):
    p = insert_project(conn, "Fresh", first_seen=NOW - timedelta(days=2))
    insert_hook(conn, "0xfresh", 1, project_id=p)
    insert_pool(conn, "0xyoung", 1, "0xfresh", created=NOW - timedelta(days=2))
    series(conn, "0xyoung", 1, {END - timedelta(days=i): 0.0 for i in range(2)}, tvl=15_000_000.0)
    q = insert_project(conn, "Gappy", first_seen=NOW - timedelta(days=60))
    insert_hook(conn, "0xgappy", 1, project_id=q)
    insert_pool(conn, "0xgap", 1, "0xgappy", created=NOW - timedelta(days=60))
    series(conn, "0xgap", 1, {END - timedelta(days=i): 0.0 for i in (0, 3, 5)}, tvl=5_000_000.0)   # 3 of 7 days known
    score.run(conn, ctx)
    assert daily(conn, p).tvl_usd == pytest.approx(15_000_000.0)
    assert daily(conn, q).tvl_usd == pytest.approx(5_000_000.0)


def test_targeted_score_blanks_only_its_own_projects_pools(conn, ctx):
    p = insert_project(conn, "A", first_seen=NOW - timedelta(days=60), slug="a")
    insert_hook(conn, "0xa", 8453, project_id=p)
    insert_pool(conn, "0xpa", 8453, "0xa", created=NOW - timedelta(days=30))
    series(conn, "0xpa", 8453, {END - timedelta(days=i): 0.0 for i in range(7)}, tvl=2_000_000.0)
    q = insert_project(conn, "B", first_seen=NOW - timedelta(days=60), slug="b")
    insert_hook(conn, "0xb", 8453, project_id=q)
    insert_pool(conn, "0xpb", 8453, "0xb", created=NOW - timedelta(days=30))
    series(conn, "0xpb", 8453, {END - timedelta(days=i): 0.0 for i in range(7)}, tvl=2_000_000.0)
    score.run(conn, ctx)                                   # creates both rows (and blanks both)
    for pool in ("0xpa", "0xpb"):
        conn.execute(db.pool_snapshots.update().where(db.pool_snapshots.c.pool_id == pool).values(tvl_usd=2_000_000.0))
    score.run(conn, ctx, project_ids={p})
    assert _snap_tvl(conn, "0xpa", END) is None and _snap_tvl(conn, "0xpb", END) == pytest.approx(2_000_000.0)


def test_a_week_old_tvl_reading_is_unknown_not_current(conn, ctx):
    """Clanker KATIE/WETH: one keyless row on 10/09 ($155.9M) still stood as "now" on 10/16 because score looked back 14
    days for the latest row. A pool not seen on the last two nights has unknown TVL."""
    p = insert_project(conn, "Stale", first_seen=NOW - timedelta(days=60))
    insert_hook(conn, "0xstale", 8453, project_id=p)
    insert_pool(conn, "0xold", 8453, "0xstale", created=NOW - timedelta(days=8))
    insert_snap(conn, "0xold", 8453, END - timedelta(days=6), 155_863_042.0, 0.12, 1, vol_day=0.12, tx_day=1)
    insert_pool(conn, "0xnow", 8453, "0xstale", created=NOW - timedelta(days=30))
    insert_snap(conn, "0xnow", 8453, END - timedelta(days=1), 50_000.0, 1_000.0, 10, vol_day=1_000.0, tx_day=10)
    score.run(conn, ctx)
    assert daily(conn, p).tvl_usd == pytest.approx(50_000.0)
