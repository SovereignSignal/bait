# Package 5: Indexer safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The on-chain indexer never counts a swap twice, never lets a stuck or unpriced swap feed override GeckoTerminal numbers, and stops growing the database with nightly rewrites.

**Architecture:** One additive migration (`0010`) mirrored in `db.py`. The indexer takes a per-chain Postgres advisory lock per sync and advances the swap cursor by compare-and-set, so a second process (Railway deploy overlap) loses cleanly instead of double counting. The swap cursor carries its block time (`indexer_state.swap_block_time`), which the nightly `onchain` step and `score._wash_7d` use as the freshness gate. Volume the chain cannot measure (custom-accounting hooks, mostly unpriced pool-days) is marked unknown and never overrides keyless numbers. The nightly `pools` copy becomes incremental (per-chain block watermark plus a catch-up for newly known hooks) and never rewrites unchanged rows.

**Tech Stack:** Python 3.12, SQLAlchemy 2 Core, psycopg 3, httpx, pytest. Postgres 15+ (`pg_ctl`, `initdb` on PATH) for `tests/test_postgres.py`.

**Spec:** Indexer audit of 2026-10-09, findings 1-6: deploy overlap double count; freshness gate blind to a stuck swap cursor; custom-accounting hooks show $0; unpriced swaps stored as $0 forever; disk growth from the nightly full `pools` upsert; `pool_daily_onchain` retention, swap logs without block timestamps, swap window start fixed on first run.

## Global Constraints

- Python 3.12. Tests: `cd pipeline && .venv/bin/python -m pytest -q`. Baseline on `main` at f1ebed2: `132 passed, 3 skipped`.
- Postgres tests: `cd pipeline && eval "$(scripts/pg-test-cluster.sh start)" && .venv/bin/python -m pytest -q tests/test_postgres.py; scripts/pg-test-cluster.sh stop`. They skip unless `TEST_DATABASE_URL` is set.
- No network in tests: HyperSync, RPC and DefiLlama are `httpx.MockTransport` fakes (see `tests/test_indexer.py`).
- Do not change HyperSync query bodies or pagination (`indexer/hypersync.py`), do not add chains, do not change `web/`.
- Schema changes go in `db/migrations/0010_indexer_safety.sql` AND `pipeline/bait/db.py` (`ADDITIVE_COLUMNS`, `ADDITIVE_INDEXES`, tables).
- Every commit leaves the suite green and `python -m bait.indexer --once` runnable.
- Commit after every task. Imperative subject, short body when the why is not obvious. End every commit message with these two trailer lines, exactly:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01A2FHLwk9NeThmyZ1JbHe3o`
- No hostnames, absolute paths, or secrets in code, docs, or commits. Do not push.

## Review Focus

1. Two indexer processes on the same chain and the same swap page (deploy overlap): the page is counted once; the loser raises `CursorMoved` and commits nothing. (Tasks 2, 3)
2. Swap queries fail every loop while Initialize pages keep committing (`updated_at` fresh): the nightly step and the 7-day wash rule treat the chain as not ready and keep keyless numbers. (Tasks 4, 6)
3. A pool whose hook has `beforeSwapReturnsDelta` and a GeckoTerminal 24h number: GeckoTerminal volume survives, on-chain wallets are used, the row says `vol_source='hook'`. (Task 9)
4. DefiLlama returns 503 for a page: the swap cursor does not move, and the retry next loop counts every swap once and priced. (Task 7)
5. A hook deferred by `ONCHAIN_MAX_NEW_HOOKS` whose pools sit below the watermark: its pools are copied the night it becomes known; a run with nothing new reads and writes nothing. (Task 10)

### Task 1: Schema, and the indexer runs the additive migration

**Files:** Create `db/migrations/0010_indexer_safety.sql`. Modify `pipeline/bait/db.py:95-107` (`pool_stats`), `:237-266` (indexer tables), `:308-324` (`ADDITIVE_COLUMNS`, `ADDITIVE_INDEXES`); `pipeline/bait/indexer/__main__.py:102-103`. Test `pipeline/tests/test_indexer.py`.
**Interfaces:** Produces `indexer_state.swap_block_time`, `pool_daily_onchain.unpriced_swaps`, `pool_stats.vol_source`, table `db.onchain_watermarks`, index `v4_pools_chain_block_idx`.

- [ ] **Step 1: Failing test** (append to `tests/test_indexer.py`)
```python
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
```
- [ ] **Step 2:** `cd pipeline && .venv/bin/python -m pytest -q tests/test_indexer.py -k additive_schema` → FAIL (`indexer_state.swap_block_time` not added).
- [ ] **Step 3: Implement.** `db/migrations/0010_indexer_safety.sql`:
```sql
-- Package 5 (indexer safety). Additive; also applied at startup by db.add_missing_columns / add_missing_indexes.
-- Block time the swap cursor has reached (a lower bound): the nightly freshness gate (steps/onchain.py).
ALTER TABLE indexer_state ADD COLUMN IF NOT EXISTS swap_block_time timestamptz;
-- Swaps with no priced side: their USD value is missing, not zero.
ALTER TABLE pool_daily_onchain ADD COLUMN IF NOT EXISTS unpriced_swaps integer NOT NULL DEFAULT 0;
-- Source of pool_stats.vol_24h: 'onchain' | 'hook' (custom accounting) | 'unpriced'. NULL = keyless / subgraph.
ALTER TABLE pool_stats ADD COLUMN IF NOT EXISTS vol_source text;
-- Nightly onchain step: highest v4_pools.block_number already copied into pools, per chain.
CREATE TABLE IF NOT EXISTS onchain_watermarks (chain_id integer PRIMARY KEY, pools_block bigint, updated_at timestamptz);
CREATE INDEX IF NOT EXISTS v4_pools_chain_block_idx ON v4_pools (chain_id, block_number, pool_id);
```
`db.py`: `pool_stats` gains `sa.Column("vol_source", sa.Text),  # 'onchain' | 'hook' | 'unpriced'; NULL = keyless (0010)`; `pool_daily_onchain` gains `sa.Column("unpriced_swaps", sa.Integer, nullable=False, server_default="0"),`; `indexer_state` gains `sa.Column("swap_block_time", TS),  # block time the swap cursor reached (lower bound)`. After `indexer_state`:
```python
# Nightly onchain step's copy watermark (0010). Owned by the pipeline, not the indexer, so the step's long transaction never
# holds a row lock that the indexer's cursor updates would wait on.
onchain_watermarks = sa.Table(
    "onchain_watermarks", metadata,
    sa.Column("chain_id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("pools_block", sa.BigInteger),
    sa.Column("updated_at", TS),
)
```
Append to `ADDITIVE_COLUMNS`: `("indexer_state", "swap_block_time", "timestamptz")`, `("pool_daily_onchain", "unpriced_swaps", "integer NOT NULL DEFAULT 0")`, `("pool_stats", "vol_source", "text")`. Append to `ADDITIVE_INDEXES`: `("v4_pools_chain_block_idx", "v4_pools", "chain_id, block_number, pool_id")`.
`indexer/__main__.py`: the indexer redeploys on every `db.py` change but never ran the additive migration, so it would write `swap_block_time` before the column exists. After `db.create_all(engine)` add `db.add_missing_columns(engine)`.
- [ ] **Step 4:** full suite → `133 passed, 3 skipped`.
- [ ] **Step 5: Commit**
```bash
git add db/migrations/0010_indexer_safety.sql pipeline/bait/db.py pipeline/bait/indexer/__main__.py pipeline/tests/test_indexer.py
git commit -m "db: 0010 indexer safety schema; the indexer now runs the additive column migration on start"
```

### Task 2: One indexer per chain (advisory lock), stop between pages

**Files:** Modify `pipeline/bait/db.py:381-397`; `pipeline/bait/indexer/__main__.py:30-60` (`Runner`), `:113-130` (`main`); `pipeline/bait/indexer/core.py:128-217`. Test `tests/test_indexer.py`, `tests/test_postgres.py`.
**Interfaces:** `db.try_advisory_lock(engine, key) -> Connection | None` (`try_run_lock` delegates to it; the lock connection's transaction is committed right after acquiring, so it is not idle in transaction for hours; session advisory locks survive commit). `sync_hypersync(..., should_stop=None)`, `Runner(..., should_stop=None)`. Chain key `f"bait-indexer:{chain_id}"`; held → `ChainStats(error="locked")`.

- [ ] **Step 1: Failing tests.** Append to `tests/test_indexer.py`:
```python
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
```
In `test_runner_one_chain_failing_does_not_stop_others` change `def fake_sync(engine_, ch, hs, prices, settings):` to `def fake_sync(engine_, ch, hs, prices, settings, **kw):`. Append to `tests/test_postgres.py`:
```python
def test_indexer_chain_lock_is_exclusive_per_chain_and_released_on_close(pg_engine):
    a = db.try_advisory_lock(pg_engine, "bait-indexer:8453")
    try:
        assert db.try_advisory_lock(pg_engine, "bait-indexer:8453") is None
        other = db.try_advisory_lock(pg_engine, "bait-indexer:1")
        assert other is not None
        other.close()
    finally:
        a.close()
    b = db.try_advisory_lock(pg_engine, "bait-indexer:8453")
    assert b is not None
    b.close()

def test_runner_skips_a_chain_another_indexer_holds(pg_engine, monkeypatch):
    import httpx
    from bait.indexer import core
    from bait.indexer.__main__ import Runner
    from bait.indexer.chains import select_chains
    seen = []
    monkeypatch.setattr("bait.indexer.__main__.sync_hypersync",
                        lambda e, ch, hs, p, s, **kw: seen.append(ch.slug) or core.ChainStats(mode="hypersync"))
    held = db.try_advisory_lock(pg_engine, "bait-indexer:8453")
    try:
        out = Runner(pg_engine, "tok", select_chains("ethereum,base"),
                     httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(500))), core.Settings()).run_once()
    finally:
        held.close()
    assert seen == ["ethereum"] and out["base"].error == "locked"
```
- [ ] **Step 2:** SQLite `-k "stop_flag or stops_between"` → FAIL (`unexpected keyword argument 'should_stop'`); Postgres `-k "chain_lock or skips_a_chain"` → FAIL (`no attribute 'try_advisory_lock'`).
- [ ] **Step 3: Implement.** `db.py`, replace `try_run_lock`:
```python
_LOCK_ENGINES: dict = {}

def try_advisory_lock(engine: sa.Engine, key: str) -> sa.Connection | None:
    """Session-level pg_try_advisory_lock(hashtext(key)) on a dedicated connection; close() releases it. None when another
    session holds it. Other dialects: no lock, an open connection. The connection comes from a NullPool engine (cached per
    URL): a pooled connection would go back to the pool on close() with its session, and so the lock, still alive. The
    transaction is committed at once: the lock survives it and the session is not left idle in transaction."""
    if engine.dialect.name != "postgresql":
        return engine.connect()
    lock_engine = _LOCK_ENGINES.get(engine.url)
    if lock_engine is None:
        lock_engine = _LOCK_ENGINES[engine.url] = sa.create_engine(engine.url, poolclass=sa.pool.NullPool, future=True)
    conn = lock_engine.connect()
    try:
        got = conn.execute(sa.text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": key}).scalar_one()
        conn.commit()
    except Exception:
        conn.close()
        raise
    if got:
        return conn
    conn.close()
    return None

def try_run_lock(engine: sa.Engine) -> sa.Connection | None:
    """One nightly run at a time; see try_advisory_lock."""
    return try_advisory_lock(engine, RUN_LOCK_KEY)
```
`core.py` `sync_hypersync`: add parameter `should_stop=None` after `now`; first body line `stop = should_stop or (lambda: False)`; append `and not stop()` to the three page loops (lines 152, 180, 201). The inner chunk loop (line 190) stays: a page commits whole or not at all.
`__main__.py` `Runner.__init__` gains `should_stop=None` → `self.should_stop = should_stop or (lambda: False)`. In `run_once`, the top of the chain loop becomes:
```python
        for ch in self.chains:
            if self.should_stop():
                log.info("stop requested: leaving the loop before chain=%s", ch.slug)
                break
            # Railway starts the new container before stopping the old one: never two processes on one chain.
            lock = db.try_advisory_lock(self.engine, f"bait-indexer:{ch.chain_id}")
            if lock is None:
                log.info("chain=%s is being synced by another indexer process; skipped this loop", ch.slug)
                out[ch.slug] = ChainStats(mode=self.mode, error="locked")
                continue
            try:
```
keep the existing body and both `except` clauses, pass `should_stop=self.should_stop` to `sync_hypersync`, and add `finally: lock.close()` after the last `except`. In `main()`, move `stop = {"flag": False}` and `_stop` above `Runner(...)`, pass `should_stop=lambda: stop["flag"]`, and change the signal log to `"signal %s: stopping after the current page"`.
- [ ] **Step 4:** full suite → `135 passed, 5 skipped`; Postgres → `5 passed`.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/db.py pipeline/bait/indexer/__main__.py pipeline/bait/indexer/core.py pipeline/tests/test_indexer.py pipeline/tests/test_postgres.py
git commit -m "indexer: per-chain advisory lock per sync and a stop check between pages and chains

Railway starts the new container 10-15 s before stopping the old one; both synced the
same chain from the same cursor and apply_swaps added the page twice."
```

### Task 3: Compare-and-set on the swap cursor

**Files:** Modify `pipeline/bait/indexer/store.py` (after `set_state`); `pipeline/bait/indexer/core.py:169-173`, `:197`, `:216`, `:228-233`. Test `tests/test_indexer.py`, `tests/test_postgres.py`.
**Interfaces:** `store.advance_swap_cursor(conn, chain_id, expected, new, head, block_time=None)` = `UPDATE indexer_state SET swap_block=:new … WHERE chain_id=:c AND swap_block=:expected`; no row → raises `store.CursorMoved`, rolling the page back. Every swap cursor write after the first-run reset goes through it.

- [ ] **Step 1: Failing tests.** Append to `tests/test_indexer.py`:
```python
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
```
Append to `tests/test_postgres.py`:
```python
def test_swap_cursor_cas_loser_waits_on_the_row_lock_then_matches_nothing(pg_engine):
    import threading
    from bait.indexer import store
    with pg_engine.begin() as c:
        store.set_state(c, 8453, initialize_block=100, swap_block=10, head_block=100)
    a = pg_engine.connect()
    a.begin()
    store.advance_swap_cursor(a, 8453, 10, 20, 100)           # first container: row locked, not committed yet
    result = {}

    def second():
        with pg_engine.begin() as b:
            try:
                store.advance_swap_cursor(b, 8453, 10, 20, 100)
                result["b"] = "advanced"
            except store.CursorMoved:
                result["b"] = "moved"
    t = threading.Thread(target=second)
    t.start()
    t.join(timeout=1.0)
    assert t.is_alive()                                        # blocked on A's row lock
    a.commit()
    a.close()
    t.join(timeout=10)
    assert result == {"b": "moved"}                            # READ COMMITTED re-checks swap_block = 10 after A commits
    with pg_engine.connect() as c:
        assert store.get_state(c, 8453).swap_block == 20
```
- [ ] **Step 2:** SQLite `-k same_page_count_it_once` → FAIL (`AttributeError: CursorMoved`; the bug itself shows as `(6, 2, 12000.0)`); Postgres `-k cas_loser` → FAIL.
- [ ] **Step 3: Implement.** `store.py`:
```python
class CursorMoved(RuntimeError):
    """Another indexer process advanced this chain's swap cursor first. The page's transaction rolls back: counted once."""

def advance_swap_cursor(conn: sa.Connection, chain_id: int, expected: int, new: int, head: int, block_time=None) -> None:
    """Compare-and-set. Swap aggregates are increments, so the cursor may only move from the value this process read.
    block_time: block timestamp the cursor has reached (a lower bound); None keeps the stored one."""
    vals = {"swap_block": new, "head_block": head, "updated_at": utcnow()}
    if block_time is not None:
        vals["swap_block_time"] = block_time
    r = conn.execute(sa.update(indexer_state)
                     .where(indexer_state.c.chain_id == chain_id, indexer_state.c.swap_block == expected).values(**vals))
    if r.rowcount != 1:
        raise CursorMoved(f"chain_id={chain_id}: swap_block is no longer {expected}")
```
`core.py`, `_commit_swaps(engine, chain, agg, expected, cursor, head, st)`: inside `engine.begin()` call `store.advance_swap_cursor(conn, chain.chain_id, expected, cursor, head)` first (a loser fails before writing), then `store.apply_swaps(...)`; drop the `set_state`. Both call sites: `_commit_swaps(engine, chain, agg, swap_from, nb, head, st)`. No-pools branch (170-173): replace `store.set_state(conn, chain.chain_id, swap_block=swap_end, head_block=head)` with `store.advance_swap_cursor(conn, chain.chain_id, swap_from, swap_end, head)`. `CursorMoved` reaches `Runner.run_once`'s generic handler (`chain=… sync failed: CursorMoved`).
- [ ] **Step 4:** full suite → `136 passed, 6 skipped`; Postgres → `6 passed`.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/indexer/store.py pipeline/bait/indexer/core.py pipeline/tests/test_indexer.py pipeline/tests/test_postgres.py
git commit -m "indexer: compare-and-set the swap cursor; a process that lost the race rolls its page back"
```

### Task 4: Persist the swap cursor's block time

**Files:** Modify `pipeline/bait/indexer/core.py` (`SwapAgg` :57-71, `aggregate_swaps` :99-101, reset :146, :165, before :219, `_commit_swaps`), `sync_rpc` :308. Test `tests/test_indexer.py`.
**Interfaces:** `swap_block_time` = newest block timestamp in the committed swap pages (any Swap log the query returned), a lower bound of the cursor's time. At the reorg-safe head with that value older than `ANCHOR_MAX_AGE` (15 min), one `hs.block_timestamp(cursor - 1)` anchors it, so quiet chains do not look stuck. NULL whenever the swap cursor is reset.

- [ ] **Step 1: Failing tests** (append to `tests/test_indexer.py`)
```python
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
```
- [ ] **Step 2:** `-k "newest_block or anchors"` → FAIL (`swap_block_time` is the seeded value / None).
- [ ] **Step 3: Implement** in `core.py`. Import `from ..db import as_utc`; add `ANCHOR_MAX_AGE = timedelta(minutes=15)` under `SWAP_BACKFILL_DAYS`. `SwapAgg` gains `seen_ts: int = 0   # newest block timestamp in the scanned pages`. First line of `aggregate_swaps`: `agg.seen_ts = max([agg.seen_ts, *(t for t in blocks.values() if t)])`. Add `swap_block_time=None` to the first-run reset `set_state` (line 146) and to the `sync_rpc` gap reset (line 308). After `swap_from = state.swap_block`: `swap_time = as_utc(state.swap_block_time)`. `_commit_swaps` (now `-> datetime | None`):
```python
    block_time = utc(agg.seen_ts) if agg.seen_ts else None
    with engine.begin() as conn:
        store.advance_swap_cursor(conn, chain.chain_id, expected, cursor, head, block_time)
        store.apply_swaps(conn, chain.chain_id, agg.daily, agg.wallets)
    st.swaps += agg.swaps
    st.unpriced += agg.unpriced
    return block_time
```
Both call sites: `swap_time = _commit_swaps(engine, chain, agg, swap_from, nb, head, st) or swap_time`. Before `st.init_lag = ...`:
```python
    if swap_from >= end and (swap_time is None or now - swap_time > ANCHOR_MAX_AGE):
        ts = hs.block_timestamp(swap_from - 1)
        if ts is not None:
            with engine.begin() as conn:
                store.advance_swap_cursor(conn, chain.chain_id, swap_from, swap_from, head, utc(ts))
```
- [ ] **Step 4:** full suite → `138 passed, 6 skipped`.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/indexer/core.py pipeline/tests/test_indexer.py
git commit -m "indexer: record the swap cursor's block time; anchor it at the head for quiet chains"
```

### Task 5: Missing block timestamps fail the page; the window starts with the swap phase

**Files:** Modify `pipeline/bait/indexer/core.py` (`aggregate_swaps` :102-110, `sync_hypersync` after the `swap_time` line, new `_start_swap_phase`). Test `tests/test_indexer.py` (add `timedelta` to the `datetime` import).
**Interfaces:** `aggregate_swaps` raises `RuntimeError("... without a block timestamp ...")` when a known pool's Swap log has no joined block. `_start_swap_phase` runs once per reset, when Initialize history first passes the swap cursor: one `block_timestamp(cursor)`, plus the existing binary search only when the cursor is older than today-14.

- [ ] **Step 1: Failing tests** (append to `tests/test_indexer.py`)
```python
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
```
- [ ] **Step 2:** `-k "without_block_timestamp or phase_begins"` → FAIL (page committed at 1700; `swap_block == 1000`).
- [ ] **Step 3: Implement.** `aggregate_swaps`: initialise `decoded, missing = [], 0`; replace `if ts is None: continue` with `if ts is None: missing += 1; continue` (two lines); after the loop:
```python
    if missing:  # the cursor would move past swaps we never counted
        raise RuntimeError(f"chain={chain.slug}: {missing} swap logs of hooked pools without a block timestamp; page not committed")
```
In `sync_hypersync`, right after `swap_time = as_utc(state.swap_block_time)`:
```python
    if swap_time is None and not state.backfilled and init_from > swap_from:
        swap_from, swap_time = _start_swap_phase(engine, chain, hs, swap_from, end, head, now)
```
New function after `swap_start_ts`:
```python
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
```
- [ ] **Step 4:** full suite → `140 passed, 6 skipped`. `test_crash_mid_backfill_resumes_from_last_committed_batch` must still pass (one extra request, `block_timestamp(1000)`, before its crash point).
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/indexer/core.py pipeline/tests/test_indexer.py
git commit -m "indexer: swap logs without a block time fail the page; the 14-day window starts when the swap phase does"
```

### Task 6: Nightly freshness gate on the swap cursor, shared with the 7-day wash rule

**Files:** Modify `pipeline/bait/steps/onchain.py:136-150`; `pipeline/bait/steps/score.py:78`, `:106-118`. Test `tests/test_onchain.py`, `tests/test_score.py`.
**Interfaces:** `onchain.ref_day(ctx)`; `onchain.ready_chains(conn, ctx, out=None) -> list[int]` (backfilled, `updated_at` within `ONCHAIN_MAX_STALE_HOURS`, `swap_block_time >=` 00:00 UTC after the reference day); `score._wash_7d(conn, day, pool_project, chains: set[int])`.

- [ ] **Step 1: Failing tests.** `tests/test_onchain.py`: add `swap_block_time=NOW - timedelta(hours=1)` to `_setup`'s `indexer_state` insert; append:
```python
def test_stuck_swap_cursor_keeps_keyless_even_when_updated_at_is_fresh(conn, ctx):
    _setup(conn)                                   # Initialize commits kept updated_at fresh...
    conn.execute(sa.update(db.indexer_state).values(swap_block_time=datetime(2026, 10, 5, 18, tzinfo=timezone.utc)))
    out = onchain.run(conn, ctx)                   # ...but swaps stopped at 10-05 18:00, inside REF (10-05)
    assert out["chains_stale"] == 1 and out["pools_usage"] == 0
    assert conn.execute(sa.select(db.pool_stats.c.vol_24h).where(db.pool_stats.c.pool_id == PA)).scalar() == 999.0
```
`tests/test_score.py`: append the helper and test, and call `_optimism_ready(conn)` right before `out = score.run(conn, ctx)` in `test_wash_7d_from_indexer_one_wallet_hundreds_of_millions`:
```python
def _optimism_ready(conn, swap_time=NOW):
    conn.execute(sa.insert(db.indexer_state).values(chain_id=10, initialize_block=1, swap_block=1, head_block=1, backfilled=True,
                                                    updated_at=NOW, swap_block_time=swap_time))

def test_wash_7d_ignores_a_chain_whose_swaps_are_not_caught_up(conn, ctx):
    wash = _onchain_project(conn, "Desk", "0xdp", wallets=1, vol_per_day=60_000_000)
    _optimism_ready(conn, swap_time=NOW - timedelta(days=2))
    out = score.run(conn, ctx)
    assert out["wash_7d"] == 0 and not daily(conn, wash).is_spam
```
- [ ] **Step 2:** `tests/test_onchain.py -k stuck_swap` → FAIL (`chains_stale == 0`, vol 500.0); `tests/test_score.py -k not_caught_up` → FAIL (`wash_7d == 1`).
- [ ] **Step 3: Implement.** `onchain.py`: import `time as dtime` from `datetime`; add above `_usage`:
```python
def ref_day(ctx: RunContext) -> date:
    """The day pool_stats reports: the last complete UTC day (today is still running when the nightly job fires)."""
    return ctx.day - timedelta(days=1) if ctx.day >= ctx.now.astimezone(timezone.utc).date() else ctx.day

def ready_chains(conn, ctx: RunContext, out: dict | None = None) -> list[int]:
    """Chains whose on-chain usage may replace keyless numbers: backfilled, written within ONCHAIN_MAX_STALE_HOURS, and the
    swap cursor's block time past the end of the reference day. updated_at alone is not enough: Initialize pages keep
    bumping it while swap queries fail every loop."""
    stale_h = _env_int("ONCHAIN_MAX_STALE_HOURS", 6)
    ref_end = datetime.combine(ref_day(ctx) + timedelta(days=1), dtime.min, tzinfo=timezone.utc)
    ready = []
    for st in conn.execute(sa.select(indexer_state).order_by(indexer_state.c.chain_id)):
        if (ctx.chains and st.chain_id not in ctx.chains) or not st.backfilled:
            continue
        fresh = st.updated_at is not None and as_utc(st.updated_at) >= ctx.now - timedelta(hours=stale_h)
        swaps_done = st.swap_block_time is not None and as_utc(st.swap_block_time) >= ref_end
        if fresh and swaps_done:
            ready.append(st.chain_id)
        elif out is not None:
            out["chains_stale"] += 1
            log.warning("onchain chain_id=%s not ready (updated_at=%s swap_block_time=%s, need >= %s); keyless usage kept",
                        st.chain_id, st.updated_at, st.swap_block_time, ref_end)
    return ready
```
In `_usage` replace lines 137-147 with `ready = ready_chains(conn, ctx, out)` and line 150 with `ref = ref_day(ctx)`.
`score.py`: `from .onchain import ready_chains`; line 78 → `wash7 = _wash_7d(conn, day, pool_project, set(ready_chains(conn, ctx)))`. `_wash_7d` gains `chains: set[int]`; first line `if not chains: return set()`; add `pool_daily_onchain.c.chain_id.in_(chains)` and `pool_wallet_day.c.chain_id.in_(chains)` to the two queries' `.where(...)`.
- [ ] **Step 4:** full suite → `142 passed, 6 skipped`.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/steps/onchain.py pipeline/bait/steps/score.py pipeline/tests/test_onchain.py pipeline/tests/test_score.py
git commit -m "onchain, score: a chain is ready only when its swap cursor has passed the end of the reference day"
```

### Task 7: Count unpriced swaps; a DefiLlama failure leaves the page for the next loop

**Files:** Modify `pipeline/bait/indexer/core.py:64-71` (`SwapAgg.add`); `pipeline/bait/indexer/store.py:63-80`; `pipeline/bait/indexer/prices.py:42-77`. Test `tests/test_indexer.py`.
**Interfaces:** `SwapAgg.daily` values `[swaps, usd, unpriced]`; `apply_swaps` adds the third into `pool_daily_onchain.unpriced_swaps`. `PriceBook.get` raises `prices.PricesUnavailable` when a batch failed transiently (network error, 429, 5xx, bad JSON); any other 4xx is "no price" so one bad id cannot wedge a chain.

- [ ] **Step 1: Failing tests.** In `test_apply_swaps_increments_and_recounts_unique_wallets` pass `[2, 10.0, 0]` and `[3, 5.5, 1]` and assert `(r.swaps, r.wallets, r.volume_usd, r.unpriced_swaps) == (5, 2, 15.5, 1)`. Append:
```python
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
```
- [ ] **Step 2:** `-k "apply_swaps or outage or unpriced_swaps_are"` → FAIL (`not enough values to unpack`; no raise on 503).
- [ ] **Step 3: Implement.** `SwapAgg.add`: `d = self.daily.setdefault((pool_id, day), [0, 0.0, 0])`, then `d[0] += 1`, `d[1] += usd or 0.0`, `d[2] += usd is None`. `apply_swaps`: rows unpack `for (p, d), (s, v, u) in daily.items()` with `"unpriced_swaps": u`, and the conflict set gains `"unpriced_swaps": pool_daily_onchain.c.unpriced_swaps + stmt.excluded.unpriced_swaps`; docstring `daily: (pool_id, day) -> [swaps, volume_usd, unpriced_swaps]`. `prices.py`:
```python
class PricesUnavailable(RuntimeError):
    """DefiLlama did not answer for a batch. The caller must not commit the page: a swap stored at $0 is never revalued."""
```
In `get`, before the batch loop `failed = 0`; replace `if got is None: continue` with `if got is None: failed += len(chunk); continue`; after the loop `if failed: raise PricesUnavailable(f"{chain.slug}: DefiLlama unavailable for {failed} coins")`. `_fetch`, replace the `try` block:
```python
        try:
            r = self.client.get(LLAMA + ",".join(ids), headers={"User-Agent": "bait-indexer/0.1"}, timeout=30)
        except httpx.HTTPError as e:
            log.warning("defillama error %r", e)
            return None
        if r.status_code == 429 or r.status_code >= 500:
            log.warning("defillama HTTP %s for %d coins; page retried next loop", r.status_code, len(ids))
            return None
        if r.status_code != 200:
            log.warning("defillama HTTP %s for %d coins; treated as unpriced", r.status_code, len(ids))
            return {}
        try:
            coins = (r.json() or {}).get("coins") or {}
        except ValueError as e:
            log.warning("defillama bad JSON %r", e)
            return None
```
- [ ] **Step 4:** full suite → `144 passed, 6 skipped`.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/indexer/core.py pipeline/bait/indexer/store.py pipeline/bait/indexer/prices.py pipeline/tests/test_indexer.py
git commit -m "indexer: count unpriced swaps per pool-day; a DefiLlama outage leaves the page uncommitted for a retry"
```

### Task 8: Static price fallback for wrapped native and major stablecoins

**Files:** Modify `pipeline/bait/indexer/prices.py` (maps after `MISMATCH`; `PriceBook.get`). Test `tests/test_indexer.py`.
**Interfaces:** `prices.WRAPPED_NATIVE: dict[int, str]`, `prices.STABLES: dict[int, dict[str, int]]` (address → decimals). A reliable DefiLlama quote wins; otherwise wrapped native = the chain's native coin price (18 decimals), stables = $1.

- [ ] **Step 1: Failing test** (append to `tests/test_indexer.py`)
```python
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
```
- [ ] **Step 2:** `-k fallback_prices` → FAIL (`ImportError: STABLES`).
- [ ] **Step 3: Implement.** `prices.py`, after `MISMATCH`:
```python
# Fallback when DefiLlama has no reliable quote (new chain keys, low confidence): the wrapped native coin (priced as the
# chain's native coin id) and major stablecoins ($1). OP-stack chains share the WETH predeploy. Unichain, World, Soneium,
# Ink, Celo, Robinhood and MegaETH entries were checked on each chain's Blockscout token page 2026-10-09; the rest are the
# canonical deployments. Monad, Zora and Robinhood/MegaETH stables stay out until checked the same way.
OP_WETH = "0x4200000000000000000000000000000000000006"
WRAPPED_NATIVE: dict[int, str] = {
    1: "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", 10: OP_WETH, 56: "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    130: OP_WETH, 137: "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270", 480: OP_WETH, 1868: OP_WETH, 4326: OP_WETH,
    4663: "0x0bd7d308f8e1639fab988df18a8011f41eacad73", 8453: OP_WETH, 42161: "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
    42220: "0x471ece3750da237f93b8e339c536989b8978a438", 43114: "0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7",
    57073: OP_WETH, 7777777: OP_WETH,
}
STABLES: dict[int, dict[str, int]] = {
    1: {"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6, "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,
        "0x6b175474e89094c44da98b954eedeac495271d0f": 18},
    10: {"0x0b2c639c533813f4aa9d7837caf62653d097ff85": 6, "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58": 6},
    56: {"0x55d398326f99059ff775485246999027b3197955": 18, "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": 18},
    130: {"0x078d782b760474a361dda0af3839290b0ef57ad6": 6},
    137: {"0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": 6, "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": 6,
          "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": 6},
    480: {"0x79a02482a880bce3f13e09da970dc34db4cd24d1": 6},
    1868: {"0xba9986d2381edf1da03b0b9c1f8b00dc4aacc369": 6},
    8453: {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6},
    42161: {"0xaf88d065e77c8cc2239327c5edb3a432268e5831": 6, "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": 6},
    42220: {"0xceba9300f2b948710d2653dd7b07f33a8b32118c": 6},
    43114: {"0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e": 6, "0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7": 6},
    57073: {"0x0200c29006150606b650577bbe7b6248f58470c1": 6},
}
```
`PriceBook.get`: replace the `ids = ...` line with
```python
        want = {a.lower() for a in addrs}
        wrapped = WRAPPED_NATIVE.get(chain.chain_id)
        ids = {a: self.coin_id(chain, a) for a in want | ({ZERO} if wrapped in want else set())}
```
keep the batch loop and `PricesUnavailable` check, and replace the final `out` loop and `return` with
```python
        found = {a: hit[1] for a, cid in ids.items() if (hit := self._cache.get(cid)) and hit[1] is not None}
        stables = STABLES.get(chain.chain_id, {})
        out = {}
        for a in want:
            if a in found:
                out[a] = found[a]
            elif a == wrapped and ZERO in found:
                out[a] = (found[ZERO][0], 18)
            elif a in stables:
                out[a] = (1.0, stables[a])
        return out
```
- [ ] **Step 4:** full suite → `146 passed, 6 skipped` (`test_pricebook_batches_maps_native_and_caches` unchanged: DefiLlama quotes win).
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/indexer/prices.py pipeline/tests/test_indexer.py
git commit -m "prices: wrapped native at the native coin price and major stablecoins at \$1 when DefiLlama has no quote"
```

### Task 9: Volume the chain cannot measure never overrides keyless numbers

**Files:** Modify `pipeline/bait/rules.py` (after `flags_from_address`); `pipeline/bait/steps/onchain.py:1-16` (docstring), `:41-43` (`out`), `:168-214` (`_usage_chunk`). Test `tests/test_onchain.py`.
**Interfaces:** `rules.custom_accounting(address) -> bool` (address bit 3, `beforeSwapReturnsDelta`). A pool's series volume is unknown when its hook is custom-accounting or `unpriced_swaps * 2 > swaps` over the window; its `pool_stats` volume when the same holds on the reference day. Unknown: the keyless series is kept (or written with `volume_usd_cum = NULL` when none exists); `pool_stats` updates only `traders_24h` and `vol_source` (`'hook'` | `'unpriced'`); known rows get `vol_source = 'onchain'`. Counter `vol_unknown`.

- [ ] **Step 1: Failing tests.** `tests/test_onchain.py`: `_daily` gains `unpriced=0`, passed as `unpriced_swaps=unpriced`; import `custom_accounting` from `bait.rules`. Append:
```python
CUSTOM, PF = "0x" + "f" * 36 + "0088", "0x" + "f" * 64          # beforeSwap + beforeSwapReturnsDelta (bit 3)

def test_custom_accounting_hook_keeps_keyless_volume_and_takes_onchain_wallets(conn, ctx):
    assert custom_accounting(CUSTOM) and not custom_accounting(HOOK2) and not custom_accounting("0x" + "1" * 36 + "0044")
    _setup(conn)
    insert_hook(conn, CUSTOM, 8453, name="Curve Hook")
    _v4(conn, 8453, PF, CUSTOM, 400)
    for i in range(20, -1, -1):
        _daily(conn, 8453, PF, DAY - timedelta(days=i), 5, 3, 0.0)         # the Swap event says 0/0: the hook took the trade
    for i in range(15, -1, -1):
        conn.execute(sa.insert(db.pool_snapshots).values(pool_id=PF, chain_id=8453, day=DAY - timedelta(days=i), tvl_usd=1.0,
                                                         volume_usd_cum=5000.0 - i, tx_count_cum=0))
    conn.execute(sa.insert(db.pool_stats).values(pool_id=PF, chain_id=8453, day=DAY, vol_24h=777.0, traders_24h=1))
    onchain.run(conn, ctx)
    st = {r.pool_id: r for r in conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.day == DAY))}
    assert (st[PF].vol_24h, st[PF].traders_24h, st[PF].vol_source) == (777.0, 3, "hook")
    assert (st[PA].vol_24h, st[PA].vol_source) == (500.0, "onchain")
    assert _snaps(conn, PF)[DAY][0] == 5000.0                               # keyless series untouched

def test_mostly_unpriced_pool_day_does_not_override_and_series_volume_is_null(conn, ctx):
    _setup(conn)
    conn.execute(sa.update(db.pool_daily_onchain).where(db.pool_daily_onchain.c.pool_id == PC).values(unpriced_swaps=3))
    onchain.run(conn, ctx)
    st = conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.pool_id == PC)).one()
    assert (st.vol_24h, st.traders_24h, st.vol_source) == (None, 2, "unpriced")
    assert _snaps(conn, PC)[DAY][:2] == (None, 4)                           # swap count kept, volume unknown
```
- [ ] **Step 2:** `tests/test_onchain.py -k "custom_accounting or mostly_unpriced"` → FAIL (`ImportError: custom_accounting`).
- [ ] **Step 3: Implement.** `rules.py`:
```python
# v4 PoolManager emits Swap with the pool's own delta, before hook deltas. A beforeSwapReturnsDelta hook can settle part or
# all of the trade itself (custom curve, NoOp), so the event understates it, often to 0/0. afterSwapReturnsDelta only
# adjusts the unspecified side after the pool swapped: the event's pool leg is real volume, so bit 2 is not included.
CUSTOM_ACCOUNTING_BITS = 1 << 3

def custom_accounting(address: str) -> bool:
    return bool(int(address, 16) & CUSTOM_ACCOUNTING_BITS)
```
`onchain.py`: import `custom_accounting`; add `"vol_unknown": 0` to `out`; add
```python
def _volume_unknown(hook: str | None, days) -> bool:
    """On-chain USD volume cannot stand in for keyless numbers: a custom-accounting hook, or most swaps had no priced side.
    days: iterable of (swaps, wallets, volume, unpriced)."""
    if hook and custom_accounting(hook):
        return True
    days = list(days)
    swaps, unpriced = sum(d[0] for d in days), sum(d[3] for d in days)
    return swaps > 0 and unpriced * 2 > swaps
```
In `_usage_chunk`: (1) the daily read stores `(r.swaps or 0, r.wallets or 0, r.volume_usd or 0.0, r.unpriced_swaps or 0)`; (2) after it `hook_of = dict(conn.execute(sa.select(pools.c.pool_id, pools.c.hook_address).where(pools.c.chain_id == cid, pools.c.pool_id.in_(chunk))).all())`; (3) replace `replace = ...` with
```python
    unknown = {pid: _volume_unknown(hook_of.get(pid), daily.get(pid, {}).values()) for pid in chunk}
    # unknown volume never replaces a keyless series; without one, the series carries swap counts and NULL volume
    replace = [pid for pid in chunk if (full or pid not in has_window) and not (unknown[pid] and pid in has_window)]
```
(4) the series loop unpacks `n, _, v, _ = daily.get(pid, {}).get(d, (0, 0, 0.0, 0))` and writes `"volume_usd_cum": None if unknown[pid] else cum_v` (and `None if unknown[pid] else 0.0` in the seed row); (5) replace everything from `stat_rows = []` to the end with
```python
    stat_rows, held = [], []
    for pid in chunk:
        n, w, v, u = daily.get(pid, {}).get(ref, (0, 0, 0.0, 0))
        hook = hook_of.get(pid)
        if _volume_unknown(hook, [(n, w, v, u)]):
            held.append({"pool_id": pid, "chain_id": cid, "day": ctx.day, "vol_24h": None, "traders_24h": w,
                         "vol_source": "hook" if hook and custom_accounting(hook) else "unpriced"})
            if extras_stats is not None:
                extras_stats[(pid, cid)] = ((extras_stats.get((pid, cid)) or (0.0, 0))[0], w)
        else:
            stat_rows.append({"pool_id": pid, "chain_id": cid, "day": ctx.day, "vol_24h": v, "traders_24h": w, "vol_source": "onchain"})
            if extras_stats is not None:
                extras_stats[(pid, cid)] = (v, w)
    keys = ["pool_id", "chain_id", "day"]
    for i in range(0, len(stat_rows), CHUNK):
        upsert(conn, pool_stats, stat_rows[i:i + CHUNK], keys=keys, update=["vol_24h", "traders_24h", "vol_source"])
    for i in range(0, len(held), CHUNK):  # keyless vol_24h survives; a new row gets NULL (unknown), never $0
        upsert(conn, pool_stats, held[i:i + CHUNK], keys=keys, update=["traders_24h", "vol_source"])
    out["stats"] += len(stat_rows) + len(held)
    out["vol_unknown"] += len(held)
```
Docstring item 3, append: "Volume the chain cannot measure (custom-accounting hooks, pool-days mostly unpriced) never overrides keyless numbers; `pool_stats.vol_source` says which source won."
- [ ] **Step 4:** full suite → `148 passed, 6 skipped`.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/rules.py pipeline/bait/steps/onchain.py pipeline/tests/test_onchain.py
git commit -m "onchain: custom-accounting hooks and mostly-unpriced pool-days keep keyless volume; pool_stats.vol_source marks it

v4 emits Swap with the pool's delta before hook deltas, so a beforeSwapReturnsDelta hook
that takes the trade shows 0/0 and overwrote GeckoTerminal's number with \$0."
```

### Task 10: Incremental pools copy and a nightly disk line

**Files:** Modify `pipeline/bait/steps/onchain.py:25-26` (imports), `:41-55` (`run`), `:111-133` (`_upsert_pools`, new `_copy_pools`, `_disk_check`). Test `tests/test_onchain.py`, `tests/test_postgres.py`.
**Interfaces:** Per chain only `v4_pools` rows above `onchain_watermarks.pools_block` are read (index `v4_pools_chain_block_idx`), then every pool of a known hook that has no `pools` row yet. The `IS DISTINCT FROM` guard skips unchanged rows. Counters `pools` (rows sent), `pools_written` (inserted or changed), `db_gb`, `disk_warn` (60% of `DISK_LIMIT_GB`, default 50; Postgres only).

- [ ] **Step 1: Failing tests.** Append to `tests/test_onchain.py`:
```python
def test_pools_copy_is_incremental_and_catches_up_hooks_known_later(conn, ctx, monkeypatch):
    monkeypatch.setenv("ONCHAIN_MAX_NEW_HOOKS", "1")
    _setup(conn)
    assert onchain.run(conn, ctx)["pools"] == 2                     # PA, PC; HOOK3 deferred, PE now below the watermark
    out = onchain.run(conn, ctx)                                    # HOOK3 known now: the catch-up copies its pool
    assert out["pools"] == 1 and (PE, 143) in {(r.pool_id, r.chain_id) for r in conn.execute(sa.select(db.pools))}
    assert onchain.run(conn, ctx)["pools"] == 0                     # nothing new: nothing read
    _v4(conn, 8453, "0x" + "9" * 64, HOOK, 500)
    assert onchain.run(conn, ctx)["pools"] == 1
```
Append to `tests/test_postgres.py`:
```python
def test_onchain_pool_copy_skips_unchanged_rows_and_reports_db_size(pg_engine):
    from datetime import datetime, timezone
    from bait.steps import onchain
    hook = "0x" + "a" * 36 + "0080"
    with pg_engine.begin() as c:
        c.execute(sa.insert(db.hooks).values(address=hook, chain_id=8453, name="H", first_seen_at=NOW, flags={}, properties={}))
        c.execute(sa.insert(db.v4_pools).values(chain_id=8453, pool_id="0x" + "a" * 64, hook_address=hook, currency0="0x0",
                                                currency1="0x1", fee=3000, tick_spacing=60, block_number=7,
                                                block_time=datetime(2026, 8, 1, tzinfo=timezone.utc)))
    with pg_engine.begin() as c:
        out = onchain.run(c, _ctx())
    assert out["pools_written"] == 1 and out["db_gb"] is not None
    with pg_engine.begin() as c:
        c.execute(sa.delete(db.onchain_watermarks))                 # force a re-read of the same row
        out = onchain.run(c, _ctx())
    assert out["pools"] == 1 and out["pools_written"] == 0         # read again, not rewritten
```
- [ ] **Step 2:** SQLite `-k incremental` → FAIL (the second run reads all three rows: `pools == 3`); Postgres `-k skips_unchanged` → FAIL (`KeyError: 'pools_written'`).
- [ ] **Step 3: Implement** in `onchain.py`. Imports: add `onchain_watermarks` to the `..db` import and `from ..indexer.chains import CHAINS`. `run()`: add `"pools_written": 0, "db_gb": None, "disk_warn": 0` to `out`, call `_disk_check(conn, out)` right after building `out`, add `pools_written=%d` to the summary log. Replace `_upsert_pools`:
```python
def _upsert_pools(conn, ctx: RunContext, known: set[tuple[str, int]], out: dict) -> None:
    """v4_pools -> pools for known hooks, incrementally: 1) per chain, rows above the block watermark (Initialize history
    only grows upward per chain); 2) every pool of a known hook with no pools row yet (hooks that became known tonight,
    incl. ones the cap deferred while their pools passed the watermark). Unchanged rows are never rewritten."""
    marks = {r.chain_id: r.pools_block for r in conn.execute(sa.select(onchain_watermarks))}
    chain_ids = {c.chain_id for c in CHAINS} | set(marks) | {r[0] for r in conn.execute(sa.select(indexer_state.c.chain_id))}
    for cid in sorted(chain_ids):
        if ctx.chains and cid not in ctx.chains:
            continue
        last, top = (-1 if marks.get(cid) is None else marks[cid], ""), None
        while True:
            batch = conn.execute(sa.select(v4_pools).where(
                v4_pools.c.chain_id == cid,
                sa.tuple_(v4_pools.c.block_number, v4_pools.c.pool_id) > sa.tuple_(sa.literal(last[0]), sa.literal(last[1])))
                .order_by(v4_pools.c.block_number, v4_pools.c.pool_id).limit(2000)).all()
            if not batch:
                break
            last = top = (batch[-1].block_number, batch[-1].pool_id)
            _copy_pools(conn, [r for r in batch if (r.hook_address, r.chain_id) in known], out)
        if top is not None:
            upsert(conn, onchain_watermarks, [{"chain_id": cid, "pools_block": top[0], "updated_at": ctx.now}], keys=["chain_id"])
    orphans = conn.execute(sa.select(hooks.c.chain_id, hooks.c.address).where(
        ~sa.exists().where(pools.c.chain_id == hooks.c.chain_id, pools.c.hook_address == hooks.c.address),
        sa.exists().where(v4_pools.c.chain_id == hooks.c.chain_id, v4_pools.c.hook_address == hooks.c.address))).all()
    for h in orphans:
        if ctx.chains and h.chain_id not in ctx.chains:
            continue
        rows = conn.execute(sa.select(v4_pools).where(v4_pools.c.chain_id == h.chain_id, v4_pools.c.hook_address == h.address)).all()
        for i in range(0, len(rows), 2000):
            _copy_pools(conn, rows[i:i + 2000], out)

def _copy_pools(conn, batch, out: dict) -> None:
    ins = postgresql.insert if conn.dialect.name == "postgresql" else sqlite.insert
    rows = [{"pool_id": r.pool_id, "chain_id": r.chain_id, "hook_address": r.hook_address,
             "token0": {"id": r.currency0, "symbol": None}, "token1": {"id": r.currency1, "symbol": None},
             "fee": r.fee, "tick_spacing": r.tick_spacing, "created_at": r.block_time} for r in batch]
    for i in range(0, len(rows), CHUNK):
        stmt = ins(pools).values(rows[i:i + CHUNK])
        ex = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["pool_id", "chain_id"],
            set_={"hook_address": ex.hook_address, "fee": ex.fee, "tick_spacing": ex.tick_spacing,
                  "created_at": sa.func.coalesce(ex.created_at, pools.c.created_at)},
            where=sa.or_(pools.c.hook_address.is_distinct_from(ex.hook_address), pools.c.fee.is_distinct_from(ex.fee),
                         pools.c.tick_spacing.is_distinct_from(ex.tick_spacing),
                         sa.and_(ex.created_at.is_not(None), pools.c.created_at.is_distinct_from(ex.created_at))))
        out["pools_written"] += conn.execute(stmt).rowcount or 0
    out["pools"] += len(rows)

def _disk_check(conn, out: dict) -> None:
    """Nightly size line. pg_database_size covers this database only (no WAL): an estimate of the volume's use."""
    if conn.dialect.name != "postgresql":
        return
    size = conn.execute(sa.text("SELECT pg_database_size(current_database())")).scalar_one()
    try:
        limit = float(os.environ.get("DISK_LIMIT_GB", "") or 50)
    except ValueError:
        limit = 50.0
    gb = size / 1024 ** 3
    pct = 100 * gb / limit
    out["db_gb"] = round(gb, 2)
    if pct >= 60:
        out["disk_warn"] = 1
        log.warning("onchain disk: database %.1f GB is %.0f%% of the %.0f GB volume (DISK_LIMIT_GB)", gb, pct, limit)
    else:
        log.info("onchain disk: database %.1f GB, %.0f%% of %.0f GB", gb, pct, limit)
```
Docstring item 2 → "upserted incrementally (per-chain `onchain_watermarks` on `block_number`, plus every pool of a newly known hook); a row is rewritten only when a value changed."
- [ ] **Step 4:** full suite → `149 passed, 7 skipped`; Postgres → `7 passed`.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/steps/onchain.py pipeline/tests/test_onchain.py pipeline/tests/test_postgres.py
git commit -m "onchain: incremental pools copy by block watermark, no rewrite of unchanged rows, nightly disk line

The step upserted every known hook's pool every night: a full rewrite of pools plus its
dead tuples, growing with the Base backfill."
```

### Task 11: Docs

**Files:** `pipeline/README.md:70`, `:84-91`, `:103-108`, `:193`; `DESIGN.md:233-234`, `:253`; `railway/README.md:41-76`; `pipeline/.env.example:31`; `.env.example:59`.

- [ ] **Step 1: Edit.**
  - `pipeline/README.md:70` → "`v4_pools` (hooked pools only), `pool_daily_onchain` (never pruned: kept for all-time and monthly totals; `unpriced_swaps` counts swaps with no priced side), `pool_wallet_day` (30 days kept), `indexer_state` (per-chain cursors; `swap_block_time` = block time the swap cursor reached)."
  - USD volume bullet, replace its last clause: "swaps with no priced side count as swaps and wallets, add $0 and are counted in `unpriced_swaps`; wrapped native and major stablecoins fall back to the native coin price / $1 (`indexer/prices.py`); when the DefiLlama call fails the page is not committed and is retried next loop."
  - Safety bullet, append: "Each chain syncs under a Postgres advisory lock (`bait-indexer:<chain_id>`) and the swap cursor moves by compare-and-set, so a second process (deploy overlap) skips the chain or rolls its page back. SIGTERM is honoured between pages. A swap log without a block timestamp fails the page."
  - Step `onchain` item 2 → Task 10's docstring sentence. Item 3: after "`ONCHAIN_MAX_STALE_HOURS` (6)" insert "and `swap_block_time` has passed the end of the reference day (the 7-day wash rule uses the same chains)"; append "Custom-accounting hooks (address bit 3) and pool-days with more than half their swaps unpriced keep keyless volume; `pool_stats.vol_source` is `onchain`, `hook` or `unpriced` (NULL = keyless)."
  - Env paragraph: after "`ONCHAIN_MAX_STALE_HOURS` (6)" add ", `DISK_LIMIT_GB` (50; nightly `pg_database_size` line, warning at 60%)".
  - `DESIGN.md:233-234` → "**Volume is approximate by design:** valued at current DefiLlama prices, not prices at swap time; unpriced swaps are counted (`unpriced_swaps`), and a mostly-unpriced pool-day or a custom-accounting hook keeps the keyless number. TVL is not computed (GeckoTerminal/subgraph reserve is kept)."
  - `DESIGN.md:253` → "- Swap history starts 14 days before each chain's swap phase and `pool_daily_onchain` is never pruned; add a per-pool per-month aggregate for all-time / monthly views."
  - `railway/README.md`, end of the "Adding the `indexer` service" section, one sentence: "Deploying the package-5 indexer over an older one overlaps old code (no lock, no compare-and-set) with new once: stop the indexer service first or accept one possible double-counted page, and expect the first start to block `v4_pools` inserts briefly while it builds `v4_pools_chain_block_idx`."
  - `pipeline/.env.example` after `ONCHAIN_MAX_STALE_HOURS=6`: `DISK_LIMIT_GB=50`. `.env.example` after `ONCHAIN_MAX_STALE_HOURS=`: a comment line `# Postgres volume size in GB for the nightly disk line (default 50); warns at 60%.` then `DISK_LIMIT_GB=`.
- [ ] **Step 2: Final run.** `cd pipeline && .venv/bin/python -m pytest -q && eval "$(scripts/pg-test-cluster.sh start)" && .venv/bin/python -m pytest -q tests/test_postgres.py; scripts/pg-test-cluster.sh stop` → `149 passed, 7 skipped`; `7 passed`.
- [ ] **Step 3: Commit**
```bash
git add pipeline/README.md DESIGN.md railway/README.md pipeline/.env.example .env.example
git commit -m "docs: indexer lock and CAS, swap cursor gate, unpriced and custom-accounting volume, pool_daily_onchain kept, DISK_LIMIT_GB"
```

## Final report (the implementer returns this)

For each task: commit SHA, test counts before and after, anything that deviated from the plan and why. Then `git log --oneline main..HEAD` and `git diff --stat main..HEAD`. Do not push.

Rollout note for the orchestrator: the deploy that ships this branch is itself an overlap of old code (no lock, no CAS) with new code; stop the `indexer` service before it, or accept one last possible double count on the overlapping page. On first start the indexer adds the columns and builds `v4_pools_chain_block_idx`, which blocks `v4_pools` inserts while it builds.
