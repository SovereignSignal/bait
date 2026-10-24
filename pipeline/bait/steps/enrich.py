"""Step 5 (enrich): projects with enriched_at null -> Blockscout verified source -> Claude what_line + category.

Parallel (team, site, GitHub, X and audit facts) was removed on 2026-10-24. Rows enriched before then may still carry
enrichment["parallel"] (facts, or a {"_failed": ...} tombstone) and parallel_run_id / parallel_run_at: they are kept as
stored, never sent to Claude, and never used as links."""
from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa

from ..config import CHAIN_BY_ID
from ..links import web_url, github_url, x_handle
from ..db import hooks, pool_stats, pools, projects, savepoint, utcnow
from . import RunContext, log

# enrichment keys written by the retired Parallel lane: stored data, not prompt input
LEGACY_KEYS = ("parallel", "parallel_run_id", "parallel_run_at")


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"candidates": 0, "source": 0, "enriched": 0, "errors": 0}
    claude = ctx.client("claude")
    if claude is None:
        log.info("enrich skipped: no ANTHROPIC_API_KEY (what_line keeps the hooklist placeholder)")
        return out
    bs = ctx.client("blockscout")
    from ..costs import BudgetExceeded
    todo = select_candidates(conn, now=ctx.now)
    out["candidates"] = len(todo)
    for p in todo:
        hs = hooks_of(conn, p.id)
        enrichment = dict(p.enrichment or {})
        try:
            with savepoint(conn):
                if bs is not None and "source" not in enrichment:
                    src = _verified_source(bs, hs)
                    if src:
                        enrichment["source"] = src
                        out["source"] += 1
                res = claude.what_line(what_context(p, hs, enrichment))
        except BudgetExceeded:
            out["budget_stop"] = 1
            if enrichment != dict(p.enrichment or {}):   # keep the verified source already fetched
                conn.execute(sa.update(projects).values(enrichment=enrichment).where(projects.c.id == p.id))
            break
        except Exception as e:
            out["errors"] += 1
            log.warning("enrich project=%s failed: %s", p.slug, e)
            continue
        conn.execute(sa.update(projects).values(**what_values(p, res, enrichment)).where(projects.c.id == p.id))
        out["enriched"] += 1
    return out


def hooks_of(conn, project_id: int) -> list:
    return conn.execute(sa.select(hooks).where(hooks.c.project_id == project_id)
                        .order_by(hooks.c.verified_source.desc(), hooks.c.first_seen_at)).all()


def what_context(p, hs, enrichment: dict) -> dict:
    """What Claude's what_line call sees: the project's hooks and its stored enrichment (verified source), minus the
    retired Parallel keys."""
    return {
        "name": p.name, "hooks_n": len(hs), "is_factory": p.is_factory,
        "chains": sorted({CHAIN_BY_ID[h.chain_id].slug if h.chain_id in CHAIN_BY_ID else str(h.chain_id) for h in hs}),
        "hooklist_descriptions": sorted({h.description for h in hs if h.description})[:5],
        "flags_union": sorted({f for h in hs for f, v in (h.flags or {}).items() if v}),
        "dynamic_fee": any((h.properties or {}).get("dynamicFee") for h in hs),
        "audit_urls": sorted({h.audit_url for h in hs if h.audit_url}),
        "enrichment": {k: v for k, v in enrichment.items() if k not in LEGACY_KEYS},
    }


def what_values(p, res: dict, enrichment: dict) -> dict:
    """The projects row update for a fresh what_line: the line (cut at a word boundary), category, enrichment, and the
    stored links re-validated (an invalid one becomes null)."""
    return {
        "what_line": cut_line(res["what_line"]), "category": res["category"], "category_source": "enrich",
        "enrichment": enrichment, "enriched_at": utcnow(),
        "website": web_url(p.website), "github": github_url(p.github), "x_handle": x_handle(p.x_handle),
    }


def cut_line(text: str, limit: int = 200) -> str:
    """Trim to `limit` chars at the last word boundary; append an ellipsis only when something was cut."""
    t = text.strip()
    if len(t) <= limit:
        return t
    head = t[: limit - 1]
    sp = head.rfind(" ")
    if sp > limit // 2:
        head = head[:sp]
    return head.rstrip(" ,;:.-") + "…"


def select_candidates(conn, now: datetime | None = None) -> list:
    """Projects with live pools, busiest first, never enriched, ENRICH_MAX (20) per run. Claude bills per call and ~95%
    of registry hooks have no pools; the rest wait until they show activity. The cost preview uses this. 20 was sized on
    the 2026-10-13 ledger (then with Parallel, removed 2026-10-24) to stay inside the $40 rolling 30-day budget.

    A project enriched to an empty what_line (the model had nothing to say, or the call returned nothing usable) is
    retried once its enrichment is ENRICH_RETRY_DAYS (7) old, behind never-enriched projects in the same cap: a busy
    project should not keep a blank card for want of one more call, and a weekly retry bounds the cost."""
    cap = _env_int("ENRICH_MAX", 20)
    retry_before = (now or utcnow()) - timedelta(days=_env_int("ENRICH_RETRY_DAYS", 7))
    empty = sa.or_(projects.c.what_line.is_(None), sa.func.trim(projects.c.what_line) == "")
    busy = (sa.select(hooks.c.project_id, sa.func.coalesce(sa.func.sum(pool_stats.c.vol_24h), 0.0).label("vol"))
            .select_from(hooks.join(pools, sa.and_(pools.c.hook_address == hooks.c.address, pools.c.chain_id == hooks.c.chain_id))
                         .outerjoin(pool_stats, sa.and_(pool_stats.c.pool_id == pools.c.pool_id, pool_stats.c.chain_id == pools.c.chain_id)))
            .where(hooks.c.project_id.is_not(None)).group_by(hooks.c.project_id).subquery())
    return conn.execute(sa.select(projects).join(busy, busy.c.project_id == projects.c.id)
                        .where(sa.or_(projects.c.enriched_at.is_(None),
                                      sa.and_(empty, projects.c.enriched_at < retry_before)))
                        .order_by(projects.c.enriched_at.is_not(None), busy.c.vol.desc(), projects.c.id).limit(cap)).all()


def _verified_source(bs, hs) -> dict | None:
    for h in hs:
        if not h.verified_source:
            continue
        info = bs.fetch_code(h.chain_id, h.address)
        if info and info.verified:
            return {"address": h.address, "chain_id": h.chain_id, "name": info.name,
                    "compiler": info.compiler, "snippet": info.source_snippet}
    return None


def _env_int(name: str, default: int) -> int:
    import os
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default
