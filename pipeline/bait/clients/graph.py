"""Uniswap v4 subgraphs via The Graph gateway. Key goes in the Authorization header, not the URL."""
from __future__ import annotations

from typing import Iterator

from ..config import GRAPH_GATEWAY, ZERO_ADDRESS, env
from . import http

POOL_FIELDS = """
  id hooks feeTier tickSpacing createdAtTimestamp
  totalValueLockedUSD volumeUSD txCount
  token0 { id symbol name decimals } token1 { id symbol name decimals }
"""

POOLS_QUERY = """
query Pools($lastId: ID!, $zero: String!) {
  pools(first: 1000, orderBy: id, orderDirection: asc,
        where: { hooks_not: $zero, id_gt: $lastId }) { %s }
}""" % POOL_FIELDS

META_QUERY = "{ _meta { block { number } deployment hasIndexingErrors } }"

ORIGINS_QUERY = """
query Origins($pool: String!, $n: Int!) {
  swaps(first: $n, orderBy: timestamp, orderDirection: desc, where: { pool: $pool }) { origin }
  modifyLiquiditys(first: $n, orderBy: timestamp, orderDirection: desc, where: { pool: $pool }) { origin }
}"""


class Graph:
    def __init__(self, api_key: str, http_client=None):
        self.c = http_client or http.client(timeout=60.0)
        self.headers = {"Authorization": f"Bearer {api_key}"}

    @classmethod
    def from_env(cls, http_client=None):
        key = env("GRAPH_API_KEY")
        return cls(key, http_client) if key else None

    def query(self, subgraph_id: str, query: str, variables: dict | None = None) -> dict:
        url = GRAPH_GATEWAY.format(subgraph_id=subgraph_id)
        data = http.post_json(self.c, url, json={"query": query, "variables": variables or {}}, headers=self.headers)
        if data.get("errors"):
            raise RuntimeError(f"subgraph {subgraph_id}: {data['errors']}")
        return data["data"]

    def meta(self, subgraph_id: str) -> dict:
        return self.query(subgraph_id, META_QUERY)["_meta"]

    def hooked_pools(self, subgraph_id: str) -> Iterator[dict]:
        last_id = ""
        while True:
            page = self.query(subgraph_id, POOLS_QUERY, {"lastId": last_id, "zero": ZERO_ADDRESS})["pools"]
            if not page:
                return
            yield from page
            if len(page) < 1000:
                return
            last_id = page[-1]["id"]

    def recent_origins(self, subgraph_id: str, pool_id: str, n: int = 25) -> set[str]:
        d = self.query(subgraph_id, ORIGINS_QUERY, {"pool": pool_id, "n": n})
        return {x["origin"].lower() for x in d.get("swaps", []) + d.get("modifyLiquiditys", []) if x.get("origin")}
