"""Package 9: python -m bait repair. GeckoTerminal and Claude are fakes; nothing here touches the network or spends."""
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from bait import db, repair
from bait.clients.claude import prose_estimate, what_line_prompt, why_blurb_prompt
from bait.costs import Budgets
from bait.steps import RunContext, score
from tests.conftest import DAY, NOW

BASE, MONAD = 8453, 143
D = timedelta(days=1)


def _t(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


class FakeGT:
    """keyless.Http stand-in: candles per pool id; None = the call failed."""

    def __init__(self, candles: dict):
        self.candles, self.paths = candles, []

    def gt(self, path):
        self.paths.append(path)
        pid = path.split("/pools/")[1].split("/")[0]
        c = self.candles.get(pid)
        return None if c is None else {"data": {"attributes": {"ohlcv_list": [[_t(d), 1, 1, 1, 1, v] for d, v in c.items()]}}}


def _project(c, slug, hook, chain=BASE, first_seen=NOW - timedelta(days=60), **cols):
    pid = c.execute(sa.insert(db.projects).values(slug=slug, name=slug.title(), norm_name=slug, first_seen_at=first_seen,
                                                  **cols)).inserted_primary_key[0]
    c.execute(sa.insert(db.hooks).values(address=hook, chain_id=chain, project_id=pid, name=slug, first_seen_at=NOW,
                                         flags={}, properties={}))
    return pid


def _pool(c, pool_id, hook, chain=BASE, created=NOW - timedelta(days=60)):
    c.execute(sa.insert(db.pools).values(pool_id=pool_id, chain_id=chain, hook_address=hook, created_at=created))


def _snap(c, pool_id, day, chain=BASE, tvl=5_000.0, cum=None, vol_day=None, est=None, tx_day=None):
    c.execute(sa.insert(db.pool_snapshots).values(pool_id=pool_id, chain_id=chain, day=day, tvl_usd=tvl, volume_usd_cum=cum,
                                                  tx_count_cum=None, volume_usd_day=vol_day, tx_count_day=tx_day, estimated=est))


def _rows(e, pool_id):
    with e.connect() as c:
        return {r.day: r for r in c.execute(sa.select(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == pool_id))}


def _ctx(http=None, day=DAY, now=NOW):
    ctx = RunContext(day=day, now=now, clients={k: None for k in ("graph", "blockscout", "claude", "exa", "parallel", "github", "names")})
    if http is not None:
        ctx.extras["keyless_http"] = http
    return ctx


# --- backfill-days ------------------------------------------------------------------------------
def test_backfill_writes_candles_to_unfilled_closed_days_and_never_the_running_day(engine):
    with engine.begin() as c:
        p = _project(c, "fill", "0xh1")
        _pool(c, "0xp1", "0xh1")
        _snap(c, "0xp1", DAY - 5 * D, vol_day=7.0, est=False, cum=100.0)   # a candle row already: stays
        _snap(c, "0xp1", DAY - 4 * D, cum=0.0)                             # a zero seed: nothing traded before, stays
        _snap(c, "0xp1", DAY - 3 * D, cum=500.0)                           # written before 0012
        _snap(c, "0xp1", DAY - 2 * D, cum=600.0)                           # written before 0012, no candle: $0
        _snap(c, "0xp1", DAY - 1 * D, vol_day=9_999.0, est=True, cum=700.0)   # GeckoTerminal's rolling 24h, an estimate
        _snap(c, "0xp1", DAY, vol_day=50.0, est=True, cum=750.0)           # the running day
    http = FakeGT({"0xp1": {DAY - 6 * D: 50.0, DAY - 5 * D: 1_000.0, DAY - 3 * D: 300.0, DAY - 1 * D: 200.0, DAY: 40.0}})
    out = repair.backfill(engine, _ctx(http), since=2)
    assert http.paths == ["/networks/base/pools/0xp1/ohlcv/day?aggregate=1&limit=30&currency=usd"]
    assert (out["done"], out["rows_updated"], out["rows_inserted"], out["calls"]) == (1, 3, 1, 1)
    r = _rows(engine, "0xp1")
    assert [(r[DAY - i * D].volume_usd_day, r[DAY - i * D].estimated) for i in (3, 2, 1)] == [(300.0, False), (0.0, False), (200.0, False)]
    assert (r[DAY].volume_usd_day, r[DAY].estimated) == (50.0, True)             # the running day is never written
    assert (r[DAY - 5 * D].volume_usd_day, r[DAY - 4 * D].volume_usd_day) == (7.0, None)
    ins = r[DAY - 6 * D]                                                         # a traded day with no row: inserted
    assert (ins.volume_usd_day, ins.estimated, ins.tvl_usd, ins.volume_usd_cum, ins.tx_count_day) == (50.0, False, 5_000.0, 0.0, None)
    again = repair.backfill(engine, _ctx(FakeGT({})), since=2)                   # resumable: nothing left to pull
    assert (again["pools_needing"], again["calls"]) == (0, 0)
    with engine.begin() as c:
        score.run(c, _ctx())
        d = c.execute(sa.select(db.project_daily).where(db.project_daily.c.project_id == p)).one()
    assert (d.vol_24h, d.vol_7d) == (200.0, 50.0 + 7.0 + 300.0 + 200.0)


def test_backfill_targets_busiest_first_skip_unscored_pools_and_unlisted_chains(engine):
    with engine.begin() as c:
        _project(c, "busy", "0xhb")
        _project(c, "quiet", "0xhq")
        _project(c, "monad", "0xhm", chain=MONAD)
        for pid, hook, chain, vol in (("0xbusy", "0xhb", BASE, 9_000.0), ("0xquiet", "0xhq", BASE, 10.0),
                                      ("0xmon", "0xhm", MONAD, 1e6), ("0xorphan", "0xnone", BASE, 1e9)):
            _pool(c, pid, hook, chain=chain)
            _snap(c, pid, DAY - 3 * D, chain=chain, cum=100.0)
            c.execute(sa.insert(db.pool_stats).values(pool_id=pid, chain_id=chain, day=DAY, vol_24h=vol, traders_24h=5))
        targets, info = repair.backfill_targets(c, DAY, since=2)
    assert [t[0] for t in targets] == ["0xbusy", "0xquiet"] and info["no_network"] == {"143": 1}
    http = FakeGT({"0xbusy": {DAY - 3 * D: 5.0}})
    out = repair.backfill(engine, _ctx(http), since=2, max_pools=1)
    assert (out["done"], out["beyond_cap"], out["no_network"]) == (1, 1, 1) and len(http.paths) == 1
    out = repair.backfill(engine, _ctx(FakeGT({"0xquiet": {}})), since=2)      # GeckoTerminal has no candles: rows kept
    assert (out["no_candles"], out["done"]) == (1, 0) and _rows(engine, "0xquiet")[DAY - 3 * D].volume_usd_day is None
    out = repair.backfill(engine, _ctx(FakeGT({})), since=2)                    # the call failed: retried by a rerun
    assert out["failed"] == 1
    out = repair.backfill(engine, _ctx(FakeGT({})), since=2, deadline=NOW, clock=lambda: NOW)
    assert (out["left_at_deadline"], out["calls"]) == (1, 0)


def test_backfill_plan_counts_calls_and_minutes_and_warns_before_a_package_8_nightly(engine):
    with engine.begin() as c:
        for i in range(3):
            _project(c, f"p{i}", f"0xh{i}")
            _pool(c, f"0xp{i}", f"0xh{i}")
            _snap(c, f"0xp{i}", DAY - 3 * D, cum=100.0)
        lines, plan = repair.backfill_plan(c, DAY, since=14, max_pools=2, spacing=10)
    text = "\n".join(lines)
    assert plan == {"calls": 2, "minutes": pytest.approx(20 / 60)}
    assert f"closed days {DAY - 28 * D}..{DAY - D}" in text and "pools needing candles: 3" in text and "1 left for a rerun" in text
    assert "WARNING: no per-day snapshot rows" in text
    with engine.begin() as c:
        _snap(c, "0xp0", DAY, vol_day=1.0, est=True, cum=101.0)
        assert "WARNING" not in "\n".join(repair.backfill_plan(c, DAY, 14, None, 10)[0])
