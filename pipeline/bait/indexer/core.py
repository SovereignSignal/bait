"""Per-chain sync. HyperSync mode: Initialize history from block 0 + Swap logs of hooked pools from 14 days back, then
follow the head. RPC mode (no token): Initialize only, new blocks only, over the public RPCs in keyless.LOGS."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone

import httpx
import sqlalchemy as sa

from ..db import as_utc, hook_pool_id
from .chains import CHAINS, IndexedChain
from .decode import INITIALIZE_TOPIC, decode_initialize, decode_swap, is_hooked, num, utc
from .hypersync import HyperSync, initialize_by_id_query, initialize_query, page, swap_query
from .prices import PriceBook, swap_usd
from . import store

log = logging.getLogger("bait.indexer")

SWAP_BACKFILL_DAYS = 14
ANCHOR_MAX_AGE = timedelta(minutes=15)   # caught-up chain without recent swaps: re-read the cursor block's time this often


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Settings:
    max_pages: int = 40              # HyperSync pages per chain per loop, so one backfilling chain cannot starve the rest
    topic_filter_max: int = 2000     # up to this many hooked pools: filter Swap logs by pool id (topic1) server side
    topic_chunk: int = 500           # pool ids per Swap query in topic mode
    rpc_max_windows: int = 30        # RPC mode: eth_getLogs windows per chain per loop before jumping to the head
    pools_per_hook: int = 2000       # v4_pools rows per (chain, hook); past it pools are counted, not stored (0 = no cap)
    pool_lookups: int = 10           # HyperSync calls per chain per loop to find pools past the cap that swap
    late_min_usd: float = 10.0       # a pool past the cap becomes a row only on a priced swap of at least this (no dust)
    sampled_per_hook: int = 1000     # second cap: such late rows per (chain, hook); the rest's swaps stay on the hook

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(max_pages=_env_int("INDEXER_MAX_PAGES", 40), topic_filter_max=_env_int("INDEXER_TOPIC_FILTER_MAX", 2000),
                   topic_chunk=max(1, _env_int("INDEXER_TOPIC_CHUNK", 500)), rpc_max_windows=_env_int("INDEXER_RPC_MAX_WINDOWS", 30),
                   pools_per_hook=_env_int("INDEXER_POOLS_PER_HOOK", 2000),
                   # at least 1: a chunk in progress always finishes, so a held swap page moves every loop
                   pool_lookups=max(1, _env_int("INDEXER_POOL_LOOKUPS", 10)),
                   late_min_usd=_env_float("INDEXER_LATE_INSERT_MIN_USD", 10.0),
                   sampled_per_hook=_env_int("INDEXER_SAMPLED_POOLS_PER_HOOK", 1000))


@dataclass
class ChainStats:
    head: int | None = None
    init_lag: int | None = None
    swap_lag: int | None = None
    new_pools: int = 0
    capped: int = 0                  # Initialize logs past the per-hook cap: counted in hook_pool_counts, not stored
    late: int = 0                    # pools past the cap inserted on their first swap (sampled)
    lookups: int = 0                 # HyperSync calls spent finding them
    folded: int = 0                  # swaps of pools past both caps (or dust), kept on the hook's aggregate pool
    swaps: int = 0
    unpriced: int = 0
    pages: int = 0
    mode: str = ""
    error: str | None = None


@dataclass
class SwapAgg:
    daily: dict = field(default_factory=dict)      # (pool_id, day) -> [swaps, usd, unpriced]
    wallets: dict = field(default_factory=dict)    # (pool_id, day, address) -> swaps
    swaps: int = 0
    unpriced: int = 0
    seen_ts: int = 0                               # newest block timestamp in the scanned pages
    best: dict = field(default_factory=dict)       # pool_id -> largest priced swap (USD) in the scanned pages

    def add(self, pool_id: str, day: date, wallet: str, usd: float | None) -> None:
        d = self.daily.setdefault((pool_id, day), [0, 0.0, 0])
        d[0] += 1
        d[1] += usd or 0.0
        d[2] += usd is None
        if usd is not None:
            self.best[pool_id] = max(self.best.get(pool_id, 0.0), usd)
        k = (pool_id, day, wallet)
        self.wallets[k] = self.wallets.get(k, 0) + 1
        self.swaps += 1
        self.unpriced += usd is None

    def fold(self, pool_id: str, into: str) -> tuple[int, float]:
        """Move a pool's swaps and wallets onto another pool id (a hook's aggregate pool) -> (swaps, usd) moved."""
        n, v = 0, 0.0
        for (p, d) in [k for k in self.daily if k[0] == pool_id]:
            s_, u, x = self.daily.pop((p, d))
            t = self.daily.setdefault((into, d), [0, 0.0, 0])
            t[0], t[1], t[2] = t[0] + s_, t[1] + u, t[2] + x
            n, v = n + s_, v + u
        for (p, d, w) in [k for k in self.wallets if k[0] == pool_id]:
            k = (into, d, w)
            self.wallets[k] = self.wallets.get(k, 0) + self.wallets.pop((p, d, w))
        return n, v


class LookupBudget(RuntimeError):
    """This loop's HyperSync budget for pool lookups is spent; the swap page waits for the next loop."""


class PoolResolver:
    """Keys of hooked pools that are not v4_pools rows (past their hook's cap, or pruned), for when one of them swaps.
    One per chain, kept by the Runner across loops. First `recent` (rows this process left out of v4_pools, the newest
    RECENT_MAX), then one HyperSync Initialize query by pool id (topic1) over [floor, page end) per `topic_chunk` ids, at
    most `pool_lookups` calls per loop (a chunk that started finishes). A swap page with ids left unresolved is not
    committed: its swaps are never dropped, and what was learned carries over to the next loop. This is also the defence
    against a hook filled to its cap with junk pools: a later real pool is a row from its first swap on. `absent` remembers ids that are not hooked pools (unhooked pools are most of a
    scan page); it is only valid for the floor it was built with, so it is cleared when the floor moves down."""
    RECENT_MAX = 50_000
    ABSENT_MAX = 1_000_000

    def __init__(self):
        self.recent: dict[str, dict] = {}
        self.absent: set[str] = set()
        self.absent_floor: int | None = None
        self.calls = 0

    def remember(self, rows) -> None:
        for r in rows:
            self.recent[r["pool_id"]] = r
        if len(self.recent) > self.RECENT_MAX:          # keep the newest half
            self.recent = dict(list(self.recent.items())[-(self.RECENT_MAX // 2):])

    def resolve(self, hs: HyperSync, chain: IndexedChain, ids, floor: int, to_block: int, settings: Settings) -> dict[str, dict]:
        """-> {pool_id: v4_pools row} for the ids that are hooked pools. A swap at a block below to_block means the pool
        was initialized below it, so one scan of [floor, to_block) settles every id it asked for."""
        if self.absent_floor is None or floor < self.absent_floor:
            self.absent.clear()
            self.absent_floor = floor
        found = {i: self.recent[i] for i in ids if i in self.recent}
        need = sorted(i for i in ids if i not in found and i not in self.absent)
        for k in range(0, len(need), settings.topic_chunk):
            if self.calls >= settings.pool_lookups:
                raise LookupBudget(f"pool lookup budget spent ({self.calls} calls), {len(need) - k} ids left")
            chunk, got, cur = need[k:k + settings.topic_chunk], {}, floor
            while cur < to_block:
                logs, blocks, _, nb, _ = page(hs.query(initialize_by_id_query(chain.pool_manager, cur, to_block, chunk)))
                self.calls += 1
                if nb is None or nb <= cur:
                    raise RuntimeError(f"HyperSync made no progress at block {cur} (next_block={nb})")
                for r in hooked_pools_from_page(chain.chain_id, logs, blocks):
                    got[r["pool_id"]] = r
                cur = nb
            self.remember(got.values())
            found.update(got)
            self.absent.update(i for i in chunk if i not in got)
        if len(self.absent) > self.ABSENT_MAX:
            self.absent.clear()
        return found


def plan(token: str | None, chains: list[IndexedChain] | None = None) -> tuple[str, list[IndexedChain]]:
    """Which path runs: with a token every chain via HyperSync; without, the RPC fallback on chains with a public RPC."""
    chains = list(CHAINS) if chains is None else chains
    if token:
        return "hypersync", chains
    return "rpc", [c for c in chains if c.rpc_net]


# ---------------------------------------------------------------- HyperSync

def _pool_row(chain_id: int, p: dict, lg: dict, block_ts: int | None, block_number: int | None) -> dict:
    return {**p, "chain_id": chain_id, "block_number": block_number, "block_time": utc(block_ts),
            "tx_hash": (lg.get("transaction_hash") or lg.get("transactionHash") or "").lower() or None}


def hooked_pools_from_page(chain_id: int, logs: list[dict], blocks: dict[int, int]) -> list[dict]:
    rows = []
    for lg in logs:
        p = decode_initialize(lg)
        if p and is_hooked(p):
            bn = num(lg.get("block_number"))
            rows.append(_pool_row(chain_id, p, lg, blocks.get(bn), bn))
    return rows


def aggregate_swaps(chain: IndexedChain, logs: list[dict], blocks: dict[int, int], txs: dict[str, str],
                    known: dict[str, tuple[str, str]], prices: PriceBook | None, agg: SwapAgg) -> None:
    """Swaps of known hooked pools -> agg. Wallet = transaction sender (tx.from), falling back to the Swap sender."""
    agg.seen_ts = max([agg.seen_ts, *(t for t in blocks.values() if t)])
    decoded, missing = [], 0
    for lg in logs:
        s = decode_swap(lg)
        if s is None or s["pool_id"] not in known:
            continue
        ts = blocks.get(num(lg.get("block_number")))
        if ts is None:
            missing += 1
            continue
        decoded.append((s, ts, txs.get((lg.get("transaction_hash") or "").lower()) or s["sender"] or "unknown"))
    if missing:  # the cursor would move past swaps we never counted
        raise RuntimeError(f"chain={chain.slug}: {missing} swap logs of hooked pools without a block timestamp; page not committed")
    if not decoded:
        return
    book = {}
    if prices is not None:
        addrs = {c for s, _, _ in decoded for c in known[s["pool_id"]]}
        book = prices.get(chain, addrs)
    for s, ts, wallet in decoded:
        c0, c1 = known[s["pool_id"]]
        usd = swap_usd(s["amount0"], s["amount1"], book.get(c0), book.get(c1))
        agg.add(s["pool_id"], datetime.fromtimestamp(ts, tz=timezone.utc).date(), wallet, usd)


def swap_start_ts(now: datetime) -> tuple[date, int]:
    d = now.astimezone(timezone.utc).date() - timedelta(days=SWAP_BACKFILL_DAYS)
    return d, int(datetime.combine(d, dtime.min, tzinfo=timezone.utc).timestamp())


def _start_swap_phase(engine, chain: IndexedChain, hs: HyperSync, swap_from: int, end: int, head: int,
                      now: datetime) -> tuple[int, datetime]:
    """Initialize history just reached the swap cursor picked on the first run, which is days old after a long backfill
    (Base). Move it up to the first block of today-14 so the window is 14 days. Always writes swap_block_time, so it runs
    once per reset. No reset_swaps_from: swap_time None means no swap page was committed since the last reset."""
    start_day, ts = swap_start_ts(now)
    cur_ts = hs.block_timestamp(swap_from)
    if cur_ts is not None and cur_ts >= ts:
        new, t = swap_from, cur_ts
    else:
        new, t = max(swap_from, hs.block_at_or_after(ts, end - 1)), ts
    with engine.begin() as conn:
        store.advance_swap_cursor(conn, chain.chain_id, swap_from, new, head, utc(t))
    if new != swap_from:
        log.info("chain=%s swap phase starts: window moved from block %d to %d (%s 00:00 UTC)", chain.slug, swap_from, new, start_day)
    return new, utc(t)


def sync_hypersync(engine: sa.Engine, chain: IndexedChain, hs: HyperSync, prices: PriceBook | None,
                   settings: Settings, now: datetime | None = None, should_stop=None,
                   resolver: PoolResolver | None = None) -> ChainStats:
    stop = should_stop or (lambda: False)
    resolver = resolver or PoolResolver()
    resolver.calls = 0
    st = ChainStats(mode="hypersync")
    now = now or datetime.now(timezone.utc)
    head = hs.height()
    st.head = head
    end = head - chain.lag + 1          # exclusive end of the reorg-safe range
    if end <= 0:
        return st
    with engine.connect() as conn:
        state = store.get_state(conn, chain.chain_id)
    if state is None or state.swap_block is None:
        # First HyperSync run for this chain (or history broken by an RPC-mode gap): full Initialize backfill from 0 and a
        # fresh 14-day Swap window starting at the first block of that UTC day.
        start_day, ts = swap_start_ts(now)
        start_block = hs.block_at_or_after(ts, end - 1)
        with engine.begin() as conn:
            store.reset_swaps_from(conn, chain.chain_id, start_day)
            store.set_state(conn, chain.chain_id, initialize_block=0, swap_block=start_block, head_block=head, backfilled=False,
                            swap_block_time=None)
        log.info("chain=%s backfill start: Initialize from 0, Swap from block %d (%s 00:00 UTC)", chain.slug, start_block, start_day)
        with engine.connect() as conn:
            state = store.get_state(conn, chain.chain_id)

    init_from = state.initialize_block or 0
    while init_from < end and st.pages < settings.max_pages and not stop():
        logs, blocks, _, nb, _ = page(hs.query(initialize_query(chain.pool_manager, init_from, end)))
        st.pages += 1
        if nb is None or nb <= init_from:
            raise RuntimeError(f"HyperSync made no progress at block {init_from} (next_block={nb})")
        nb = min(nb, end)
        rows = hooked_pools_from_page(chain.chain_id, logs, blocks)
        with engine.begin() as conn:
            stored, over = store.store_pools(conn, chain.chain_id, rows, settings.pools_per_hook)   # before the upsert, same tx
            store.set_state(conn, chain.chain_id, initialize_block=nb, head_block=head)
        resolver.remember(over)
        st.new_pools += len(stored)
        st.capped += len(over)
        init_from = nb

    swap_from = state.swap_block
    swap_time = as_utc(state.swap_block_time)
    if swap_time is None and not state.backfilled and init_from > swap_from:
        swap_from, swap_time = _start_swap_phase(engine, chain, hs, swap_from, end, head, now)
    swap_end = min(init_from, end)      # never count swaps past the block our pool list is complete for
    with engine.connect() as conn:
        n_pools = store.pool_count(conn, chain.chain_id)
        floor = store.capped_floor(conn, chain.chain_id)   # None: no hook past the cap, every hooked pool is a row
    if n_pools == 0 and floor is None:
        if swap_from < swap_end:
            with engine.begin() as conn:
                store.advance_swap_cursor(conn, chain.chain_id, swap_from, swap_end, head)
            swap_from = swap_end
    elif n_pools <= settings.topic_filter_max and floor is None:
        st.mode = "hypersync/topic"
        with engine.connect() as conn:
            known = store.all_pools(conn, chain.chain_id)
        ids = sorted(known)
        chunks = [ids[i:i + settings.topic_chunk] for i in range(0, len(ids), settings.topic_chunk)]
        while swap_from < swap_end and st.pages < settings.max_pages and not stop():
            agg = SwapAgg()
            logs, blocks, txs, nb, _ = page(hs.query(swap_query(chain.pool_manager, swap_from, swap_end, chunks[0])))
            st.pages += 1
            if nb is None or nb <= swap_from:
                raise RuntimeError(f"HyperSync made no progress at block {swap_from} (next_block={nb})")
            nb = min(nb, swap_end)
            aggregate_swaps(chain, logs, blocks, txs, known, prices, agg)
            for chunk in chunks[1:]:   # same block range for the other id chunks, so one cursor covers them all
                cur = swap_from
                while cur < nb:
                    logs, blocks, txs, n2, _ = page(hs.query(swap_query(chain.pool_manager, cur, nb, chunk)))
                    st.pages += 1
                    if n2 is None or n2 <= cur:
                        raise RuntimeError(f"HyperSync made no progress at block {cur} (next_block={n2})")
                    aggregate_swaps(chain, logs, blocks, txs, known, prices, agg)
                    cur = n2
            swap_time = _commit_swaps(engine, chain, agg, swap_from, nb, head, st) or swap_time
            swap_from = nb
    else:
        st.mode = "hypersync/scan"
        while swap_from < swap_end and st.pages < settings.max_pages and not stop():
            agg = SwapAgg()
            logs, blocks, txs, nb, _ = page(hs.query(swap_query(chain.pool_manager, swap_from, swap_end)))
            st.pages += 1
            if nb is None or nb <= swap_from:
                raise RuntimeError(f"HyperSync made no progress at block {swap_from} (next_block={nb})")
            nb = min(nb, swap_end)
            ids = set()
            for lg in logs:
                s = decode_swap(lg)
                if s:
                    ids.add(s["pool_id"])
            with engine.connect() as conn:
                known = store.pools_by_id(conn, chain.chain_id, ids)
            late: dict[str, dict] = {}
            unknown = ids - set(known)
            if floor is not None and unknown:      # some may be hooked pools past the cap: find their keys
                try:
                    late = resolver.resolve(hs, chain, unknown, floor, nb, settings)
                except LookupBudget as e:
                    log.info("chain=%s %s; the swap page at block %d waits for the next loop", chain.slug, e, swap_from)
                    break
                known.update({p: (r["currency0"], r["currency1"]) for p, r in late.items()})
            aggregate_swaps(chain, logs, blocks, txs, known, prices, agg)
            rows, adds = _admit_late(engine, chain, late, agg, settings, st) if late else ([], {})
            swap_time = _commit_swaps(engine, chain, agg, swap_from, nb, head, st, rows, adds) or swap_time
            swap_from = nb

    if swap_from >= end and (swap_time is None or now - swap_time > ANCHOR_MAX_AGE):
        ts = hs.block_timestamp(swap_from - 1)
        if ts is not None:
            with engine.begin() as conn:
                store.advance_swap_cursor(conn, chain.chain_id, swap_from, swap_from, head, utc(ts))
    st.lookups = resolver.calls
    st.init_lag = max(end - init_from, 0)
    st.swap_lag = max(end - swap_from, 0)
    if not state.backfilled and init_from >= end and swap_from >= end:
        with engine.begin() as conn:
            store.set_state(conn, chain.chain_id, backfilled=True, head_block=head)
        log.info("chain=%s backfill complete at block %d", chain.slug, end - 1)
    return st


def _admit_late(engine: sa.Engine, chain: IndexedChain, late: dict[str, dict], agg: SwapAgg, settings: Settings,
                st: ChainStats) -> tuple[list[dict], dict[str, list]]:
    """Which pools past the cap that swapped in this page become rows: a priced swap of at least late_min_usd (dust and
    unpriced swaps cannot buy a row), at most sampled_per_hook per hook, biggest swap first. The others' swaps and wallets
    are folded into the hook's aggregate pool, so its usage is never lost. A hook with no counter (a pool missing for
    another reason, e.g. a gap in Initialize history) gets a plain row. -> (rows to insert, {hook: [sampled, swaps, usd]})"""
    with engine.connect() as conn:
        counters = store.hook_counters(conn, chain.chain_id, {r["hook_address"] for r in late.values()})
    free = {h: settings.sampled_per_hook - (c.sampled_total or 0) for h, c in counters.items()}
    rows, adds = [], {}
    for p, r in sorted(late.items(), key=lambda kv: (-agg.best.get(kv[0], -1.0), kv[0])):
        h = r["hook_address"]
        if h not in counters:
            rows.append(r)
            continue
        a = adds.setdefault(h, [0, 0, 0.0])
        if agg.best.get(p, 0.0) >= settings.late_min_usd and p in agg.best and free[h] > 0:
            rows.append(r)
            free[h] -= 1
            a[0] += 1
        else:
            n, v = agg.fold(p, hook_pool_id(h))
            a[1] += n
            a[2] += v
    return rows, adds


def _commit_swaps(engine: sa.Engine, chain: IndexedChain, agg: SwapAgg, expected: int, cursor: int, head: int,
                  st: ChainStats, late=(), hook_adds: dict | None = None) -> datetime | None:
    """late: pools past their hook's cap admitted as rows in this page (sampled); hook_adds: hook_pool_counts increments.
    Both are written with the page, after the CAS."""
    block_time = utc(agg.seen_ts) if agg.seen_ts else None
    with engine.begin() as conn:
        store.advance_swap_cursor(conn, chain.chain_id, expected, cursor, head, block_time)   # first: a loser fails before writing
        store.insert_late_pools(conn, list(late))
        store.add_hook_usage(conn, chain.chain_id, hook_adds or {})
        store.apply_swaps(conn, chain.chain_id, agg.daily, agg.wallets)
    st.late += len(late)
    st.folded += sum(a[1] for a in (hook_adds or {}).values())
    st.swaps += agg.swaps
    st.unpriced += agg.unpriced
    return block_time


# ---------------------------------------------------------------- RPC fallback (no token)

class RpcError(RuntimeError):
    pass


class Rpc:
    def __init__(self, url: str, client: httpx.Client, sleep=None):
        import time
        self.url = url
        self.client = client
        self.calls = 0
        self.sleep = sleep or time.sleep

    def call(self, method: str, params: list):
        """One JSON-RPC call; public endpoints throttle (429), so back off twice before giving up for this loop."""
        for attempt in range(3):
            self.calls += 1
            try:
                r = self.client.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                     headers={"User-Agent": "bait-indexer/0.1"}, timeout=30)
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise RpcError(f"{self.url} {method}: {e!r}") from e
                self.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (429, 502, 503, 504) and attempt < 2:
                self.sleep(3 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise RpcError(f"{self.url} {method}: HTTP {r.status_code}")
            body = r.json()
            if "error" in body:
                raise RpcError(f"{self.url} {method}: {body['error']}")
            return body.get("result")
        raise RpcError(f"{self.url} {method}: gave up")


# Following the head needs an endpoint that tolerates one eth_getLogs per loop. mainnet.base.org and bsc.drpc.org
# (keyless.LOGS) answered 429 from the first call on 2026-10-08; publicnode served 1000-block Initialize windows on both.
RPC_OVERRIDES = {"base": ("https://base-rpc.publicnode.com", 500), "bsc": ("https://bsc-rpc.publicnode.com", 200)}


def rpc_endpoint(chain: IndexedChain) -> tuple[str, str, int]:
    """(pool manager, url, eth_getLogs window in blocks): keyless.LOGS unless overridden above."""
    from .. import keyless as K
    pm, url, window = K.LOGS[chain.rpc_net]
    url, window = RPC_OVERRIDES.get(chain.rpc_net, (url, window))
    return pm.lower(), url, window


def sync_rpc(engine: sa.Engine, chain: IndexedChain, rpc: Rpc, window: int, settings: Settings) -> ChainStats:
    st = ChainStats(mode="rpc")
    pm = chain.pool_manager
    head = num(rpc.call("eth_blockNumber", []))
    st.head = head
    end = head - chain.lag + 1
    with engine.connect() as conn:
        state = store.get_state(conn, chain.chain_id)
    if state is None or state.initialize_block is None:
        with engine.begin() as conn:
            store.set_state(conn, chain.chain_id, initialize_block=end, head_block=head)
        log.info("chain=%s rpc mode: following new blocks from %d (no backfill without HYPERSYNC_API_TOKEN)", chain.slug, end)
        st.init_lag = 0
        return st
    cur = state.initialize_block
    max_gap = window * settings.rpc_max_windows
    if end - cur > max_gap:
        skipped = end - max_gap - cur
        cur = end - max_gap
        # History now has a hole: clear the swap cursor so a later HyperSync run re-backfills from block 0.
        with engine.begin() as conn:
            store.set_state(conn, chain.chain_id, initialize_block=cur, swap_block=None, backfilled=False, head_block=head,
                            swap_block_time=None)
        log.warning("chain=%s rpc mode fell %d blocks behind; skipped them (a HyperSync run will backfill)", chain.slug, skipped)
    while cur < end:
        to = min(cur + window, end) - 1
        logs = rpc.call("eth_getLogs", [{"address": pm, "topics": [INITIALIZE_TOPIC], "fromBlock": hex(cur), "toBlock": hex(to)}]) or []
        st.pages += 1
        rows = []
        need_ts = {}
        for lg in logs:
            p = decode_initialize(lg)
            if p and is_hooked(p):
                bn = num(lg.get("blockNumber"))
                ts = num(lg.get("blockTimestamp")) or None    # some nodes (Robinhood) send 0x0
                row = _pool_row(chain.chain_id, p, lg, ts, bn)
                rows.append(row)
                if ts is None and bn is not None:
                    need_ts.setdefault(bn, []).append(row)
        for bn, rs in need_ts.items():
            b = rpc.call("eth_getBlockByNumber", [hex(bn), False]) or {}
            for r in rs:
                r["block_time"] = utc(num(b.get("timestamp")))
        with engine.begin() as conn:
            stored, _ = store.store_pools(conn, chain.chain_id, rows, settings.pools_per_hook)
            store.set_state(conn, chain.chain_id, initialize_block=to + 1, head_block=head)
        st.new_pools += len(stored)
        cur = to + 1
    st.init_lag = max(end - cur, 0)
    return st
