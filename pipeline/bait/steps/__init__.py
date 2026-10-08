"""Pipeline steps. Each exposes run(conn, ctx) -> dict of counts. Idempotent per ctx.day."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from ..db import utcnow

log = logging.getLogger("bait")

STEP_ORDER = ["ingest", "snapshot", "activity", "onchain", "group", "enrich", "score", "signals", "radar", "digest"]


@dataclass
class RunContext:
    day: date
    chains: list[int] | None = None          # chain ids; None = all
    hooklist_file: str | None = None
    force_digest: bool = False
    max_code_fetch: int = 300                # Blockscout code lookups per run (group)
    signals_limit: int = 50                  # New ∪ Trending projects per night (signals)
    now: datetime = field(default_factory=utcnow)
    clients: dict = field(default_factory=dict)   # name -> client instance or None (injected in tests)
    extras: dict = field(default_factory=dict)    # step-to-step hand-offs within one run (e.g. keyless pool stats)

    def client(self, name: str):
        """Lazy from_env() per client; tests pre-populate ctx.clients."""
        if name in self.clients:
            return self.clients[name]
        from ..clients import blockscout, claude, exa, github, graph, nansen, parallel
        factory = {
            "graph": graph.Graph.from_env, "blockscout": blockscout.Blockscout.from_env,
            "claude": claude.Claude.from_env, "exa": exa.Exa.from_env, "parallel": parallel.Parallel.from_env,
            "nansen": nansen.Nansen.from_env, "github": github.GitHub.from_env,
        }[name]
        c = factory()
        if c is None:
            log.info("client=%s skipped: no key in env", name)
        self.clients[name] = c
        return c
