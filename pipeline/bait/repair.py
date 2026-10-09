"""Package 9: one-off data repair. `python -m bait repair <command>`; see pipeline/README.md "Repair".

backfill-days  GeckoTerminal daily candles (free, paced) -> pool_snapshots.volume_usd_day for closed days, where a row has
               no per-day value (written before 0012) or only an estimate. One call per pool, busiest first; resumable.
rescore        score.run for each stored run day in the range, oldest first, after a one-time backup of that day's rows.
prose          Claude only, paid: what_line for projects whose Parallel facts are stored but whose line was never written,
               or whose line the old code cut mid-word at 200 chars; why_blurb for the signals targets whose blurb predates
               the package 8 window text. Every call passes the spend Meter; --max-usd caps this run on top of it.
verify         Free: Bait's 24h / 7d / prev-7d against GeckoTerminal candle sums for a few projects, as a markdown table.
all            The plan (--dry-run), or backfill-days -> rescore -> verify, then prose only within --prose-budget-usd.

Every command that changes or checks data takes the run lock (exit 3 while the nightly runs) and writes one run_receipts
row per sub-step (day = today, notes = "repair <name>"). Dry runs read only and write nothing.
"""
from __future__ import annotations

import argparse
import math
import os
import random
from datetime import date, datetime, time as dtime, timedelta, timezone

import sqlalchemy as sa

from . import db
from . import keyless as K
from .clients.claude import prose_estimate, what_line_prompt, why_blurb_prompt
from .costs import BudgetExceeded, Meter
from .db import (api_spend, as_utc, hooks, pool_daily_onchain, pool_snapshots, pool_stats, pools, project_daily, projects,
                 run_receipts, upsert)
from .steps import RunContext, enrich, log, score, signals
from .steps.snapshot import keyless_http

PRIOR_DAYS = 14            # a run day's windows reach 14 closed days back (7d and prev 7d)
OHLCV_LIMIT = 30           # daily candles per GeckoTerminal call, one call per pool
MAX_SINCE = OHLCV_LIMIT - PRIOR_DAYS - 1   # 15: the oldest run day whose windows 30 candles still cover
# Blurbs written before package 8 merged used the old window text ("swaps in 7 days" that were 24h).
P8_MERGED_AT = datetime(2026, 10, 9, 15, 29, 25, tzinfo=timezone.utc)
NET_BY_CHAIN = {cid: net for net, (cid, *_rest) in K.CHAINS.items()}
SLUG_BY_CHAIN = {cid: slug for _net, (cid, slug, *_rest) in K.CHAINS.items()}
EXIT_PRECONDITION = 5

_D = timedelta(days=1)
_COLS = [c.name for c in project_daily.c]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------------------------------------
# backfill-days
# ------------------------------------------------------------------------------------------------
def backfill_window(today: date, since: int) -> tuple[date, date]:
    """Closed days a re-score of the run days today-since .. today reads: (today - since - 14) .. (today - 1).
    `today` itself is the running day and is never written."""
    return today - timedelta(days=since + PRIOR_DAYS), today - _D


def replaceable(r) -> bool:
    """A row a candle may fill: written before 0012 with a known, non-zero cumulative (a zero cumulative is a seed: nothing
    traded up to that day, so the day adds $0 either way; a NULL cumulative is volume the onchain step marked unknown), or
    an estimate on a closed day (GeckoTerminal's rolling 24h, or a running-day candle that was never replaced)."""
    return r.estimated is True or (r.volume_usd_day is None and r.volume_usd_cum is not None and r.volume_usd_cum > 0)


def backfill_targets(conn, today: date, since: int) -> tuple[list[tuple[str, int, float]], dict]:
    """Pools of scored projects with a replaceable row in the window, as (pool_id, chain_id, 7-day volume), busiest first
    (7-day volume = the last 7 nightly pool_stats.vol_24h). Pools on chains GeckoTerminal does not list are counted, not
    returned."""
    start, end = backfill_window(today, since)
    c = pool_snapshots.c
    cond = sa.or_(c.estimated.is_(True), sa.and_(c.volume_usd_day.is_(None), c.volume_usd_cum.is_not(None), c.volume_usd_cum > 0))
    need: dict[tuple[str, int], int] = {}
    for r in conn.execute(sa.select(c.pool_id, c.chain_id, sa.func.count().label("n"))
                          .where(c.day >= start, c.day <= end, cond).group_by(c.pool_id, c.chain_id)):
        need[(r.pool_id, r.chain_id)] = r.n
    scored = {(r.pool_id, r.chain_id) for r in conn.execute(
        sa.select(pools.c.pool_id, pools.c.chain_id)
        .select_from(pools.join(hooks, (hooks.c.address == pools.c.hook_address) & (hooks.c.chain_id == pools.c.chain_id)))
        .where(hooks.c.project_id.is_not(None)))}
    vol7 = {(r.pool_id, r.chain_id): r.v or 0.0 for r in conn.execute(
        sa.select(pool_stats.c.pool_id, pool_stats.c.chain_id, sa.func.sum(pool_stats.c.vol_24h).label("v"))
        .where(pool_stats.c.day > today - 7 * _D, pool_stats.c.day <= today)
        .group_by(pool_stats.c.pool_id, pool_stats.c.chain_id))}
    info = {"rows": 0, "no_network": {}, "by_chain": {}}
    out = []
    for key, n in need.items():
        if key not in scored:
            continue
        cid = key[1]
        if cid not in NET_BY_CHAIN:
            info["no_network"][str(cid)] = info["no_network"].get(str(cid), 0) + 1
            continue
        info["rows"] += n
        slug = SLUG_BY_CHAIN[cid]
        info["by_chain"][slug] = info["by_chain"].get(slug, 0) + 1
        out.append((key[0], cid, vol7.get(key, 0.0)))
    out.sort(key=lambda t: (-t[2], t[1], t[0]))
    return out, info


def fill_pool(conn, pool_id: str, chain_id: int, candles: list[tuple[int, float]], start: date, end: date) -> tuple[int, int]:
    """Write one pool's candles to its replaceable rows in start..end (volume_usd_day, estimated False); a day without a
    candle had no trades ($0). A day with volume and no row gets one, carrying TVL and the legacy cumulatives from the
    nearest earlier row (TVL from the nearest later one when there is none), so it never changes a pool's known/unknown
    state. tx_count_day is left as it is: GeckoTerminal candles carry no trade counts. Returns (updated, inserted)."""
    vols: dict[date, float] = {}
    for ts, v in candles:
        vols[datetime.fromtimestamp(ts, tz=timezone.utc).date()] = v
    complete = len(candles) < OHLCV_LIMIT       # fewer than asked: the response is the pool's whole trading history
    first = min(vols)
    c = pool_snapshots.c
    rows = {r.day: r for r in conn.execute(sa.select(pool_snapshots).where(c.pool_id == pool_id, c.chain_id == chain_id))}
    updates, inserts = [], []
    d = start
    while d <= end:
        if complete or d >= first:
            v = vols.get(d, 0.0)
            r = rows.get(d)
            if r is not None:
                if replaceable(r):
                    updates.append({"p": pool_id, "c": chain_id, "d": d, "v": v})
            elif v > 0:
                earlier = [x for x in rows if x < d]
                later = [x for x in rows if x > d]
                base = rows[max(earlier)] if earlier else None
                tvl = base.tvl_usd if base is not None else (rows[min(later)].tvl_usd if later else None)
                inserts.append({"pool_id": pool_id, "chain_id": chain_id, "day": d, "tvl_usd": tvl,
                                "volume_usd_cum": base.volume_usd_cum if base is not None else 0.0,
                                "tx_count_cum": base.tx_count_cum if base is not None else None,
                                "volume_usd_day": v, "tx_count_day": None, "estimated": False})
        d += _D
    if updates:
        conn.execute(sa.update(pool_snapshots)
                     .where(c.pool_id == sa.bindparam("p"), c.chain_id == sa.bindparam("c"), c.day == sa.bindparam("d"))
                     .values(volume_usd_day=sa.bindparam("v"), estimated=False), updates)
    if inserts:
        upsert(conn, pool_snapshots, inserts, keys=["pool_id", "chain_id", "day"], update=[])
    return len(updates), len(inserts)


def backfill(engine, ctx: RunContext, since: int, max_pools: int | None = None, deadline: datetime | None = None,
             clock=_utcnow) -> dict:
    """Pull candles for every target pool (or the busiest max_pools), one committed transaction per pool, so a stop at any
    point keeps what is done and a rerun skips it. Stops between pools at `deadline`."""
    today = ctx.day
    start, end = backfill_window(today, since)
    with engine.connect() as conn:
        targets, info = backfill_targets(conn, today, since)
    take = targets if max_pools is None else targets[:max_pools]
    out = {"window": f"{start}..{end}", "pools_needing": len(targets), "no_network": sum(info["no_network"].values()),
           "done": 0, "failed": 0, "no_candles": 0, "beyond_cap": len(targets) - len(take), "left_at_deadline": 0,
           "rows_updated": 0, "rows_inserted": 0, "calls": 0}
    http = keyless_http(ctx)
    log.info("repair backfill-days: %d pools need candles for %s..%s; pulling %d", len(targets), start, end, len(take))
    for i, (pid, cid, _v) in enumerate(take):
        if deadline is not None and clock() >= deadline:
            out["left_at_deadline"] = len(take) - i
            log.warning("repair backfill-days: stop time %s reached, %d pools left; rerun to resume", deadline, len(take) - i)
            break
        candles = K.fetch_daily(http, NET_BY_CHAIN[cid], pid, limit=OHLCV_LIMIT)
        out["calls"] += 1
        if candles is None:
            out["failed"] += 1
            continue
        if not candles:
            out["no_candles"] += 1          # GeckoTerminal has no volume for it: leave the rows alone
            continue
        with engine.begin() as conn:
            u, n = fill_pool(conn, pid, cid, candles, start, end)
        out["done"] += 1
        out["rows_updated"] += u
        out["rows_inserted"] += n
        if (i + 1) % 50 == 0:
            log.info("repair backfill-days: %d/%d pools, %d rows updated, %d inserted, %d failed",
                     i + 1, len(take), out["rows_updated"], out["rows_inserted"], out["failed"])
    return out


def backfill_plan(conn, today: date, since: int, max_pools: int | None, spacing: float) -> tuple[list[str], dict]:
    start, end = backfill_window(today, since)
    targets, info = backfill_targets(conn, today, since)
    calls = len(targets) if max_pools is None else min(max_pools, len(targets))
    minutes = calls * spacing / 60
    chains = ", ".join(f"{k} {v}" for k, v in sorted(info["by_chain"].items(), key=lambda kv: -kv[1])) or "none"
    lines = [f"backfill-days: closed days {start}..{end} (the running day {today} is never written)",
             f"  pools needing candles: {len(targets)} ({info['rows']} rows); by chain: {chains}"]
    if info["no_network"]:
        lines.append(f"  skipped, chain not on GeckoTerminal: {sum(info['no_network'].values())} pools "
                     f"(chain ids {', '.join(sorted(info['no_network']))})")
    left = len(targets) - calls
    lines.append(f"  GeckoTerminal calls: {calls}{f' (--pools {max_pools}; {left} left for a rerun)' if left else ''}, "
                 f"{spacing:g} s apart: about {minutes:.0f} min, free")
    if not p8_rows_present(conn, today):
        lines.append("  WARNING: no per-day snapshot rows from the last day. Run this after a package 8 nightly; before "
                     "it, every on-chain pool still looks unfilled and the call count above is inflated.")
    return lines, {"calls": calls, "minutes": minutes}


def p8_rows_present(conn, today: date) -> bool:
    """A package 8 nightly ran within the last day: it writes estimated on every row it touches."""
    return conn.execute(sa.select(sa.func.count()).select_from(pool_snapshots).where(
        pool_snapshots.c.estimated.is_not(None), pool_snapshots.c.day >= today - _D)).scalar_one() > 0


# ------------------------------------------------------------------------------------------------
# rescore
# ------------------------------------------------------------------------------------------------
def rescore_days(conn, today: date, since: int | None = None, day: date | None = None) -> list[date]:
    """Run days that already have rows (a re-score never creates a day the nightly has not written), oldest first."""
    q = sa.select(project_daily.c.day).distinct()
    q = q.where(project_daily.c.day == day) if day is not None else q.where(
        project_daily.c.day >= today - timedelta(days=since), project_daily.c.day <= today)
    return sorted(r[0] for r in conn.execute(q))


def trending_top(conn, day: date, n: int = 10) -> list[str]:
    """Trending as the feed ranks it (signals.select_targets): score > 0, not spam, not wash; score, then TVL."""
    rows = conn.execute(sa.select(projects.c.slug, project_daily.c.score_trending, project_daily.c.tvl_usd)
                        .join(project_daily, project_daily.c.project_id == projects.c.id)
                        .where(project_daily.c.day == day, project_daily.c.is_spam.is_(False),
                               project_daily.c.is_wash.is_(False), project_daily.c.score_trending > 0)).all()
    return [r.slug for r in sorted(rows, key=lambda r: (-(r.score_trending or 0), -(r.tvl_usd or 0), r.slug))[:n]]


def backup_day(conn, day: date) -> int:
    """Copy the day's project_daily rows into project_daily_p9_backup once: the first copy is the pre-repair state."""
    b = db.project_daily_p9_backup
    if conn.execute(sa.select(sa.func.count()).select_from(b).where(b.c.day == day)).scalar_one():
        return 0
    conn.execute(sa.insert(b).from_select(_COLS, sa.select(*[project_daily.c[n] for n in _COLS])
                                          .where(project_daily.c.day == day)))
    # counted, not rowcount: psycopg reports -1 for INSERT ... SELECT
    return conn.execute(sa.select(sa.func.count()).select_from(b).where(b.c.day == day)).scalar_one()


def restore_day(conn, day: date) -> int | None:
    """Put the backed-up rows of `day` back (projects merged away since are skipped). None when there is no backup."""
    b = db.project_daily_p9_backup
    if not conn.execute(sa.select(sa.func.count()).select_from(b).where(b.c.day == day)).scalar_one():
        return None
    conn.execute(sa.delete(project_daily).where(project_daily.c.day == day))
    conn.execute(sa.insert(project_daily).from_select(_COLS, sa.select(*[b.c[n] for n in _COLS]).where(
        b.c.day == day, b.c.project_id.in_(sa.select(projects.c.id)))))
    return conn.execute(sa.select(sa.func.count()).select_from(project_daily).where(project_daily.c.day == day)).scalar_one()


def pending_note(engine, today: date, since: int) -> str:
    """Re-scoring before backfill-days has finished reads $0 for the days still unfilled: say so up front."""
    with engine.connect() as conn:
        n = len(backfill_targets(conn, today, max(min(since, MAX_SINCE), 0))[0])
    return (f"backfill-days: {n} pools still need candles; their unfilled days count as $0 in this re-score" if n
            else "backfill-days: complete for these days")


def onchain_coverage(conn) -> dict[int, date]:
    return {r.chain_id: r.first for r in conn.execute(
        sa.select(pool_daily_onchain.c.chain_id, sa.func.min(pool_daily_onchain.c.day).label("first"))
        .group_by(pool_daily_onchain.c.chain_id))}


def rescore(engine, days: list[date], now: datetime, echo=print) -> dict:
    """score.run per day, oldest first, each day in its own transaction after its backup. Prints one block per day."""
    per: dict[str, dict] = {}
    with engine.connect() as conn:
        cov = onchain_coverage(conn)
    for d in days:
        with engine.begin() as conn:
            before = trending_top(conn, d)
            backed = backup_day(conn, d)
            counts = score.run(conn, RunContext(day=d, now=now))
            after = trending_top(conn, d)
        came, went = [s for s in after if s not in before], [s for s in before if s not in after]
        wash = " ".join(f"{k[5:]}={v}" for k, v in counts.items() if k.startswith("wash_") and v)
        partial = sorted(SLUG_BY_CHAIN.get(c, str(c)) for c, first in cov.items() if first > d - 7 * _D)
        echo(f"{d}  projects {counts['projects']}  new {counts['new']}  trending {counts['trending']}  "
             f"wash {counts['wash']}{f' ({wash})' if wash else ''}  backed up {backed}")
        echo(f"            top 10: {len(after) - len(came)} kept" + (f"; in {', '.join(came)}" if came else "")
             + (f"; out {', '.join(went)}" if went else ""))
        if partial:
            echo(f"            on-chain history starts after this day's 7-day window opens on: {', '.join(partial)}")
        per[d.isoformat()] = {**counts, "backed_up": backed, "top10_in": came, "top10_out": went}
    return per


# ------------------------------------------------------------------------------------------------
# prose
# ------------------------------------------------------------------------------------------------
class CappedMeter(Meter):
    """The run's Meter plus an extra cap on what this repair run may spend (--max-usd / --prose-budget-usd)."""

    def __init__(self, engine, cap_usd: float | None, **kw):
        super().__init__(engine, **kw)
        self.cap_usd = cap_usd

    def check(self, provider: str, est_usd: float) -> None:
        if self.cap_usd is not None and self.run_usd + est_usd > self.cap_usd:
            log.warning("spend stop: %s call (~$%.4f) would take this repair past its $%.2f cap", provider, est_usd, self.cap_usd)
            raise BudgetExceeded(f"{provider}: repair cap ${self.cap_usd:.2f}")
        super().check(provider, est_usd)


def stored_parallel(enrichment) -> bool:
    par = (enrichment or {}).get("parallel")
    return bool(par) and "_failed" not in par


def cut_mid_word(line: str | None) -> bool:
    """The old code stored strip()[:200]: a line of exactly 200 chars that does not end a sentence was cut. cut_line's own
    cuts end in an ellipsis and are left alone."""
    return line is not None and len(line) == 200 and line[-1] not in ".!?…\"')"


def latest_day(conn) -> date | None:
    return conn.execute(sa.select(sa.func.max(project_daily.c.day))).scalar_one()


def what_line_targets(conn) -> list[tuple[object, str]]:
    """("missing") Parallel facts stored but no Claude line yet (enriched_at NULL, the budget stopped enrich), then ("cut")
    lines cut mid-word; each busiest first by the latest 7-day volume."""
    day = latest_day(conn)
    vol = {r.project_id: r.vol_7d or 0.0 for r in conn.execute(
        sa.select(project_daily.c.project_id, project_daily.c.vol_7d).where(project_daily.c.day == day))} if day else {}
    missing, cut = [], []
    for p in conn.execute(sa.select(projects).order_by(projects.c.id)):
        if p.enriched_at is None and stored_parallel(p.enrichment):
            missing.append(p)
        elif cut_mid_word(p.what_line):
            cut.append(p)
    key = lambda p: (-vol.get(p.id, 0.0), p.id)   # noqa: E731
    return [(p, "missing") for p in sorted(missing, key=key)] + [(p, "cut") for p in sorted(cut, key=key)]


def blurb_targets(conn, day: date, signals_limit: int, stale_before: datetime) -> tuple[list[tuple], dict]:
    """The signals step's targets for `day` (Trending top 20 then New; spam and wash never) whose blurb predates
    `stale_before`. A target whose row has no window_end was scored before package 8: re-score first."""
    rows = signals.select_targets(conn, RunContext(day=day, signals_limit=signals_limit))
    out, info = [], {"targets": len(rows), "fresh": 0, "old_row": 0}
    for p, daily in rows:
        if daily.window_end is None:
            info["old_row"] += 1
        elif p.blurb_at is not None and as_utc(p.blurb_at) >= stale_before:
            info["fresh"] += 1
        else:
            out.append((p, daily))
    return out, info


def what_line_estimates(conn, targets) -> list[float]:
    return [prose_estimate(what_line_prompt(enrich.what_context(p, enrich.hooks_of(conn, p.id), dict(p.enrichment or {}))))
            for p, _kind in targets]


def blurb_estimates(conn, targets) -> list[float]:
    return [prose_estimate(why_blurb_prompt(signals._blurb_ctx(conn, p, daily))) for p, daily in targets]


def write_what_lines(engine, claude, targets) -> dict:
    """One Claude call per target from stored facts only (no Parallel, no Blockscout); each line committed on its own.
    "missing" gets enrich's full write (line, category, links, enriched_at); "cut" gets the line only."""
    out = {"missing": 0, "cut": 0, "errors": 0}
    for p, kind in targets:
        enr = dict(p.enrichment or {})
        try:
            with engine.connect() as conn:
                hs = enrich.hooks_of(conn, p.id)
            res = claude.what_line(enrich.what_context(p, hs, enr))
        except BudgetExceeded:
            out["budget_stop"] = 1
            break
        except Exception as e:
            out["errors"] += 1
            log.warning("repair prose what_line project=%s failed: %s", p.slug, e)
            continue
        vals = enrich.what_values(p, res, enr) if kind == "missing" else {"what_line": enrich.cut_line(res["what_line"])}
        with engine.begin() as conn:
            conn.execute(sa.update(projects).values(**vals).where(projects.c.id == p.id))
        out[kind] += 1
    return out


def write_blurbs(engine, claude, targets, now: datetime) -> dict:
    out = {"blurbs": 0, "errors": 0}
    for p, daily in targets:
        try:
            with engine.connect() as conn:
                bctx = signals._blurb_ctx(conn, p, daily)
            blurb = claude.why_blurb(bctx)
        except BudgetExceeded:
            out["budget_stop"] = 1
            break
        except Exception as e:
            out["errors"] += 1
            log.warning("repair prose blurb project=%s failed: %s", p.slug, e)
            continue
        with engine.begin() as conn:
            conn.execute(sa.update(projects).values(why_blurb=blurb.strip()[:400], blurb_at=now).where(projects.c.id == p.id))
        out["blurbs"] += 1
    return out


def typical_nightly_usd(engine, now: datetime) -> float:
    """Mean paid spend per day over the last 7 days, repair runs left out."""
    with engine.connect() as c:
        total = c.execute(sa.select(sa.func.coalesce(sa.func.sum(api_spend.c.usd), 0.0)).where(
            api_spend.c.ts >= now - timedelta(days=7), sa.not_(api_spend.c.step.like("repair%")))).scalar_one()
    return float(total or 0.0) / 7


def _cents_up(v: float) -> float:
    return math.ceil(round(v * 100, 6)) / 100


def budget_lines(meter: Meter, worst: float, nightly: float, flag: str) -> list[str]:
    """Two ways to fit `worst` under the daily budget: spread it over days after the nightly, or raise the budget for one
    run. The extra cap (flag) never raises a budget; it only stops this run earlier."""
    b = meter.budgets
    today, d30 = meter.spent(today=True), meter.spent(days=30)
    per_day = max(b.daily - nightly, 0.0)
    when = ("never: the nightly uses the whole daily budget" if per_day <= 0
            else f"about {max(math.ceil(worst / per_day), 1)} day(s)")
    lines = [f"  spent today ${today:.2f} of ${b.daily:.2f}; last 30 days ${d30:.2f} of ${b.monthly:.2f}",
             f"  option A, no budget change: run after the nightly (last 7 days: ${nightly:.2f}/day); about ${per_day:.2f} a "
             f"day is left, so the worst case takes {when} (one {flag} run per day; reruns skip what is done)"]
    daily = max(b.daily, _cents_up(today + worst))
    monthly = _cents_up(d30 + worst)
    lines.append(f"  option B, one run: the owner sets SPEND_DAILY_USD={daily:.2f}"
                 + (f" and SPEND_MONTHLY_USD={monthly:.2f}" if monthly > b.monthly else "")
                 + f" on the pipeline service for this run only, passes {flag} {_cents_up(worst):.2f}, "
                   "and restores the default before 06:00 UTC")
    return lines


def prose_plan(engine, meter: Meter, now: datetime, what: bool, blurbs: bool, limit: int | None, signals_limit: int,
               stale_before: datetime, flag: str = "--max-usd") -> tuple[list[str], float]:
    lines, worst = [], 0.0
    with engine.connect() as conn:
        if what:
            t = what_line_targets(conn)
            t = t[:limit] if limit is not None else t
            est = what_line_estimates(conn, t)
            n_missing = sum(1 for _, k in t if k == "missing")
            w = sum(est)
            worst += w
            lines.append(f"prose what-lines: {n_missing} missing (stored Parallel facts, no Claude line), {len(t) - n_missing} "
                         f"cut mid-word; worst case ${w:.2f} (max ${max(est, default=0):.4f} a call), Claude only")
        if blurbs:
            day = latest_day(conn)
            t, info = blurb_targets(conn, day, signals_limit, stale_before) if day else ([], {"targets": 0, "fresh": 0, "old_row": 0})
            t = t[:limit] if limit is not None else t
            est = blurb_estimates(conn, t)
            w = sum(est)
            worst += w
            lines.append(f"prose blurbs: {len(t)} of {info['targets']} signals targets on {day} predate {stale_before:%Y-%m-%d %H:%M} UTC "
                         f"({info['fresh']} fresh, {info['old_row']} on a row scored before package 8: re-score first); "
                         f"worst case ${w:.2f}, Claude only, no Exa")
    lines += budget_lines(meter, worst, typical_nightly_usd(engine, now), flag)
    return lines, worst


def prose(engine, ctx: RunContext, what: bool, blurbs: bool, limit: int | None, signals_limit: int,
          stale_before: datetime) -> dict:
    claude = ctx.client("claude")
    if claude is None:
        raise RuntimeError("no ANTHROPIC_API_KEY")
    out: dict = {}
    if what:
        with engine.connect() as conn:
            t = what_line_targets(conn)
        out["what_lines"] = write_what_lines(engine, claude, t[:limit] if limit is not None else t)
    if blurbs and not out.get("what_lines", {}).get("budget_stop"):
        with engine.connect() as conn:
            day = latest_day(conn)
            t, info = blurb_targets(conn, day, signals_limit, stale_before) if day else ([], {})
        out["blurbs"] = {**write_blurbs(engine, claude, t[:limit] if limit is not None else t, ctx.now), **info}
    ctx.meter.flush()
    out["usd"] = round(ctx.meter.run_usd, 4)
    return out
