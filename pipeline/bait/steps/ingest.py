"""Step 1: hooklist.json -> hooks upsert. first_seen_at only set on insert. hooks.deployer: see _registry_deployers."""
from __future__ import annotations

import sqlalchemy as sa

from ..clients import hooklist
from ..db import hooks, upsert
from ..rules import deployer_or_none
from . import RunContext, log

UPDATE_COLS = ["name", "description", "flags", "properties", "verified_source", "audit_url", "hooklisted"]


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"hooks": 0, "new": 0, "updated": 0, "invalid": 0, "errors": 0, "delisted": 0, "regroup": 0}
    try:
        entries, out["invalid"] = hooklist.load_entries(path=ctx.hooklist_file)
    except Exception as e:  # registry unreachable: keep last night's hooks, let the other steps run
        out["errors"] = 1
        log.error("ingest: hooklist unavailable, keeping existing hooks: %s", e)
        return out
    prev = {(r.address, r.chain_id): r for r in conn.execute(
        sa.select(hooks.c.address, hooks.c.chain_id, hooks.c.name, hooks.c.hooklisted, hooks.c.project_id))}
    existing = set(prev)
    rows, seen = [], set()
    for e in entries:
        key = (e.hook.address.lower(), e.hook.chainId)
        if key in seen:
            continue
        seen.add(key)
        deployer = deployer_or_none(e.hook.deployer)      # a shared CREATE2 deployer is no deployer
        rows.append({
            "address": key[0], "chain_id": key[1], "name": e.hook.name or None,
            "description": e.hook.description or None, "flags": e.flags, "properties": e.properties,
            "verified_source": e.hook.verifiedSource, "audit_url": e.hook.auditUrl or None,
            "deployer": deployer, "first_seen_at": ctx.now, "hooklisted": True,
            "deployer_source": "hooklist" if deployer else None,
        })
    for i in range(0, len(rows), 500):
        upsert(conn, hooks, rows[i:i + 500], keys=["address", "chain_id"], update=UPDATE_COLS)
    _registry_deployers(conn, rows)
    listed = {(r["address"], r["chain_id"]) for r in rows}
    stale = [(a, c) for (a, c) in existing if (a, c) not in listed]        # discovered on-chain, or dropped from the registry
    for i in range(0, len(stale), 500):
        conn.execute(sa.update(hooks).values(hooklisted=False)
                     .where(sa.tuple_(hooks.c.address, hooks.c.chain_id).in_(stale[i:i + 500])))
    out["delisted"] = len(stale)
    # A hook grouped before the registry knew it ("Unlisted hook ..." or its verified-source name) is regrouped by the
    # group step once the registry names it or lists it.
    named = {(r["address"], r["chain_id"]): r["name"] for r in rows}
    regroup = [k for k, nm in named.items() if (p := prev.get(k)) is not None and p.project_id is not None
               and ((nm and not (p.name or "").strip()) or p.hooklisted is False)]
    for i in range(0, len(regroup), 500):
        conn.execute(sa.update(hooks).values(regroup=True)
                     .where(sa.tuple_(hooks.c.address, hooks.c.chain_id).in_(regroup[i:i + 500])))
    out["regroup"] = len(regroup)
    new = [r for r in rows if (r["address"], r["chain_id"]) not in existing]
    out.update(hooks=len(rows), new=len(new), updated=len(rows) - len(new))
    if new:
        log.info("ingest new hooks: %s", ", ".join(f"{r['name'] or r['address'][:10]}@{r['chain_id']}" for r in new[:10]))
    return out


def _registry_deployers(conn: sa.Connection, rows: list[dict]) -> None:
    """The registry's deployer wins wherever it names one. Where it names none (most entries), the deployer the lookup
    found (steps/deployers.py) stays: rewriting it to NULL every night, as this step did until 2026-10-24, kept a
    looked-up deployer for one day in fourteen. A deployer the registry used to name and no longer does is cleared
    with its stamp, so the lookup fills it."""
    named = [{"a": r["address"], "c": r["chain_id"], "d": r["deployer"]} for r in rows if r["deployer"]]
    if named:
        conn.execute(sa.update(hooks).where(hooks.c.address == sa.bindparam("a"), hooks.c.chain_id == sa.bindparam("c"))
                     .values(deployer=sa.bindparam("d"), deployer_source="hooklist"), named)
    unnamed = [(r["address"], r["chain_id"]) for r in rows if not r["deployer"]]
    for i in range(0, len(unnamed), 500):
        conn.execute(sa.update(hooks)
                     .where(sa.tuple_(hooks.c.address, hooks.c.chain_id).in_(unnamed[i:i + 500]), hooks.c.deployer.is_not(None),
                            sa.or_(hooks.c.deployer_source.is_(None), hooks.c.deployer_source == "hooklist"))
                     .values(deployer=None, deployer_source=None, deployer_checked_at=None))
