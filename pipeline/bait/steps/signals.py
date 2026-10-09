"""Step 7 (signals): for New ∪ Trending (top N): Exa receipts, GitHub activity, Nansen labels on top-pool LPs/swappers,
then Claude why_blurb when signals changed since blurb_at."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import sqlalchemy as sa

from ..clients.nansen import is_smart_money
from ..rules import name_tokens, receipt_relevant
from ..config import CHAIN_BY_ID
from ..db import as_utc, hooks, pool_snapshots, pools, project_daily, projects, receipts, savepoint, signals, upsert, utcnow, wallet_labels
from ..rules import is_alive
from . import RunContext, log


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"projects": 0, "exa": 0, "receipts": 0, "github": 0, "nansen": 0, "nansen_skipped_chain": 0, "blurbs": 0, "errors": 0}
    targets = select_targets(conn, ctx)
    out["projects"] = len(targets)
    if not targets:
        return out
    from ..costs import BudgetExceeded
    exa, gh, nansen, graph, claude = (ctx.client(n) for n in ("exa", "github", "nansen", "graph", "claude"))
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
                if nansen is not None:
                    res = _nansen(conn, ctx, p, nansen, graph, out)
                    if res is not None:
                        changed |= _set_signal(conn, p.id, "nansen", res, ctx.now)
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
    """Trending top 20 by score (tie-break TVL), then New, capped at ctx.signals_limit; spam excluded."""
    q = (sa.select(projects, project_daily).join(project_daily, project_daily.c.project_id == projects.c.id)
         .where(project_daily.c.day == ctx.day, project_daily.c.is_spam.is_(False)))
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


def _nansen(conn, ctx, p, nansen, graph, out) -> dict | None:
    if graph is None:
        return None
    top = conn.execute(
        sa.select(pools.c.pool_id, pools.c.chain_id, pool_snapshots.c.tvl_usd)
        .join(pool_snapshots, (pool_snapshots.c.pool_id == pools.c.pool_id) & (pool_snapshots.c.chain_id == pools.c.chain_id))
        .join(hooks, (hooks.c.address == pools.c.hook_address) & (hooks.c.chain_id == pools.c.chain_id))
        .where(hooks.c.project_id == p.id).order_by(pool_snapshots.c.day.desc(), pool_snapshots.c.tvl_usd.desc()).limit(3)).all()
    addrs: dict[str, int] = {}
    for pool in top:
        ch = CHAIN_BY_ID.get(pool.chain_id)
        if not ch or not ch.subgraph_id:
            continue
        if not nansen.supports(pool.chain_id):
            out["nansen_skipped_chain"] += 1
            continue
        for a in graph.recent_origins(ch.subgraph_id, pool.pool_id):
            addrs.setdefault(a, pool.chain_id)
    if not addrs:
        return None
    labeled = []
    for a, cid in list(addrs.items())[:10]:   # credit cap per project
        labs = _labels_cached(conn, ctx, nansen, a, cid, out)
        if labs:
            labeled.append({"address": a, "chain_id": cid, "labels": labs})
    out["nansen"] += 1
    return {"addresses_checked": min(len(addrs), 10), "labeled": labeled,
            "smart_money": any(is_smart_money(x["labels"]) for x in labeled)}


def _labels_cached(conn, ctx, nansen, address: str, chain_id: int, out) -> list[str]:
    """wallet_labels is the shared cache with the activity step: a fresh row costs no credits; a paid answer is stored."""
    address = address.lower()
    from ..clients import nansen as N
    days = N.relabel_days()
    row = conn.execute(sa.select(wallet_labels.c.labels, wallet_labels.c.as_of)
                       .where(wallet_labels.c.address == address, wallet_labels.c.chain_id == chain_id)).first()
    if row is not None and as_utc(row.as_of) >= ctx.now - timedelta(days=days):
        out["nansen_cached"] = out.get("nansen_cached", 0) + 1
        return list(row.labels or [])
    if N.budget_left(conn, ctx.now) < N.cost_per_hit():
        out["nansen_budget_stop"] = 1
        return []
    labs = list(nansen.labels(address, chain_id) or [])
    upsert(conn, wallet_labels, [{"address": address, "chain_id": chain_id, "labels": labs, "smart_money": is_smart_money(labs),
                                  "source": getattr(nansen, "source", "nansen"), "as_of": ctx.now}], keys=["address", "chain_id"])
    return labs


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


def _blurb_ctx(conn, p, daily) -> dict:
    sig = {r.kind: r.value for r in conn.execute(sa.select(signals.c.kind, signals.c.value).where(signals.c.project_id == p.id))}
    rec = conn.execute(sa.select(receipts.c.title, receipts.c.source, receipts.c.published_at)
                       .where(receipts.c.project_id == p.id).order_by(receipts.c.published_at.desc()).limit(5)).all()
    return {
        "name": p.name, "category": p.category, "what_line": p.what_line,
        "metrics": {"tvl_usd": daily.tvl_usd, "vol_7d": daily.vol_7d, "vol_7d_prev": daily.vol_7d_prev,
                    "swaps_7d": daily.swaps_7d, "pools_n": daily.pools_n, "is_new": daily.is_new},
        "signals": sig, "receipts": [dict(r._mapping) for r in rec],
        "enrichment": (p.enrichment or {}).get("parallel"),
    }
