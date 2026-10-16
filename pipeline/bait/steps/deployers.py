"""Deployer lookup (part of step group): fill hooks.deployer for grouped hooks the registry did not supply one for,
from the explorer's contract creator. Bounded per run (DEPLOYER_LOOKUPS, 200), busiest projects first, newest hooks
first; a definitive miss (EOA, no record, no instance for the chain) is stamped and retried after RECHECK_DAYS, a
transient failure (429, 5xx, network) is not stamped and is asked again next run. An instance that blocks bots (403,
a Cloudflare challenge) is found with one probe and skipped for the night, so it cannot consume the budget; its hooks
are not stamped and the probe repeats next run. Never overwrites a registry-supplied deployer. The score step reads the result for deployer_swap_share_7d."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import sqlalchemy as sa

from ..db import hooks, project_daily
from . import RunContext, log

RECHECK_DAYS = 14
CANDIDATE_FACTOR = 5          # candidates read per cap slot, so blocked chains can be skipped and the cap still filled


def run(conn: sa.Connection, ctx: RunContext, out: dict) -> None:
    for k in ("deployers_checked", "deployers_found", "deployers_transient"):
        out.setdefault(k, 0)
    out.setdefault("deployers_blocked_chains", [])
    bs = ctx.client("blockscout")
    cap = _env_int("DEPLOYER_LOOKUPS", 200)
    if bs is None or cap == 0 or not callable(getattr(bs, "fetch_creator", None)):
        return
    has_instance = getattr(bs, "base_url", None)
    latest = conn.execute(sa.select(sa.func.max(project_daily.c.day))).scalar_one()
    vol = sa.select(project_daily.c.project_id, project_daily.c.vol_7d).where(project_daily.c.day == latest).subquery()
    stale = ctx.now - timedelta(days=RECHECK_DAYS)
    # Read well past the cap: a chain whose instance blocks us is skipped for the night, and the cap is then filled
    # from the chains that answer instead of being spent on hooks no one can look up.
    q = (sa.select(hooks.c.chain_id, hooks.c.address)
         .outerjoin(vol, vol.c.project_id == hooks.c.project_id)
         .where(hooks.c.project_id.is_not(None), hooks.c.deployer.is_(None),
                sa.or_(hooks.c.deployer_checked_at.is_(None), hooks.c.deployer_checked_at < stale))
         .order_by(sa.func.coalesce(vol.c.vol_7d, 0).desc(), hooks.c.first_seen_at.desc(), hooks.c.chain_id, hooks.c.address)
         .limit(cap * CANDIDATE_FACTOR))
    cands = [h for h in conn.execute(q).all() if not callable(has_instance) or has_instance(h.chain_id)]
    if not cands:
        return

    def one(h):
        try:
            return bs.fetch_creator(h.chain_id, h.address)
        except Exception as e:  # a client bug must not stop the group step; treat as transient
            log.warning("deployers: %s/%s lookup raised %s", h.chain_id, h.address, e)
            return None, "transient"

    # One sequential probe per chain, busiest chain first: a blocked instance costs one call a night, not the budget.
    results: list[tuple] = []
    probed: set[tuple] = set()
    blocked: set[int] = set()
    for h in cands:
        if len(results) >= cap:
            break
        if h.chain_id in blocked or any(p[0] == h.chain_id for p in probed):
            continue
        res = one(h)
        probed.add((h.chain_id, h.address))
        if res[1] == "blocked":
            blocked.add(h.chain_id)
        else:
            results.append((h, res))
    rest = [h for h in cands if h.chain_id not in blocked and (h.chain_id, h.address) not in probed][:max(0, cap - len(results))]
    with ThreadPoolExecutor(max_workers=6) as ex:      # network only; all writes stay on this thread
        results += list(zip(rest, ex.map(one, rest)))
    for h, (creator, outcome) in results:
        if outcome == "blocked":                       # an instance that blocks mid-run: same as the probe, no stamp
            blocked.add(h.chain_id)
            continue
        out["deployers_checked"] += 1
        if outcome == "transient":
            out["deployers_transient"] += 1
            continue
        vals = {"deployer_checked_at": ctx.now}
        if creator:
            vals.update(deployer=creator.lower(), deployer_source="explorer")
            out["deployers_found"] += 1
        conn.execute(sa.update(hooks).values(**vals).where(hooks.c.address == h.address, hooks.c.chain_id == h.chain_id))
    out["deployers_blocked_chains"] = sorted(set(out["deployers_blocked_chains"]) | blocked)
    log.info("deployers: checked %d hooks, %d creators found, %d transient, blocked chains %s", out["deployers_checked"],
             out["deployers_found"], out["deployers_transient"], out["deployers_blocked_chains"] or "none")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default
