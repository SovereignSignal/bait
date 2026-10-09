"""Step 7 (signals): for New ∪ Trending (top N): Exa receipts, GitHub activity,
then Claude why_blurb when signals changed since blurb_at."""
from __future__ import annotations

import json
import os
from datetime import datetime

import sqlalchemy as sa

from ..rules import name_tokens, receipt_relevant
from ..db import as_utc, project_daily, projects, receipts, savepoint, signals, upsert, utcnow
from ..rules import is_alive
from . import RunContext, log


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"projects": 0, "exa": 0, "receipts": 0, "github": 0, "blurbs": 0, "errors": 0}
    targets = select_targets(conn, ctx)
    out["projects"] = len(targets)
    if not targets:
        return out
    from ..costs import BudgetExceeded
    exa, gh, claude = (ctx.client(n) for n in ("exa", "github", "claude"))
    for p, daily in targets:
        changed = False
        hits = None
        try:
            with savepoint(conn):
                if exa is not None and name_tokens(p.name):   # unnamed projects can never keep a receipt: don't pay to search
                    try:
                        hits = exa.search_recent(f'"{p.name}" Uniswap v4 hook', now=ctx.now)
                    except BudgetExceeded:
                        out["budget_stop"] = 1
                        exa = None            # no more paid searches this run; free signals continue
                        hits = None
                if exa is not None and hits is not None:
                    out["exa"] += 1
                    hits = [h for h in hits if receipt_relevant(p.name, h["title"], h["source"])]
                    rows = [{"project_id": p.id, "url": h["url"], "title": h["title"], "source": h["source"], "published_at": h["published_at"]} for h in hits]
                    out["receipts"] += upsert(conn, receipts, rows, keys=["project_id", "url"])
                    latest = max((h["published_at"] for h in hits if h["published_at"]), default=None)
                    changed |= _set_signal(conn, p.id, "exa", {"hits_30d": len(hits), "latest": latest.isoformat() if latest else None,
                                                                "top": [{"url": h["url"], "title": h["title"]} for h in hits[:2]]}, ctx.now)
                if gh is not None and p.github:
                    act = gh.activity(p.github, now=ctx.now)
                    if act:
                        out["github"] += 1
                        changed |= _set_signal(conn, p.id, "github", act, ctx.now)
                changed |= _set_signal(conn, p.id, "alive", {"alive": _alive(conn, p.id, ctx.now)}, ctx.now)
        except Exception as e:
            out["errors"] += 1
            log.warning("signals project=%s failed: %s", p.slug, e)
        if claude is not None and _needs_blurb(conn, p):
            try:
                with savepoint(conn):
                    blurb = claude.why_blurb(_blurb_ctx(conn, p, daily))
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


def _set_signal(conn, pid: int, kind: str, value: dict, now: datetime) -> bool:
    """Upsert; as_of only moves when the value changed. Returns changed."""
    prev = conn.execute(sa.select(signals.c.value).where(signals.c.project_id == pid, signals.c.kind == kind)).scalar_one_or_none()
    if prev is not None and json.dumps(prev, sort_keys=True, default=str) == json.dumps(value, sort_keys=True, default=str):
        return False
    upsert(conn, signals, [{"project_id": pid, "kind": kind, "value": value, "as_of": now}], keys=["project_id", "kind"])
    return True


def _alive(conn, pid: int, now: datetime) -> bool:
    vals = {r.kind: r.value for r in conn.execute(sa.select(signals.c.kind, signals.c.value).where(signals.c.project_id == pid))}
    gh = (vals.get("github") or {}).get("last_commit_at")
    ex = (vals.get("exa") or {}).get("latest")
    return is_alive(_dt(gh), _dt(ex), now)


def _dt(s):
    if not s:
        return None
    try:
        return as_utc(datetime.fromisoformat(str(s).replace("Z", "+00:00")))
    except ValueError:
        return None


def _needs_blurb(conn, p) -> bool:
    latest = conn.execute(sa.select(sa.func.max(signals.c.as_of)).where(signals.c.project_id == p.id)).scalar_one()
    if p.blurb_at is None:
        return True
    return latest is not None and as_utc(latest) > as_utc(p.blurb_at)


def _parallel_facts(enrichment) -> dict | None:
    """Parallel output for the blurb prompt; a {"_failed": ...} tombstone is not a fact."""
    par = (enrichment or {}).get("parallel")
    return None if not par or "_failed" in par else par


def _blurb_ctx(conn, p, daily) -> dict:
    sig = {r.kind: r.value for r in conn.execute(sa.select(signals.c.kind, signals.c.value).where(signals.c.project_id == p.id))}
    rec = conn.execute(sa.select(receipts.c.title, receipts.c.source, receipts.c.published_at)
                       .where(receipts.c.project_id == p.id).order_by(receipts.c.published_at.desc()).limit(5)).all()
    return {
        "name": p.name, "category": p.category, "what_line": p.what_line,
        "metrics": {
            "windows": f"closed UTC days ending {daily.window_end}: 24h = that day; 7d = it and the 6 days before; "
                       "prev_7d = the 7 days before those. A null count is unknown: do not quote it.",
            "tvl_usd": daily.tvl_usd, "vol_24h": daily.vol_24h, "vol_7d": daily.vol_7d, "vol_7d_prev": daily.vol_7d_prev,
            "swaps_24h": daily.swaps_24h, "swaps_7d": daily.swaps_7d, "wallets_24h": daily.wallets_24h,
            "wallets_24h_kind": None if daily.wallets_24h is None else (
                "distinct" if daily.wallets_exact else "upper bound (buyers + sellers per pool)"),
            "pools_n": daily.pools_n, "is_new": daily.is_new},
        "signals": sig, "receipts": [dict(r._mapping) for r in rec],
        "enrichment": _parallel_facts(p.enrichment),
    }
