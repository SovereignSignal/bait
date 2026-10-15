"""Deployer lookup (part of step group): fill hooks.deployer for grouped hooks the registry did not supply one for,
from the explorer's contract creator. Bounded per run (DEPLOYER_LOOKUPS, 200), busiest projects first, newest hooks
first; a definitive miss (EOA, no record, no instance for the chain) is stamped and retried after RECHECK_DAYS, a
transient failure (bot challenge, 5xx, network) is not stamped and is asked again next run. Never overwrites a
registry-supplied deployer. The score step reads the result for deployer_swap_share_7d."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import sqlalchemy as sa

from ..db import hooks, project_daily
from . import RunContext, log

RECHECK_DAYS = 14


def run(conn: sa.Connection, ctx: RunContext, out: dict) -> None:
    out.setdefault("deployers_checked", 0)
    out.setdefault("deployers_found", 0)
    out.setdefault("deployers_transient", 0)
    bs = ctx.client("blockscout")
    cap = _env_int("DEPLOYER_LOOKUPS", 200)
    if bs is None or cap == 0 or not callable(getattr(bs, "fetch_creator", None)):
        return
    has_instance = getattr(bs, "base_url", None)
    latest = conn.execute(sa.select(sa.func.max(project_daily.c.day))).scalar_one()
    vol = sa.select(project_daily.c.project_id, project_daily.c.vol_7d).where(project_daily.c.day == latest).subquery()
    stale = ctx.now - timedelta(days=RECHECK_DAYS)
    q = (sa.select(hooks.c.chain_id, hooks.c.address)
         .outerjoin(vol, vol.c.project_id == hooks.c.project_id)
         .where(hooks.c.project_id.is_not(None), hooks.c.deployer.is_(None),
                sa.or_(hooks.c.deployer_checked_at.is_(None), hooks.c.deployer_checked_at < stale))
         .order_by(sa.func.coalesce(vol.c.vol_7d, 0).desc(), hooks.c.first_seen_at.desc(), hooks.c.chain_id, hooks.c.address)
         .limit(cap))
    todo = [h for h in conn.execute(q).all() if not callable(has_instance) or has_instance(h.chain_id)]
    if not todo:
        return

    def one(h):
        try:
            return bs.fetch_creator(h.chain_id, h.address)
        except Exception as e:  # a client bug must not stop the group step; treat as transient
            log.warning("deployers: %s/%s lookup raised %s", h.chain_id, h.address, e)
            return None, False

    with ThreadPoolExecutor(max_workers=6) as ex:      # network only; all writes stay on this thread
        found = list(ex.map(one, todo))
    for h, (creator, definitive) in zip(todo, found):
        if not definitive:
            out["deployers_transient"] += 1
            continue
        vals = {"deployer_checked_at": ctx.now}
        if creator:
            vals.update(deployer=creator.lower(), deployer_source="explorer")
            out["deployers_found"] += 1
        conn.execute(sa.update(hooks).values(**vals).where(hooks.c.address == h.address, hooks.c.chain_id == h.chain_id))
    out["deployers_checked"] += len(todo)
    log.info("deployers: checked %d hooks, %d creators found, %d transient", len(todo), out["deployers_found"],
             out["deployers_transient"])


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default
