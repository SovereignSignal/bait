"""On-chain indexer: decoding, HyperSync request shape, cursor resume, aggregation, prices, RPC fallback. No network:
HyperSync, RPC and DefiLlama are httpx.MockTransport fakes."""
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from bait import db, keyless as K
from bait.indexer import core, store
from bait.indexer.__main__ import Runner, heartbeat
from bait.indexer.chains import CHAIN_BY_ID, CHAINS, select_chains
from bait.indexer.decode import INITIALIZE_TOPIC, SWAP_TOPIC, ZERO, decode_initialize, decode_swap
from bait.indexer.hypersync import HyperSync, HyperSyncAuthError, swap_query
from bait.indexer.prices import PriceBook, reliable, swap_usd

BASE = CHAIN_BY_ID[8453]
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
START_DAY = date(2026, 9, 22)                       # NOW - 14 days
BT = 60                                             # fake chain: one block per minute
T0 = int(datetime(2026, 9, 22, tzinfo=timezone.utc).timestamp()) - 1000 * BT   # block 1000 = 2026-09-22 00:00 UTC
HEAD = 22_000


# ------------------------------------------------------------------ keccak (verifies the hardcoded topic hashes)

def _keccak256(data: bytes) -> str:
    rc = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
          0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
          0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
          0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
          0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
    rot = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
    m = (1 << 64) - 1
    rl = lambda v, n: ((v << n) | (v >> (64 - n))) & m if n else v
    rate = 136
    msg = bytearray(data) + b"\x01"
    while len(msg) % rate:
        msg.append(0)
    msg[-1] |= 0x80
    s = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        for i in range(rate // 8):
            s[i % 5][i // 5] ^= int.from_bytes(msg[off + 8 * i: off + 8 * i + 8], "little")
        for r in range(24):
            c = [s[x][0] ^ s[x][1] ^ s[x][2] ^ s[x][3] ^ s[x][4] for x in range(5)]
            d = [c[(x - 1) % 5] ^ rl(c[(x + 1) % 5], 1) for x in range(5)]
            s = [[s[x][y] ^ d[x] for y in range(5)] for x in range(5)]
            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    b[y][(2 * x + 3 * y) % 5] = rl(s[x][y], rot[x][y])
            s = [[b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
            s[0][0] ^= rc[r]
    return "0x" + b"".join(s[i % 5][i // 5].to_bytes(8, "little") for i in range(4)).hex()


def test_topic_hashes_are_keccak_of_v4_signatures():
    assert _keccak256(b"Transfer(address,address,uint256)") == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    assert _keccak256(b"Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)") == INITIALIZE_TOPIC
    assert _keccak256(b"Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)") == SWAP_TOPIC
    assert INITIALIZE_TOPIC == K.INITIALIZE


# ------------------------------------------------------------------ canned logs

def _w(v: int) -> str:
    return format(v % (1 << 256), "064x")


def _t(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:].lower()


def pid(n: int) -> str:
    return "0x" + format(n, "064x")


def init_log(pool: str, c0: str, c1: str, hook: str, block: int, fee=3000, spacing=60, tick=-887220, idx=0):
    return {"block_number": block, "log_index": idx, "transaction_hash": "0x" + format(block, "064x"),
            "topic0": INITIALIZE_TOPIC, "topic1": pool, "topic2": _t(c0), "topic3": _t(c1),
            "data": "0x" + _w(fee) + _w(spacing) + _w(int(hook, 16)) + _w(79228162514264337593543950336) + _w(tick)}


def swap_log(pool: str, block: int, a0: int, a1: int, sender="0x" + "e" * 40, idx=0):
    return {"block_number": block, "log_index": idx, "transaction_hash": "0x" + format(block * 100 + idx, "064x"),
            "topic0": SWAP_TOPIC, "topic1": pool, "topic2": _t(sender), "topic3": None,
            "data": "0x" + _w(a0) + _w(a1) + _w(1 << 96) + _w(10 ** 18) + _w(-5) + _w(500)}


HOOK = "0x" + "a" * 36 + "0080"
HOOK2 = "0x" + "b" * 36 + "00c0"
TOKX = "0x" + "c" * 40
PA, PB, PC = pid(0xA), pid(0xB), pid(0xC)
W1, W2, W3 = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40


def chain_events():
    inits = [init_log(PA, ZERO, USDC, HOOK, 500), init_log(PB, ZERO, USDC, ZERO, 600), init_log(PC, TOKX, USDC, HOOK2, 1500)]
    swaps = [
        (swap_log(PA, 900, -10 ** 18, 2000 * 10 ** 6), W1),           # before the 14-day window: not counted
        (swap_log(PA, 1100, -10 ** 18, 2000 * 10 ** 6), W1),          # 2026-09-22
        (swap_log(PA, 1200, 10 ** 18, -2000 * 10 ** 6, idx=1), W1),   # same wallet, other router sender
        (swap_log(PA, 1300, -(10 ** 18), 2000 * 10 ** 6), W2),
        (swap_log(PB, 1400, -10 ** 18, 2000 * 10 ** 6), W3),          # unhooked pool: ignored
        (swap_log(PC, 2500, 5 * 10 ** 18, -7 * 10 ** 6), W3),         # 2026-09-23, TOKX unpriced -> USDC side
    ]
    return inits, swaps


class FakeHyperSync:
    """Serves /height and /query like HyperSync: at most `span` blocks per response, next_block = end of scanned range."""

    def __init__(self, span=700, fail_after=None):
        self.inits, self.swaps = chain_events()
        self.span = span
        self.fail_after = fail_after
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != "Bearer test-token":
            return httpx.Response(401, json={"error": "Your token is malformed."})
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            return httpx.Response(503, text="down")
        if request.url.path == "/height":
            return httpx.Response(200, json={"height": HEAD})
        q = json.loads(request.content)
        frm, to = q["from_block"], q.get("to_block") or HEAD + 1
        if q.get("include_all_blocks"):
            return httpx.Response(200, json={"data": [{"blocks": [{"number": b, "timestamp": hex(T0 + b * BT)} for b in range(frm, to)]}],
                                             "archive_height": HEAD, "next_block": to})
        end = min(to, frm + self.span)
        sel = q["logs"][0]
        topic0 = sel["topics"][0][0]
        ids = set(sel["topics"][1]) if len(sel["topics"]) > 1 else None
        logs, txs = [], []
        src = [(lg, None) for lg in self.inits] if topic0 == INITIALIZE_TOPIC else self.swaps
        for lg, frm_addr in src:
            if frm <= lg["block_number"] < end and (ids is None or lg["topic1"] in ids):
                logs.append({k: lg[k] for k in q["field_selection"]["log"] if k in lg})
                if frm_addr and "transaction" in q["field_selection"]:
                    txs.append({"hash": lg["transaction_hash"], "from": frm_addr})
        blocks = [{"number": b, "timestamp": hex(T0 + b * BT)} for b in sorted({lg["block_number"] for lg in logs})]
        return httpx.Response(200, json={"data": [{"blocks": blocks, "transactions": txs, "logs": logs}],
                                         "archive_height": HEAD, "next_block": end, "total_execution_time": 3})


class FakePrices:
    def __init__(self):
        self.asked = []

    def get(self, chain, addrs):
        self.asked.append(set(addrs))
        book = {ZERO: (2000.0, 18), USDC: (1.0, 6)}
        return {a: book[a] for a in addrs if a in book}


def _hs(fake):
    return HyperSync(BASE.hypersync, "test-token", httpx.Client(transport=httpx.MockTransport(fake)), sleep=lambda s: None)


def _sync_until_done(engine, fake, settings, max_loops=200):
    for _ in range(max_loops):
        st = core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), settings, now=NOW)
        with engine.connect() as c:
            if store.get_state(c, BASE.chain_id).backfilled:
                return st
    raise AssertionError("never finished")


def _tables(engine):
    with engine.connect() as c:
        pools = {r.pool_id: r for r in c.execute(sa.select(db.v4_pools))}
        daily = {(r.pool_id, r.day): (r.swaps, r.wallets, round(r.volume_usd, 2)) for r in c.execute(sa.select(db.pool_daily_onchain))}
        wallets = {(r.pool_id, r.day, r.address): r.swaps for r in c.execute(sa.select(db.pool_wallet_day))}
        state = store.get_state(c, BASE.chain_id)
    return pools, daily, wallets, state


EXPECTED_DAILY = {(PA, date(2026, 9, 22)): (3, 2, 6000.0), (PC, date(2026, 9, 23)): (1, 1, 7.0)}


# ------------------------------------------------------------------ decoding

def test_decode_initialize_and_swap_from_hypersync_shaped_logs():
    p = decode_initialize(init_log(PA, ZERO, USDC, HOOK, 500, fee=0x800000, spacing=-10, tick=-200))
    assert p == {"pool_id": PA, "currency0": ZERO, "currency1": USDC, "fee": 0x800000, "tick_spacing": -10,
                 "hook_address": HOOK, "sqrt_price_x96": "79228162514264337593543950336", "tick": -200}
    s = decode_swap(swap_log(PC, 7, -3 * 10 ** 18, 42, sender=W2))
    assert (s["pool_id"], s["sender"], s["amount0"], s["amount1"], s["tick"], s["fee"]) == (PC, W2, -3 * 10 ** 18, 42, -5, 500)
    assert decode_swap(init_log(PA, ZERO, USDC, HOOK, 1)) is None and decode_initialize(swap_log(PA, 1, 1, 1)) is None
    # RPC-shaped log (topics list) decodes the same
    lg = init_log(PA, ZERO, USDC, HOOK, 500)
    rpc = {"topics": [lg["topic0"], lg["topic1"], lg["topic2"], lg["topic3"]], "data": lg["data"], "blockNumber": hex(500)}
    assert decode_initialize(rpc)["hook_address"] == HOOK


def test_request_shape_and_auth_error():
    fake = FakeHyperSync()
    hs = _hs(fake)
    hs.query(swap_query(BASE.pool_manager, 10, 20, [PA, PC]))
    req = fake.requests[-1]
    assert req.method == "POST" and str(req.url) == "https://base.hypersync.xyz/query"
    assert req.headers["authorization"] == "Bearer test-token" and req.headers["content-type"] == "application/json"
    body = json.loads(req.content)
    assert body["from_block"] == 10 and body["to_block"] == 20
    assert body["logs"] == [{"address": [BASE.pool_manager], "topics": [[SWAP_TOPIC], [PA, PC]]}]
    assert body["field_selection"]["transaction"] == ["hash", "from"] and "timestamp" in body["field_selection"]["block"]
    bad = HyperSync(BASE.hypersync, "nope", httpx.Client(transport=httpx.MockTransport(fake)), sleep=lambda s: None)
    with pytest.raises(HyperSyncAuthError):
        bad.height()


def test_a_request_that_never_answers_is_cut_at_the_wall_clock_deadline_and_retried():
    """Seen live 2026-10-10: one /query sat 40 minutes on attempt 0 because the socket kept receiving bytes with no
    response in them, so the 60 s idle timeout never fired. The deadline is wall clock, independent of idle time."""
    import time
    calls = []

    def hang(request):
        calls.append(time.monotonic())
        time.sleep(5)                       # interrupted by the deadline: the request never completes on its own
        return httpx.Response(200, json={"height": 1})
    slept = []
    hs = HyperSync(BASE.hypersync, "t", httpx.Client(transport=httpx.MockTransport(hang)), retries=2, sleep=slept.append,
                   deadline=0.2)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="failed after 2 attempts.*deadline"):
        hs.height()
    assert len(calls) == 2 and time.monotonic() - t0 < 2 and len(slept) == 2
    ok = HyperSync(BASE.hypersync, "t", httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"height": 7}))),
                   deadline=0.2)
    assert ok.height() == 7                 # the timer is cleared after each call: a fast request is not affected


# ------------------------------------------------------------------ full sync, modes, resume

@pytest.mark.parametrize("settings", [core.Settings(), core.Settings(topic_chunk=1), core.Settings(topic_filter_max=1)],
                         ids=["topic", "topic-chunked", "scan"])
def test_sync_backfills_pools_and_swaps(engine, settings):
    _sync_until_done(engine, FakeHyperSync(), settings)
    pools, daily, wallets, state = _tables(engine)
    assert set(pools) == {PA, PC}                      # unhooked PB never stored
    a = pools[PA]
    assert (a.hook_address, a.currency0, a.currency1, a.fee, a.tick_spacing, a.tick, a.block_number) == (HOOK, ZERO, USDC, 3000, 60, -887220, 500)
    assert a.block_time is not None and a.tx_hash == "0x" + format(500, "064x")
    assert daily == EXPECTED_DAILY
    assert wallets == {(PA, date(2026, 9, 22), W1): 2, (PA, date(2026, 9, 22), W2): 1, (PC, date(2026, 9, 23), W3): 1}
    assert state.initialize_block == HEAD - BASE.lag + 1 == state.swap_block and state.backfilled and state.head_block == HEAD


def test_swap_window_starts_at_first_block_of_day_minus_14(engine):
    fake = FakeHyperSync()
    assert _hs(fake).block_at_or_after(int(datetime(2026, 9, 22, tzinfo=timezone.utc).timestamp()), HEAD) == 1000
    core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=0), now=NOW)
    _, _, _, state = _tables(engine)
    assert (state.initialize_block, state.swap_block, state.backfilled) == (0, 1000, False)


def test_cursor_resume_one_page_per_loop_counts_each_swap_once(engine):
    _sync_until_done(engine, FakeHyperSync(), core.Settings(max_pages=1))
    _, daily, _, _ = _tables(engine)
    assert daily == EXPECTED_DAILY


def test_crash_mid_backfill_resumes_from_last_committed_batch(engine):
    crashing = FakeHyperSync(span=300, fail_after=130)
    with pytest.raises(RuntimeError):
        for _ in range(50):
            core.sync_hypersync(engine, BASE, _hs(crashing), FakePrices(), core.Settings(), now=NOW)
    _, daily_mid, _, mid = _tables(engine)
    # crashed inside the swap backfill, after the batches holding every swap were committed
    assert 3000 < mid.swap_block < mid.initialize_block and not mid.backfilled and daily_mid == EXPECTED_DAILY
    _sync_until_done(engine, FakeHyperSync(span=300), core.Settings())
    _, daily, _, _ = _tables(engine)
    assert daily == EXPECTED_DAILY


def test_follow_live_adds_new_pool_and_swaps(engine):
    fake = FakeHyperSync()
    _sync_until_done(engine, fake, core.Settings())
    global HEAD
    old = HEAD
    try:
        HEAD = old + 100
        fake.inits.append(init_log(pid(0xD), ZERO, USDC, HOOK, old + 20))
        fake.swaps.append((swap_log(pid(0xD), old + 30, -(10 ** 17), 200 * 10 ** 6), W2))
        st = core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(), now=NOW)
    finally:
        HEAD = old
    pools, daily, _, _ = _tables(engine)
    assert pid(0xD) in pools and st.new_pools == 1 and st.swaps == 1
    assert [v for (p, _), v in daily.items() if p == pid(0xD)] == [(1, 1, 200.0)]


# ------------------------------------------------------------------ aggregation + prices

def test_apply_swaps_increments_and_recounts_unique_wallets(engine):
    d = date(2026, 10, 1)
    with engine.begin() as c:
        store.apply_swaps(c, 1, {(PA, d): [2, 10.0, 0]}, {(PA, d, W1): 2})
        store.apply_swaps(c, 1, {(PA, d): [3, 5.5, 1]}, {(PA, d, W1): 1, (PA, d, W2): 2})
    with engine.connect() as c:
        r = c.execute(sa.select(db.pool_daily_onchain)).one()
        w = dict(c.execute(sa.select(db.pool_wallet_day.c.address, db.pool_wallet_day.c.swaps)).all())
    assert (r.swaps, r.wallets, r.volume_usd, r.unpriced_swaps) == (5, 2, 15.5, 1) and w == {W1: 3, W2: 2}
    with engine.begin() as c:
        assert store.prune_wallets(c, d.replace(month=11, day=5)) == 2


def test_swap_usd_side_selection():
    eth, usdc = (2000.0, 18), (1.0, 6)
    assert swap_usd(-10 ** 18, 2000 * 10 ** 6, eth, usdc) == 2000.0
    assert swap_usd(-10 ** 18, 1990 * 10 ** 6, eth, usdc) == 2000.0            # both priced: larger
    assert swap_usd(-10 ** 18, 5 * 10 ** 6, eth, usdc) == 5.0                  # 400x apart: one quote is wrong, smaller
    assert swap_usd(7, 3 * 10 ** 6, None, usdc) == 3.0                         # only one side priced
    assert swap_usd(7, 3, None, None) is None
    assert swap_usd(10 ** 30, 0, eth, None) is None                             # absurd quote: unpriced, not $0
    # stable/stable (Aegis USD₮0/USDC on Ink): one leg, never in + out; 664 such swaps on 2026-10-12 sum to $292.8K, not $585K
    assert swap_usd(-1000 * 10 ** 6, 999 * 10 ** 6, usdc, usdc) == 1000.0
    assert reliable({"price": 1.0, "decimals": 6, "confidence": 0.99}) == (1.0, 6)
    assert reliable({"price": 1.0, "decimals": 6, "confidence": 0.2}) is None
    assert reliable({"price": 1e12, "decimals": 18}) is None
    assert reliable({"price": 3.0}) is None and reliable({"price": 3.0}, native=True) == (3.0, 18)


def test_pricebook_batches_maps_native_and_caches():
    calls = []

    def llama(request):
        calls.append(str(request.url))
        ids = request.url.path.split("/prices/current/")[1].split(",")
        coins = {}
        for i in ids:
            if i == "coingecko:ethereum":
                coins[i] = {"price": 2000.0, "symbol": "ETH", "confidence": 0.99}
            elif i == f"base:{USDC}":
                coins[i] = {"price": 1.0, "decimals": 6, "symbol": "USDC", "confidence": 0.99}
        return httpx.Response(200, json={"coins": coins})

    clock = [1000.0]
    pb = PriceBook(httpx.Client(transport=httpx.MockTransport(llama)), clock=lambda: clock[0])
    got = pb.get(BASE, [ZERO, USDC, TOKX])
    assert got == {ZERO: (2000.0, 18), USDC: (1.0, 6)} and len(calls) == 1
    assert "coingecko:ethereum" in calls[0] and f"base:{USDC}" in calls[0]
    pb.get(BASE, [USDC, TOKX])                     # hits and cached miss: no call
    assert len(calls) == 1
    clock[0] += 31 * 60
    pb.get(BASE, [USDC])
    assert len(calls) == 2


# ------------------------------------------------------------------ chains, fallback, runner

def test_plan_without_token_uses_rpc_on_ten_chains():
    mode, chains = core.plan(None)
    assert mode == "rpc" and len(chains) == 10 and all(c.rpc_net in K.LOGS for c in chains)
    mode, chains = core.plan("tok")
    assert mode == "hypersync" and len(chains) == 16
    for c in CHAINS:                                # one source of truth for PoolManager addresses
        if c.rpc_net:
            assert K.LOGS[c.rpc_net][0].lower() == c.pool_manager and K.CHAINS[c.rpc_net][0] == c.chain_id
    assert [c.slug for c in select_chains("base, 130")] == ["base", "unichain"]
    with pytest.raises(ValueError):
        select_chains("solana")


class FakeRpc:
    def __init__(self, head):
        self.head = head
        self.logs = []
        self.calls = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append(body["method"])
        m, p = body["method"], body["params"]
        if m == "eth_blockNumber":
            res = hex(self.head)
        elif m == "eth_getLogs":
            f, t = int(p[0]["fromBlock"], 16), int(p[0]["toBlock"], 16)
            assert t - f + 1 <= 120 and p[0]["topics"] == [INITIALIZE_TOPIC]
            res = [lg for lg in self.logs if f <= int(lg["blockNumber"], 16) <= t]
        elif m == "eth_getBlockByNumber":
            res = {"timestamp": hex(T0 + int(p[0], 16) * BT)}
        else:
            raise AssertionError(m)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": res})


def test_rpc_fallback_follows_new_blocks_only(engine):
    fake = FakeRpc(5000)
    rpc = core.Rpc("https://mainnet.base.org", httpx.Client(transport=httpx.MockTransport(fake)))
    st = core.sync_rpc(engine, BASE, rpc, 120, core.Settings())
    _, _, _, state = _tables(engine)
    assert state.initialize_block == 5000 - BASE.lag + 1 and state.swap_block is None and not state.backfilled
    assert fake.calls == ["eth_blockNumber"] and st.new_pools == 0          # no backfill
    lg = init_log(PC, TOKX, USDC, HOOK2, 5100)
    unhooked = init_log(PB, ZERO, USDC, ZERO, 5101)
    fake.logs = [{"topics": [x["topic0"], x["topic1"], x["topic2"], x["topic3"]], "data": x["data"],
                  "blockNumber": hex(x["block_number"]), "transactionHash": x["transaction_hash"]} for x in (lg, unhooked)]
    fake.head = 5300
    st = core.sync_rpc(engine, BASE, rpc, 120, core.Settings())
    pools, _, _, state = _tables(engine)
    assert set(pools) == {PC} and pools[PC].block_time is not None and st.new_pools == 1
    assert state.initialize_block == 5300 - BASE.lag + 1


def test_rpc_gap_clears_swap_cursor_for_later_hypersync_backfill(engine):
    with engine.begin() as c:
        store.set_state(c, BASE.chain_id, initialize_block=100, swap_block=90, backfilled=True)
    rpc = core.Rpc("https://x", httpx.Client(transport=httpx.MockTransport(FakeRpc(100_000))))
    core.sync_rpc(engine, BASE, rpc, 120, core.Settings(rpc_max_windows=2))
    _, _, _, state = _tables(engine)
    assert state.swap_block is None and not state.backfilled
    assert state.initialize_block == 100_000 - BASE.lag + 1


def test_runner_one_chain_failing_does_not_stop_others(engine, monkeypatch, caplog):
    seen = []

    def fake_sync(engine_, ch, hs, prices, settings, **kw):
        seen.append(ch.slug)
        if ch.slug == "ethereum":
            raise RuntimeError("boom")
        return core.ChainStats(head=10, init_lag=1, swap_lag=2, new_pools=3, mode="hypersync")

    monkeypatch.setattr("bait.indexer.__main__.sync_hypersync", fake_sync)
    r = Runner(engine, "tok", select_chains("ethereum,base"), httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))), core.Settings())
    with caplog.at_level("INFO", logger="bait.indexer"):
        out = r.run_once()
    assert seen == ["ethereum", "base"] and out["ethereum"].error == "RuntimeError" and out["base"].new_pools == 3
    line = heartbeat(1, "hypersync", out)
    assert "ethereum:ERR(RuntimeError)" in line and "base:lag=1/2,new=3" in line and "new_pools=3" in line
    assert any("heartbeat loop=1" in m for m in caplog.messages)


def test_additive_schema_upgrades_an_existing_indexer_database():
    e = db.make_engine("sqlite://")
    with e.begin() as c:
        c.execute(sa.text("CREATE TABLE indexer_state (chain_id integer PRIMARY KEY, initialize_block bigint, swap_block bigint, "
                          "head_block bigint, backfilled boolean NOT NULL DEFAULT 0, updated_at timestamp)"))
        c.execute(sa.text("CREATE TABLE pool_daily_onchain (chain_id integer, pool_id text, day date, swaps integer NOT NULL DEFAULT 0, "
                          "wallets integer NOT NULL DEFAULT 0, volume_usd float NOT NULL DEFAULT 0, PRIMARY KEY (chain_id, pool_id, day))"))
        c.execute(sa.text("INSERT INTO pool_daily_onchain (chain_id, pool_id, day, swaps) VALUES (1, '0xp', '2026-10-01', 4)"))
    db.create_all(e)
    assert {"indexer_state.swap_block_time", "pool_daily_onchain.unpriced_swaps"} <= set(db.add_missing_columns(e))
    assert "v4_pools_chain_block_idx" in db.add_missing_indexes(e)
    with e.connect() as c:
        assert c.execute(sa.text("SELECT unpriced_swaps FROM pool_daily_onchain")).scalar_one() == 0
    assert "onchain_watermarks" in sa.inspect(e).get_table_names()


def test_stop_flag_ends_a_sync_between_pages(engine):
    asked = {"n": 0}

    def stop():
        asked["n"] += 1
        return asked["n"] > 3
    st = core.sync_hypersync(engine, BASE, _hs(FakeHyperSync()), FakePrices(), core.Settings(), now=NOW, should_stop=stop)
    assert st.pages == 3                                  # three Initialize pages, then the stop was honoured


def test_runner_stops_between_chains(engine, monkeypatch):
    seen = []
    monkeypatch.setattr("bait.indexer.__main__.sync_hypersync",
                        lambda e, ch, hs, p, s, **kw: seen.append(ch.slug) or core.ChainStats(mode="hypersync"))
    r = Runner(engine, "tok", select_chains("ethereum,base"), httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(500))),
               core.Settings(), should_stop=lambda: len(seen) >= 1)
    assert set(r.run_once()) == {"ethereum"} and seen == ["ethereum"]


def _seed_pool_and_cursor(engine, swap_block=1000):
    """PA known, Initialize history complete, swap cursor at `swap_block` (block 1000 = 09-22 00:00): swap phase running."""
    with engine.begin() as c:
        store.upsert_pools(c, core.hooked_pools_from_page(BASE.chain_id, [init_log(PA, ZERO, USDC, HOOK, 500)], {500: T0 + 500 * BT}))
        store.set_state(c, BASE.chain_id, initialize_block=HEAD - BASE.lag + 1, swap_block=swap_block, head_block=HEAD,
                        backfilled=False, swap_block_time=core.utc(T0 + swap_block * BT))


def test_two_indexers_on_the_same_page_count_it_once(engine, monkeypatch):
    _seed_pool_and_cursor(engine)
    with engine.connect() as c:
        stale = store.get_state(c, BASE.chain_id)          # what the second container read before the first committed
    core.sync_hypersync(engine, BASE, _hs(FakeHyperSync()), FakePrices(), core.Settings(max_pages=1), now=NOW)
    monkeypatch.setattr(store, "get_state", lambda conn, chain_id: stale)
    with pytest.raises(store.CursorMoved):
        core.sync_hypersync(engine, BASE, _hs(FakeHyperSync()), FakePrices(), core.Settings(max_pages=1), now=NOW)
    monkeypatch.undo()
    _, daily, _, state = _tables(engine)
    assert state.swap_block == 1700 and daily == {(PA, date(2026, 9, 22)): (3, 2, 6000.0)}


def test_swap_block_time_is_the_newest_block_of_the_committed_page(engine):
    _seed_pool_and_cursor(engine)
    core.sync_hypersync(engine, BASE, _hs(FakeHyperSync()), FakePrices(), core.Settings(max_pages=1), now=NOW)
    _, _, _, st = _tables(engine)
    assert st.swap_block == 1700 and db.as_utc(st.swap_block_time) == core.utc(T0 + 1300 * BT)   # last swap in 1000..1700


def test_caught_up_quiet_chain_anchors_swap_block_time_at_the_head(engine):
    _sync_until_done(engine, FakeHyperSync(), core.Settings())
    _, _, _, st = _tables(engine)
    end = HEAD - BASE.lag + 1                         # newest swap is block 2500; the anchor reads block end-1
    assert db.as_utc(st.swap_block_time) == core.utc(T0 + (end - 1) * BT)


class NoSwapBlocks(FakeHyperSync):
    """Swap pages arrive without the joined blocks."""

    def __call__(self, request):
        r = super().__call__(request)
        q = json.loads(request.content) if request.content else {}
        if "transaction" not in q.get("field_selection", {}):
            return r
        body = r.json()
        for b in body["data"]:
            b["blocks"] = []
        return httpx.Response(200, json=body)


def test_swap_logs_without_block_timestamp_fail_the_page_instead_of_vanishing(engine):
    _seed_pool_and_cursor(engine)
    with pytest.raises(RuntimeError, match="without a block timestamp"):
        core.sync_hypersync(engine, BASE, _hs(NoSwapBlocks()), FakePrices(), core.Settings(max_pages=1), now=NOW)
    _, daily, _, st = _tables(engine)
    assert st.swap_block == 1000 and daily == {}


def test_swap_window_start_moves_up_when_the_swap_phase_begins_days_later(engine):
    fake = FakeHyperSync()
    core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=0), now=NOW)   # first run: block 1000 (09-22)
    later = NOW + timedelta(days=2)                                     # Initialize backfill took two more days
    core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=2), now=later)
    _, daily, _, st = _tables(engine)
    assert (st.initialize_block, st.swap_block, daily) == (1400, 3880, {})                  # block 3880 = 09-24 00:00
    assert db.as_utc(st.swap_block_time) == datetime(2026, 9, 24, tzinfo=timezone.utc)


def test_defillama_outage_does_not_commit_the_page(engine):
    from bait.indexer.prices import PricesUnavailable
    _seed_pool_and_cursor(engine)
    down = PriceBook(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    with pytest.raises(PricesUnavailable):
        core.sync_hypersync(engine, BASE, _hs(FakeHyperSync()), down, core.Settings(max_pages=1), now=NOW)
    _, daily, _, st = _tables(engine)
    assert st.swap_block == 1000 and daily == {}
    core.sync_hypersync(engine, BASE, _hs(FakeHyperSync()), FakePrices(), core.Settings(max_pages=1), now=NOW)
    _, daily, _, st = _tables(engine)
    assert st.swap_block == 1700 and daily == {(PA, date(2026, 9, 22)): (3, 2, 6000.0)}
    bad_request = PriceBook(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400))))
    assert bad_request.get(BASE, [TOKX]) == {}                       # a 4xx is "no price", never a wedged chain


def test_unpriced_swaps_are_counted_not_silently_zero(engine):
    class NoPrices:
        def get(self, chain, addrs):
            return {}
    _seed_pool_and_cursor(engine)
    core.sync_hypersync(engine, BASE, _hs(FakeHyperSync()), NoPrices(), core.Settings(max_pages=1), now=NOW)
    with engine.connect() as c:
        r = c.execute(sa.select(db.pool_daily_onchain)).one()
    assert (r.swaps, r.unpriced_swaps, r.volume_usd) == (3, 3, 0.0)


def test_fallback_prices_wrapped_native_and_stables_when_llama_has_no_quote():
    from bait.indexer.prices import STABLES, WRAPPED_NATIVE

    def llama(request):
        ids = request.url.path.split("/prices/current/")[1].split(",")
        coins = {"coingecko:ethereum": {"price": 2500.0, "confidence": 0.99}} if "coingecko:ethereum" in ids else {}
        return httpx.Response(200, json={"coins": coins})
    pb = PriceBook(httpx.Client(transport=httpx.MockTransport(llama)))
    rh_weth = WRAPPED_NATIVE[4663]
    assert pb.get(CHAIN_BY_ID[4663], [rh_weth, TOKX]) == {rh_weth: (2500.0, 18)}     # Robinhood WETH via the ETH coin id
    assert pb.get(BASE, [USDC]) == {USDC: (1.0, 6)}                                    # no DefiLlama quote for USDC
    for cid, addr in WRAPPED_NATIVE.items():
        assert cid in CHAIN_BY_ID and addr == addr.lower() and len(addr) == 42
    for cid, m in STABLES.items():
        assert cid in CHAIN_BY_ID and all(a == a.lower() and len(a) == 42 and d in (6, 18) for a, d in m.items())


def test_runner_retries_a_locked_schema_migration_next_loop(engine, monkeypatch):
    seen, tries = [], []
    monkeypatch.setattr("bait.indexer.__main__.sync_hypersync",
                        lambda e, ch, hs, p, s, **kw: seen.append(ch.slug) or core.ChainStats(mode="hypersync"))

    def migrate():
        tries.append(1)
        return len(tries) > 1                          # first attempt hits a lock timeout
    r = Runner(engine, "tok", select_chains("base"), httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(500))),
               core.Settings(), migrate=migrate)
    assert r.run_once() == {} and seen == []
    assert set(r.run_once()) == {"base"} and seen == ["base"]
    r.run_once()
    assert len(tries) == 2                             # not re-run once it succeeded


def test_runner_survives_a_connect_error_while_taking_the_chain_lock(engine, monkeypatch):
    seen = []
    monkeypatch.setattr("bait.indexer.__main__.sync_hypersync",
                        lambda e, ch, hs, p, s, **kw: seen.append(ch.slug) or core.ChainStats(mode="hypersync"))
    real = db.try_advisory_lock

    def flaky(engine_, key):
        if key.endswith(":1"):
            raise sa.exc.OperationalError("SELECT pg_try_advisory_lock", {}, Exception("connection refused"))
        return real(engine_, key)
    monkeypatch.setattr(db, "try_advisory_lock", flaky)
    r = Runner(engine, "tok", select_chains("ethereum,base"), httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(500))),
               core.Settings())
    out = r.run_once()
    assert out["ethereum"].error == "lock" and seen == ["base"]


def test_a_rejected_batch_is_split_so_one_bad_id_does_not_unprice_the_rest():
    def llama(request):
        ids = request.url.path.split("/prices/current/")[1].split(",")
        if f"base:{TOKX}" in ids:
            return httpx.Response(400, json={"message": "bad coin id"})
        return httpx.Response(200, json={"coins": {i: {"price": 2000.0, "confidence": 0.99} for i in ids if i == "coingecko:ethereum"}})
    pb = PriceBook(httpx.Client(transport=httpx.MockTransport(llama)))
    assert pb.get(BASE, [ZERO, TOKX]) == {ZERO: (2000.0, 18)}


def test_runner_warns_after_three_consecutive_price_outages_on_a_chain(engine, monkeypatch, caplog):
    from bait.indexer.prices import PricesUnavailable
    results = iter([PricesUnavailable("base"), PricesUnavailable("base"), PricesUnavailable("base"), None, PricesUnavailable("base")])

    def fake_sync(e, ch, hs, p, s, **kw):
        r = next(results)
        if r is not None:
            raise r
        return core.ChainStats(mode="hypersync")
    monkeypatch.setattr("bait.indexer.__main__.sync_hypersync", fake_sync)
    r = Runner(engine, "tok", select_chains("base"), httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(500))),
               core.Settings())
    warned = []
    for _ in range(5):
        caplog.clear()
        with caplog.at_level("INFO", logger="bait.indexer"):
            out = r.run_once()
        warned.append(any(rec.levelname == "WARNING" and "consecutive" in rec.getMessage() for rec in caplog.records))
        assert out["base"].error in ("prices", None)
    assert warned == [False, False, True, False, False]    # third in a row warns; a success resets the count


@pytest.mark.parametrize("status", [401, 403])
def test_defillama_auth_errors_are_transient_not_a_rejected_batch(status):
    from bait.indexer.prices import PricesUnavailable
    pb = PriceBook(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status))))
    with pytest.raises(PricesUnavailable):
        pb.get(BASE, [TOKX, ZERO])
    assert pb.calls == 1                                   # not split into per-coin retries


def test_runner_escalates_repeated_schema_locks_and_reports_them_in_the_heartbeat(engine, caplog):
    r = Runner(engine, "tok", select_chains("base"), httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(500))),
               core.Settings(), migrate=lambda: False)
    levels, beats = [], []
    for _ in range(3):
        caplog.clear()
        with caplog.at_level("INFO", logger="bait.indexer"):
            r.run_once()
        levels.append([rec.levelname for rec in caplog.records if "schema migration" in rec.getMessage()])
        beats.append([rec.getMessage() for rec in caplog.records if rec.getMessage().startswith("heartbeat")])
    assert levels == [["WARNING"], ["WARNING"], ["ERROR"]]
    assert [b[0].split("schema_locked=")[1].split()[0] for b in beats] == ["1", "2", "3"]


def test_pool_cap_schema_is_additive_on_an_existing_database():
    e = db.make_engine("sqlite://")
    with e.begin() as c:
        c.execute(sa.text("CREATE TABLE v4_pools (chain_id integer, pool_id text, hook_address text NOT NULL, currency0 text NOT NULL, "
                          "currency1 text NOT NULL, fee integer, tick_spacing integer, sqrt_price_x96 text, tick integer, "
                          "block_number bigint, block_time timestamp, tx_hash text, PRIMARY KEY (chain_id, pool_id))"))
        c.execute(sa.text("INSERT INTO v4_pools (chain_id, pool_id, hook_address, currency0, currency1) VALUES (1, '0xp', '0xh', '0x0', '0x1')"))
    db.create_all(e)
    assert "v4_pools.sampled" in db.add_missing_columns(e)
    with e.connect() as c:
        assert c.execute(sa.text("SELECT sampled FROM v4_pools")).scalar_one() == 0
    assert {c["name"] for c in sa.inspect(e).get_columns("hook_pool_counts")} == {
        "chain_id", "hook_address", "pools_total", "first_block", "last_block", "last_seen", "sampled_total", "swaps_total",
        "volume_usd_total"}
    assert "pools_total" in {c["name"] for c in sa.inspect(e).get_columns("hooks")}


# ------------------------------------------------------------------ per-hook pool cap (package 5b)

def _row(pool, hook, block):
    return core.hooked_pools_from_page(BASE.chain_id, [init_log(pool, ZERO, USDC, hook, block)], {block: T0 + block * BT})[0]


def _counter(engine, hook=HOOK):
    with engine.connect() as c:
        return c.execute(sa.select(db.hook_pool_counts).where(db.hook_pool_counts.c.hook_address == hook)).first()


def test_cap_stores_the_first_pools_of_a_hook_and_counts_the_rest(engine):
    page = [_row(pid(n), HOOK, 100 + n) for n in (1, 2, 3)] + [_row(PC, HOOK2, 50)]
    with engine.begin() as c:
        stored, over = store.store_pools(c, BASE.chain_id, page, cap=2)
    assert [r["pool_id"] for r in stored] == [pid(1), pid(2), PC] and [r["pool_id"] for r in over] == [pid(3)]
    k = _counter(engine)
    assert (k.pools_total, k.first_block, k.last_block) == (3, 103, 103)
    with engine.begin() as c:                          # the same page again (a re-backfill from block 0): counted once
        assert store.store_pools(c, BASE.chain_id, page, cap=2)[1] == []
    assert _counter(engine).pools_total == 3
    with engine.begin() as c:
        store.store_pools(c, BASE.chain_id, [_row(pid(9), HOOK, 900)], cap=2)
    k = _counter(engine)
    assert (k.pools_total, k.first_block, k.last_block) == (4, 103, 900) and db.as_utc(k.last_seen) == core.utc(T0 + 900 * BT)
    with engine.connect() as c:
        assert store.pool_count(c, BASE.chain_id) == 3 and store.capped_floor(c, BASE.chain_id) == 103


def test_cap_crossing_never_counts_a_stored_row_twice_and_rolls_back_with_its_page(engine):
    with pytest.raises(RuntimeError):
        with engine.begin() as c:
            store.store_pools(c, BASE.chain_id, [_row(pid(n), HOOK, 100 + n) for n in (1, 2, 3)], cap=1)
            raise RuntimeError("the page failed after the cap check")
    assert _counter(engine) is None
    with engine.begin() as c:
        store.store_pools(c, BASE.chain_id, [_row(pid(1), HOOK, 101)], cap=2)
        stored, over = store.store_pools(c, BASE.chain_id, [_row(pid(n), HOOK, 100 + n) for n in (1, 2, 3)], cap=2)
    assert [r["pool_id"] for r in stored] == [pid(2)] and [r["pool_id"] for r in over] == [pid(3)]
    assert _counter(engine).pools_total == 3


def test_sync_caps_a_launchpad_hook_and_the_heartbeat_says_so(engine):
    fake = FakeHyperSync()
    fake.inits += [init_log(pid(0xD), ZERO, USDC, HOOK, 1600), init_log(pid(0xE), ZERO, USDC, HOOK, 3000)]
    st = core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=32, pools_per_hook=1), now=NOW)
    pools, _, _, state = _tables(engine)
    assert state.initialize_block == HEAD - BASE.lag + 1 and set(pools) == {PA, PC}
    assert (st.new_pools, st.capped) == (2, 2) and _counter(engine).pools_total == 3
    assert "base:lag=0/20991,new=2,capped=2" in heartbeat(1, "hypersync", {"base": st})


LAUNCH = pid(0xD)


def _launchpad_fake():
    """HOOK holds PA; LAUNCH (block 1600) is past a cap of 1 and swaps at block 2000 (2026-09-22) for $200."""
    fake = FakeHyperSync()
    fake.inits.append(init_log(LAUNCH, ZERO, USDC, HOOK, 1600))
    fake.swaps.append((swap_log(LAUNCH, 2000, -(10 ** 17), 200 * 10 ** 6), W2))
    return fake


def _lookups(fake):
    """Pool id lists of the Initialize-by-id queries the fake served."""
    out = []
    for r in fake.requests:
        sel = (json.loads(r.content).get("logs") or [{}])[0] if r.content else {}
        if sel.get("topics", [[None]])[0] == [INITIALIZE_TOPIC] and len(sel["topics"]) > 1:
            out.append(sel["topics"][1])
    return out


def test_capped_pool_that_swaps_becomes_a_sampled_row_with_its_swaps(engine):
    fake = _launchpad_fake()
    st = core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(pools_per_hook=1), now=NOW)
    pools, daily, _, _ = _tables(engine)
    assert pools[LAUNCH].sampled and not pools[PA].sampled and (st.mode, st.late) == ("hypersync/scan", 1)
    assert daily[(LAUNCH, date(2026, 9, 22))] == (1, 1, 200.0) and daily[(PA, date(2026, 9, 22))] == (3, 2, 6000.0)
    assert _lookups(fake) == [[PB]]                  # LAUNCH came from this process's Initialize pages; unhooked PB was asked once
    assert _counter(engine).pools_total == 2         # still the true total: a late row is not counted again
    assert ",late=1" in heartbeat(1, "hypersync", {"base": st})


def test_capped_pool_from_before_this_process_is_looked_up_by_pool_id(engine):
    fake = _launchpad_fake()
    core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=32, pools_per_hook=1), now=NOW)
    resolver = core.PoolResolver()                   # a restarted process: nothing in memory
    st = core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=3, pools_per_hook=1), now=NOW,
                             resolver=resolver)
    pools, daily, _, _ = _tables(engine)
    assert pools[LAUNCH].sampled and daily[(LAUNCH, date(2026, 9, 22))] == (1, 1, 200.0)
    asked = _lookups(fake)
    assert asked.count([PB]) == 1 and [LAUNCH] in asked and PB in resolver.absent and st.lookups == resolver.calls >= 2


def test_lookup_budget_holds_the_swap_page_for_the_next_loop(engine):
    fake = _launchpad_fake()
    core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=32, pools_per_hook=1), now=NOW)
    resolver = core.PoolResolver()
    st = core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(pools_per_hook=1, pool_lookups=0), now=NOW,
                             resolver=resolver)
    _, daily, _, state = _tables(engine)
    assert (state.swap_block, daily, st.error) == (1000, {}, None)       # page not committed, swaps not skipped
    core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=3, pools_per_hook=1), now=NOW,
                        resolver=resolver)
    _, daily, _, state = _tables(engine)
    assert state.swap_block == 3100 and daily[(LAUNCH, date(2026, 9, 22))] == (1, 1, 200.0)


def test_a_lookup_cut_by_the_budget_resumes_where_it_stopped_next_loop():
    """Live 2026-10-10: after the prune, BNB's lookup floor was 44M blocks below the page and one 500-id chunk needed ~70
    calls, each ~45 s, with the budget checked only between chunks: the whole loop waited on one chain. Now the budget
    bounds every call and a cut chunk carries its scan cursor to the next loop."""
    fake = FakeHyperSync(span=100)                   # 100 blocks per answer: [1000, 1700) is 7 calls
    hs = _hs(fake)
    resolver = core.PoolResolver()
    fake.inits.append(init_log(LAUNCH, ZERO, USDC, HOOK, 1600))
    s = core.Settings()
    froms = lambda: [json.loads(r.content)["from_block"] for r in fake.requests]
    resolver.start_loop()
    with pytest.raises(core.LookupBudget, match=r"3 of 3 calls.*1 ids left.*resumes at block 1300"):
        resolver.resolve(hs, BASE, {LAUNCH}, 1000, 1700, s, budget=3)
    assert froms() == [1000, 1100, 1200] and resolver.scanned == {LAUNCH: 1300}
    resolver.start_loop()
    with pytest.raises(core.LookupBudget, match=r"resumes at block 1600"):
        resolver.resolve(hs, BASE, {LAUNCH}, 1000, 1750, s, budget=3)        # the page end moved; the scan carries on
    assert froms()[3:] == [1300, 1400, 1500]
    resolver.start_loop()
    with pytest.raises(core.LookupBudget, match=r"1 ids left.*resumes at block 1100"):
        resolver.resolve(hs, BASE, {LAUNCH, PB}, 1000, 1750, s, budget=3)    # PB is new and starts at the floor
    assert froms()[6:9] == [1600, 1700, 1000]                                # the chunk closest to done goes first
    assert LAUNCH in resolver.pending and resolver.scanned == {PB: 1100}     # LAUNCH found and settled; PB cut after 1 call
    resolver.start_loop()
    assert resolver.resolve(hs, BASE, {PB}, 1000, 1750, s, budget=10) == {} and PB in resolver.absent and not resolver.scanned
    assert froms()[9] == 1100                                                # PB resumed, not restarted
    resolver.start_loop()
    resolver.resolve(hs, BASE, {PB}, 900, 1750, s, budget=10)                 # a lower floor forgets absent and cursors
    assert froms()[-9] == 900 and PB in resolver.absent


def test_rate_limit_units_are_accounted_from_the_response_headers():
    def served(request):
        return httpx.Response(200, json={"data": [], "archive_height": 5, "next_block": 5},
                              headers={"x-ratelimit-cost": "1000", "x-ratelimit-remaining": "12000"})
    hs = HyperSync(BASE.hypersync, "t", httpx.Client(transport=httpx.MockTransport(served)))
    hs.query({"from_block": 0, "to_block": 5}); hs.query({"from_block": 0, "to_block": 5})
    assert (hs.units, hs.remaining) == (2000, 12000)
    assert (_hs(FakeHyperSync()).units, _hs(FakeHyperSync()).remaining) == (0, None)   # no headers: nothing claimed


def test_late_row_and_its_swaps_count_once_when_two_indexers_race(engine, monkeypatch):
    fake = _launchpad_fake()
    core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=33, pools_per_hook=1), now=NOW)
    with engine.connect() as c:
        stale = store.get_state(c, BASE.chain_id)    # both containers read swap_block 1700
    core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=1, pools_per_hook=1), now=NOW)
    monkeypatch.setattr(store, "get_state", lambda conn, chain_id: stale)
    with pytest.raises(store.CursorMoved):
        core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=1, pools_per_hook=1), now=NOW)
    monkeypatch.undo()
    pools, daily, _, state = _tables(engine)
    assert state.swap_block == 2400 and pools[LAUNCH].sampled and daily[(LAUNCH, date(2026, 9, 22))] == (1, 1, 200.0)


def test_runner_keeps_one_resolver_per_chain_across_loops(engine, monkeypatch):
    got = []
    monkeypatch.setattr("bait.indexer.__main__.sync_hypersync",
                        lambda e, ch, hs, p, s, **kw: got.append((ch.slug, kw["resolver"])) or core.ChainStats(mode="hypersync"))
    r = Runner(engine, "tok", select_chains("ethereum,base"), httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(500))),
               core.Settings())
    r.run_once()
    r.run_once()
    by = {}
    for slug, res in got:
        by.setdefault(slug, set()).add(id(res))
    assert len(got) == 4 and all(len(v) == 1 for v in by.values()) and by["ethereum"] != by["base"]


def test_griefing_a_hook_up_to_its_cap_never_hides_a_later_pools_swaps(engine):
    """Junk pools fill HOOK's cap first; the real pool initialized after them is not a row, but its swap makes it one,
    with the swap counted, even when the first loop's lookup budget is spent (the page waits, nothing is dropped)."""
    fake = FakeHyperSync()
    fake.inits += [init_log(pid(0x100 + n), TOKX, USDC, HOOK, 1500 + n) for n in range(3)]      # junk, blocks 1500-1502
    fake.inits.append(init_log(LAUNCH, ZERO, USDC, HOOK, 1600))                                  # the real launch
    fake.swaps.append((swap_log(LAUNCH, 2000, -(10 ** 17), 200 * 10 ** 6), W2))
    s = dict(pools_per_hook=4)                                                                    # PA + 3 junk fill it
    core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=32, **s), now=NOW)
    assert LAUNCH not in _tables(engine)[0] and _counter(engine).pools_total == 5
    resolver = core.PoolResolver()                                                                # restarted: nothing in memory
    core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(pool_lookups=0, **s), now=NOW, resolver=resolver)
    assert _tables(engine)[3].swap_block == 1000                                                 # held, not skipped
    for _ in range(3):                                                                            # the next loops carry on
        core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=3, pool_lookups=1, **s), now=NOW,
                            resolver=resolver)
    pools, daily, _, state = _tables(engine)
    assert state.swap_block >= 2400 and pools[LAUNCH].sampled and daily[(LAUNCH, date(2026, 9, 22))] == (1, 1, 200.0)
    assert daily[(PA, date(2026, 9, 22))] == (3, 2, 6000.0) and _counter(engine).pools_total == 5


def test_lookup_budget_from_env_is_at_least_one_call_so_a_held_page_always_moves(monkeypatch):
    monkeypatch.setenv("INDEXER_POOL_LOOKUPS", "0")
    assert core.Settings.from_env().pool_lookups == 1


L2 = pid(0xE)


def test_a_dust_or_unpriced_swap_makes_no_row_but_its_usage_stays_on_the_hook(engine):
    fake = FakeHyperSync()
    fake.inits += [init_log(LAUNCH, ZERO, USDC, HOOK, 1600), init_log(L2, TOKX, TOKX, HOOK, 1601)]
    fake.swaps += [(swap_log(LAUNCH, 2000, -(10 ** 12), 2 * 10 ** 3), W2),        # $0.002: dust
                   (swap_log(L2, 2001, -(10 ** 18), 10 ** 18), W3),                # no priced side
                   (swap_log(LAUNCH, 3000, -(10 ** 17), 200 * 10 ** 6), W2)]      # later a real $200 swap (2026-09-23)
    st = core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(pools_per_hook=1), now=NOW)
    pools, daily, wallets, _ = _tables(engine)
    agg = db.hook_pool_id(HOOK)
    assert L2 not in pools and pools[LAUNCH].sampled and (st.late, st.folded) == (1, 2)
    assert daily[(agg, date(2026, 9, 22))] == (2, 2, 0.0) and daily[(LAUNCH, date(2026, 9, 23))] == (1, 1, 200.0)
    assert {(p, a) for p, _, a in wallets if p == agg} == {(agg, W2), (agg, W3)}
    k = _counter(engine)
    assert (k.pools_total, k.sampled_total, k.swaps_total, round(k.volume_usd_total, 3)) == (3, 1, 2, 0.002)


def test_late_rows_stop_at_the_sampled_cap_and_the_rest_stays_on_the_hook(engine):
    fake = FakeHyperSync()
    fake.inits += [init_log(LAUNCH, ZERO, USDC, HOOK, 1600), init_log(L2, ZERO, USDC, HOOK, 1601)]
    fake.swaps += [(swap_log(LAUNCH, 2000, -(10 ** 17), 200 * 10 ** 6), W2),
                   (swap_log(L2, 2001, -(10 ** 17), 300 * 10 ** 6), W3)]       # $300: the bigger swap takes the one slot
    st = core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(pools_per_hook=1, sampled_per_hook=1), now=NOW)
    pools, daily, _, _ = _tables(engine)
    d = date(2026, 9, 22)
    assert pools[L2].sampled and LAUNCH not in pools and (st.late, st.folded) == (1, 1)
    assert daily[(L2, d)] == (1, 1, 300.0) and daily[(db.hook_pool_id(HOOK), d)] == (1, 1, 200.0)
    k = _counter(engine)
    assert (k.pools_total, k.sampled_total, k.swaps_total, k.volume_usd_total) == (3, 1, 1, 200.0)
    assert ",late=1,folded=1" in heartbeat(1, "hypersync", {"base": st})


def test_late_insert_settings_from_env(monkeypatch):
    monkeypatch.setenv("INDEXER_LATE_INSERT_MIN_USD", "25.5")
    monkeypatch.setenv("INDEXER_SAMPLED_POOLS_PER_HOOK", "7")
    s = core.Settings.from_env()
    assert (s.late_min_usd, s.sampled_per_hook) == (25.5, 7) and (core.Settings().late_min_usd, core.Settings().sampled_per_hook) == (10.0, 1000)


def test_a_held_page_commits_while_initialize_churn_evicts_the_recent_cache(engine, monkeypatch):
    """Reviewer's scenario: a tiny recent cache, one id per lookup chunk, a budget of 2 calls, and 5 new capped pools per
    loop from the Initialize phase. Looked-up keys must survive that churn, or the same chunks are asked every loop."""
    global HEAD
    monkeypatch.setattr(core.PoolResolver, "RECENT_MAX", 4)
    fake = FakeHyperSync()
    held = [pid(0x200 + n) for n in range(4)]
    fake.inits += [init_log(p, ZERO, USDC, HOOK, 1600 + n) for n, p in enumerate(held)]
    fake.swaps += [(swap_log(p, 2000 + n, -(10 ** 17), 200 * 10 ** 6), W2) for n, p in enumerate(held)]
    s = dict(pools_per_hook=1, topic_chunk=1, pool_lookups=2)
    core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=32, **s), now=NOW)   # Initialize only
    resolver, old = core.PoolResolver(), HEAD
    try:
        for loop in range(12):
            end = HEAD - BASE.lag + 1
            fake.inits += [init_log(pid(0x300 + 5 * loop + n), ZERO, USDC, HOOK, end + n) for n in range(5)]
            HEAD += 700
            core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=3, **s), now=NOW, resolver=resolver)
            if _tables(engine)[3].swap_block >= 2400:
                break
    finally:
        HEAD = old
    pools, daily, _, state = _tables(engine)
    assert state.swap_block >= 2400 and loop < 8, loop
    assert all(pools[p].sampled and daily[(p, date(2026, 9, 22))] == (1, 1, 200.0) for p in held)


def test_lookup_cost_is_logged_per_page_and_shown_in_the_heartbeat(engine, caplog):
    fake = _launchpad_fake()
    core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=32, pools_per_hook=1), now=NOW)
    with caplog.at_level("INFO", logger="bait.indexer"):
        st = core.sync_hypersync(engine, BASE, _hs(fake), FakePrices(), core.Settings(max_pages=3, pools_per_hook=1), now=NOW,
                                 resolver=core.PoolResolver())
    # PB: one call over [1600, 1700); LAUNCH: [1600, 2400) is two 700-block responses, both counted against the budget
    assert (st.unknown, st.lookups, st.chunk_calls) == (2, 3, 2)
    assert ",unknown=2" in heartbeat(1, "hypersync", {"base": st}) and ",chunk_calls=2" in heartbeat(1, "hypersync", {"base": st})
    assert any("swap page 1700-2400" in m and "unknown=1" in m and "calls=2" in m for m in caplog.messages)


def test_lookup_budget_triples_while_the_swap_cursor_is_far_behind(monkeypatch):
    s = core.Settings(pool_lookups=10)
    assert (core.lookup_budget(s, 100_000), core.lookup_budget(s, 100_001)) == (10, 30)
    monkeypatch.setenv("INDEXER_POOL_LOOKUPS_CATCHUP", "5")
    assert core.lookup_budget(core.Settings.from_env(), 200_000) == 50


def test_a_swap_window_reset_takes_its_folded_days_out_of_the_hook_totals(engine):
    agg = db.hook_pool_id(HOOK)
    with engine.begin() as c:
        c.execute(sa.insert(db.hook_pool_counts).values(chain_id=BASE.chain_id, hook_address=HOOK, pools_total=9, first_block=1,
                                                        last_block=9, swaps_total=5, volume_usd_total=100.0))
        store.apply_swaps(c, BASE.chain_id, {(agg, date(2026, 9, 20)): [2, 40.0, 0], (agg, date(2026, 9, 22)): [3, 60.0, 0]}, {})
        store.reset_swaps_from(c, BASE.chain_id, date(2026, 9, 21))
    k = _counter(engine)
    assert (k.swaps_total, k.volume_usd_total) == (2, 40.0)


def test_a_late_pool_of_a_hook_without_a_counter_is_a_plain_row(engine):
    with engine.begin() as c:
        c.execute(sa.insert(db.hook_pool_counts).values(chain_id=BASE.chain_id, hook_address=HOOK, pools_total=2, first_block=1600,
                                                        last_block=1600))
    agg = core.SwapAgg()
    agg.add(LAUNCH, date(2026, 9, 22), W1, 200.0)
    agg.add(L2, date(2026, 9, 22), W2, 200.0)
    late = {LAUNCH: _row(LAUNCH, HOOK, 1600), L2: _row(L2, HOOK2, 1601)}     # HOOK2 has no counter: a gap, not the cap
    rows, adds = core._admit_late(engine, BASE, late, agg, core.Settings(), core.ChainStats())
    with engine.begin() as c:
        store.insert_late_pools(c, rows)
    pools = _tables(engine)[0]
    assert pools[LAUNCH].sampled and not pools[L2].sampled and adds == {HOOK: [1, 0, 0.0]}


def test_a_history_reset_widens_the_lookup_floor_to_block_zero(engine):
    with engine.begin() as c:
        c.execute(sa.insert(db.hook_pool_counts).values(chain_id=BASE.chain_id, hook_address=HOOK, pools_total=9, first_block=500,
                                                        last_block=900))
        store.set_state(c, BASE.chain_id, initialize_block=100, swap_block=90, backfilled=True)
    rpc = core.Rpc("https://x", httpx.Client(transport=httpx.MockTransport(FakeRpc(100_000))))
    core.sync_rpc(engine, BASE, rpc, 120, core.Settings(rpc_max_windows=2))          # RPC mode skipped blocks: a hole
    assert _counter(engine).first_block == 0
    with engine.begin() as c:
        c.execute(sa.update(db.hook_pool_counts).values(first_block=500))
    core.sync_hypersync(engine, BASE, _hs(FakeHyperSync()), FakePrices(), core.Settings(max_pages=0), now=NOW)   # re-backfill
    assert _counter(engine).first_block == 0


def test_a_history_reset_floors_lookups_at_the_pool_managers_deployment_block_when_known(engine, monkeypatch):
    import dataclasses
    from bait.indexer import chains
    with engine.begin() as c:
        c.execute(sa.insert(db.hook_pool_counts).values(chain_id=BASE.chain_id, hook_address=HOOK, pools_total=9, first_block=500,
                                                        last_block=900))
    monkeypatch.setitem(chains.CHAIN_BY_ID, BASE.chain_id, dataclasses.replace(BASE, deploy_block=120))
    with engine.begin() as c:
        store.widen_lookup_floor(c, BASE.chain_id)
    assert _counter(engine).first_block == 120
    assert all(c.deploy_block is None or c.deploy_block >= 0 for c in chains.CHAINS)
