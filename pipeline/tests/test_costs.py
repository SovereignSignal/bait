"""Cost controls: prices, ledger, budgets, client metering, group caps, preview. Hand-computed expectations."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from bait import costs, db
from bait.costs import BudgetExceeded, Budgets, Meter
from bait.steps import enrich, group
from tests.conftest import DAY, NOW, insert_hook, insert_pool, insert_project


# --- prices -------------------------------------------------------------------------------------
def test_anthropic_prices_hand_computed():
    # Sonnet 5.5: $2 in / $10 out per MTok -> 1,000 in + 500 out = 0.002 + 0.005
    assert costs.anthropic_usd("claude-sonnet-5-5", 1000, 500) == pytest.approx(0.007)
    # Haiku 4.5: $1 / $5 -> 2,000 in + 300 out = 0.002 + 0.0015
    assert costs.anthropic_usd("claude-haiku-4-5", 2000, 300) == pytest.approx(0.0035)
    # Haiku 5.5: $0.10 / $0.50 -> 2,000 in + 300 out = 0.0002 + 0.00015
    assert costs.anthropic_usd("claude-haiku-5-5", 2000, 300) == pytest.approx(0.00035)
    # cache write 1.25x input, cache read 0.10x input (Sonnet): 1,000 written + 10,000 read = 0.0025 + 0.002
    assert costs.anthropic_usd("claude-sonnet-5-5", 0, 0, 1000, 10000) == pytest.approx(0.0045)
    # unknown model id is priced high ($5 / $25), never under-counted
    assert costs.anthropic_usd("claude-something-new", 1000, 1000) == pytest.approx(0.03)


def test_anthropic_estimate_is_an_upper_bound():
    # 3,000 chars -> 1,000 + 50 tokens at $2 + full 600 output at $10 = 0.0021 + 0.006
    assert costs.anthropic_estimate("claude-sonnet-5-5", 3000, 600) == pytest.approx(0.0081)


def test_parallel_and_exa_prices():
    assert costs.parallel_usd("core") == 0.025 and costs.parallel_usd("lite") == 0.005
    assert costs.parallel_usd("mystery") == 0.30          # unknown processor priced as ultra
    assert costs.exa_usd(10) == pytest.approx(0.017)      # 0.007 search + 10 highlight pages x 0.001
    assert costs.exa_usd(10, highlights=False) == pytest.approx(0.007)
    assert costs.exa_usd(20) == pytest.approx(0.007 + 0.010 + 0.020)   # 10 extra results + 20 highlight pages


# --- ledger + budgets ---------------------------------------------------------------------------
@pytest.fixture
def file_engine(tmp_path):
    e = db.make_engine(f"sqlite:///{tmp_path}/spend.sqlite")
    db.create_all(e)
    return e


def _meter(engine, daily=1.0, monthly=10.0, per=None, now=NOW):
    return Meter(engine, now_fn=lambda: now, budgets=Budgets(daily, monthly, per or {}))


def test_daily_budget_blocks_before_the_call(file_engine):
    m = _meter(file_engine, daily=0.05)
    m.record("anthropic", 0.04)
    m.check("anthropic", 0.005)                       # 0.045 <= 0.05: allowed
    with pytest.raises(BudgetExceeded):
        m.check("anthropic", 0.02)                    # 0.06 > 0.05: refused before any call
    m.flush()
    assert m.spent(today=True) == pytest.approx(0.04)


def test_monthly_window_is_rolling_30_days(file_engine):
    with file_engine.begin() as c:
        c.execute(sa.insert(db.api_spend), [
            {"ts": NOW - timedelta(days=20), "provider": "exa", "step": "signals", "units": 1, "usd": 5.0, "detail": ""},
            {"ts": NOW - timedelta(days=40), "provider": "exa", "step": "signals", "units": 1, "usd": 50.0, "detail": ""}])
    m = _meter(file_engine, daily=100, monthly=6.0)
    assert m.spent(days=30) == pytest.approx(5.0)     # the 40-day-old row is outside the window
    m.check("exa", 0.9)
    with pytest.raises(BudgetExceeded):
        m.check("exa", 1.1)


def test_per_provider_cap(file_engine):
    m = _meter(file_engine, daily=100, monthly=100, per={"parallel": (0.05, None)})
    m.record("parallel", 0.025)
    m.record("parallel", 0.025)
    m.check("exa", 1.0)                               # other providers unaffected
    with pytest.raises(BudgetExceeded):
        m.check("parallel", 0.025)


def test_ledger_survives_a_rolled_back_step(file_engine):
    m = _meter(file_engine)
    with pytest.raises(RuntimeError):
        with file_engine.begin() as c:                # the step's transaction
            c.execute(sa.insert(db.projects).values(slug="x", name="X", first_seen_at=NOW))
            m.record("anthropic", 0.123, detail="claude-haiku-4-5")
            raise RuntimeError("step failed")
    m.flush()                                         # __main__ flushes after failed steps too
    with file_engine.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(db.projects)).scalar_one() == 0
        assert c.execute(sa.select(db.api_spend.c.usd)).scalar_one() == pytest.approx(0.123)


# --- clients are metered ------------------------------------------------------------------------
class FakeAnthropic:
    def __init__(self, model="claude-haiku-4-5", tin=1500, tout=200):
        self.calls = 0
        resp = SimpleNamespace(stop_reason="end_turn", model=model,
                               usage=SimpleNamespace(input_tokens=tin, output_tokens=tout, cache_creation_input_tokens=0,
                                                     cache_read_input_tokens=0),
                               content=[SimpleNamespace(type="text", text='{"project": "Foo", "category": "other", "reason": "r"}')])
        outer = self

        class _M:
            def create(self, **kw):
                outer.calls += 1
                return resp
        self.messages = _M()
        self.beta = SimpleNamespace(messages=_M())


def test_claude_client_records_actual_usage(file_engine):
    from bait.clients.claude import Claude
    fake = FakeAnthropic(tin=1500, tout=200)
    c = Claude(client=fake)
    c.meter = _meter(file_engine)
    c.classify_hook("x", "", "0xabc", [])
    # Haiku 4.5: 1,500 x $1 + 200 x $5 per MTok = 0.0015 + 0.001
    assert c.meter.run_usd == pytest.approx(0.0025)


def test_claude_client_refuses_before_calling_when_over_budget(file_engine):
    from bait.clients.claude import Claude
    fake = FakeAnthropic()
    c = Claude(client=fake)
    c.meter = _meter(file_engine, daily=0.0001)
    with pytest.raises(BudgetExceeded):
        c.classify_hook("x", "", "0xabc", [])
    assert fake.calls == 0                             # no request was sent


def test_exa_and_parallel_clients_record(file_engine):
    from bait.clients.exa import Exa
    from bait.clients.parallel import Parallel

    class Resp:
        def __init__(self, data, status=200):
            self._d, self.status_code = data, status

        def json(self):
            return self._d

        def raise_for_status(self):
            return None

    class Http:
        def post(self, url, **kw):
            return Resp({"results": []} if "exa" in url else {"run_id": "r1"})

        def get(self, url, **kw):
            return Resp({"output": {"content": {"team": "T"}}})

    m = _meter(file_engine)
    e = Exa("k", http_client=Http()); e.meter = m
    e.search_recent("q")
    p = Parallel("k", http_client=Http(), poll_s=0); p.meter = m
    p.enrich("Proj", "")
    assert m.run_usd == pytest.approx(0.017 + 0.025)


# --- group: cap + shortlist ---------------------------------------------------------------------
def test_group_caps_claude_calls_and_sends_a_shortlist(conn, ctx, monkeypatch):
    monkeypatch.setenv("GROUP_CLAUDE_MAX", "2")
    for i in range(30):
        insert_project(conn, f"Project{i}", NOW, slug=f"p{i}")
    for i in range(5):
        insert_hook(conn, f"0x{i:040x}", 8453, name=None)
        conn.execute(sa.update(db.hooks).where(db.hooks.c.address == f"0x{i:040x}").values(description=f"swap fee hook number {i}"))

    class FakeClaude:
        def __init__(self):
            self.known_sizes = []

        def classify_hook(self, name, description, address, known):
            self.known_sizes.append(len(known))
            return {"project": f"New {address[-4:]}", "category": "other"}

    fc = FakeClaude()
    ctx.clients.update(claude=fc)
    group.run(conn, ctx)
    assert len(fc.known_sizes) == 2                    # capped at GROUP_CLAUDE_MAX
    assert all(n <= group.SHORTLIST_N for n in fc.known_sizes)
    left = conn.execute(sa.select(sa.func.count()).select_from(db.hooks).where(db.hooks.c.project_id.is_(None))).scalar_one()
    assert left == 3                                   # the rest wait for later runs


def test_shortlist_finds_the_matching_project():
    known = ["EulerSwap", "Bunni", "Angstrom", "Clanker"] + [f"Other{i}" for i in range(100)]
    s = group.shortlist("BunniHook v2", "Bunni liquidity density hook", known)
    assert s[0] == "Bunni" and len(s) <= group.SHORTLIST_N
    assert group.shortlist("", "", known) == []        # nothing to match on: Claude names it from scratch


# --- preview matches what the steps would do ----------------------------------------------------
def test_preview_counts_and_totals(conn, ctx, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "PARALLEL_API_KEY", "EXA_API_KEY"):
        monkeypatch.setenv(k, "test")
    monkeypatch.delenv("NANSEN_API_KEY", raising=False)
    monkeypatch.setenv("ENRICH_MAX", "40")
    monkeypatch.setenv("GROUP_CLAUDE_MAX", "100")
    monkeypatch.setenv("ONCHAIN_MAX_NEW_HOOKS", "10")
    # 3 projects with live pools (enrich candidates), 1 without (never enriched)
    for i in range(3):
        pid = insert_project(conn, f"Busy{i}", NOW - timedelta(days=30), slug=f"b{i}")
        a = f"0x{(i + 1):040x}"
        insert_hook(conn, a, 8453, project_id=pid, name=f"Busy{i}")
        insert_pool(conn, f"0xpool{i}", 8453, a, NOW - timedelta(days=30))
        conn.execute(sa.insert(db.project_daily).values(project_id=pid, day=DAY, tvl_usd=1000, vol_7d=50_000, vol_7d_prev=10_000,
                                                        swaps_7d=10, pools_n=1, score_trending=5.0 + i, is_new=False, is_spam=False))
    insert_project(conn, "Idle", NOW, slug="idle")
    insert_hook(conn, "0x" + "9" * 40, 8453, name=None)   # bare address: never sent to Claude
    insert_hook(conn, "0x" + "8" * 40, 8453, name=None)   # described, unnamed: one Claude call
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == "0x" + "8" * 40).values(description="limit order hook"))
    m = Meter(None, budgets=Budgets(3.0, 40.0, {}))
    rows = {(r["step"], r["provider"]): r for r in costs.preview(conn, ctx, m)}
    assert rows[("enrich", "parallel")]["calls"] == 3 and rows[("enrich", "anthropic")]["calls"] == 3
    assert rows[("signals", "exa")]["calls"] == 3 and rows[("signals", "anthropic")]["calls"] == 3
    assert rows[("group", "anthropic")]["calls"] == 1   # only the described one; bare addresses cost nothing
    assert rows[("radar", "exa")]["calls"] == 6 and rows[("radar", "anthropic")]["calls"] == 30
    assert rows[("activity", "nansen")]["calls"] == 0   # no key
    total = sum(r["worst_usd"] for r in rows.values())
    hand = (1 * costs.anthropic_estimate("claude-haiku-4-5", group.PROMPT_CHARS_MAX, 400)
            + 3 * 0.025 + 3 * costs.anthropic_estimate("claude-sonnet-5-5", 6000, 600)
            + 3 * 0.017 + 3 * costs.anthropic_estimate("claude-sonnet-5-5", 6000, 600)
            + 6 * 0.017 + 30 * costs.anthropic_estimate("claude-haiku-4-5", 4000, 300))
    assert total == pytest.approx(hand, rel=1e-6)
