"""Step 7 (signals): for New ∪ Trending (top N): GitHub activity and the alive signal, then Claude why_blurb when
signals changed since blurb_at, or when the stored blurb is empty or fails the prose checks against tonight's context
(once it is ENRICH_RETRY_DAYS old). Projects outside the targets whose stored blurb is due the same way get the same
treatment afterwards, busiest first, up to BLURB_RECHECK_MAX (20) a night: a blurb stays on its page after the project
leaves the targets. First, for every project, published audit links that are gone (audit_links.py): that row is for
the "Audit linked" badge, so it is never sent as a signal and never makes a blurb stale (FROZEN_KINDS); it only keeps
dead links out of the blurb's audit_urls, as out of the badge.

The per-project Exa receipts search was removed on 2026-10-24 (Exa now serves only the radar step). The receipts table
and the "exa" / "nansen" signal rows written before then stay stored; they no longer change, so they are left out of
the blurb's signals and out of `alive`. `alive` is null (unknown) without a GitHub reading, and then left out of the
blurb's signals too."""
from __future__ import annotations

import re

import json
from datetime import datetime, timedelta

import sqlalchemy as sa

from ..db import as_utc, project_daily, projects, receipts, savepoint, signals, upsert
from ..prose_checks import blurb_problems
from ..rules import is_alive
from ..audit_links import dead_links
from . import RunContext, log
from .enrich import hook_facts, hooks_of, retry_days

# Signal kinds that are no blurb input: never in the prompt, and a change to one never makes a blurb stale. exa and
# nansen are frozen (no step writes them since 2026-10-24 and 2026-10-08). audit_links (audit_links.py) is refreshed
# nightly for the badge; a changed dead-link list alone is no reason to pay for a new why_blurb.
FROZEN_KINDS = ("exa", "nansen", "audit_links")


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"projects": 0, "github": 0, "blurbs": 0, "blurbs_flagged": 0, "errors": 0}
    links = _audit_links(conn, ctx)
    out["errors"] += links.pop("failed", 0)            # a soft error in the run receipt
    out.update(links)
    targets = select_targets(conn, ctx)
    out["projects"] = len(targets)
    gh, claude = (ctx.client(n) for n in ("github", "claude"))
    elsewhere = flagged_elsewhere(conn, ctx, {p.id for p, _ in targets}) if claude is not None else []
    if not targets and not elsewhere:
        return out
    from ..costs import BudgetExceeded
    for p, daily in targets + elsewhere:
        try:
            with savepoint(conn):
                if gh is not None and p.github:
                    act = gh.activity(p.github, now=ctx.now)
                    if act:
                        out["github"] += 1
                        _set_signal(conn, p.id, "github", act, ctx.now)
                _set_signal(conn, p.id, "alive", {"alive": _alive(conn, p.id, ctx.now)}, ctx.now)
        except Exception as e:
            out["errors"] += 1
            log.warning("signals project=%s failed: %s", p.slug, e)
        if claude is None:
            continue
        try:
            with savepoint(conn):
                why, bctx = _blurb_due(conn, p, daily, ctx.now)
                if why is not None:
                    if why == "flagged":
                        out["blurbs_flagged"] += 1
                    blurb = claude.why_blurb(bctx)
                    conn.execute(sa.update(projects).values(why_blurb=blurb.strip()[:400], blurb_at=ctx.now).where(projects.c.id == p.id))
                    out["blurbs"] += 1
        except BudgetExceeded:
            out["budget_stop"] = 1
            claude = None
        except Exception as e:
            out["errors"] += 1
            log.warning("signals blurb project=%s failed: %s", p.slug, e)
    return out


def select_targets(conn, ctx: RunContext) -> list[tuple]:
    """Trending top 20 by score (tie-break TVL), then New, capped at ctx.signals_limit; spam and wash-like volume excluded."""
    q = (sa.select(projects, project_daily).join(project_daily, project_daily.c.project_id == projects.c.id)
         .where(project_daily.c.day == ctx.day, project_daily.c.is_spam.is_(False), project_daily.c.is_wash.is_(False)))
    rows = conn.execute(q).all()
    trending = sorted([r for r in rows if (r.score_trending or 0) > 0], key=lambda r: (-(r.score_trending or 0), -(r.tvl_usd or 0)))[:20]
    new = sorted([r for r in rows if r.is_new], key=lambda r: -(r.tvl_usd or 0))
    seen, out = set(), []
    for r in trending + new:
        if r.id in seen:
            continue
        seen.add(r.id)
        out.append((r, r))
        if len(out) >= ctx.signals_limit:
            break
    return out


def flagged_elsewhere(conn, ctx: RunContext, exclude: set[int]) -> list[tuple]:
    """Projects outside tonight's targets whose stored blurb is due a rewrite on the checks' account (_stale_blurb, once
    ENRICH_RETRY_DAYS old), busiest first by 7-day volume, at most BLURB_RECHECK_MAX (20); spam and wash-like volume are
    left out, as for targets. Trending multiplies by the week-on-week ratio, so a project whose volume fell leaves the
    targets while its page keeps the blurb: 8 of the 19 projects with wrong text in the 2026-10-24 fact-check had lost
    volume week on week."""
    cap = _env_int("BLURB_RECHECK_MAX", 20)
    if cap <= 0:
        return []
    before = ctx.now - timedelta(days=retry_days())
    q = (sa.select(projects, project_daily).join(project_daily, project_daily.c.project_id == projects.c.id)
         .where(project_daily.c.day == ctx.day, project_daily.c.is_spam.is_(False), project_daily.c.is_wash.is_(False),
                projects.c.blurb_at.is_not(None), projects.c.blurb_at <= before)
         .order_by(project_daily.c.vol_7d.is_(None), project_daily.c.vol_7d.desc(), projects.c.id))
    out = []
    for r in conn.execute(q).all():
        if r.id in exclude or not _stale_blurb(r, _blurb_ctx(conn, r, r)):
            continue
        out.append((r, r))
        if len(out) >= cap:
            break
    return out


def _set_signal(conn, pid: int, kind: str, value: dict, now: datetime) -> bool:
    """Upsert; as_of only moves when the value changed. Returns changed."""
    prev = conn.execute(sa.select(signals.c.value).where(signals.c.project_id == pid, signals.c.kind == kind)).scalar_one_or_none()
    if prev is not None and json.dumps(prev, sort_keys=True, default=str) == json.dumps(value, sort_keys=True, default=str):
        return False
    upsert(conn, signals, [{"project_id": pid, "kind": kind, "value": value, "as_of": now}], keys=["project_id", "kind"])
    return True


def _audit_links(conn, ctx: RunContext) -> dict:
    """Every published audit link, not only tonight's targets: a dead one leaves the "Audit linked" badge (audit_links.py).
    Free; a failure is logged and never stops the step."""
    from .. import audit_links
    try:
        with savepoint(conn):
            return audit_links.check(conn, ctx.now, _set_signal, ctx.extras.get("audit_probe"))
    except Exception as e:
        log.warning("signals audit links failed: %s", e)
        return {"failed": 1}


def _alive(conn, pid: int, now: datetime) -> bool | None:
    """A GitHub commit in the last 30 days; None (unknown) when Bait has no GitHub reading for the project. GitHub is the
    only input since the Exa half went with the receipts search on 2026-10-24, so no reading is no evidence either way,
    never "not alive" (as false it reached the blurb: "the alive signal reads false" of a project with no GitHub link)."""
    gh = conn.execute(sa.select(signals.c.value).where(signals.c.project_id == pid, signals.c.kind == "github")).scalar_one_or_none()
    if not gh:
        return None
    return is_alive(_dt(gh.get("last_commit_at")), now)


def _dt(s):
    if not s:
        return None
    try:
        return as_utc(datetime.fromisoformat(str(s).replace("Z", "+00:00")))
    except ValueError:
        return None


# A blurb written while unknown TVL was stored as $0 says so ("Reported TVL is zero"); rewrite it once TVL reads unknown.
_ZERO_TVL = re.compile(r"\b(zero|no|\$0)\b[^.]{0,24}\bTVL\b|\bTVL\b[^.]{0,24}\b(zero|\$0)\b", re.I)


def _needs_blurb(conn, p, daily=None) -> bool:
    latest = conn.execute(sa.select(sa.func.max(signals.c.as_of))
                          .where(signals.c.project_id == p.id, signals.c.kind.not_in(FROZEN_KINDS))).scalar_one()
    if p.blurb_at is None:
        return True
    if daily is not None and daily.tvl_usd is None and p.why_blurb and _ZERO_TVL.search(p.why_blurb):
        return True
    return latest is not None and as_utc(latest) > as_utc(p.blurb_at)


def _retry_due(blurb_at, now: datetime) -> bool:
    return blurb_at is not None and as_utc(blurb_at) <= now - timedelta(days=retry_days())


def _stale_blurb(p, bctx: dict) -> str | None:
    """Why the stored blurb fails tonight: "empty", or the problems prose_checks.blurb_problems finds against tonight's
    context (figures and size words against the current metrics, trend words against this week's volumes, chains,
    registry status, audits, coverage, internal words); None when it passes."""
    text = (p.why_blurb or "").strip()
    if not text:
        return "empty"
    found = blurb_problems(text, bctx)
    return " | ".join(map(str, found)) if found else None


def _blurb_due(conn, p, daily, now: datetime) -> tuple[str | None, dict | None]:
    """Whether tonight writes p's blurb, and the context to write it from. "flagged": the stored blurb is at least
    ENRICH_RETRY_DAYS old and empty or failing the checks against tonight's context (counted as blurbs_flagged);
    "changed": _needs_blurb (new, signals changed, an unknown TVL called zero); None: left as it is.
    The window is the loop guard: a new blurb is checked against that night's context before it is stored, so it fails
    only once the figures move, and then waits a full window from its blurb_at before the next rewrite."""
    changed = _needs_blurb(conn, p, daily)
    due = _retry_due(p.blurb_at, now)
    if not (changed or due):
        return None, None
    bctx = _blurb_ctx(conn, p, daily)
    stale = _stale_blurb(p, bctx) if due else None
    if stale:
        log.info("signals blurb project=%s flagged: %s", p.slug, stale)
        return "flagged", bctx
    return ("changed", bctx) if changed else (None, None)


def _blurb_ctx(conn, p, daily) -> dict:
    sig = {r.kind: r.value for r in conn.execute(sa.select(signals.c.kind, signals.c.value).where(signals.c.project_id == p.id))
           if r.kind not in FROZEN_KINDS}
    if (sig.get("alive") or {}).get("alive") is None:
        sig.pop("alive", None)              # unknown (no GitHub reading): left out, so it cannot be read as "not alive"
    rec = conn.execute(sa.select(receipts.c.title, receipts.c.source, receipts.c.published_at)
                       .where(receipts.c.project_id == p.id).order_by(receipts.c.published_at.desc()).limit(5)).all()
    return {
        "name": p.name, "category": p.category, "what_line": p.what_line,
        # chains, registry status and the audit links the badge shows: what the prose checks compare
        **hook_facts(hooks_of(conn, p.id), dead_links(conn, p.id)),
        "metrics": {
            "windows": f"closed UTC days ending {daily.window_end}: 24h = that day; 7d = it and the 6 days before; "
                       "prev_7d = the 7 days before those. A null count is unknown.",
            "tvl_usd": daily.tvl_usd, "vol_24h": daily.vol_24h, "vol_7d": daily.vol_7d, "vol_7d_prev": daily.vol_7d_prev,
            "swaps_24h": daily.swaps_24h, "swaps_7d": daily.swaps_7d, "wallets_24h": daily.wallets_24h,
            "wallets_24h_kind": None if daily.wallets_24h is None else (
                "distinct" if daily.wallets_exact else "upper bound (buyers + sellers per pool)"),
            "wallets_7d": daily.wallets_7d,
            "pools_n": daily.pools_n, "is_new": daily.is_new},
        "signals": sig, "receipts": [dict(r._mapping) for r in rec],
    }


def _env_int(name: str, default: int) -> int:
    import os
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default
