"""Step 5 (enrich): projects with enriched_at null -> Parallel (optional) -> Blockscout verified source -> Claude what_line + category."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

import sqlalchemy as sa

from ..config import CHAIN_BY_ID
from ..links import web_url, github_url, x_handle
from ..db import as_utc, hooks, pool_stats, pools, projects, savepoint, utcnow
from . import RunContext, log


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"candidates": 0, "parallel": 0, "parallel_failed": 0, "source": 0, "enriched": 0, "errors": 0}
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
    _, waiting = _parallel_batch(conn, ctx, par, todo, out) if par is not None else ({}, set())
    # the batch may have tombstoned or collected Parallel data: re-read enrichment rather than trust the stale rows
    fresh_enr = {r.id: r.enrichment for r in conn.execute(
        sa.select(projects.c.id, projects.c.enrichment).where(projects.c.id.in_([p.id for p in todo])))} if todo else {}
    for p in todo:
        if p.id in waiting:
            continue        # paid Parallel run still in progress: collect it next run, then write what_line once
        hs = hooks_of(conn, p.id)
        enrichment = dict(fresh_enr.get(p.id) or {})
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
            if enrichment != dict(p.enrichment or {}):   # keep what was already paid for
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
    """What Claude's what_line call sees: the project's hooks and its stored enrichment (Parallel facts, verified source)."""
    return {
        "name": p.name, "hooks_n": len(hs), "is_factory": p.is_factory,
        "chains": sorted({CHAIN_BY_ID[h.chain_id].slug if h.chain_id in CHAIN_BY_ID else str(h.chain_id) for h in hs}),
        "hooklist_descriptions": sorted({h.description for h in hs if h.description})[:5],
        "flags_union": sorted({f for h in hs for f, v in (h.flags or {}).items() if v}),
        "dynamic_fee": any((h.properties or {}).get("dynamicFee") for h in hs),
        "audit_urls": sorted({h.audit_url for h in hs if h.audit_url}),
        "enrichment": enrichment,
    }


def what_values(p, res: dict, enrichment: dict) -> dict:
    """The projects row update for a fresh what_line: the line (cut at a word boundary), category, enrichment, links."""
    par_res = enrichment.get("parallel") or {}
    return {
        "what_line": cut_line(res["what_line"]), "category": res["category"],
        "enrichment": enrichment, "enriched_at": utcnow(),
        "website": web_url(p.website) or web_url(par_res.get("website")),
        "github": github_url(p.github) or github_url(par_res.get("github")),
        "x_handle": x_handle(p.x_handle) or x_handle(par_res.get("x_handle")),
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


def select_candidates(conn) -> list:
    """Projects with live pools, busiest first, never enriched, ENRICH_MAX (20) per run. Parallel and Claude bill per
    call and ~95% of registry hooks have no pools; the rest wait until they show activity. The cost preview uses this.
    20 keeps a night near $1.25 all in (2026-10-13 ledger: Parallel $0.025 a project, Exa ~$0.55, Claude ~$0.25), inside
    the $40 rolling 30-day budget; 40 ran ~$1.75 a night and would have hit it on day 23."""
    cap = _env_int("ENRICH_MAX", 20)
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


def _parse_iso(s):
    if not s:
        return None
    try:
        return as_utc(datetime.fromisoformat(str(s).replace("Z", "+00:00")))
    except ValueError:
        return None


_SQLITE_WRITE = threading.Lock()


def _persist_enrichment(conn, pid: int, enrichment: dict) -> None:
    """Durable write of paid facts. Postgres: own connection + commit, so a later exception, budget stop, or kill
    in this step cannot roll back a result we already paid for. SQLite allows one writer and the step holds it,
    so the step connection is used (tests), serialised because worker threads share it."""
    stmt = sa.update(projects).values(enrichment=enrichment).where(projects.c.id == pid)
    if conn.dialect.name == "postgresql":
        with conn.engine.begin() as c:
            c.execute(stmt)
    else:
        with _SQLITE_WRITE:
            conn.execute(stmt)


def _mark_failed(conn, p, enr: dict, run_id: str, reason: str, out: dict) -> None:
    """A paid run that will never deliver. Keep a tombstone under "parallel" so it is never resubmitted."""
    enr.pop("parallel_run_id", None)
    enr.pop("parallel_run_at", None)
    enr["parallel"] = {"_failed": run_id, "reason": reason}
    _persist_enrichment(conn, p.id, enr)
    out["parallel_failed"] += 1
    log.warning("enrich parallel project=%s run=%s %s: continuing without Parallel facts", p.slug, run_id, reason)


def _parallel_batch(conn, ctx, par, todo, out) -> tuple[dict, set]:
    """Submit Parallel enrichment for every candidate that lacks it, concurrently; returns {project_id: result}."""
    import os
    from concurrent.futures import ThreadPoolExecutor

    from ..costs import BudgetExceeded, parallel_usd
    need = [p for p in todo if "parallel" not in (p.enrichment or {})]
    if not need:
        return {}, set()
    price = parallel_usd(getattr(par, "processor", "core"))
    meter = getattr(par, "meter", None)
    max_age = timedelta(hours=_env_int("PARALLEL_RUN_MAX_AGE_H", 48))
    pending, fresh = [], []
    for p in need:
        enr = dict(p.enrichment or {})
        rid = enr.get("parallel_run_id")
        if not rid:
            fresh.append(p)
            continue
        started = _parse_iso(enr.get("parallel_run_at"))
        if started is not None and ctx.now - started > max_age:
            _mark_failed(conn, p, enr, rid, "expired", out)
            continue
        pending.append(p)                                # already paid: free to collect
    while fresh and meter is not None:         # shrink the paid batch until the whole of it fits the budget
        try:
            meter.check("parallel", price * len(fresh))
            break
        except BudgetExceeded:
            out["budget_stop"] = 1
            fresh = fresh[: len(fresh) // 2] if len(fresh) > 1 else []
    need = pending + fresh
    if not need:
        return {}, set()
    submitted: dict[int, str] = {}
    gone: dict[int, str] = {}
    hints = {}
    for p in need:
        descs = sorted({r.description for r in conn.execute(sa.select(hooks.c.description).where(
            hooks.c.project_id == p.id, hooks.c.description.is_not(None))) if r.description})[:5]
        hints[p.id] = "; ".join(descs)[:800]

    from ..clients.parallel import RunGone

    def one(p):
        # A run submitted earlier (paid) but not yet collected is fetched for free instead of resubmitted.
        run_id = (p.enrichment or {}).get("parallel_run_id")
        try:
            if not run_id:
                run_id = par.submit(p.name, hints[p.id])
                submitted[p.id] = run_id
                _persist_enrichment(conn, p.id, dict(p.enrichment or {}) | {"parallel_run_id": run_id, "parallel_run_at": ctx.now.isoformat()})
            return p.id, par.result(run_id)
        except RunGone:
            gone[p.id] = run_id
            return p.id, None
        except BudgetExceeded:
            return p.id, None
        except Exception as e:  # one task failing must not lose the others
            log.warning("enrich parallel project=%s failed: %s", p.slug, e)
            return p.id, None

    try:
        workers = max(1, int(os.environ.get("PARALLEL_CONCURRENCY", "") or 8))
    except ValueError:
        workers = 8
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = {pid: res for pid, res in ex.map(one, need) if res}
    by_id = {p.id: p for p in need}
    for pid, res in results.items():          # paid output is durable before any Claude call can fail
        enr = dict(by_id[pid].enrichment or {})
        enr.pop("parallel_run_id", None)
        enr.pop("parallel_run_at", None)
        enr["parallel"] = res
        _persist_enrichment(conn, pid, enr)
        out["parallel"] += 1
    # Remember paid runs whose output did not arrive in time, so the next run collects them instead of paying again.
    for p in need:
        if p.id in gone:
            _mark_failed(conn, p, dict(p.enrichment or {}), gone[p.id], "404", out)
    waiting: set[int] = set()
    for p in need:
        if p.id in gone:
            continue
        rid = submitted.get(p.id) or (p.enrichment or {}).get("parallel_run_id")
        if rid and p.id not in results:
            enr = dict(p.enrichment or {}) | {"parallel_run_id": rid}
            enr.setdefault("parallel_run_at", ctx.now.isoformat())   # never reset the clock of a run already in flight
            conn.execute(sa.update(projects).values(enrichment=enr).where(projects.c.id == p.id))
            waiting.add(p.id)
    out["parallel_pending"] = len(waiting)
    return results, waiting
