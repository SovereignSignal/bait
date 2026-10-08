"""Envio HyperSync over its JSON HTTP API with plain httpx (no client library).

Request (docs.envio.dev/docs/HyperSync/hypersync-query, /hypersync-curl-examples):
    POST https://<chain>.hypersync.xyz/query
    Authorization: Bearer <HYPERSYNC_API_TOKEN>     (missing/bad token -> 401 "Your token is malformed")
    {"from_block": N, "to_block": M (exclusive), "logs": [{"address": [...], "topics": [[topic0], [topic1 values]]}],
     "field_selection": {"block": [...], "log": [...], "transaction": [...]}, "max_num_logs": K}
Response: {"data": [{"blocks": [...], "transactions": [...], "logs": [...]}, ...], "archive_height": H,
           "next_block": B, "total_execution_time": ms, "rollback_guard": {...} | null}
Each call scans up to ~5 s of data; resume from next_block (exclusive end of what was scanned, no gaps, no dupes).
Default join mode returns the transactions and blocks of matched logs, which is how Swap logs get tx `from`
and block timestamps. GET /height -> {"height": H}.
"""
from __future__ import annotations

import logging
import time
from typing import Iterable

import httpx

from .decode import INITIALIZE_TOPIC, SWAP_TOPIC, num

log = logging.getLogger("bait.indexer")

LOG_FIELDS = ["block_number", "log_index", "transaction_hash", "topic0", "topic1", "topic2", "topic3", "data"]
BLOCK_FIELDS = ["number", "timestamp"]
MAX_NUM_LOGS = 20_000


class HyperSyncAuthError(RuntimeError):
    pass


class HyperSync:
    def __init__(self, url: str, token: str, client: httpx.Client, retries: int = 5, sleep=time.sleep):
        self.url = url.rstrip("/")
        self.token = token
        self.client = client
        self.retries = retries
        self.sleep = sleep
        self.calls = 0

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "User-Agent": "bait-indexer/0.1"}

    def _do(self, method: str, path: str, body: dict | None = None) -> dict:
        last: Exception | None = None
        for attempt in range(self.retries):
            self.calls += 1
            try:
                r = self.client.request(method, self.url + path, json=body, headers=self._headers(), timeout=60)
            except httpx.HTTPError as e:
                last = e
                self.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code in (401, 403):
                raise HyperSyncAuthError(f"{self.url} HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code == 429:
                reset = r.headers.get("x-ratelimit-reset")
                try:
                    wait = float(reset) if reset else 10.0
                except ValueError:
                    wait = 10.0
                self.sleep(min(max(wait, 1.0), 60.0))
                last = RuntimeError(f"{self.url} HTTP 429")
                continue
            if r.status_code >= 500:
                last = RuntimeError(f"{self.url} HTTP {r.status_code}")
                self.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{self.url}{path} HTTP {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError(f"{self.url}{path} failed after {self.retries} attempts: {last}")

    def height(self) -> int:
        return int(num(self._do("GET", "/height")["height"]))

    def query(self, body: dict) -> dict:
        return self._do("POST", "/query", body)

    def block_timestamp(self, block: int) -> int | None:
        res = self.query({"from_block": block, "to_block": block + 1, "include_all_blocks": True,
                          "field_selection": {"block": BLOCK_FIELDS}})
        for b in iter_rows(res, "blocks"):
            if num(b.get("number")) == block:
                return num(b.get("timestamp"))
        return None

    def block_at_or_after(self, ts: int, head: int) -> int:
        """First block whose timestamp >= ts (binary search on block timestamps, ~25 calls once per chain)."""
        lo, hi = 0, head
        t_head = self.block_timestamp(head)
        if t_head is None or t_head < ts:
            return head
        # narrow with the average block time first so the search starts close
        span = min(head, 1_000_000)
        t_back = self.block_timestamp(head - span) if span else None
        if t_back is not None and span:
            bt = max((t_head - t_back) / span, 0.001)
            est = int(head - (t_head - ts) / bt)
            for width in (2_000, 50_000, 1_000_000):
                a, b = max(0, est - width), min(head, est + width)
                ta, tb = self.block_timestamp(a), self.block_timestamp(b)
                if ta is not None and tb is not None and ta < ts <= tb:
                    lo, hi = a, b
                    break
        while hi - lo > 1:
            mid = (lo + hi) // 2
            tm = self.block_timestamp(mid)
            if tm is None or tm < ts:
                lo = mid
            else:
                hi = mid
        return hi


def iter_rows(res: dict, kind: str) -> Iterable[dict]:
    """data is a list of per-block-group batches; tolerate a single dict too."""
    data = res.get("data") or []
    if isinstance(data, dict):
        data = [data]
    for batch in data:
        for row in (batch or {}).get(kind) or []:
            yield row


def initialize_query(pool_manager: str, from_block: int, to_block: int) -> dict:
    return {
        "from_block": from_block, "to_block": to_block,
        "logs": [{"address": [pool_manager], "topics": [[INITIALIZE_TOPIC]]}],
        "field_selection": {"block": BLOCK_FIELDS, "log": LOG_FIELDS},
        "max_num_logs": MAX_NUM_LOGS,
    }


def swap_query(pool_manager: str, from_block: int, to_block: int, pool_ids: list[str] | None = None) -> dict:
    topics = [[SWAP_TOPIC]]
    if pool_ids:
        topics.append(list(pool_ids))
    return {
        "from_block": from_block, "to_block": to_block,
        "logs": [{"address": [pool_manager], "topics": topics}],
        "field_selection": {"block": BLOCK_FIELDS, "log": LOG_FIELDS, "transaction": ["hash", "from"]},
        "max_num_logs": MAX_NUM_LOGS,
    }


def page(res: dict) -> tuple[list[dict], dict[int, int], dict[str, str], int | None, int | None]:
    """-> (logs, block number -> timestamp, tx hash -> from, next_block, archive_height)."""
    blocks = {num(b.get("number")): num(b.get("timestamp")) for b in iter_rows(res, "blocks") if b.get("number") is not None}
    txs = {str(t["hash"]).lower(): str(t["from"]).lower() for t in iter_rows(res, "transactions") if t.get("hash") and t.get("from")}
    logs = list(iter_rows(res, "logs"))
    return logs, blocks, txs, num(res.get("next_block")), num(res.get("archive_height"))
