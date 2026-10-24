"""Step 7 (signals): for New ∪ Trending (top N): GitHub activity and the alive signal, then Claude why_blurb when
signals changed since blurb_at.

The per-project Exa receipts search was removed on 2026-10-24 (Exa now serves only the radar step). The receipts table
and the "exa" / "nansen" signal rows written before then stay stored; they no longer change, so they are left out of
the blurb's signals and out of `alive`. `alive` is null (unknown) without a GitHub reading, and then left out of the
blurb's signals too."""
from __future__ import annotations

import re

import json
from datetime import datetime

import sqlalchemy as sa

from ..db import as_utc, project_daily, projects, receipts, savepoint, signals, upsert
from ..rules import is_alive
from . import RunContext, log

# signal kinds no step writes any more (Exa receipts until 2026-10-24, Nansen until 2026-10-08): frozen, never prompt input
FROZEN_KINDS = ("exa", "nansen")


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"projects": 0, "github": 0, "blurbs": 0, "errors": 0}
    targets = select_targets(conn, ctx)
    out["projects"] = len(targets)
    if not targets:
        return out
    from ..costs import BudgetExceeded
    gh, claude = (ctx.client(n) for n in ("github", "claude"))
    for p, daily in targets:
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
        if claude is not None and _needs_blurb(conn, p, daily):
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
    latest = conn.execute(sa.select(sa.func.max(signals.c.as_of)).where(signals.c.project_id == p.id)).scalar_one()
    if p.blurb_at is None:
        return True
    if daily is not None and daily.tvl_usd is None and p.why_blurb and _ZERO_TVL.search(p.why_blurb):
        return True
    return latest is not None and as_utc(latest) > as_utc(p.blurb_at)


def _blurb_ctx(conn, p, daily) -> dict:
    sig = {r.kind: r.value for r in conn.execute(sa.select(signals.c.kind, signals.c.value).where(signals.c.project_id == p.id))
           if r.kind not in FROZEN_KINDS}
    if (sig.get("alive") or {}).get("alive") is None:
        sig.pop("alive", None)              # unknown (no GitHub reading): left out, so it cannot be read as "not alive"
    rec = conn.execute(sa.select(receipts.c.title, receipts.c.source, receipts.c.published_at)
                       .where(receipts.c.project_id == p.id).order_by(receipts.c.published_at.desc()).limit(5)).all()
    return {
        "name": p.name, "category": p.category, "what_line": p.what_line,
        "metrics": {
            "windows": f"closed UTC days ending {daily.window_end}: 24h = that day; 7d = it and the 6 days before; "
                       "prev_7d = the 7 days before those. A null count is unknown.",
            "tvl_usd": daily.tvl_usd, "vol_24h": daily.vol_24h, "vol_7d": daily.vol_7d, "vol_7d_prev": daily.vol_7d_prev,
            "swaps_24h": daily.swaps_24h, "swaps_7d": daily.swaps_7d, "wallets_24h": daily.wallets_24h,
            "wallets_24h_kind": None if daily.wallets_24h is None else (
                "distinct" if daily.wallets_exact else "upper bound (buyers + sellers per pool)"),
            "pools_n": daily.pools_n, "is_new": daily.is_new},
        "signals": sig, "receipts": [dict(r._mapping) for r in rec],
    }
