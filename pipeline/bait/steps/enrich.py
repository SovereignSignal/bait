"""Step 5 (enrich): projects with enriched_at null -> Parallel (optional) -> Blockscout verified source -> Claude what_line + category."""
from __future__ import annotations

import sqlalchemy as sa

from ..config import CHAIN_BY_ID
from ..db import hooks, pool_stats, pools, projects, utcnow
from . import RunContext, log


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"candidates": 0, "parallel": 0, "source": 0, "enriched": 0, "errors": 0}
    claude = ctx.client("claude")
    if claude is None:
        log.info("enrich skipped: no ANTHROPIC_API_KEY (what_line keeps the hooklist placeholder)")
        return out
    par = ctx.client("parallel")
    bs = ctx.client("blockscout")
    from ..costs import BudgetExceeded
    todo = select_candidates(conn)
    out["candidates"] = len(todo)
    # Parallel tasks take ~1-2 minutes each; run them side by side (PARALLEL_CONCURRENCY, default 8) before the
    # per-project loop. The whole batch is budget-checked up front, so concurrency cannot overshoot the budget.
    par_results = _parallel_batch(conn, par, todo, out) if par is not None else {}
    for p in todo:
        hs = conn.execute(sa.select(hooks).where(hooks.c.project_id == p.id).order_by(hooks.c.verified_source.desc(), hooks.c.first_seen_at)).all()
        ctxd = {
            "name": p.name, "hooks_n": len(hs), "is_factory": p.is_factory,
            "chains": sorted({CHAIN_BY_ID[h.chain_id].slug if h.chain_id in CHAIN_BY_ID else str(h.chain_id) for h in hs}),
            "hooklist_descriptions": sorted({h.description for h in hs if h.description})[:5],
            "flags_union": sorted({f for h in hs for f, v in (h.flags or {}).items() if v}),
            "dynamic_fee": any((h.properties or {}).get("dynamicFee") for h in hs),
            "audit_urls": sorted({h.audit_url for h in hs if h.audit_url}),
        }
        enrichment = dict(p.enrichment or {})
        try:
            if "parallel" not in enrichment and par_results.get(p.id):
                enrichment["parallel"] = par_results[p.id]
                out["parallel"] += 1
            if bs is not None and "source" not in enrichment:
                src = _verified_source(bs, hs)
                if src:
                    enrichment["source"] = src
                    out["source"] += 1
            ctxd["enrichment"] = enrichment
            res = claude.what_line(ctxd)
        except BudgetExceeded:
            out["budget_stop"] = 1
            if enrichment != dict(p.enrichment or {}):   # keep what was already paid for
                conn.execute(sa.update(projects).values(enrichment=enrichment).where(projects.c.id == p.id))
            break
        except Exception as e:
            out["errors"] += 1
            log.warning("enrich project=%s failed: %s", p.slug, e)
            continue
        par_res = enrichment.get("parallel") or {}
        vals = {
            "what_line": res["what_line"].strip()[:200], "category": res["category"],
            "enrichment": enrichment, "enriched_at": utcnow(),
            "website": p.website or par_res.get("website") or None,
            "github": p.github or par_res.get("github") or None,
            "x_handle": p.x_handle or (par_res.get("x_handle") or "").lstrip("@") or None,
        }
        conn.execute(sa.update(projects).values(**vals).where(projects.c.id == p.id))
        out["enriched"] += 1
    return out


def select_candidates(conn) -> list:
    """Projects with live pools, busiest first, never enriched, ENRICH_MAX (40) per run. Parallel and Claude bill per
    call and ~95% of registry hooks have no pools; the rest wait until they show activity. The cost preview uses this."""
    cap = _env_int("ENRICH_MAX", 40)
    busy = (sa.select(hooks.c.project_id, sa.func.coalesce(sa.func.sum(pool_stats.c.vol_24h), 0.0).label("vol"))
            .select_from(hooks.join(pools, sa.and_(pools.c.hook_address == hooks.c.address, pools.c.chain_id == hooks.c.chain_id))
                         .outerjoin(pool_stats, sa.and_(pool_stats.c.pool_id == pools.c.pool_id, pool_stats.c.chain_id == pools.c.chain_id)))
            .where(hooks.c.project_id.is_not(None)).group_by(hooks.c.project_id).subquery())
    return conn.execute(sa.select(projects).join(busy, busy.c.project_id == projects.c.id)
                        .where(projects.c.enriched_at.is_(None)).order_by(busy.c.vol.desc(), projects.c.id).limit(cap)).all()


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


def _parallel_batch(conn, par, todo, out) -> dict:
    """Submit Parallel enrichment for every candidate that lacks it, concurrently; returns {project_id: result}."""
    import os
    from concurrent.futures import ThreadPoolExecutor

    from ..costs import BudgetExceeded, parallel_usd
    need = [p for p in todo if "parallel" not in (p.enrichment or {})]
    if not need:
        return {}
    price = parallel_usd(getattr(par, "processor", "core"))
    meter = getattr(par, "meter", None)
    while need and meter is not None:          # shrink the batch until the whole of it fits the budget
        try:
            meter.check("parallel", price * len(need))
            break
        except BudgetExceeded:
            out["budget_stop"] = 1
            need = need[: len(need) // 2] if len(need) > 1 else []
    if not need:
        return {}
    hints = {}
    for p in need:
        descs = sorted({r.description for r in conn.execute(sa.select(hooks.c.description).where(
            hooks.c.project_id == p.id, hooks.c.description.is_not(None))) if r.description})[:5]
        hints[p.id] = "; ".join(descs)[:800]

    def one(p):
        try:
            return p.id, par.enrich(p.name, hints[p.id])
        except Exception as e:  # one task failing must not lose the others
            log.warning("enrich parallel project=%s failed: %s", p.slug, e)
            return p.id, None

    try:
        workers = max(1, int(os.environ.get("PARALLEL_CONCURRENCY", "") or 8))
    except ValueError:
        workers = 8
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return {pid: res for pid, res in ex.map(one, need) if res}
