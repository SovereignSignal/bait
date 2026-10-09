"""Package 9: python -m bait repair. GeckoTerminal and Claude are fakes; nothing here touches the network or spends."""
from datetime import date, datetime, time as dtime, timedelta, timezone

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
    per = repair.rescore(engine, [DAY], NOW, echo=lines.append)["days"]
    assert per[DAY.isoformat()]["top10_in"] == ["riser"] and per[DAY.isoformat()]["backed_up"] == 1
    assert lines[0].startswith(f"{DAY}  projects 1  new 0  trending 1  wash 0") and "in riser" in lines[1]
    with engine.connect() as c:
        row = c.execute(sa.select(db.project_daily)).one()
    assert row.window_end == end and row.vol_7d == 140_000.0 and not row.is_spam and row.score_trending > 0
    assert repair.rescore(engine, [DAY], NOW, echo=lines.append)["days"][DAY.isoformat()]["backed_up"] == 0   # first copy kept
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


# --- verify -------------------------------------------------------------------------------------
def test_verify_prints_bait_and_geckoterminal_for_the_same_pools_and_closed_windows(engine):
    end = DAY - D
    with engine.begin() as c:
        p = _project(c, "top", "0xht")
        _pool(c, "0xpt", "0xht")
        for i in range(14):
            _snap(c, "0xpt", end - i * D, cum=1.0, vol_day=1_000.0, est=False)
        _old_row(c, p, DAY, is_spam=False, score_trending=5.0, vol_24h=1_000.0, vol_7d=7_000.0, vol_7d_prev=7_000.0,
                 window_end=end)
    http = FakeGT({"0xpt": {end - i * D: (1_100.0 if i < 7 else 1_000.0) for i in range(14)} | {DAY: 99.0}})
    lines = []
    out = repair.verify(engine, _ctx(http), sample=5, per_project=3, seed=1, echo=lines.append)
    assert out == {"day": DAY.isoformat(), "projects": 1, "pools": 1, "calls": 1, "failed": 0}
    assert "| top | top trending | 1/1 | 24h | $1,000 | $1,000 | $1,100 | -9.1% |" in lines
    assert "| top | top trending | 1/1 | prev 7d | $7,000 | $7,000 | $7,000 | +0.0% |" in lines


# --- CLI ----------------------------------------------------------------------------------------
def _today():
    return datetime.now(timezone.utc).date()


def _at(monkeypatch, hh, mm=0):
    """Pin the repair's clock to today hh:mm UTC (the CLI databases are dated today)."""
    at = datetime.combine(_today(), dtime(hh, mm), tzinfo=timezone.utc)
    monkeypatch.setattr(repair, "_utcnow", lambda: at)
    return at


def _nightly(e, day, rc=0):
    with e.begin() as c:
        c.execute(sa.insert(db.run_receipts).values(day=day, started_at=NOW, finished_at=NOW, exit_code=rc, soft_errors=0, steps={}))


def _cli_db(tmp_path, nightly_rc=0, p8=True, nightly_day=None):
    url = f"sqlite:///{tmp_path}/r.sqlite"
    e = db.make_engine(url)
    db.create_all(e)
    db.add_missing_columns(e)
    t = _today()
    with e.begin() as c:
        p = _project(c, "cli", "0xhc")
        _pool(c, "0xpc", "0xhc")
        _snap(c, "0xpc", t - 3 * D, cum=100.0)
        if p8:
            _snap(c, "0xpc", t, vol_day=1.0, est=True, cum=101.0)
        _old_row(c, p, t)
    if nightly_rc is not None:
        _nightly(e, nightly_day or t, nightly_rc)
    return url, e


def _receipts(e):
    with e.connect() as c:
        return c.execute(sa.select(db.run_receipts).where(db.run_receipts.c.notes.is_not(None)).order_by(db.run_receipts.c.id)).all()


def test_repair_respects_the_run_lock_the_since_range_and_a_failed_nightly(tmp_path, monkeypatch):
    from bait.__main__ import main
    _at(monkeypatch, 12)
    url, e = _cli_db(tmp_path)
    assert main(["repair", "rescore", "--since", "16", "--database-url", url]) == 2
    assert main(["repair", "prose", "--dry-run", "--database-url", url]) == 2          # neither --what-lines nor --blurbs
    monkeypatch.setattr(db, "try_run_lock", lambda engine: None)
    assert main(["repair", "rescore", "--database-url", url]) == 3
    monkeypatch.undo()
    _at(monkeypatch, 12)
    (tmp_path / "failed").mkdir()
    url2, e2 = _cli_db(tmp_path / "failed", nightly_rc=1)
    assert main(["repair", "rescore", "--database-url", url2]) == repair.EXIT_PRECONDITION
    (tmp_path / "pre8").mkdir()
    url3, e3 = _cli_db(tmp_path / "pre8", p8=False)
    assert main(["repair", "backfill-days", "--database-url", url3]) == repair.EXIT_PRECONDITION
    assert main(["repair", "rescore", "--database-url", url3]) == repair.EXIT_PRECONDITION     # would read $0 windows
    assert _receipts(e) == [] and _receipts(e2) == [] and _receipts(e3) == []


def test_repair_all_dry_run_prints_the_plan_and_writes_nothing(tmp_path, monkeypatch, capsys):
    from bait.__main__ import main
    _at(monkeypatch, 12)
    url, e = _cli_db(tmp_path)
    assert main(["repair", "all", "--since", "14", "--dry-run", "--database-url", url]) == 0
    text = capsys.readouterr().out
    for part in ("backfill-days: closed days", "GeckoTerminal calls: 1", "rescore: 1 stored run day(s)", "verify: up to 15",
                 "prose what-lines:", "prose blurbs:", "option A", "option B", "--prose-budget-usd"):
        assert part in text, part
    assert _receipts(e) == []
    with e.connect() as c:
        assert c.execute(sa.select(db.pool_snapshots.c.volume_usd_day).where(db.pool_snapshots.c.day == _today() - 3 * D)).scalar_one() is None


def test_repair_all_yes_runs_the_free_steps_with_a_receipt_each_and_stops_before_prose(tmp_path, monkeypatch, capsys):
    from bait.__main__ import main
    _at(monkeypatch, 12)
    url, e = _cli_db(tmp_path)
    t = _today()
    monkeypatch.setattr(repair, "keyless_http", lambda ctx: FakeGT({"0xpc": {t - 3 * D: 123.0}}))
    assert main(["repair", "all", "--since", "14", "--yes", "--stop-at", "", "--database-url", url]) == 0
    assert "prose not run" in capsys.readouterr().out
    got = _receipts(e)
    assert [r.notes for r in got] == ["repair backfill-days", "repair rescore", "repair verify"]
    assert all(r.exit_code == 0 and r.soft_errors == 0 and r.day == t for r in got)
    assert got[0].steps["backfill-days"]["rows_updated"] == 1
    assert _rows(e, "0xpc")[t - 3 * D].volume_usd_day == 123.0


def test_repair_receipts_carry_the_latest_nightly_day_never_their_own(tmp_path, monkeypatch):
    # /api/health reads the latest receipt: a repair dated today with exit 0 would pass for a nightly that never ran
    from bait.__main__ import main
    _at(monkeypatch, 12)
    url, e = _cli_db(tmp_path, nightly_day=_today() - D)              # today's nightly has not written
    assert main(["repair", "rescore", "--database-url", url]) == 0
    assert [(r.notes, r.day) for r in _receipts(e)] == [("repair rescore", _today() - D)]
    (tmp_path / "none").mkdir()
    url2, e2 = _cli_db(tmp_path / "none", nightly_rc=None)             # no nightly receipt at all
    assert main(["repair", "rescore", "--database-url", url2]) == 0
    assert _receipts(e2)[0].day == repair.NO_NIGHTLY_DAY


def test_repair_refuses_between_0530_and_0800_utc_until_todays_nightly_has_written(tmp_path, monkeypatch):
    from bait.__main__ import main
    url, e = _cli_db(tmp_path, nightly_day=_today() - D)
    _at(monkeypatch, 7)
    assert main(["repair", "rescore", "--database-url", url]) == repair.EXIT_PRECONDITION
    assert _receipts(e) == []
    _nightly(e, _today())                                              # the nightly is done: a repair may follow it
    assert main(["repair", "rescore", "--database-url", url]) == 0
    _at(monkeypatch, 8)                                                # past the health deadline the guard is off
    (tmp_path / "late").mkdir()
    url2, _ = _cli_db(tmp_path / "late", nightly_day=_today() - D)
    assert main(["repair", "rescore", "--database-url", url2]) == 0


def test_the_stop_time_is_the_one_before_the_next_nightly_and_a_start_inside_it_is_refused(tmp_path, monkeypatch):
    from bait.__main__ import main
    t = _today()
    at = lambda h, m=0, d=0: datetime.combine(t + d * D, dtime(h, m), tzinfo=timezone.utc)   # noqa: E731
    assert repair.deadline_at("05:30", at(2)) == at(5, 30)
    assert repair.deadline_at("05:30", at(23)) == at(5, 30, 1)
    assert repair.deadline_at("05:30", at(6, 30)) == at(5, 30, 1)          # the nightly's first 30 minutes are over
    assert [repair.in_blackout("05:30", at(h, m)) for h, m in ((5, 29), (5, 45), (6, 20), (6, 30))] == [False, True, True, False]
    assert repair.deadline_at("", at(2)) is None and not repair.in_blackout("", at(5, 45))
    url, e = _cli_db(tmp_path)                                             # today's nightly receipt exists
    _at(monkeypatch, 5, 45)                                                # a start after the stop time: never rolls to tomorrow
    assert main(["repair", "backfill-days", "--database-url", url]) == repair.EXIT_PRECONDITION
    assert main(["repair", "rescore", "--stop-at", "05:00", "--database-url", url]) == repair.EXIT_PRECONDITION
    _at(monkeypatch, 12)
    assert main(["repair", "rescore", "--stop-at", "07:00", "--database-url", url]) == 2   # must be before 06:00
    assert _receipts(e) == []


def test_all_started_late_stops_before_rescore_once_the_deadline_has_passed(tmp_path, monkeypatch, capsys):
    from bait.__main__ import main
    t = _today()
    clock = [datetime.combine(t, dtime(12), tzinfo=timezone.utc)]
    monkeypatch.setattr(repair, "_utcnow", lambda: clock[0])

    class SlowGT(FakeGT):                                                  # the backfill runs into the stop time
        def gt(self, path):
            clock[0] = datetime.combine(t + D, dtime(5, 31), tzinfo=timezone.utc)
            return super().gt(path)

    url, e = _cli_db(tmp_path)
    monkeypatch.setattr(repair, "keyless_http", lambda ctx: SlowGT({"0xpc": {t - 3 * D: 123.0}}))
    assert main(["repair", "all", "--since", "14", "--yes", "--database-url", url]) == repair.EXIT_INCOMPLETE
    assert "INCOMPLETE: rerun to resume" in capsys.readouterr().out
    assert [(r.notes, r.exit_code) for r in _receipts(e)] == [("repair backfill-days", 0)]     # rescore never started


def test_rescore_and_prose_stop_between_items_at_the_deadline(engine):
    with engine.begin() as c:
        _prose_seed(c)
        targets = repair.what_line_targets(c)
    out = repair.rescore(engine, [DAY - D, DAY], NOW, echo=lambda s: None, deadline=NOW, clock=lambda: NOW)
    assert out == {"days": {}, "left_at_deadline": 2}
    claude = FakeClaude(repair.CappedMeter(None, None, budgets=Budgets(3.0, 40.0, {})))
    out = repair.write_what_lines(engine, claude, targets, deadline=NOW, clock=lambda: NOW)
    assert out["left_at_deadline"] == len(targets) and claude.what == []
    out = repair.write_blurbs(engine, claude, [("p", "d")], NOW, deadline=NOW, clock=lambda: NOW)
    assert out["left_at_deadline"] == 1 and claude.why == []


def test_restore_puts_back_every_backed_up_day_whatever_today_is_and_even_after_a_failed_nightly(tmp_path, monkeypatch, capsys):
    from bait.__main__ import main
    _at(monkeypatch, 12)
    url, e = _cli_db(tmp_path, nightly_rc=1)            # the latest nightly failed: a restore is still allowed
    t = _today()
    days = [t - (i + 2) * D for i in range(15)]         # backed up two days ago, as a 15-day re-score would
    with e.begin() as c:
        pid = c.execute(sa.select(db.projects.c.id).where(db.projects.c.slug == "cli")).scalar_one()
        c.execute(sa.delete(db.project_daily))
        for d in days:
            _old_row(c, pid, d)
            repair.backup_day(c, d)
        c.execute(sa.update(db.project_daily).values(is_spam=False))      # what the re-score wrote
        assert repair.backup_days(c) == sorted(days) and repair.backup_days(c, days[0]) == [days[0]]
    assert main(["repair", "rescore", "--restore", "--dry-run", "--database-url", url]) == 0
    assert f"restore: 15 backed-up day(s) {days[-1]} .. {days[0]}" in capsys.readouterr().out
    assert main(["repair", "rescore", "--restore", "--day", days[-1].isoformat(), "--database-url", url]) == 0
    with e.connect() as c:
        assert c.execute(sa.select(db.project_daily.c.day).where(db.project_daily.c.is_spam.is_(True))).scalars().all() == [days[-1]]
    assert main(["repair", "rescore", "--restore", "--database-url", url]) == 0
    with e.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(db.project_daily).where(db.project_daily.c.is_spam.is_(True))).scalar_one() == 15
    assert [r.notes for r in _receipts(e)] == ["repair rescore-restore", "repair rescore-restore"]
    assert main(["repair", "rescore", "--database-url", url]) == repair.EXIT_PRECONDITION   # a re-score is not exempt


def test_backfill_keeps_estimates_before_the_first_candle_and_never_fills_unknown_volume(engine):
    with engine.begin() as c:
        _project(c, "edge", "0xhe")
        _pool(c, "0xpe", "0xhe")
        _snap(c, "0xpe", DAY - 6 * D, vol_day=500.0, est=True, cum=10.0)   # an estimate before the first candle: kept
        _snap(c, "0xpe", DAY - 5 * D, cum=20.0)                             # no per-day value before the first candle: $0
        _snap(c, "0xpe", DAY - 3 * D, vol_day=9.0, est=True, cum=None)      # an estimate where volume is unknown: kept
        _snap(c, "0xpe", DAY - 2 * D, vol_day=7.0, est=True, cum=30.0)      # an estimate after the first candle, no candle: $0
        _project(c, "unknown", "0xhu")
        _pool(c, "0xpu", "0xhu")
        _snap(c, "0xpu", DAY - 3 * D, vol_day=9.0, est=True, cum=None)      # only unknown-volume estimates: not a target
        assert [t[0] for t in repair.backfill_targets(c, DAY, since=2)[0]] == ["0xpe"]
    repair.backfill(engine, _ctx(FakeGT({"0xpe": {DAY - 4 * D: 100.0}})), since=2)
    r = _rows(engine, "0xpe")
    assert [(r[DAY - i * D].volume_usd_day, r[DAY - i * D].estimated) for i in (6, 5, 4, 3, 2)] == [
        (500.0, True), (0.0, False), (100.0, False), (9.0, True), (0.0, False)]
