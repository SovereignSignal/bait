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


# --- rescore ------------------------------------------------------------------------------------
def _old_row(c, pid, day, **cols):
    vals = dict(project_id=pid, day=day, tvl_usd=1.0, vol_7d=0.0, vol_7d_prev=0.0, swaps_7d=0, pools_n=1, score_trending=0.0,
                is_new=False, is_spam=True)
    vals.update(cols)
    c.execute(sa.insert(db.project_daily).values(**vals))


def test_rescore_backs_up_once_reports_the_top_10_change_and_restore_puts_the_old_rows_back(engine):
    end = DAY - D
    with engine.begin() as c:
        p = _project(c, "riser", "0xhr")
        _pool(c, "0xpr", "0xhr")
        for i in range(14):
            _snap(c, "0xpr", end - i * D, tvl=50_000.0, cum=1.0, vol_day=20_000.0 if i < 7 else 2_000.0, est=False)
        _snap(c, "0xpr", DAY, tvl=50_000.0, cum=1.0, vol_day=5.0, est=True)
        _old_row(c, p, DAY)                                   # scored by the old code: spam, no window_end
        assert repair.rescore_days(c, DAY, since=14) == [DAY]
    lines = []
    per = repair.rescore(engine, [DAY], NOW, echo=lines.append)
    assert per[DAY.isoformat()]["top10_in"] == ["riser"] and per[DAY.isoformat()]["backed_up"] == 1
    assert lines[0].startswith(f"{DAY}  projects 1  new 0  trending 1  wash 0") and "in riser" in lines[1]
    with engine.connect() as c:
        row = c.execute(sa.select(db.project_daily)).one()
    assert row.window_end == end and row.vol_7d == 140_000.0 and not row.is_spam and row.score_trending > 0
    assert repair.rescore(engine, [DAY], NOW, echo=lines.append)[DAY.isoformat()]["backed_up"] == 0   # first copy kept
    with engine.begin() as c:
        assert repair.restore_day(c, DAY) == 1
        assert repair.restore_day(c, DAY - D) is None
        row = c.execute(sa.select(db.project_daily)).one()
    assert row.is_spam and row.window_end is None and row.score_trending == 0.0


# --- prose --------------------------------------------------------------------------------------
class FakeClaude:
    """Goes through the meter like the real client: check the worst case, then record what was spent."""

    def __init__(self, meter, usd=0.004):
        self.meter, self.usd, self.what, self.why = meter, usd, [], []

    def what_line(self, ctx):
        self.meter.check("anthropic", prose_estimate(what_line_prompt(ctx)))
        self.meter.record("anthropic", self.usd)
        self.what.append(ctx)
        return {"what_line": f"{ctx['name']} routes swaps through a hook.", "category": "orders"}

    def why_blurb(self, ctx):
        self.meter.check("anthropic", prose_estimate(why_blurb_prompt(ctx)))
        self.meter.record("anthropic", self.usd)
        self.why.append(ctx)
        return f"{ctx['name']} moved ${ctx['metrics']['vol_7d']:,.0f} in the 7 closed days. More next week.  "


CUT = ("word " * 40)[:199] + "x"          # 200 chars, mid-word, the old strip()[:200]


def _prose_seed(c):
    ids = {}
    ids["missing"] = _project(c, "missing", "0xm", what_line="placeholder from the hooklist",
                              enrichment={"parallel": {"website": "https://m.xyz", "team": "M"}})
    ids["failed"] = _project(c, "failed", "0xf", enrichment={"parallel": {"_failed": "run1", "reason": "404"}})
    ids["cut_small"] = _project(c, "cut-small", "0xc1", what_line=CUT, category="other", enriched_at=NOW)
    ids["cut_big"] = _project(c, "cut-big", "0xc2", what_line=CUT, category="other", enriched_at=NOW)
    _project(c, "sentence", "0xc3", what_line=CUT[:-1] + ".", enriched_at=NOW)
    _project(c, "ellipsis", "0xc4", what_line=CUT[:-1] + "…", enriched_at=NOW)
    for k, vol in (("cut_small", 10.0), ("cut_big", 1e6)):
        _old_row(c, ids[k], DAY, vol_7d=vol, is_spam=False)
    return ids


def test_what_line_targets_missing_first_then_cut_mid_word_busiest_first(engine):
    with engine.begin() as c:
        _prose_seed(c)
        got = [(p.slug, kind) for p, kind in repair.what_line_targets(c)]
    assert got == [("missing", "missing"), ("cut-big", "cut"), ("cut-small", "cut")]
    assert repair.cut_mid_word(CUT) and not repair.cut_mid_word(CUT[:-1] + "…") and not repair.cut_mid_word("short")


def test_prose_what_lines_use_stored_facts_and_stop_at_the_repair_cap(engine):
    with engine.begin() as c:
        ids = _prose_seed(c)
        targets = repair.what_line_targets(c)
    meter = repair.CappedMeter(None, cap_usd=0.0085, budgets=Budgets(3.0, 40.0, {}))
    claude = FakeClaude(meter)
    out = repair.write_what_lines(engine, claude, targets)
    # each call is checked at its worst case (~$0.007) on top of what this run spent: the second call no longer fits $0.0085
    assert out == {"missing": 1, "cut": 0, "errors": 0, "budget_stop": 1}
    assert claude.what[0]["enrichment"] == {"parallel": {"website": "https://m.xyz", "team": "M"}}
    with engine.connect() as c:
        rows = {r.slug: r for r in c.execute(sa.select(db.projects))}
    m = rows["missing"]
    assert (m.what_line, m.category, m.website) == ("Missing routes swaps through a hook.", "orders", "https://m.xyz")
    assert m.enriched_at is not None and rows["cut-big"].what_line == CUT
    out = repair.write_what_lines(engine, FakeClaude(repair.CappedMeter(None, None, budgets=Budgets(3.0, 40.0, {}))),
                                  [(rows["cut-big"], "cut")])
    with engine.connect() as c:
        big = c.execute(sa.select(db.projects).where(db.projects.c.slug == "cut-big")).one()
    assert out["cut"] == 1 and big.what_line == "Cut-Big routes swaps through a hook." and big.category == "other"


def test_blurbs_go_to_stale_signals_targets_scored_with_package_8_windows_only(engine):
    with engine.begin() as c:
        old = _project(c, "old-blurb", "0xo", blurb_at=repair.P8_MERGED_AT - D, why_blurb="22,874 swaps in 7 days.")
        fresh = _project(c, "fresh-blurb", "0xn", blurb_at=repair.P8_MERGED_AT + D, why_blurb="ok")
        wash = _project(c, "wash", "0xw", blurb_at=None)
        unscored = _project(c, "unscored", "0xu", blurb_at=None)
        for pid, sc in ((old, 9.0), (fresh, 8.0), (wash, 7.0)):
            _old_row(c, pid, DAY, is_spam=False, score_trending=sc, vol_7d=70_000.0, window_end=DAY - D, is_wash=pid == wash)
        _old_row(c, unscored, DAY, is_spam=False, score_trending=6.0)               # window_end NULL: the old score
        targets, info = repair.blurb_targets(c, DAY, 50, repair.P8_MERGED_AT)
    assert [p.slug for p, _ in targets] == ["old-blurb"] and info == {"targets": 3, "fresh": 1, "old_row": 1}
    claude = FakeClaude(repair.CappedMeter(None, None, budgets=Budgets(3.0, 40.0, {})))
    assert repair.write_blurbs(engine, claude, targets, NOW) == {"blurbs": 1, "errors": 0}
    assert "closed UTC days ending" in claude.why[0]["metrics"]["windows"]
    with engine.connect() as c:
        r = c.execute(sa.select(db.projects).where(db.projects.c.id == old)).one()
    assert r.why_blurb == "Old-Blurb moved $70,000 in the 7 closed days. More next week." and db.as_utc(r.blurb_at) == NOW


def test_prose_plan_shows_counts_worst_case_and_both_budget_options(engine):
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        _prose_seed(c)
        c.execute(sa.insert(db.api_spend).values(ts=now, provider="anthropic", step="enrich", units=1, usd=1.02, detail=""))
        expect = sum(repair.what_line_estimates(c, repair.what_line_targets(c)))
    meter = repair.CappedMeter(engine, None, budgets=Budgets(3.0, 40.0, {}))
    lines, worst = repair.prose_plan(engine, meter, now, True, True, None, 50, repair.P8_MERGED_AT)
    text = "\n".join(lines)
    assert worst == pytest.approx(expect)
    assert "prose what-lines: 1 missing (stored Parallel facts, no Claude line), 2 cut mid-word" in text
    assert "spent today $1.02 of $3.00" in text and "option A, no budget change" in text
    assert f"option B, one run: the owner sets SPEND_DAILY_USD=3.00 on the pipeline service" in text   # 1.02 + worst fits
    assert f"--max-usd {repair._cents_up(worst):.2f}" in text
    lines = repair.budget_lines(meter, 2.5, 1.02, "--max-usd")
    assert "SPEND_DAILY_USD=3.52" in lines[2] and "about 2 day(s)" in lines[1]
