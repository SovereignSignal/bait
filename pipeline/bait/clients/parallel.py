"""Parallel Task API: one enrichment run per new project (team, website, github, X, audit)."""
from __future__ import annotations

import time

from ..config import env
from . import http


class RunGone(Exception):
    """The run id is unknown to Parallel (HTTP 404): failed or expired. It will never deliver; do not poll again."""

OUTPUT_SCHEMA = {
    "type": "json",
    "json_schema": {
        "type": "object",
        "properties": {
            "team": {"type": "string", "description": "Team or company behind the project, or empty"},
            "website": {"type": "string", "description": "Official website URL, or empty"},
            "github": {"type": "string", "description": "GitHub org or repo URL, or empty"},
            "x_handle": {"type": "string", "description": "X/Twitter handle without @, or empty"},
            "audit_url": {"type": "string", "description": "Public audit report URL, or empty"},
            "description": {"type": "string", "description": "One paragraph on what the project does"},
        },
        "required": ["team", "website", "github", "x_handle", "audit_url", "description"],
        "additionalProperties": False,
    },
}


class Parallel:
    def __init__(self, api_key: str, http_client=None, processor: str = "core", poll_s: float = 5.0, max_wait_s: float = 600):
        self.c = http_client or http.client(timeout=120.0)
        self.headers = {"x-api-key": api_key}
        self.processor, self.poll_s, self.max_wait_s = processor, poll_s, max_wait_s
        from ..costs import NullMeter
        self.meter = NullMeter()

    @classmethod
    def from_env(cls, http_client=None):
        key = env("PARALLEL_API_KEY")
        return cls(key, http_client) if key else None

    # GET .../result blocks until the run completes (server default timeout=600 s). We ask the server to give up
    # after RESULT_WAIT_S so it answers 408 "still running" well before the client read timeout, then ask again.
    RESULT_WAIT_S = 60

    def submit(self, project_name: str, hints: str) -> str:
        """Start a run (this is the billed step) and return its run_id. Budget-checked and recorded."""
        from ..costs import parallel_usd
        price = parallel_usd(self.processor)
        self.meter.check("parallel", price)
        run = http.post_json(self.c, "https://api.parallel.ai/v1/tasks/runs", headers=self.headers, json={
            "input": f"Uniswap v4 hook project '{project_name}'. Known facts: {hints}",
            "task_spec": {"output_schema": OUTPUT_SCHEMA},
            "processor": self.processor,
        })
        run_id = run["run_id"]
        self.meter.record("parallel", price, detail=f"{self.processor} run={run_id} {project_name[:60]}")
        return run_id

    def result(self, run_id: str, max_wait_s: float | None = None) -> dict | None:
        """Fetch a run's output (free). None if it is still running at the deadline; the caller keeps run_id."""
        import httpx
        deadline = time.monotonic() + (self.max_wait_s if max_wait_s is None else max_wait_s)
        while time.monotonic() < deadline:
            try:
                r = self.c.get(f"https://api.parallel.ai/v1/tasks/runs/{run_id}/result",
                               params={"timeout": self.RESULT_WAIT_S}, headers=self.headers)
            except httpx.TimeoutException:
                continue                       # still running; never a failure
            if r.status_code == 200:
                d = r.json()
                out = (d.get("output") or {}).get("content") or {}
                return {k: (v or "") for k, v in out.items()} | {"_run_id": run_id}
            if r.status_code == 404:
                raise RunGone(run_id)
            if r.status_code not in (202, 408, 425):
                r.raise_for_status()
            time.sleep(self.poll_s)
        return None

    def enrich(self, project_name: str, hints: str) -> dict | None:
        return self.result(self.submit(project_name, hints))
