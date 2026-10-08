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
    # Only projects with live pools, busiest first, capped per run: Parallel and Claude cost money per call and
    # ~95% of registry hooks have no pools. The rest wait until they show activity.
    cap = _env_int("ENRICH_MAX", 40)
    busy = (sa.select(hooks.c.project_id, sa.func.coalesce(sa.func.sum(pool_stats.c.vol_24h), 0.0).label("vol"))
            .select_from(hooks.join(pools, sa.and_(pools.c.hook_address == hooks.c.address, pools.c.chain_id == hooks.c.chain_id))
                         .outerjoin(pool_stats, sa.and_(pool_stats.c.pool_id == pools.c.pool_id, pool_stats.c.chain_id == pools.c.chain_id)))
            .where(hooks.c.project_id.is_not(None)).group_by(hooks.c.project_id).subquery())
    todo = conn.execute(sa.select(projects).join(busy, busy.c.project_id == projects.c.id)
                        .where(projects.c.enriched_at.is_(None)).order_by(busy.c.vol.desc(), projects.c.id).limit(cap)).all()
    out["candidates"] = len(todo)
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
            if par is not None and "parallel" not in enrichment:
                res = par.enrich(p.name, "; ".join(ctxd["hooklist_descriptions"])[:800])
                if res:
                    enrichment["parallel"] = res
                    out["parallel"] += 1
            if bs is not None and "source" not in enrichment:
                src = _verified_source(bs, hs)
                if src:
                    enrichment["source"] = src
                    out["source"] += 1
            ctxd["enrichment"] = enrichment
            res = claude.what_line(ctxd)
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
