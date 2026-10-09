"""Step 1: hooklist.json -> hooks upsert. first_seen_at only set on insert."""
from __future__ import annotations

import sqlalchemy as sa

from ..clients import hooklist
from ..db import hooks, upsert
from . import RunContext, log

UPDATE_COLS = ["name", "description", "flags", "properties", "verified_source", "audit_url", "deployer"]


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"hooks": 0, "new": 0, "updated": 0, "invalid": 0, "errors": 0}
    try:
        entries, out["invalid"] = hooklist.load_entries(path=ctx.hooklist_file)
    except Exception as e:  # registry unreachable: keep last night's hooks, let the other steps run
        out["errors"] = 1
        log.error("ingest: hooklist unavailable, keeping existing hooks: %s", e)
        return out
    existing = {(r.address, r.chain_id) for r in conn.execute(sa.select(hooks.c.address, hooks.c.chain_id))}
    rows, seen = [], set()
    for e in entries:
        key = (e.hook.address.lower(), e.hook.chainId)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "address": key[0], "chain_id": key[1], "name": e.hook.name or None,
            "description": e.hook.description or None, "flags": e.flags, "properties": e.properties,
            "verified_source": e.hook.verifiedSource, "audit_url": e.hook.auditUrl or None,
            "deployer": (e.hook.deployer or "").lower() or None, "first_seen_at": ctx.now,
        })
    for i in range(0, len(rows), 500):
        upsert(conn, hooks, rows[i:i + 500], keys=["address", "chain_id"], update=UPDATE_COLS)
    new = [r for r in rows if (r["address"], r["chain_id"]) not in existing]
    out.update(hooks=len(rows), new=len(new), updated=len(rows) - len(new))
    if new:
        log.info("ingest new hooks: %s", ", ".join(f"{r['name'] or r['address'][:10]}@{r['chain_id']}" for r in new[:10]))
    return out
