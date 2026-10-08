"""Paid API spend: price table, a durable ledger, hard budgets, and a no-spend preview.

Every paid call goes through a Meter:
  meter.check(provider, est_usd)  -> raises BudgetExceeded before the call when it would break a budget
  meter.record(provider, usd, ...) -> one api_spend row, written on its own connection so it survives a step
                                     transaction rolling back (the money is spent either way)

Budgets (USD, all providers together; per-provider caps optional):
  SPEND_DAILY_USD    default 3.00   (UTC day)
  SPEND_MONTHLY_USD  default 40.00  (rolling 30 days)
  SPEND_DAILY_USD_<PROVIDER> / SPEND_MONTHLY_USD_<PROVIDER>  e.g. SPEND_DAILY_USD_PARALLEL=1
Nansen is metered in credits by clients/nansen.py (NANSEN_MONTHLY_CREDITS); it is logged here with usd=0.

Prices checked 2026-10-08 from the providers' published pricing:
  Anthropic per million tokens (input / output): Sonnet 5.5 2/10, Haiku 4.5 1/5, Haiku 5.5 0.10/0.50
  Parallel Task API per run: lite 0.005, base 0.01, core 0.025, core2x 0.05, pro 0.10, ultra 0.30
  Exa /search auto: 0.007 per request (10 results included) + highlights 0.001 per result page
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

log = logging.getLogger("bait")

ANTHROPIC_PER_MTOK = {               # (input, output) USD per million tokens
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.00, 20.00),   # possible server-side fallback target; priced so it is never under-counted
}
ANTHROPIC_UNKNOWN = (5.00, 25.00)      # unknown model id: assume an expensive one
PARALLEL_PER_RUN = {"lite": 0.005, "base": 0.01, "core": 0.025, "core2x": 0.05, "pro": 0.10, "ultra": 0.30}
EXA_SEARCH = 0.007
EXA_PER_HIGHLIGHT_PAGE = 0.001

DEFAULT_DAILY_USD = 3.00
DEFAULT_MONTHLY_USD = 40.00


class BudgetExceeded(RuntimeError):
    """Raised before a paid call that would break a budget. Steps stop their paid loop on it."""


def anthropic_usd(model: str, input_tokens: int, output_tokens: int,
                  cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
    pin, pout = ANTHROPIC_PER_MTOK.get(model, ANTHROPIC_UNKNOWN)
    return (input_tokens * pin + cache_write_tokens * pin * 1.25 + cache_read_tokens * pin * 0.10
            + output_tokens * pout) / 1_000_000


def anthropic_estimate(model: str, prompt_chars: int, max_tokens: int) -> float:
    """Worst case before the call: ~3 chars per token (conservative) and a full max_tokens of output."""
    return anthropic_usd(model, prompt_chars // 3 + 50, max_tokens)


def parallel_usd(processor: str) -> float:
    return PARALLEL_PER_RUN.get(processor, PARALLEL_PER_RUN["ultra"])


def exa_usd(num_results: int, highlights: bool = True) -> float:
    extra = max(0, num_results - 10) * 0.001
    return EXA_SEARCH + extra + (num_results * EXA_PER_HIGHLIGHT_PAGE if highlights else 0.0)


def _env_float(name: str, default: float | None) -> float | None:
    v = os.environ.get(name, "")
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        return default


@dataclass
class Budgets:
    daily: float
    monthly: float
    per_provider: dict[str, tuple[float | None, float | None]]

    @classmethod
    def from_env(cls) -> "Budgets":
        per = {}
        for p in ("anthropic", "parallel", "exa"):
            per[p] = (_env_float(f"SPEND_DAILY_USD_{p.upper()}", None), _env_float(f"SPEND_MONTHLY_USD_{p.upper()}", None))
        return cls(_env_float("SPEND_DAILY_USD", DEFAULT_DAILY_USD), _env_float("SPEND_MONTHLY_USD", DEFAULT_MONTHLY_USD), per)


class Meter:
    def __init__(self, engine: sa.Engine | None, now_fn=None, budgets: Budgets | None = None):
        self.engine = engine
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.budgets = budgets or Budgets.from_env()
        self.step: str | None = None
        self.run_usd = 0.0
        self._warned: set[str] = set()
        # Postgres: each row commits on its own connection at once. SQLite allows one writer, and the step's
        # transaction holds it, so rows are buffered and flushed after the step (also after a failed step).
        self._buffer: list[dict] = []
        self._immediate = engine is not None and engine.dialect.name != "sqlite"

    # --- reads -------------------------------------------------------------------------------
    def spent(self, provider: str | None = None, days: float | None = None, today: bool = False) -> float:
        from .db import api_spend
        now = self.now_fn()
        buffered = sum(r["usd"] for r in self._buffer if provider in (None, r["provider"]))
        if self.engine is None:
            return buffered
        q = sa.select(sa.func.coalesce(sa.func.sum(api_spend.c.usd), 0.0))
        if today:
            start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
            q = q.where(api_spend.c.ts >= start)
        elif days is not None:
            q = q.where(api_spend.c.ts >= now - timedelta(days=days))
        if provider:
            q = q.where(api_spend.c.provider == provider)
        with self.engine.connect() as c:
            return float(c.execute(q).scalar_one() or 0.0) + buffered

    # --- the two calls every paid client makes -------------------------------------------------
    def check(self, provider: str, est_usd: float) -> None:
        b = self.budgets
        problems = []
        if self.spent(today=True) + est_usd > b.daily:
            problems.append(f"daily budget ${b.daily:.2f}")
        if self.spent(days=30) + est_usd > b.monthly:
            problems.append(f"30-day budget ${b.monthly:.2f}")
        pd, pm = b.per_provider.get(provider, (None, None))
        if pd is not None and self.spent(provider, today=True) + est_usd > pd:
            problems.append(f"{provider} daily ${pd:.2f}")
        if pm is not None and self.spent(provider, days=30) + est_usd > pm:
            problems.append(f"{provider} 30-day ${pm:.2f}")
        if problems:
            key = f"{provider}:{','.join(problems)}"
            if key not in self._warned:
                log.warning("spend stop: %s call (~$%.4f) would exceed %s; skipping paid calls of this kind",
                            provider, est_usd, " and ".join(problems))
                self._warned.add(key)
            raise BudgetExceeded(f"{provider}: {', '.join(problems)}")

    def record(self, provider: str, usd: float, units: int = 1, detail: str = "") -> None:
        from .db import api_spend
        self.run_usd += usd
        if self.engine is None:
            return
        row = {"ts": self.now_fn(), "provider": provider, "step": self.step or "", "units": int(units),
               "usd": float(usd), "detail": (detail or "")[:200]}
        if not self._immediate:
            self._buffer.append(row)
            return
        # Own connection + commit: the spend row must persist even if the step's transaction rolls back.
        with self.engine.begin() as c:
            c.execute(sa.insert(api_spend).values(**row))

    def flush(self) -> None:
        """Write buffered rows (SQLite). Called after every step, including failed ones."""
        from .db import api_spend
        if not self._buffer or self.engine is None:
            self._buffer.clear() if self.engine is None else None
            return
        rows, self._buffer = self._buffer, []
        with self.engine.begin() as c:
            c.execute(sa.insert(api_spend), rows)


class NullMeter(Meter):
    """No ledger, no budget: tests and code paths constructed without a run."""

    def __init__(self):
        super().__init__(None, budgets=Budgets(float("inf"), float("inf"), {}))

    def check(self, provider: str, est_usd: float) -> None:
        return None


# ------------------------------------------------------------------------------------------------
# Preview: what a full run would call and cost right now, without calling anything paid.
# ------------------------------------------------------------------------------------------------
def preview(conn: sa.Connection, ctx, meter: Meter) -> list[dict]:
    from .clients import nansen as N
    from .config import CLAUDE_BLURB_MODEL, CLAUDE_CLASSIFY_MODEL, env
    from .steps import enrich, group, radar, signals

    rows: list[dict] = []
    have = {k: bool(env(v)) for k, v in (("anthropic", "ANTHROPIC_API_KEY"), ("parallel", "PARALLEL_API_KEY"),
                                         ("exa", "EXA_API_KEY"), ("nansen", "NANSEN_API_KEY"))}
    sonnet_call = anthropic_estimate(CLAUDE_BLURB_MODEL, 6000, 600)       # what_line / why_blurb worst case
    haiku_group = anthropic_estimate(CLAUDE_CLASSIFY_MODEL, group.PROMPT_CHARS_MAX, 400)
    haiku_radar = anthropic_estimate(CLAUDE_CLASSIFY_MODEL, 4000, 300)
    exa_call = exa_usd(10)

    # Hooks added by snapshot/onchain carry no name or description, so they never reach Claude (named for free).
    n_group = min(group.claude_cap(), group.claude_candidates_count(conn)) if have["anthropic"] else 0
    rows.append({"step": "group", "provider": "anthropic", "what": f"{CLAUDE_CLASSIFY_MODEL} classify described, unnamed hooks",
                 "calls": n_group, "unit_usd": haiku_group, "note": "bare-address hooks are never sent"})

    n_enrich = len(enrich.select_candidates(conn)) if have["anthropic"] else 0
    rows.append({"step": "enrich", "provider": "parallel", "what": "Parallel core enrichment",
                 "calls": n_enrich if have["parallel"] else 0, "unit_usd": parallel_usd("core"), "note": "new busy projects only"})
    rows.append({"step": "enrich", "provider": "anthropic", "what": f"{CLAUDE_BLURB_MODEL} what_line",
                 "calls": n_enrich, "unit_usd": sonnet_call, "note": ""})

    n_sig = len(signals.select_targets(conn, ctx))
    rows.append({"step": "signals", "provider": "exa", "what": "Exa receipts search",
                 "calls": n_sig if have["exa"] else 0, "unit_usd": exa_call, "note": "one search per New/Trending project"})
    rows.append({"step": "signals", "provider": "anthropic", "what": f"{CLAUDE_BLURB_MODEL} why_blurb",
                 "calls": n_sig if have["anthropic"] else 0, "unit_usd": sonnet_call, "note": "upper bound: only when signals changed"})

    rows.append({"step": "radar", "provider": "exa", "what": "Exa radar searches",
                 "calls": radar.exa_query_cap() if have["exa"] and have["anthropic"] else 0, "unit_usd": exa_call, "note": ""})
    rows.append({"step": "radar", "provider": "anthropic", "what": f"{CLAUDE_CLASSIFY_MODEL} radar classify",
                 "calls": radar.classify_cap() if have["exa"] and have["anthropic"] else 0, "unit_usd": haiku_radar, "note": ""})

    n_nansen = 0
    if have["nansen"]:
        n_nansen = min(N.max_addresses(), max(0, N.budget_left(conn, ctx.now)) // N.cost_per_hit())
    rows.append({"step": "activity", "provider": "nansen", "what": "Nansen wallet labels (credits, not USD)",
                 "calls": n_nansen, "unit_usd": 0.0, "note": f"~{N.cost_per_hit()} credits each, budget left {N.budget_left(conn, ctx.now)}"})
    for r in rows:
        r["worst_usd"] = r["calls"] * r["unit_usd"]   # full precision; formatting rounds for display only
    return rows


def format_preview(rows: list[dict], meter: Meter) -> str:
    total = sum(r["worst_usd"] for r in rows)
    b = meter.budgets
    lines = [f"{'step':9} {'provider':10} {'calls':>6} {'unit $':>9} {'worst $':>9}  what / note"]
    for r in rows:
        lines.append(f"{r['step']:9} {r['provider']:10} {r['calls']:>6} {r['unit_usd']:>9.4f} {r['worst_usd']:>9.4f}  "
                     f"{r['what']}{' (' + r['note'] + ')' if r['note'] else ''}")
    lines.append(f"{'':9} {'TOTAL':10} {'':>6} {'':>9} {total:>9.4f}  worst case for one full run")
    lines.append(f"spent today ${meter.spent(today=True):.4f} of ${b.daily:.2f}; last 30 days ${meter.spent(days=30):.4f} of ${b.monthly:.2f}")
    if total > b.daily:
        lines.append(f"note: worst case exceeds the daily budget; the run will stop paid calls at ${b.daily:.2f}")
    return "\n".join(lines)
