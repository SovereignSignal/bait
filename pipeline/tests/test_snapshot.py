import json

import httpx
import sqlalchemy as sa

from hookfeed import db
from hookfeed.clients.graph import Graph
from hookfeed.config import ZERO_ADDRESS
from hookfeed.steps import snapshot


def pool(i, hook="0x0000fe59823933ac763611a69c88f91d45f81888"):
    return {"id": f"0x{i:064x}", "hooks": hook, "feeTier": "3000", "tickSpacing": "60", "createdAtTimestamp": "1759000000",
            "totalValueLockedUSD": str(1000 + i), "volumeUSD": str(10 * i), "txCount": str(i),
            "token0": {"id": "0xa", "symbol": "WETH", "name": "Wrapped Ether", "decimals": "18"},
            "token1": {"id": "0xb", "symbol": "USDC", "name": "USD Coin", "decimals": "6"}}


def make_graph(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.path, request.headers.get("authorization"), body["variables"]))
        if "_meta" in body["query"]:
            return httpx.Response(200, json={"data": {"_meta": {"block": {"number": 1}, "deployment": "Qm", "hasIndexingErrors": False}}})
        assert body["variables"]["zero"] == ZERO_ADDRESS
        last = body["variables"]["lastId"]
        if last == "":
            page = [pool(i) for i in range(1, 1001)]           # full page -> client must paginate
        elif last == f"0x{1000:064x}":
            page = [pool(1001), pool(1002)]                      # short page -> stop
        else:
            page = []
        return httpx.Response(200, json={"data": {"pools": page}})
    return Graph("test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_snapshot_paginates_and_writes_pools_and_snapshots(conn, ctx):
    seen = []
    ctx.clients["graph"] = make_graph(seen)
    ctx.chains = [8453]
    out = snapshot.run(conn, ctx)
    assert out == {"chains": 1, "skipped": 0, "pools": 1002, "errors": 0, "keyless_chains": 0, "keyless_pools": 0}
    assert all(auth == "Bearer test-key" for _, auth, _ in seen)
    assert "/subgraphs/id/Gqm2b5J85n1bhCyDMpGbtbVn4935EvvdyHdHrx3dibyj" in seen[0][0]
    assert [v["lastId"] for _, _, v in seen[1:]] == ["", f"0x{1000:064x}"]
    assert conn.execute(sa.select(sa.func.count()).select_from(db.pools)).scalar_one() == 1002
    snaps = conn.execute(sa.select(db.pool_snapshots).where(db.pool_snapshots.c.day == ctx.day)).all()
    assert len(snaps) == 1002
    p = conn.execute(sa.select(db.pools).where(db.pools.c.pool_id == f"0x{7:064x}")).one()
    assert p.chain_id == 8453 and p.fee == 3000 and p.tick_spacing == 60 and p.token1["symbol"] == "USDC"
    s = next(x for x in snaps if x.pool_id == f"0x{7:064x}")
    assert (s.tvl_usd, s.volume_usd_cum, s.tx_count_cum) == (1007.0, 70.0, 7)
    # idempotent per day
    snapshot.run(conn, ctx)
    assert conn.execute(sa.select(sa.func.count()).select_from(db.pool_snapshots)).scalar_one() == 1002


def test_snapshot_skips_chains_without_subgraph(conn, ctx):
    seen = []
    ctx.clients["graph"] = make_graph(seen)
    ctx.chains = [4663, 57073]     # robinhood, ink
    out = snapshot.run(conn, ctx)
    assert out == {"chains": 0, "skipped": 2, "pools": 0, "errors": 0, "keyless_chains": 0, "keyless_pools": 0}
    assert seen == []


def test_snapshot_no_key_skips_everything(conn, ctx):
    out = snapshot.run(conn, ctx)
    assert out["chains"] == 0 and out["skipped"] == 10
