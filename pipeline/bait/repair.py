"""Package 9: one-off data repair. `python -m bait repair <command>`; see pipeline/README.md "Repair".

backfill-days  GeckoTerminal daily candles (free, paced) -> pool_snapshots.volume_usd_day for closed days, where a row has
               no per-day value (written before 0012) or only an estimate. One call per pool, busiest first; resumable.
rescore        score.run for each stored run day in the range, oldest first, after a one-time backup of that day's rows.
prose          Claude only, paid: what_line for projects whose line the old code cut mid-word at 200 chars; why_blurb
               for the signals targets whose blurb predates the package 8 window text. Every call passes the spend Meter; --max-usd caps this run on top of it.
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
# /api/health reads the latest receipt and expects the nightly's day. A repair receipt carries the latest nightly's day,
# never its own; with no nightly receipt at all it carries this, which health reports as stale.
NO_NIGHTLY_DAY = date(1970, 1, 1)
MORNING = (dtime(5, 30), dtime(8, 0))   # from the stop time before the 06:00 nightly to health's 08:00 deadline
NIGHTLY_AT = dtime(6, 0)                 # the pipeline cron; a repair stops before it (--stop-at) and never starts near it
NIGHTLY_GRACE = timedelta(minutes=30)    # a nightly that started less than this ago is still "the next one"
EXIT_INCOMPLETE = 6                      # stopped at --stop-at: what is done is kept, a rerun resumes
BACKFILL_FAIL_STREAK = 10                # failed GeckoTerminal calls in a row before backfill-days gives up (resumable)

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
    traded up to that day, so the day adds $0 either way), or an estimate on a closed day (GeckoTerminal's rolling 24h, or
    a running-day candle that was never replaced). Never a row whose cumulative is NULL: volume the onchain step marked
    unknown stays unknown."""
    if r.volume_usd_cum is None:
        return False
    return r.estimated is True or (r.volume_usd_day is None and r.volume_usd_cum > 0)


def backfill_targets(conn, today: date, since: int) -> tuple[list[tuple[str, int, float]], dict]:
    """Pools of scored projects with a replaceable row in the window, as (pool_id, chain_id, 7-day volume), busiest first
    (7-day volume = the last 7 nightly pool_stats.vol_24h). Pools on chains GeckoTerminal does not list are counted, not
    returned."""
    start, end = backfill_window(today, since)
    c = pool_snapshots.c
    # an estimate before the pool's first traded candle-backed day is one fill_pool keeps: never a target again
    s2 = pool_snapshots.alias("s2")
    first_traded = (sa.select(sa.func.min(s2.c.day)).where(s2.c.pool_id == c.pool_id, s2.c.chain_id == c.chain_id,
                                                          s2.c.estimated.is_(False), s2.c.volume_usd_day > 0)
                    .scalar_subquery())
    estimate = sa.and_(c.estimated.is_(True), sa.or_(first_traded.is_(None), c.day > first_traded))
    cond = sa.and_(c.volume_usd_cum.is_not(None), sa.or_(estimate, sa.and_(c.volume_usd_day.is_(None), c.volume_usd_cum > 0)))
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
                # before the first candle only a missing value becomes $0; an estimate there is kept
                if replaceable(r) and (d >= first or r.volume_usd_day is None):
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
             clock=None) -> dict:
    """Pull candles for every target pool (or the busiest max_pools), one committed transaction per pool, so a stop at any
    point keeps what is done and a rerun skips it. Stops between pools at `deadline`."""
    clock = clock or _utcnow
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
    streak = 0                              # failed calls in a row: a rate-limit storm is not worth the night
    for i, (pid, cid, _v) in enumerate(take):
        if deadline is not None and clock() >= deadline:
            out["left_at_deadline"] = len(take) - i
            log.warning("repair backfill-days: stop time %s reached, %d pools left; rerun to resume", deadline, len(take) - i)
            break
        candles = K.fetch_daily(http, NET_BY_CHAIN[cid], pid, limit=OHLCV_LIMIT)
        out["calls"] += 1
        if candles is None:
            out["failed"] += 1
            streak += 1
            if streak >= BACKFILL_FAIL_STREAK:
                out["left_at_failures"] = len(take) - i - 1
                log.warning("repair backfill-days: %d GeckoTerminal calls failed in a row, stopping with %d pools left; "
                            "rerun to resume", streak, len(take) - i - 1)
                break
        elif not candles:
            out["no_candles"] += 1          # GeckoTerminal has no volume for it: leave the rows alone
            streak = 0
        else:
            streak = 0
            with engine.begin() as conn:
                u, n = fill_pool(conn, pid, cid, candles, start, end)
            out["done"] += 1
            out["rows_updated"] += u
            out["rows_inserted"] += n
        if (i + 1) % 50 == 0:               # every outcome counts: a silent run is a run nobody can judge
            log.info("repair backfill-days: %d/%d pools, %d filled, %d no candles, %d failed, %d rows updated, %d inserted",
                     i + 1, len(take), out["done"], out["no_candles"], out["failed"], out["rows_updated"], out["rows_inserted"])
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


def backup_days(conn, day: date | None = None) -> list[date]:
    """The days project_daily_p9_backup holds (or just `day`): what --restore puts back, whatever today's date is."""
    b = db.project_daily_p9_backup
    q = sa.select(b.c.day).distinct()
    if day is not None:
        q = q.where(b.c.day == day)
    return sorted(r[0] for r in conn.execute(q))


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


def rescore(engine, days: list[date], now: datetime, echo=print, deadline: datetime | None = None, clock=None) -> dict:
    """score.run per day, oldest first, each day in its own transaction after its backup; stops between days at
    `deadline`. Prints one block per day. Returns {"days": {day: counts...}, "left_at_deadline": n}."""
    clock = clock or _utcnow
    per: dict[str, dict] = {}
    with engine.connect() as conn:
        cov = onchain_coverage(conn)
    for i, d in enumerate(days):
        if deadline is not None and clock() >= deadline:
            return {"days": per, "left_at_deadline": len(days) - i}
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
    return {"days": per, "left_at_deadline": 0}


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


def cut_mid_word(line: str | None) -> bool:
    """The old code stored strip()[:200]: a line of exactly 200 chars that does not end a sentence was cut. cut_line's own
    cuts end in an ellipsis and are left alone."""
    return line is not None and len(line) == 200 and line[-1] not in ".!?…\"')"


def latest_day(conn) -> date | None:
    return conn.execute(sa.select(sa.func.max(project_daily.c.day))).scalar_one()


def what_line_targets(conn) -> list[tuple[object, str]]:
    """("cut") lines cut mid-word, busiest first by the latest 7-day volume. The "missing" kind (Parallel facts paid for,
    no Claude line yet) went with Parallel on 2026-10-24: a project never enriched waits for the nightly enrich."""
    day = latest_day(conn)
    vol = {r.project_id: r.vol_7d or 0.0 for r in conn.execute(
        sa.select(project_daily.c.project_id, project_daily.c.vol_7d).where(project_daily.c.day == day))} if day else {}
    cut = [p for p in conn.execute(sa.select(projects).order_by(projects.c.id)) if cut_mid_word(p.what_line)]
    return [(p, "cut") for p in sorted(cut, key=lambda p: (-vol.get(p.id, 0.0), p.id))]


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


def _past(deadline: datetime | None, clock) -> bool:
    return deadline is not None and (clock or _utcnow)() >= deadline


def write_what_lines(engine, claude, targets, deadline: datetime | None = None, clock=None) -> dict:
    """One Claude call per target from stored facts only (no Blockscout); each line committed on its own, the line only.
    Stops between calls at `deadline`."""
    out = {"cut": 0, "errors": 0}
    for i, (p, kind) in enumerate(targets):
        if _past(deadline, clock):
            out["left_at_deadline"] = len(targets) - i
            break
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
        with engine.begin() as conn:
            conn.execute(sa.update(projects).values(what_line=enrich.cut_line(res["what_line"])).where(projects.c.id == p.id))
        out[kind] += 1
    return out


def write_blurbs(engine, claude, targets, now: datetime, deadline: datetime | None = None, clock=None) -> dict:
    out = {"blurbs": 0, "errors": 0}
    for i, (p, daily) in enumerate(targets):
        if _past(deadline, clock):
            out["left_at_deadline"] = len(targets) - i
            break
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
            w = sum(est)
            worst += w
            lines.append(f"prose what-lines: {len(t)} cut mid-word; worst case ${w:.2f} (max ${max(est, default=0):.4f} a call), "
                         "Claude only")
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
          stale_before: datetime, deadline: datetime | None = None, clock=None) -> dict:
    claude = ctx.client("claude")
    if claude is None:
        raise RuntimeError("no ANTHROPIC_API_KEY")
    out: dict = {}
    if what:
        with engine.connect() as conn:
            t = what_line_targets(conn)
        out["what_lines"] = write_what_lines(engine, claude, t[:limit] if limit is not None else t, deadline, clock)
    stopped = out.get("what_lines", {})
    if blurbs and not stopped.get("budget_stop") and not stopped.get("left_at_deadline"):
        with engine.connect() as conn:
            day = latest_day(conn)
            t, info = blurb_targets(conn, day, signals_limit, stale_before) if day else ([], {})
        out["blurbs"] = {**write_blurbs(engine, claude, t[:limit] if limit is not None else t, ctx.now, deadline, clock),
                         **info}
    ctx.meter.flush()
    out["usd"] = round(ctx.meter.run_usd, 4)
    return out


# ------------------------------------------------------------------------------------------------
# verify
# ------------------------------------------------------------------------------------------------
def verify_sample(conn, day: date, sample: int, seed: int) -> list[tuple[str, object]]:
    """Top trending, top new, the busiest wash-flagged project, then random projects with pools (seeded, so a rerun on
    the same day picks the same ones). Spam is never picked."""
    rows = conn.execute(sa.select(projects.c.id, projects.c.slug, project_daily)
                        .join(project_daily, project_daily.c.project_id == projects.c.id)
                        .where(project_daily.c.day == day, project_daily.c.is_spam.is_(False))
                        .order_by(projects.c.id)).all()
    live = [r for r in rows if not r.is_wash]
    picks: list[tuple[str, object]] = []

    def take(label, cands):
        for r in cands:
            if all(r.id != q.id for _, q in picks):
                picks.append((label, r))
                return

    take("top trending", sorted([r for r in live if (r.score_trending or 0) > 0], key=lambda r: (-(r.score_trending or 0), -(r.tvl_usd or 0))))
    take("top new", sorted([r for r in live if r.is_new], key=lambda r: -(r.tvl_usd or 0)))
    take("wash", sorted([r for r in rows if r.is_wash], key=lambda r: -(r.vol_7d or 0)))
    picks = picks[:sample]
    rest = [r for r in rows if (r.pools_n or 0) > 0 and all(r.id != q.id for _, q in picks)]
    picks += [("random", r) for r in random.Random(seed).sample(rest, min(len(rest), max(sample - len(picks), 0)))]
    return picks


def _pct(bait: float, gt: float) -> str:
    if gt == 0:
        return "0" if bait == 0 else "n/a"
    return f"{(bait - gt) / gt * 100:+.1f}%"


def _usd(v) -> str:
    return "–" if v is None else f"${v:,.0f}"


def verify(engine, ctx: RunContext, sample: int, per_project: int, seed: int | None, echo=print) -> dict:
    with engine.connect() as conn:
        day = latest_day(conn)
        if day is None:
            echo("verify: no project_daily rows")
            return {"projects": 0}
        end = day - _D
        windows = (("24h", end, end), ("7d", end - 6 * _D, end), ("prev 7d", end - 13 * _D, end - 7 * _D))
        picks = verify_sample(conn, day, sample, day.toordinal() if seed is None else seed)
        plan = []
        for label, r in picks:
            ps = conn.execute(sa.select(pools.c.pool_id, pools.c.chain_id).select_from(
                pools.join(hooks, (hooks.c.address == pools.c.hook_address) & (hooks.c.chain_id == pools.c.chain_id)))
                .where(hooks.c.project_id == r.id)).all()
            per_pool = {}
            for p in ps:
                if p.chain_id not in NET_BY_CHAIN:
                    continue
                snap = {s.day: s.volume_usd_day for s in conn.execute(sa.select(pool_snapshots.c.day, pool_snapshots.c.volume_usd_day)
                        .where(pool_snapshots.c.pool_id == p.pool_id, pool_snapshots.c.chain_id == p.chain_id,
                               pool_snapshots.c.day >= windows[2][1], pool_snapshots.c.day <= end))}
                per_pool[(p.pool_id, p.chain_id)] = snap
            busiest = sorted(per_pool, key=lambda k: -sum(v or 0.0 for d, v in per_pool[k].items() if d >= windows[1][1]))[:per_project]
            plan.append((label, r, len(ps), {k: per_pool[k] for k in busiest}))
    http = keyless_http(ctx)
    out = {"day": day.isoformat(), "projects": len(plan), "pools": 0, "calls": 0, "failed": 0}
    echo(f"Bait vs GeckoTerminal, run day {day}: closed UTC days ending {end}. Bait (checked pools) and GeckoTerminal sum the "
         f"same pools' daily volume; Bait stored is the project row (all pools).")
    echo("")
    echo("| project | picked as | pools checked | window | Bait stored | Bait (checked pools) | GeckoTerminal | delta |")
    echo("|---|---|---|---|---|---|---|---|")
    for label, r, n_pools, chosen in plan:
        gt: dict[date, float] = {}
        ok = 0
        for (pid, cid) in chosen:
            candles = K.fetch_daily(http, NET_BY_CHAIN[cid], pid, limit=OHLCV_LIMIT)
            out["calls"] += 1
            if candles is None:
                out["failed"] += 1
                continue
            ok += 1
            for ts, v in candles:
                d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
                gt[d] = gt.get(d, 0.0) + v
        out["pools"] += ok
        stored = {"24h": r.vol_24h, "7d": r.vol_7d, "prev 7d": r.vol_7d_prev}
        for name, a, b in windows:
            bait = sum(v or 0.0 for snap in chosen.values() for d, v in snap.items() if a <= d <= b)
            g = sum(v for d, v in gt.items() if a <= d <= b)
            echo(f"| {r.slug} | {label} | {ok}/{n_pools} | {name} | {_usd(stored[name])} | {_usd(bait)} | {_usd(g)} | {_pct(bait, g)} |")
    return out


# ------------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------------
def add_parsers(sub) -> None:
    def common(p):
        p.add_argument("--database-url", default=None)

    b = sub.add_parser("backfill-days", help="free: GeckoTerminal daily candles into pool_snapshots for closed days")
    b.add_argument("--since", type=int, default=14, help=f"run days to cover (1..{MAX_SINCE}); writes since + 14 closed days")
    b.add_argument("--pools", type=int, default=None, help="pull at most N pools, busiest first (default all)")
    b.add_argument("--stop-at", default="05:30", help="UTC HH:MM before 06:00: stop between pools here so the nightly gets the lock")
    b.add_argument("--dry-run", action="store_true")
    common(b)
    r = sub.add_parser("rescore", help="free: score.run for stored run days, oldest first, after a backup")
    r.add_argument("--since", type=int, default=14, help=f"run days today-N .. today that have rows (1..{MAX_SINCE})")
    r.add_argument("--day", type=date.fromisoformat, default=None, help="one run day instead of --since")
    r.add_argument("--restore", action="store_true",
                   help="put back every day project_daily_p9_backup holds (only --day when given); ignores --since")
    r.add_argument("--stop-at", default="05:30", help="UTC HH:MM before 06:00: stop between days here")
    r.add_argument("--dry-run", action="store_true")
    common(r)
    p = sub.add_parser("prose", help="PAID: Claude what_line / why_blurb rewrites from stored facts")
    p.add_argument("--what-lines", action="store_true")
    p.add_argument("--blurbs", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="at most N projects per kind, busiest first")
    p.add_argument("--max-usd", type=float, default=None,
                   help="required with --yes: cap on this run's spend, on top of the daily/30-day budgets")
    p.add_argument("--signals-limit", type=int, default=50)
    p.add_argument("--stop-at", default="05:30", help="UTC HH:MM before 06:00: stop between calls here")
    p.add_argument("--stale-before", type=datetime.fromisoformat, default=P8_MERGED_AT,
                   help="blurbs written before this UTC time are rewritten (default: package 8's merge)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--yes", action="store_true")
    common(p)
    v = sub.add_parser("verify", help="free: Bait vs GeckoTerminal for a few projects, as a markdown table")
    v.add_argument("--sample", type=int, default=5)
    v.add_argument("--pools-per-project", type=int, default=3)
    v.add_argument("--seed", type=int, default=None, help="random picks; default: the run day, so reruns agree")
    common(v)
    a = sub.add_parser("all", help="the whole plan (--dry-run), or backfill-days, rescore, verify (and prose with a budget)")
    a.add_argument("--since", type=int, default=14)
    a.add_argument("--pools", type=int, default=None)
    a.add_argument("--stop-at", default="05:30", help="UTC HH:MM before 06:00: every step stops here")
    a.add_argument("--sample", type=int, default=5)
    a.add_argument("--limit", type=int, default=None, help="prose: at most N projects per kind")
    a.add_argument("--prose-budget-usd", type=float, default=None, help="run prose too, spending at most this")
    ga = a.add_mutually_exclusive_group(required=True)
    ga.add_argument("--dry-run", action="store_true")
    ga.add_argument("--yes", action="store_true")
    common(a)


def _stop_time(hhmm: str) -> dtime:
    h, m = (int(x) for x in hhmm.split(":"))
    return dtime(h, m)


def next_nightly(now: datetime) -> datetime:
    """The 06:00 UTC run a repair must leave room for: today's until 30 minutes after it starts, then tomorrow's."""
    n = datetime.combine(now.date(), NIGHTLY_AT, tzinfo=timezone.utc)
    return n if now < n + NIGHTLY_GRACE else n + _D


def deadline_at(hhmm: str | None, now: datetime) -> datetime | None:
    """--stop-at on the day of the next nightly, so a start after it never rolls the deadline to tomorrow while the lock
    is held through 06:00 (main refuses that start instead). None: no stop time."""
    if not hhmm:
        return None
    n = next_nightly(now)
    stop = datetime.combine(n.date(), _stop_time(hhmm), tzinfo=timezone.utc)
    return stop if stop <= n else stop - _D


def in_blackout(hhmm: str | None, now: datetime) -> bool:
    """now is between the stop time and 30 minutes after the nightly starts."""
    stop = deadline_at(hhmm, now)
    return stop is not None and stop <= now


def _receipt(engine, name: str, started: datetime, rc: int, out: dict) -> None:
    """/api/health reads the latest receipt as the nightly's, so a repair receipt never reads better than the latest
    nightly: its day (a repair dated today would pass for a nightly that never ran), its exit code unless the repair
    itself failed, and its soft_errors (a restore after a failed nightly, or a verify after a nightly with 10+ soft
    errors, must not turn health green). The repair's own misses stay in its steps."""
    last = _latest_nightly(engine)
    db.write_receipt(engine, day=last.day if last is not None else NO_NIGHTLY_DAY, started_at=started,
                     finished_at=_utcnow(), exit_code=rc or (last.exit_code if last is not None else 0),
                     soft_errors=last.soft_errors if last is not None else 0, steps={name: out},
                     code_sha=os.environ.get("RAILWAY_GIT_COMMIT_SHA"), notes=f"repair {name}")


def _latest_nightly(engine):
    with engine.connect() as c:
        return c.execute(sa.select(run_receipts).where(run_receipts.c.notes.is_(None))
                         .order_by(run_receipts.c.finished_at.desc()).limit(1)).first()


def _failed_nightly(engine):
    r = _latest_nightly(engine)
    return r if r is not None and r.exit_code != 0 else None


def _waiting_for_nightly(engine, now: datetime) -> bool:
    """05:30-08:00 UTC with no nightly receipt for today: the nightly is due or running, and a repair must not hold the
    lock it needs or write a receipt before it."""
    if not MORNING[0] <= now.time() < MORNING[1]:
        return False
    with engine.connect() as c:
        return not c.execute(sa.select(sa.func.count()).select_from(run_receipts).where(
            run_receipts.c.notes.is_(None), run_receipts.c.day == now.date())).scalar_one()


def _spacing() -> float:
    return float(os.environ.get("KEYLESS_SPACING", "10"))


def _step(engine, name: str, fn) -> tuple[int, dict]:
    started = _utcnow()
    rc, out = 1, {}
    try:
        out = fn()
        rc = 0
        return rc, out
    except Exception as e:
        log.exception("repair %s failed", name)
        out = {"failed": type(e).__name__}
        return rc, out
    finally:
        _receipt(engine, name, started, rc, out)


def main(args: argparse.Namespace, engine) -> int:
    cmd, now = args.repair_cmd, _utcnow()
    today = now.date()
    since = getattr(args, "since", None)
    if since is not None and not 1 <= since <= MAX_SINCE:
        log.error("--since must be 1..%d: %d candles cover since + %d closed days", MAX_SINCE, OHLCV_LIMIT, PRIOR_DAYS)
        return 2
    if cmd == "prose" and not (args.what_lines or args.blurbs):
        log.error("prose: pass --what-lines and/or --blurbs")
        return 2
    if cmd == "prose" and args.yes and args.max_usd is None:
        log.error("prose --yes: pass --max-usd X (the most this run may spend; see the dry run's worst case)")
        return 2
    stop_at = getattr(args, "stop_at", None)
    if stop_at:
        try:
            ok = _stop_time(stop_at) < NIGHTLY_AT
        except ValueError:
            ok = False
        if not ok:
            log.error("--stop-at must be HH:MM UTC before %s (the nightly), or empty for none", NIGHTLY_AT.strftime("%H:%M"))
            return 2
    if getattr(args, "dry_run", False):
        return _dry_run(cmd, args, engine, now)
    if in_blackout(stop_at, now):
        log.error("%s UTC is between --stop-at %s and 30 minutes after the 06:00 nightly: start the repair after it",
                  now.strftime("%H:%M"), stop_at)
        return EXIT_PRECONDITION
    lock = db.try_run_lock(engine)
    if lock is None:
        log.error("another bait run holds the run lock (the nightly?); repair not started")
        return 3
    try:
        if _waiting_for_nightly(engine, now):
            log.error("%s UTC and no nightly receipt for %s yet: the nightly comes first; run the repair after it",
                      now.strftime("%H:%M"), today)
            return EXIT_PRECONDITION
        restore = cmd == "rescore" and args.restore     # undoing a re-score is always allowed
        bad = None if restore else _failed_nightly(engine)
        if bad is not None:
            log.error("the last nightly receipt (day %s) exited %s; fix or rerun the nightly first, so a repair receipt "
                      "never hides it from /api/health", bad.day, bad.exit_code)
            return EXIT_PRECONDITION
        if cmd in ("backfill-days", "all") or (cmd == "rescore" and not restore):
            with engine.connect() as conn:
                if not p8_rows_present(conn, today):
                    log.error("no per-day snapshot rows from the last day: run after a package 8 nightly (before it, every "
                              "window would read $0)")
                    return EXIT_PRECONDITION
        return _run(cmd, args, engine, now)
    finally:
        lock.close()


def _ctx(now: datetime, meter=None) -> RunContext:
    return RunContext(day=now.date(), now=now, meter=meter)


def _incomplete(out: dict) -> bool:
    """A step that stopped at the deadline: say so and let the caller exit EXIT_INCOMPLETE."""
    outs = [out] + [v for v in out.values() if isinstance(v, dict)]
    if any(o.get("left_at_failures") for o in outs):
        print("INCOMPLETE: rerun to resume (stopped after %d failed GeckoTerminal calls in a row)" % BACKFILL_FAIL_STREAK)
        return True
    left = any(o.get("left_at_deadline") for o in outs)
    if left:
        print("INCOMPLETE: rerun to resume (stopped at --stop-at before the 06:00 UTC nightly)")
    return bool(left)


def _run(cmd: str, args, engine, now: datetime) -> int:
    """One stop time for the whole command, checked between pools, days and calls, and before every step of `all`."""
    today = now.date()
    deadline = deadline_at(getattr(args, "stop_at", None), now)
    clock = lambda: _utcnow()   # noqa: E731  (late-bound: tests move the clock)

    def finish(rc: int, out: dict) -> int:
        return EXIT_INCOMPLETE if rc == 0 and _incomplete(out) else rc

    if cmd == "backfill-days":
        return finish(*_step(engine, "backfill-days", lambda: backfill(engine, _ctx(now), args.since, args.pools, deadline, clock)))
    if cmd == "rescore":
        with engine.connect() as conn:
            days = backup_days(conn, args.day) if args.restore else rescore_days(conn, today, args.since, args.day)
        if args.restore:
            def go():
                done = {}
                for d in days:
                    with engine.begin() as conn:
                        done[d.isoformat()] = restore_day(conn, d)
                    print(f"{d}  restored {done[d.isoformat()]} rows" if done[d.isoformat()] is not None else f"{d}  no backup")
                return {"restored": done}
            return _step(engine, "rescore-restore", go)[0]
        print(pending_note(engine, today, args.since if args.day is None else (today - args.day).days))
        return finish(*_step(engine, "rescore", lambda: rescore(engine, days, now, deadline=deadline, clock=clock)))
    if cmd == "verify":
        return _step(engine, "verify", lambda: verify(engine, _ctx(now), args.sample, args.pools_per_project, args.seed))[0]
    if cmd == "prose":
        meter = CappedMeter(engine, args.max_usd)
        meter.step = "repair-prose"
        return finish(*_step(engine, "prose", lambda: prose(engine, _ctx(now, meter), args.what_lines, args.blurbs, args.limit,
                                                            args.signals_limit, as_utc(args.stale_before), deadline, clock)))

    # all: the free steps, then prose only within an explicit budget; the stop time is checked before every step
    def late() -> bool:
        if _past(deadline, clock):
            print("INCOMPLETE: rerun to resume (stopped at --stop-at before the 06:00 UTC nightly)")
            return True
        return False

    rc = finish(*_step(engine, "backfill-days", lambda: backfill(engine, _ctx(now), args.since, args.pools, deadline, clock)))
    if rc:
        return rc
    if late():
        return EXIT_INCOMPLETE
    with engine.connect() as conn:
        days = rescore_days(conn, today, args.since)
    rc = finish(*_step(engine, "rescore", lambda: rescore(engine, days, now, deadline=deadline, clock=clock)))
    if rc:
        return rc
    if late():
        return EXIT_INCOMPLETE
    rc = _step(engine, "verify", lambda: verify(engine, _ctx(now), args.sample, 3, None))[0]
    if rc or args.prose_budget_usd is None:
        if not rc:
            print("prose not run: pass --prose-budget-usd X after the owner's go (see `repair all --dry-run`)")
        return rc
    if late():
        return EXIT_INCOMPLETE
    meter = CappedMeter(engine, args.prose_budget_usd)
    meter.step = "repair-prose"
    return finish(*_step(engine, "prose", lambda: prose(engine, _ctx(now, meter), True, True, args.limit, 50, P8_MERGED_AT,
                                                        deadline, clock)))


def _dry_run(cmd: str, args, engine, now: datetime) -> int:
    today = now.date()
    lines: list[str] = []
    with engine.connect() as conn:
        if cmd in ("backfill-days", "all"):
            lines += backfill_plan(conn, today, args.since, args.pools, _spacing())[0]
        if cmd == "rescore" and args.restore:
            days = backup_days(conn, args.day)
            print(f"restore: {len(days)} backed-up day(s) {days[0]} .. {days[-1]}" if days else "restore: no backed-up days")
            return 0
        if cmd in ("rescore", "all"):
            days = rescore_days(conn, today, args.since, getattr(args, "day", None))
            lines.append(f"rescore: {len(days)} stored run day(s) {', '.join(map(str, days)) or '-'}; free, seconds per day; "
                         "each day is backed up once before its first re-score")
        if cmd == "rescore":
            lines.append(pending_note(engine, today, args.since))
        if cmd == "all":
            sample = args.sample
            lines.append(f"verify: up to {sample * 3} GeckoTerminal calls ({sample} projects x 3 pools), about "
                         f"{sample * 3 * _spacing() / 60:.0f} min, free")
    if cmd in ("prose", "all"):
        meter = CappedMeter(engine, None)
        what, blurbs = (True, True) if cmd == "all" else (args.what_lines, args.blurbs)
        stale = as_utc(getattr(args, "stale_before", P8_MERGED_AT))
        flag = "--prose-budget-usd" if cmd == "all" else "--max-usd"
        lines += prose_plan(engine, meter, now, what, blurbs, args.limit, getattr(args, "signals_limit", 50), stale, flag)[0]
    if cmd == "all":
        lines.append("`repair all --yes` runs backfill-days, rescore and verify; prose only with --prose-budget-usd.")
    print("\n".join(lines))
    return 0
