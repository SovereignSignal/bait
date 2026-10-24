"""Deployer lookup (part of step group): fill hooks.deployer for grouped hooks the registry did not supply one for.

The deployer is the sender of the hook's creation transaction: the wallet, never a CREATE2 proxy or a factory, and the
same tx.from the indexer counts swaps by. Sources, in order: Sourcify (keyless, every chain: deployment.deployer of a
verified contract), then the chain's Blockscout instance for what Sourcify cannot name (unverified, or verified from
the runtime alone with no deployment record): the address record's creation transaction and its sender. A shared
CREATE2 deployer (rules.SHARED_DEPLOYERS) is never stored, whichever source gives it; such a hook stays unknown.

Bounded per run (DEPLOYER_LOOKUPS, 200), busiest projects first, newest hooks first. A hook is stamped
(deployer_checked_at) once a source answered and none failed transiently, and a miss is retried after RECHECK_DAYS; a
transient failure (429, 5xx, network) is not stamped and is asked again next run. A Blockscout instance that blocks
bots (403, a Cloudflare challenge) is found with one probe and skipped for the night. A hook it leaves open is stamped
when Sourcify answered it (not verified there either: rechecked with the other misses, rather than asked again every
night ahead of hooks that can be answered); without Sourcify it is not stamped and does not count against the cap.
Never overwrites a registry-supplied deployer. The score step reads the result for deployer_swap_share_7d."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import sqlalchemy as sa

from ..db import hooks, project_daily
from ..rules import SHARED_DEPLOYERS, deployer_or_none
from . import RunContext, log

RECHECK_DAYS = 14
CANDIDATE_FACTOR = 5          # explorer-only runs: candidates read per cap slot, so blocked chains can be skipped
SOURCES = ("sourcify", "explorer")   # hooks.deployer_source for each lookup, in the order they are asked


def run(conn: sa.Connection, ctx: RunContext, out: dict) -> None:
    for k in ("deployers_checked", "deployers_found", "deployers_transient", "deployers_sourcify", "deployers_cleared"):
        out.setdefault(k, 0)
    out.setdefault("deployers_blocked_chains", [])
    out["deployers_cleared"] += _clear_shared(conn)
    src = _usable(ctx.client("sourcify"), "fetch_deployer")
    bs = _usable(ctx.client("blockscout"), "fetch_creator")
    cap = _env_int("DEPLOYER_LOOKUPS", 200)
    if cap == 0 or (src is None and bs is None):
        return
    base_url = getattr(bs, "base_url", None)

    def has_instance(chain_id: int) -> bool:
        return bs is not None and (not callable(base_url) or bool(base_url(chain_id)))

    latest = conn.execute(sa.select(sa.func.max(project_daily.c.day))).scalar_one()
    vol = sa.select(project_daily.c.project_id, project_daily.c.vol_7d).where(project_daily.c.day == latest).subquery()
    stale = ctx.now - timedelta(days=RECHECK_DAYS)
    # Sourcify answers every chain, so the cap is the first `cap` hooks. Without it, read well past the cap: a chain
    # whose instance blocks us is skipped for the night, and the cap is filled from the chains that answer.
    q = (sa.select(hooks.c.chain_id, hooks.c.address)
         .outerjoin(vol, vol.c.project_id == hooks.c.project_id)
         .where(hooks.c.project_id.is_not(None), hooks.c.deployer.is_(None),
                sa.or_(hooks.c.deployer_checked_at.is_(None), hooks.c.deployer_checked_at < stale))
         .order_by(sa.func.coalesce(vol.c.vol_7d, 0).desc(), hooks.c.first_seen_at.desc(), hooks.c.chain_id, hooks.c.address)
         .limit(cap if src is not None else cap * CANDIDATE_FACTOR))
    cands = [h for h in conn.execute(q).all() if src is not None or has_instance(h.chain_id)]
    if not cands:
        return

    answers: dict[tuple, dict[str, tuple]] = {}      # (chain_id, address) -> {source: (deployer, outcome)}
    queue = cands
    if src is not None:
        with ThreadPoolExecutor(max_workers=6) as ex:   # network only (Sourcify paces itself); writes stay on this thread
            for h, res in zip(cands, ex.map(lambda h: _ask(src.fetch_deployer, h), cands)):
                answers[(h.chain_id, h.address)] = {"sourcify": res}
        queue = [h for h in cands if not _named(answers[(h.chain_id, h.address)]["sourcify"]) and has_instance(h.chain_id)]
    blocked = _explorer(bs, queue, cap, answers) if bs is not None and queue else set()

    for h in cands:
        a = answers.get((h.chain_id, h.address))
        if not a:                                      # explorer-only run: past the cap or skipped on a blocked chain
            continue
        deployer, source, outcome = _settle(a)
        if outcome == "blocked":                       # nobody answered: same as before Sourcify, no stamp, not counted
            continue
        out["deployers_checked"] += 1
        if outcome == "transient":
            out["deployers_transient"] += 1
            continue
        vals = {"deployer_checked_at": ctx.now}
        if deployer:
            vals.update(deployer=deployer, deployer_source=source)
            out["deployers_found"] += 1
            out["deployers_sourcify"] += source == "sourcify"
        conn.execute(sa.update(hooks).values(**vals).where(hooks.c.address == h.address, hooks.c.chain_id == h.chain_id))
    out["deployers_blocked_chains"] = sorted(set(out["deployers_blocked_chains"]) | blocked)
    log.info("deployers: checked %d hooks, %d deployers found (%d from Sourcify), %d transient, blocked chains %s",
             out["deployers_checked"], out["deployers_found"], out["deployers_sourcify"], out["deployers_transient"],
             out["deployers_blocked_chains"] or "none")


def _explorer(bs, queue: list, cap: int, answers: dict) -> set[int]:
    """Blockscout for the hooks in queue, into answers[key]["explorer"]; returns the chains whose instance blocked.
    One sequential probe per chain, busiest chain first: a blocked instance costs one call a night, not the budget.
    With Sourcify the queue is already within the cap; hooks left on a blocked chain are marked blocked."""
    def ask(h):
        return _ask(bs.fetch_creator, h)

    done = 0
    probed: set[tuple] = set()
    blocked: set[int] = set()
    for h in queue:
        if done >= cap:
            break
        if h.chain_id in blocked or any(c == h.chain_id for c, _ in probed):
            continue
        res = ask(h)
        probed.add((h.chain_id, h.address))
        answers.setdefault((h.chain_id, h.address), {})["explorer"] = res
        if res[1] == "blocked":
            blocked.add(h.chain_id)
        else:
            done += 1
    rest = [h for h in queue if h.chain_id not in blocked and (h.chain_id, h.address) not in probed][:max(0, cap - done)]
    with ThreadPoolExecutor(max_workers=6) as ex:      # network only; all writes stay on this thread
        for h, res in zip(rest, ex.map(ask, rest)):
            answers.setdefault((h.chain_id, h.address), {})["explorer"] = res
            if res[1] == "blocked":                     # an instance that blocks mid-run: same as the probe
                blocked.add(h.chain_id)
    for h in queue:                                    # Sourcify asked these; the explorer was skipped as blocked
        a = answers.get((h.chain_id, h.address))
        if a is not None and "sourcify" in a and "explorer" not in a and h.chain_id in blocked:
            a["explorer"] = (None, "blocked")
    return blocked


def _settle(a: dict[str, tuple]) -> tuple[str | None, str | None, str]:
    """(deployer, source, outcome) from each source's (deployer, outcome). The first source to name a usable deployer
    wins; else a transient failure anywhere keeps the hook open ("transient"); else a source that answered makes it a
    definitive miss, stamped ("miss": a rejected shared CREATE2 deployer counts as an answer); else "blocked"."""
    for source in SOURCES:
        if dep := _named(a.get(source, (None, None))):
            return dep, source, "found"
    outcomes = {o for _, o in a.values()}
    if "transient" in outcomes:
        return None, None, "transient"
    if outcomes & {"miss", "found"}:
        return None, None, "miss"
    return None, None, "blocked"


def _named(answer: tuple) -> str | None:
    """The usable deployer of one source's answer: found, and a wallet address that is not a shared CREATE2 deployer."""
    dep, outcome = answer
    return deployer_or_none(dep) if outcome == "found" else None


def _ask(fetch, h) -> tuple[str | None, str]:
    try:
        return fetch(h.chain_id, h.address)
    except Exception as e:  # a client bug must not stop the group step; treat as transient
        log.warning("deployers: %s/%s lookup raised %s", h.chain_id, h.address, e)
        return None, "transient"


def _clear_shared(conn: sa.Connection) -> int:
    """A shared CREATE2 deployer stored before it was rejected (an explorer creator, a registry value) is nobody's
    wallet: forget it and the stamp, so the hook is looked up again."""
    return conn.execute(sa.update(hooks).where(hooks.c.deployer.in_(sorted(SHARED_DEPLOYERS)))
                        .values(deployer=None, deployer_source=None, deployer_checked_at=None)).rowcount or 0


def _usable(client, method: str):
    return client if callable(getattr(client, method, None)) else None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default
