# Package 8: Windows, counts and wash Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every number Bait shows covers the window its label says (closed UTC days), swap and wallet counts are never inflated or built from one day, a pool that leaves the OHLCV cap never loses its history, and wash-like projects are a separate, explained flag that stays listed in All instead of 404ing.

**Architecture:** One additive migration (`0012`) mirrored in `db.py`. `pool_snapshots` gains per-day columns (`volume_usd_day`, `tx_count_day`, `estimated`) that every source writes to the UTC day they belong to: GeckoTerminal candles, the rolling 24h number for pools past the OHLCV cap (yesterday only, estimated), subgraph deltas, indexer days. `score` sums those columns over windows that end on `window_end = day - 1` and stores `window_end`, `vol_24h`, `swaps_24h` / `swaps_7d` (NULL unless every day is known), and `wallets_24h` + `wallets_exact` (distinct across pools on ready chains, else an upper bound). Wash becomes `project_daily.is_wash` + `wash_reason` from four documented rules; `is_spam` keeps only true spam. The web reads the new columns, labels upper-bound wallets with "≤", shows wash projects in All and on their page with a red badge, and keeps them out of Trending, New, movers and Leaders. The legacy cumulative columns (`volume_usd_cum`, `tx_count_cum`) stay written but no window reads them (see Task 6 for the bounded reason).

**Tech Stack:** Python 3.12, SQLAlchemy 2 Core, pytest; Next.js 16, Drizzle, postgres.js, `tsx --test`. Postgres 15+ (`pg_ctl`, `initdb`, `psql` on PATH) for `tests/test_postgres.py` and the web smoke checks.

**Spec:** Audit of 2026-10-09 (Astra H5/M4/M5 and Sov), findings 1-7: window semantics; "swaps 7d" is a 24-hour count; wallet counts double count; the OHLCV cap zeroes history; wash handling (owner decision); `[keyless]` lines on stderr; honest labels, pace and week delta.

## The window convention (read this first)

The nightly run on UTC day `D` (06:00) reports days that are over:

| label | window | stored as |
|---|---|---|
| 24h | the UTC day `D-1` | `project_daily.vol_24h`, `swaps_24h`, `wallets_24h` |
| 7d | `D-7` .. `D-1` | `project_daily.vol_7d`, `swaps_7d` |
| prev 7d | `D-14` .. `D-8` | `project_daily.vol_7d_prev` |

`project_daily.window_end = D - 1` on every row. `pool_snapshots` rows are per UTC day; the row for `D` itself is the running day and no reader touches it. GeckoTerminal's `volume_usd.h24` is a rolling window ending at run time: it is stored as `pool_stats.vol_24h_rolling`, used only by the pace line ("Last 24h vs daily avg"), and written as `D-1`'s volume only for pools past the OHLCV cap (marked `estimated`). `pool_stats.window_end` records the end of the 24 h each row covers (run time for keyless rows, `D 00:00` for on-chain rows).

## Amendment before implementation (coordinator, 2026-10-09)

`rules.corroborated` also accepts "no liquidity": 7-day volume >= $1M with a KNOWN TVL <= $1,000 (unknown TVL still never
corroborates). Without it the Optimism 0x69af case ($436M through 8 swaps at $0 TVL) escapes the 7-day rule. It is also
a fifth reason, `no_liquidity` (`WASH_REASONS = ("24h", "7d", "vol_tvl", "no_liquidity", "symmetric")`): a pool with
>= $1M in 7 days, a known TVL <= $1,000 and fewer than 50 wallets (the same wallet test as `vol_tvl`, whose ratio is
undefined at $0). Test: the 0x69af shape (1 wallet, 8 swaps, $436M, TVL 0 known) gets `7d,no_liquidity`. Task 9 is
accepted. Package 6 is not on main yet: Tasks 11-13 are applied to the files as they are on this branch.

## Global Constraints

- Work only in this worktree, branch `fix/p8-windows-counts`. Task 0 merges `origin/main`; record the suite counts after it as **B passed / S skipped** (SQLite) and **P passed** (Postgres). On 572d449 alone they are 162 / 10 / 10. Every task states its expected counts relative to those.
- Tests: `cd pipeline && .venv/bin/python -m pytest -q`. Postgres: `cd pipeline && PG_TEST_DIR=/tmp/bait-pg-p8 PG_TEST_PORT=5560 eval "$(scripts/pg-test-cluster.sh start)" && .venv/bin/python -m pytest -q tests/test_postgres.py; scripts/pg-test-cluster.sh stop`. Web: `cd web && npm test && npx tsc --noEmit && npm run build`.
- No network and no paid API calls in tests (GeckoTerminal, RPC, Claude are monkeypatched fakes as in `tests/test_snapshot_keyless.py`).
- Do not touch `pipeline/bait/indexer/`, `fixtures/routable_cases.json`, `fixtures/relevance_cases.json`. Consume package 5's `onchain.ready_chains` and `pool_stats.vol_source`; do not change their semantics.
- Schema changes go in `db/migrations/0012_windows.sql` AND `pipeline/bait/db.py` (`ADDITIVE_COLUMNS` and the tables).
- Web changes are limited to `web/src/db/queries.ts`, `web/src/lib/feed.ts`, `web/src/lib/present.ts`, `web/src/lib/types.ts`, `web/src/components/Detail.tsx`, `web/src/components/ProjectList.tsx`, `web/src/app/leaders/page.tsx`, plus two files the change cannot avoid: `web/src/db/schema.ts` (the Drizzle mirror of the new columns) and `web/src/components/TradePanel.tsx` (the pace line lives there), and fixtures/tests (`web/src/fixtures/sample.ts`, `web/src/lib/ux.test.ts`).
- Line numbers below are at 572d449; when Task 0 moved them, anchor on the quoted code.
- Commit after every task. Imperative subject, short body when the why is not obvious. Every `git commit -m` below is shorthand: end every commit message with these two trailer lines, exactly:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01A2FHLwk9NeThmyZ1JbHe3o`
- No hostnames, absolute paths, or secrets in code, docs, or commits. Do not push.

## Review Focus

1. The run on `D` reads only closed days: "24h" is `D-1`, "7d" is `D-7..D-1`, the running-day row is never read, and `python -m bait run --steps score --day <d>` writes identical rows however often and however late it runs (no in-run hand-offs). (Tasks 6, 10)
2. A keyless pool that drops out of the OHLCV cap: only `D-1` is written, marked `estimated`; no earlier day changes; a candle row already at `D-1` wins; a same-day rerun is a no-op. (Task 6)
3. `swaps_7d` is never one day presented as seven: NULL unless every day of the window is known (days before the pool existed count as 0, a pool with no volume adds 0); the blurb receives the NULL and the window text. (Tasks 4, 6, 10)
4. Wallets: distinct across a project's pools on ready chains (`pool_wallet_day`), distinct per pool where the activity fetch saw every trade, otherwise `buyers + sellers` labelled "≤" on the web; every wash rule compares a lower bound (`max(buyers, sellers)`), never `buyers + sellers`. (Tasks 3, 7, 11, 13)
5. Wash vs spam on the live cases: OUROBOROS/CRCL (866x volume/TVL, 13 + 13 traders, symmetric bots) is flagged with its reasons; the Base launchpad family 0x0f48 (345x, ~30K wallets) and a private RFQ hook (≤ 10 wallets, real TVL) are not; flagged projects stay in All with a badge and a page, and never reach Trending, New, movers, Leaders, digest totals or a Claude blurb. (Tasks 3, 8, 9, 10, 12, 13)

### Task 0: Sync with main and record the baseline

- [ ] **Step 1:** `git fetch origin && git merge origin/main`. Resolve conflicts by keeping both sides (expected only in `pipeline/bait/db.py` `ADDITIVE_COLUMNS`, `pipeline/bait/rules.py`, `pipeline/bait/keyless.py` imports, `web/src/db/queries.ts` imports if package 7 is on main).
- [ ] **Step 2:** Run the three suites; write the counts as B / S / P in your task notes. Check whether package 6 is on main: `git merge-base --is-ancestor origin/fix/p6-ux HEAD && echo p6-in`. Tasks 1-10 do not need it; Task 11 does.
- [ ] **Step 3:** No commit unless the merge produced one (then `git commit --no-edit` with the trailers appended).

### Task 1: Schema

**Files:** Create `db/migrations/0012_windows.sql`. Modify `pipeline/bait/db.py:85-94` (`pool_snapshots`), `:97-109` (`pool_stats`), `:137-150` (`project_daily`), `:321-332` (`ADDITIVE_COLUMNS`). Test `pipeline/tests/test_score.py`.
**Interfaces:** Produces the 13 columns listed in Step 3. `estimated` NULL means "written before 0012" and is treated as replaceable; `is_wash` defaults false.

- [ ] **Step 1: Failing test** (append to `tests/test_score.py`)
```python
def test_windows_schema_is_additive_on_an_existing_database():
    e = db.make_engine("sqlite://")
    with e.begin() as c:
        c.execute(sa.text("CREATE TABLE project_daily (project_id integer, day date, tvl_usd float, vol_7d float, vol_7d_prev float, "
                          "swaps_7d bigint, pools_n integer, score_trending float, is_new boolean NOT NULL DEFAULT 0, "
                          "is_spam boolean NOT NULL DEFAULT 0, PRIMARY KEY (project_id, day))"))
        c.execute(sa.text("INSERT INTO project_daily (project_id, day, is_spam) VALUES (1, '2026-10-05', 1)"))
        c.execute(sa.text("CREATE TABLE pool_snapshots (pool_id text, chain_id integer, day date, tvl_usd float, "
                          "volume_usd_cum float, tx_count_cum bigint, PRIMARY KEY (pool_id, chain_id, day))"))
        c.execute(sa.text("CREATE TABLE pool_stats (pool_id text, chain_id integer, day date, vol_24h float, traders_24h integer, "
                          "PRIMARY KEY (pool_id, chain_id, day))"))
    db.create_all(e)
    added = set(db.add_missing_columns(e))
    assert {"project_daily.window_end", "project_daily.vol_24h", "project_daily.swaps_24h", "project_daily.wallets_24h",
            "project_daily.wallets_exact", "project_daily.is_wash", "project_daily.wash_reason",
            "pool_snapshots.volume_usd_day", "pool_snapshots.tx_count_day", "pool_snapshots.estimated",
            "pool_stats.vol_24h_rolling", "pool_stats.wallets_24h", "pool_stats.window_end"} <= added
    with e.connect() as c:
        assert c.execute(sa.text("SELECT is_wash, wash_reason FROM project_daily")).one() == (0, None)
```
- [ ] **Step 2:** `cd pipeline && .venv/bin/python -m pytest -q tests/test_score.py -k windows_schema` → FAIL (`AssertionError`: the set is not a subset; nothing from 0012 added).
- [ ] **Step 3: Implement.** `db/migrations/0012_windows.sql`:
```sql
-- Package 8 (windows, counts, wash). Additive; also applied at startup by db.add_missing_columns.
-- One window convention: the run on `day` reports closed UTC days. window_end = day - 1; "24h" = window_end;
-- "7d" = window_end-6 .. window_end; "prev 7d" = the 7 days before. Windows sum the per-day snapshot columns.
-- That UTC day's volume / swaps. NULL = unknown, or a row written before 0012.
ALTER TABLE pool_snapshots ADD COLUMN IF NOT EXISTS volume_usd_day double precision;
ALTER TABLE pool_snapshots ADD COLUMN IF NOT EXISTS tx_count_day bigint;
-- true = volume_usd_day from a rolling 24h number or a run-time cumulative; NULL = written before 0012.
ALTER TABLE pool_snapshots ADD COLUMN IF NOT EXISTS estimated boolean;
-- GeckoTerminal volume_usd.h24 at the run (a rolling window); the onchain step never overrides it.
ALTER TABLE pool_stats ADD COLUMN IF NOT EXISTS vol_24h_rolling double precision;
-- Distinct wallets in the row's window; NULL = unknown (traders_24h is buyers + sellers, an upper bound).
ALTER TABLE pool_stats ADD COLUMN IF NOT EXISTS wallets_24h integer;
-- vol_24h and the wallet counts cover the 24 hours before this instant.
ALTER TABLE pool_stats ADD COLUMN IF NOT EXISTS window_end timestamptz;
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS window_end date;
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS vol_24h double precision;
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS swaps_24h bigint;
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS wallets_24h integer;
-- true = distinct across the project's pools; false = an upper bound (pools added, or buyers + sellers).
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS wallets_exact boolean;
-- Wash-like volume, separate from spam: listed in All with a badge, never ranked. Reasons: 24h, 7d, vol_tvl, symmetric.
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS is_wash boolean NOT NULL DEFAULT false;
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS wash_reason text;
```
`db.py`, `pool_snapshots`, after `tx_count_cum`:
```python
    # Per UTC day (0012): score windows sum these. NULL = unknown, or a row written before 0012.
    sa.Column("volume_usd_day", sa.Float),
    sa.Column("tx_count_day", sa.BigInteger),
    sa.Column("estimated", sa.Boolean),        # volume_usd_day from a rolling 24h number or a run-time cumulative
```
`pool_stats`, after `vol_source`:
```python
    sa.Column("vol_24h_rolling", sa.Float),    # GeckoTerminal volume_usd.h24 at the run; onchain never overrides it (0012)
    sa.Column("wallets_24h", sa.Integer),      # distinct wallets in the window; NULL = unknown (traders_24h = buyers + sellers)
    sa.Column("window_end", TS),               # vol_24h and the wallet counts cover the 24 h before this (0012)
```
`project_daily`, after `is_spam` (before the `sa.Index`):
```python
    # 0012: the closed UTC days this row reports. window_end = day - 1; 7d = window_end-6 .. window_end.
    sa.Column("window_end", sa.Date),
    sa.Column("vol_24h", sa.Float),
    sa.Column("swaps_24h", sa.BigInteger),     # NULL = not every pool's day is known (swaps_7d likewise)
    sa.Column("wallets_24h", sa.Integer),
    sa.Column("wallets_exact", sa.Boolean),    # True = distinct across the project's pools; False = an upper bound
    sa.Column("is_wash", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("wash_reason", sa.Text),         # comma list in rules.WASH_REASONS order
```
Append to `ADDITIVE_COLUMNS`:
```python
    ("pool_snapshots", "volume_usd_day", "double precision"),
    ("pool_snapshots", "tx_count_day", "bigint"),
    ("pool_snapshots", "estimated", "boolean"),
    ("pool_stats", "vol_24h_rolling", "double precision"),
    ("pool_stats", "wallets_24h", "integer"),
    ("pool_stats", "window_end", "timestamptz"),
    ("project_daily", "window_end", "date"),
    ("project_daily", "vol_24h", "double precision"),
    ("project_daily", "swaps_24h", "bigint"),
    ("project_daily", "wallets_24h", "integer"),
    ("project_daily", "wallets_exact", "boolean"),
    ("project_daily", "is_wash", "boolean NOT NULL DEFAULT false"),
    ("project_daily", "wash_reason", "text"),
```
- [ ] **Step 4:** full suite → **B+1 passed, S skipped**. Check the SQL file on Postgres: start the cluster as in Global Constraints, then `.venv/bin/python -m bait init-db --database-url "$TEST_DATABASE_URL" && psql "host=/tmp/bait-pg-p8 port=5560 user=postgres dbname=postgres" -v ON_ERROR_STOP=1 -q -f ../db/migrations/0012_windows.sql && echo ok` → `ok` (every statement a no-op after `init-db`, so this checks syntax); stop the cluster.
- [ ] **Step 5: Commit**
```bash
git add db/migrations/0012_windows.sql pipeline/bait/db.py pipeline/tests/test_score.py
git commit -m "db: 0012 windows schema: per-day snapshot columns, pool_stats rolling volume and distinct wallets, project_daily windows and wash"
```

### Task 2: `[keyless]` lines through the bait logger

**Files:** Modify `pipeline/bait/keyless.py:13-26` (imports), `:83-84` (`log`), `:133`, `:136`, `:150`, `:155`, `:325`, `:328` (error lines), `:507-508` (`main`). Test `pipeline/tests/test_keyless.py`.
**Interfaces:** `keyless.log(msg)` → `logging.getLogger("bait").info("[keyless] %s", msg)`; new `keyless.warn(msg)` at WARNING. Nothing is printed to stderr, so Railway gets package 4's JSON line with the right level.

- [ ] **Step 1: Failing test** (append to `tests/test_keyless.py`)
```python
def test_keyless_progress_goes_through_the_bait_logger_not_stderr(caplog, capsys):
    import logging
    from bait import keyless as K
    with caplog.at_level(logging.INFO, logger="bait"):
        K.log("base: 12 v4 pools from GeckoTerminal (uniswap_v4_base)")
        K.warn("GT 500 /networks/base/dexes?page=1")
    assert capsys.readouterr().err == ""
    recs = [(r.name, r.levelname, r.getMessage()) for r in caplog.records]
    assert ("bait", "INFO", "[keyless] base: 12 v4 pools from GeckoTerminal (uniswap_v4_base)") in recs
    assert ("bait", "WARNING", "[keyless] GT 500 /networks/base/dexes?page=1") in recs
```
- [ ] **Step 2:** `-k keyless_progress` → FAIL (`AttributeError: module 'bait.keyless' has no attribute 'warn'`).
- [ ] **Step 3: Implement.** Replace `import sys` with `import logging` (`sys` is used only by `log`). Replace `log`:
```python
_log = logging.getLogger("bait")


def log(msg: str) -> None:
    """Progress line. Through the "bait" logger, so Railway gets package 4's JSON line at level info (plain text on stderr
    was tagged as an error)."""
    _log.info("[keyless] %s", msg)


def warn(msg: str) -> None:
    _log.warning("[keyless] %s", msg)
```
Change `log(` to `warn(` on the six failure lines: `GT {e.code} {path}`, `GT error {e!r} {path}`, `RPC {url} HTTP {e.code}`, `RPC {url} error {e!r}`, `Blockscout {bs} HTTP {e.code}`, `Blockscout {bs} error {e!r}`. In `main`, first line of the body: `configure()` with `from .logsetup import configure` added to the imports (the CLI keeps printing its progress; `BAIT_LOG_FORMAT=text` gives the human format).
- [ ] **Step 4:** full suite → **B+2 passed, S skipped**.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/keyless.py pipeline/tests/test_keyless.py
git commit -m "keyless: progress and failure lines through the bait logger instead of plain stderr (Railway read them as errors)"
```

### Task 3: Wash rule functions (pure)

**Files:** Modify `pipeline/bait/rules.py:114-123` (the 7-day block); `pipeline/bait/steps/score.py:44-58`, `:84`, `:112-140`. Test `pipeline/tests/test_rules.py`, `pipeline/tests/test_score.py:133-135`.
**Interfaces:** `wallets_lower_bound(distinct, buyers, sellers, traders) -> int | None`; `is_wash_24h(vol_24h, wallets) -> bool`; `vol_tvl(vol, tvl) -> float | None`; `corroborated(vol_7d, tvl, symmetric) -> bool`; `is_wash_7d(vol_7d, wallets_7d, tvl, symmetric) -> bool` (new signature); `is_wash_vol_tvl(vol_7d, tvl, wallets) -> bool`; `is_symmetric(top, vol_24h) -> bool`; constants `WASH_REASONS = ("24h", "7d", "vol_tvl", "symmetric")`, `WASH_SHARE = 0.5`. `keyless.is_wash` stays until Task 7.

- [ ] **Step 1: Failing tests.** `tests/test_rules.py`, add the import line `from bait.rules import corroborated, is_symmetric, is_wash_24h, is_wash_7d, is_wash_vol_tvl, vol_tvl, wallets_lower_bound` and append:
```python
def test_wallets_lower_bound_never_uses_buyers_plus_sellers():
    assert wallets_lower_bound(7, 534, 511, 1045) == 7            # a distinct count wins
    assert wallets_lower_bound(None, 13, 13, 26) == 13            # OUROBOROS/CRCL: at least 13 wallets, not 26
    assert wallets_lower_bound(None, 5, None, None) == 5
    assert wallets_lower_bound(None, None, None, 27) == 14        # rows with buyers + sellers only: at least half
    assert wallets_lower_bound(None, None, None, None) is None


def test_wash_24h_counts_a_wallet_that_bought_and_sold_once():
    assert is_wash_24h(300_000, wallets_lower_bound(None, 13, 13, 26))      # 26 "traders" used to pass the <= 25 test
    assert not is_wash_24h(249_999, 1) and not is_wash_24h(300_000, 26) and not is_wash_24h(300_000, None)
    assert not is_wash_24h(None, 1)


def test_wash_7d_needs_corroboration_not_few_wallets_alone():
    assert vol_tvl(100.0, 2.0) == 50.0 and vol_tvl(1.0, 0.0) is None and vol_tvl(1.0, None) is None
    assert is_wash_7d(436_000_000, 1, tvl=50_000, symmetric=False)            # Optimism 0x69af: 8,720x its TVL
    assert not is_wash_7d(5_000_000, 4, tvl=2_000_000, symmetric=False)       # private / RFQ hook: real liquidity
    assert is_wash_7d(5_000_000, 4, tvl=2_000_000, symmetric=True)
    assert not is_wash_7d(5_000_000, 4, tvl=None, symmetric=False)            # TVL unknown: no ratio
    assert not is_wash_7d(999_999, 1, tvl=1, symmetric=True) and not is_wash_7d(5_000_000, 11, tvl=1, symmetric=True)
    assert corroborated(5_100, 100, False) and not corroborated(5_000, 100, False) and corroborated(None, None, True)


def test_vol_tvl_flags_robinhood_bots_not_base_launchpads():
    assert is_wash_vol_tvl(866 * 10_000, 10_000, wallets_lower_bound(None, 13, 13, 26))     # OUROBOROS/CRCL
    assert not is_wash_vol_tvl(345 * 200_000, 200_000, 30_000)                                  # Base 0x0f48 family
    assert not is_wash_vol_tvl(301 * 10, 10, 3)                                                 # under $10K: ranks nowhere
    assert not is_wash_vol_tvl(1e9, 0.0, 1) and not is_wash_vol_tvl(1e9, None, 1) and not is_wash_vol_tvl(1e9, 1, None)
    assert not is_wash_vol_tvl(1e9, 1, 50)                                                      # "fewer than 50"


def test_symmetric_traders():
    bots = [(50_000.0, 5, 5)] * 10
    assert is_symmetric(bots, 900_000)                       # $500K of $900K from 10 wallets, each 5 buys / 5 sells
    assert not is_symmetric(bots, 1_000_000)                 # exactly half is not more than half
    assert not is_symmetric(bots[:9] + [(50_000.0, 6, 5)], 900_000)
    assert not is_symmetric([(50_000.0, 5, 5)], 60_000)      # one wallet is not a pattern
    assert not is_symmetric(bots, None) and not is_symmetric([(5_000.0, 1, 1)] * 2, 9_000)    # under $10K
    assert is_symmetric(bots + [(1.0, 1, 0)], 900_000)       # only the top 10 by volume are judged
```
`tests/test_score.py`, replace `test_is_wash_7d_thresholds`:
```python
def test_is_wash_7d_thresholds():
    from bait.rules import is_wash_7d
    assert is_wash_7d(1_000_000, 10, tvl=1_000, symmetric=False)
    assert not is_wash_7d(999_999, 1, tvl=1, symmetric=False) and not is_wash_7d(5_000_000, 11, tvl=1, symmetric=False)
```
- [ ] **Step 2:** `tests/test_rules.py tests/test_score.py -k "wash or wallets or symmetric or vol_tvl"` → FAIL (`ImportError: cannot import name 'corroborated'`).
- [ ] **Step 3: Implement.** `rules.py`, replace lines 114-123 (comment, `WASH_MIN_VOL_7D`, `WASH_MAX_WALLETS_7D`, `is_wash_7d`) with:
```python
# --- wash rules (DESIGN.md "Wash rules"). A project is wash-like for a reason when the pools that rule flags carry
# >= WASH_SHARE of its volume in that rule's window; reasons are stored in WASH_REASONS order. ------------------------
WASH_REASONS = ("24h", "7d", "vol_tvl", "symmetric")
WASH_SHARE = 0.5
WASH_SIGNAL_MIN_VOL = 10_000        # below this nothing ranks anywhere (Trending needs $10K in 7 days)


def wallets_lower_bound(distinct: int | None, buyers: int | None, sellers: int | None, traders: int | None) -> int | None:
    """GeckoTerminal counts unique buyers and unique sellers, not unique wallets: buyers + sellers (traders_24h) counts a
    wallet that bought and sold twice, an upper bound. The "at most N wallets" rules need a lower bound: the distinct count
    when one is known, else max(buyers, sellers), else half of buyers + sellers. None = nothing known."""
    if distinct is not None:
        return distinct
    if buyers is not None or sellers is not None:
        return max(buyers or 0, sellers or 0)
    if traders is not None:
        return (traders + 1) // 2
    return None


# 24h (GeckoTerminal pool_stats; on ready chains the onchain step's closed day): big volume from a handful of wallets.
WASH_MIN_VOL_24H = 250_000
WASH_MAX_WALLETS_24H = 25


def is_wash_24h(vol_24h: float | None, wallets: int | None) -> bool:
    return vol_24h is not None and wallets is not None and vol_24h >= WASH_MIN_VOL_24H and wallets <= WASH_MAX_WALLETS_24H


def vol_tvl(vol: float | None, tvl: float | None) -> float | None:
    """Volume over liquidity. None when TVL is unknown or not positive: a ratio over $0 says nothing."""
    if vol is None or tvl is None or tvl <= 0:
        return None
    return vol / tvl


WASH_MIN_VOL_TVL = 50               # 7-day volume over TVL that corroborates a few-wallet pool


def corroborated(vol_7d: float | None, tvl: float | None, symmetric: bool) -> bool:
    """Few wallets alone also describes private / RFQ hooks (Arrakis private hook) that move real volume against real
    liquidity. A wash flag needs one more sign: 7-day volume > 50x a known TVL, or symmetric traders."""
    r = vol_tvl(vol_7d, tvl)
    return (r is not None and r > WASH_MIN_VOL_TVL) or symmetric


# 7 days (indexer: pool_daily_onchain + pool_wallet_day, ready chains only). 10/8: Optimism 0x69af...7fff moved $436M
# in 7 days from 1 wallet in 8 swaps.
WASH_MIN_VOL_7D = 1_000_000
WASH_MAX_WALLETS_7D = 10


def is_wash_7d(vol_7d: float, wallets_7d: int, tvl: float | None, symmetric: bool) -> bool:
    return vol_7d >= WASH_MIN_VOL_7D and wallets_7d <= WASH_MAX_WALLETS_7D and corroborated(vol_7d, tvl, symmetric)


# Volume far above liquidity from few wallets. Launchpads churn many times their liquidity too (Base 0x0f48 family:
# 345x) but through thousands of wallets; OUROBOROS/CRCL on Robinhood: 866x through 13 buyers and 13 sellers.
WASH_VOL_TVL = 300
WASH_VOL_TVL_WALLETS = 50           # fewer than this


def is_wash_vol_tvl(vol_7d: float, tvl: float | None, wallets: int | None) -> bool:
    r = vol_tvl(vol_7d, tvl)
    return (r is not None and r > WASH_VOL_TVL and vol_7d >= WASH_SIGNAL_MIN_VOL
            and wallets is not None and wallets < WASH_VOL_TVL_WALLETS)


# Bots trading with themselves: the pool's top wallets that day (activity step, pool_top_wallets) each buy and sell the
# same number of times and carry more than half its volume.
SYMMETRIC_TOP = 10
SYMMETRIC_SHARE = 0.5


def is_symmetric(top: list[tuple[float, int, int]], vol_24h: float | None) -> bool:
    """top: (volume_usd, buys, sells) per wallet, any order. The top 10 by volume (at least 2) are judged; vol_24h is the
    pool's rolling 24h volume, the window the trades come from."""
    if not vol_24h or vol_24h < WASH_SIGNAL_MIN_VOL:
        return False
    t = sorted(top, key=lambda x: -x[0])[:SYMMETRIC_TOP]
    return len(t) >= 2 and all(b == s and b > 0 for _, b, s in t) and sum(v for v, _, _ in t) > SYMMETRIC_SHARE * vol_24h
```
`score.py` (keeps the suite green under the new 7-day signature): next to `unknown_vol` (line 44) add `pool_tvl: dict[tuple, float | None] = {}`; after `a["max_tvl"] = ...` (line 57) add `pool_tvl[key] = cur.tvl_usd`; line 84 → `wash7 = _wash_7d(conn, day, pool_project, set(ready_chains(conn, ctx)), unknown_vol, pool_tvl)`; `_wash_7d` gains the last parameter `tvl: dict` (docstring: "tvl: latest TVL per pool, for the corroboration"), line 135 → `if is_wash_7d(v, wallets.get(key, 0), tvl.get(key), False):`, and the log line → `log.info("score: %d projects flagged as 7-day wash (>= $1M through <= 10 wallets, corroborated)", len(out))`.
- [ ] **Step 4:** full suite → **B+7 passed, S skipped** (`test_wash_7d_from_indexer_one_wallet_hundreds_of_millions` still passes: TVL $50K, 8,400x).
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/rules.py pipeline/bait/steps/score.py pipeline/tests/test_rules.py pipeline/tests/test_score.py
git commit -m "rules: wallet lower bound, corroborated 7-day rule, volume/TVL and symmetric-trader signals

Few wallets alone flags private and RFQ hooks (Arrakis private hook) that move real
volume against real liquidity; the 7-day rule now also needs volume > 50x a known TVL or
symmetric traders."
```

### Task 4: The onchain step writes per-day values and keeps older days

**Files:** Modify `pipeline/bait/steps/onchain.py:11-20` (docstring item 3), `:258-322` (`_usage_chunk`), new `_swap_counts`. Test `pipeline/tests/test_onchain.py`.
**Interfaces:** Series rows carry `volume_usd_day` (NULL from the first unknown day, as p5's cumulative), `tx_count_day`, `estimated=False`. The replace deletes only `day >= start`; the seed day upserts only the legacy cumulative columns. Pools whose keyless series is kept get exact `tx_count_day` for `start..ref` (only existing rows). `pool_stats` rows gain `wallets_24h = w` and `window_end = ref + 1 day 00:00 UTC`. `vol_source`, `ready_chains`, the held-back rule: unchanged.

- [ ] **Step 1: Failing tests** (append to `tests/test_onchain.py`)
```python
def _snapd(conn, pool_id):
    return {r.day: (r.volume_usd_day, r.tx_count_day, r.estimated) for r in conn.execute(
        sa.select(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == pool_id))}


def test_onchain_series_carries_daily_values_and_keeps_older_days(conn, ctx):
    _setup(conn)
    old = DAY - timedelta(days=20)
    conn.execute(sa.insert(db.pool_snapshots).values(pool_id=PA, chain_id=8453, day=old, tvl_usd=1.0, volume_usd_cum=5.0,
                                                     volume_usd_day=5.0, tx_count_day=1))
    conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == PA,
                                                    db.pool_snapshots.c.day == DAY - timedelta(days=15))
                 .values(volume_usd_day=777.0, tx_count_day=3))
    onchain.run(conn, ctx)
    d = _snapd(conn, PA)
    assert d[REF] == (500.0, 9, False) and d[REF - timedelta(days=1)] == (100.0, 2, False)
    assert d[DAY - timedelta(days=15)] == (777.0, 3, None)      # the seed day resets the cumulative only
    assert d[old] == (5.0, 1, None)                             # days before the series stay, for re-scoring past days
    c = _snapd(conn, PC)
    assert c[REF] == (50.0, 4, False) and c[REF - timedelta(days=1)] == (0.0, 0, False)   # no swaps that day: 0, known


def test_kept_keyless_series_gets_onchain_swap_counts(conn, ctx):
    _setup(conn, coverage_days=3)                               # history too short: PA keeps its keyless series
    onchain.run(conn, ctx)
    d = _snapd(conn, PA)
    assert (d[REF][1], d[REF - timedelta(days=1)][1], d[DAY - timedelta(days=5)][1]) == (9, 2, None)
    assert d[REF][0] is None                                    # volume columns untouched


def test_onchain_pool_stats_carry_distinct_wallets_and_the_closed_day_boundary(conn, ctx):
    _setup(conn)
    onchain.run(conn, ctx)
    st = conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.pool_id == PA)).one()
    assert (st.wallets_24h, st.traders_24h) == (7, 7)
    assert db.as_utc(st.window_end) == datetime(2026, 10, 6, tzinfo=timezone.utc)    # REF (10-05) ends 10-06 00:00
```
- [ ] **Step 2:** `tests/test_onchain.py -k "daily_values or swap_counts or closed_day_boundary"` → FAIL (`(None, None, None) != (500.0, 9, False)`; `wallets_24h` None).
- [ ] **Step 3: Implement** in `onchain.py`. Add `SNAP_KEYS = ["pool_id", "chain_id", "day"]` under `CHUNK`. In `_usage_chunk`, replace from `snap_rows = []` through the `upsert(conn, pool_snapshots, ...)` loop with:
```python
    snap_rows, seeds = [], []
    for pid in replace:
        cum_v, cum_n = 0.0, 0
        seeds.append({"pool_id": pid, "chain_id": cid, "day": start - timedelta(days=1), "tvl_usd": tvl.get(pid),
                      "volume_usd_cum": 0.0, "tx_count_cum": 0})
        d = start
        while d <= ctx.day:
            n, _, v, _ = daily.get(pid, {}).get(d, (0, 0, 0.0, 0))
            cum_v += v
            cum_n += n
            unknown = bad_from[pid] is not None and d >= bad_from[pid]
            snap_rows.append({"pool_id": pid, "chain_id": cid, "day": d, "tvl_usd": tvl.get(pid),
                              "volume_usd_cum": None if unknown else cum_v, "tx_count_cum": cum_n,
                              "volume_usd_day": None if unknown else v, "tx_count_day": n, "estimated": False})
            d += timedelta(days=1)
    if replace:
        # Rewrites the series from `start` on. The seed day only resets the legacy cumulative (its daily values stay) and
        # older days are kept, so `score --day <past day>` still finds their daily values.
        conn.execute(sa.delete(pool_snapshots).where(pool_snapshots.c.chain_id == cid, pool_snapshots.c.pool_id.in_(replace),
                                                     pool_snapshots.c.day >= start, pool_snapshots.c.day <= ctx.day))
        for i in range(0, len(seeds), CHUNK):
            upsert(conn, pool_snapshots, seeds[i:i + CHUNK], keys=SNAP_KEYS, update=["tvl_usd", "volume_usd_cum", "tx_count_cum"])
        for i in range(0, len(snap_rows), CHUNK):
            upsert(conn, pool_snapshots, snap_rows[i:i + CHUNK], keys=SNAP_KEYS)
    replaced = set(replace)
    _swap_counts(conn, cid, [pid for pid in chunk if pid not in replaced], daily, start, ref)
```
Before `stat_rows, held = [], []` add `window_end = datetime.combine(ref + timedelta(days=1), dtime.min, tzinfo=timezone.utc)`. In the `held.append({...})` dict add `"wallets_24h": w, "window_end": window_end,`; in the `stat_rows.append({...})` dict add the same two keys. The two `upsert` calls: `update=["vol_24h", "traders_24h", "wallets_24h", "window_end", "vol_source"]` and `update=["traders_24h", "wallets_24h", "window_end", "vol_source"]`. New function after `_usage_chunk`:
```python
def _swap_counts(conn, cid: int, pids: list[str], daily: dict, start: date, ref: date) -> None:
    """Pools whose keyless series is kept (history shorter than the window, or volume held back) still get exact swap
    counts: on a ready chain the indexer sees every swap, so a day without a row is 0. Only existing rows are updated."""
    if not pids or ref < start:
        return
    params, d = [], start
    while d <= ref:
        params += [{"p": pid, "d": d, "n": daily.get(pid, {}).get(d, (0, 0, 0.0, 0))[0]} for pid in pids]
        d += timedelta(days=1)
    conn.execute(sa.update(pool_snapshots)
                 .where(pool_snapshots.c.chain_id == cid, pool_snapshots.c.pool_id == sa.bindparam("p"),
                        pool_snapshots.c.day == sa.bindparam("d"))
                 .values(tx_count_day=sa.bindparam("n")), params)
```
Docstring item 3, replace "pool_snapshots becomes a cumulative volume / swap series from the on-chain daily numbers (zero-seeded the day before, like snapshot keyless), replacing the pool's earlier snapshots so one scale is used" with "pool_snapshots gets one row per day from `start` (volume_usd_day, tx_count_day; the legacy cumulative columns zero-seeded the day before), replacing the pool's rows from `start` on; older rows and the seed day's daily values are kept so a past day can be re-scored", replace "pool_stats(day) gets vol_24h / traders_24h from the last complete UTC day" with "pool_stats(day) gets vol_24h / traders_24h / wallets_24h (distinct) from the last complete UTC day, window_end = its end", and append "Pools whose keyless series is kept still get exact tx_count_day from the indexer."
- [ ] **Step 4:** full suite → **B+10 passed, S skipped** (every existing p5 onchain test still passes: the legacy cumulative columns are written exactly as before).
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/steps/onchain.py pipeline/tests/test_onchain.py
git commit -m "onchain: per-day volume and swaps in the series, exact swap counts for kept keyless series, distinct wallets and window end in pool_stats; older days kept"
```

### Task 5: Subgraph snapshots are the closed day, with deltas

**Files:** Modify `pipeline/bait/steps/snapshot.py:1` (docstring), `:5` (imports), `:148-172` (`_snapshot_chain`), new `_delta`. Test `pipeline/tests/test_snapshot.py`.
**Interfaces:** The subgraph row goes to `end = ctx.day - 1` (the cumulative read at run time, about 6 h after the boundary: `estimated=True`); `volume_usd_day` / `tx_count_day` are the deltas from the row for `end - 1`, NULL when there is none (never an all-time number). Subgraph `txCount` includes liquidity modifications; documented.

- [ ] **Step 1: Failing test.** In `tests/test_snapshot.py` add `from datetime import timedelta`; in `test_snapshot_paginates_and_writes_pools_and_snapshots` change `db.pool_snapshots.c.day == ctx.day` to `db.pool_snapshots.c.day == ctx.day - timedelta(days=1)`; append:
```python
def test_subgraph_row_is_the_closed_day_with_deltas_from_the_day_before(conn, ctx):
    ctx.clients["graph"] = make_graph([])
    ctx.chains = [8453]
    end = ctx.day - timedelta(days=1)
    pid = f"0x{7:064x}"
    conn.execute(sa.insert(db.pool_snapshots).values(pool_id=pid, chain_id=8453, day=end - timedelta(days=1), tvl_usd=1.0,
                                                     volume_usd_cum=40.0, tx_count_cum=4))
    snapshot.run(conn, ctx)
    s = conn.execute(sa.select(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == pid, db.pool_snapshots.c.day == end)).one()
    assert (s.volume_usd_cum, s.volume_usd_day, s.tx_count_day, s.estimated) == (70.0, 30.0, 3, True)
    other = conn.execute(sa.select(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == f"0x{8:064x}")).one()
    assert (other.volume_usd_day, other.tx_count_day) == (None, None)     # no row for the day before: unknown, not all-time
```
- [ ] **Step 2:** `tests/test_snapshot.py` → FAIL (the paginate test finds 0 rows on `end`; the new one `NoResultFound`).
- [ ] **Step 3: Implement.** Docstring line 1 → `"""Step 2: per chain with a confirmed subgraph, paginate hooked pools -> pools + pool_snapshots(day - 1); keyless for the rest."""`. Imports: `from datetime import date, datetime, timedelta, timezone`. In `_snapshot_chain`, after the `_meta` block:
```python
    # The cumulative is read at run time (06:00 UTC): it is written as the closed day `end`, about 6 h late (estimated).
    end = ctx.day - timedelta(days=1)
    prev = {r.pool_id: r for r in conn.execute(
        sa.select(pool_snapshots.c.pool_id, pool_snapshots.c.volume_usd_cum, pool_snapshots.c.tx_count_cum)
        .where(pool_snapshots.c.chain_id == ch.chain_id, pool_snapshots.c.day == end - timedelta(days=1)))}
```
and replace the `snap_rows.append({...})` with:
```python
        vol, txs, b = _float(p.get("volumeUSD")), _int(p.get("txCount")), prev.get(pid)
        snap_rows.append({
            "pool_id": pid, "chain_id": ch.chain_id, "day": end, "tvl_usd": _float(p.get("totalValueLockedUSD")),
            "volume_usd_cum": vol, "tx_count_cum": txs,   # txCount includes liquidity modifications
            "volume_usd_day": _delta(vol, b.volume_usd_cum if b else None), "tx_count_day": _delta(txs, b.tx_count_cum if b else None),
            "estimated": True,
        })
```
After `_float`:
```python
def _delta(cur, before):
    """That day's amount from two cumulatives; None (unknown) when either is missing, never the all-time total."""
    return None if cur is None or before is None else max(cur - before, 0)
```
- [ ] **Step 4:** full suite → **B+11 passed, S skipped**.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/steps/snapshot.py pipeline/tests/test_snapshot.py
git commit -m "snapshot: subgraph cumulatives land on the closed day with per-day deltas, NULL without a previous day"
```

### Task 6: The window switch: keyless writes per-day values, score sums closed days

**Files:** Modify `pipeline/bait/steps/snapshot.py:46-122` (`_keyless`) plus new `_candle_rows`, `_before`, `_carry_forward`, `_rolling_swaps`; replace `pipeline/bait/steps/score.py` (whole file below); `pipeline/bait/steps/onchain.py:227-245`, `:258-259`, `:310-315` (drop the `keyless_pool_stats` hand-off); `pipeline/tests/conftest.py:56-57`. Replace `pipeline/tests/test_score.py` and `pipeline/tests/test_snapshot_keyless.py` (whole files below); `pipeline/tests/test_onchain.py:63`, `:89-91`.
**Interfaces:** `score.window_end(day) -> date`. Candle rows: one per UTC day from the first candle (at most 15 days back) through the running day, `$0` for a day without a candle, `estimated=False`. A pool past `KEYLESS_OHLCV_CAP`: only `end` is written (rolling h24, `estimated=True`), skipped when `end` already holds a candle row; no other day is written. Rolling buys + sells become `end`'s `tx_count_day`; `tx_count_cum` continues from the last known value. `ctx.extras["keyless_pool_stats"]` is gone: score reads `pool_stats(day)` only. `snapshot.run` adds `estimated_pools` to its counts on keyless runs.
**Why the legacy cumulative columns stay:** the running web (before Tasks 11-13 deploy) reads them, `volume_usd_cum IS NULL` is p5's "volume unknown from here on" marker, and `tx_count_cum` is `is_spam`'s lifetime count. Remove them once package 9 has re-scored and the web reads only the per-day columns.

- [ ] **Step 1: Failing tests.** `tests/conftest.py`, replace `insert_snap`:
```python
def insert_snap(conn, pool_id, chain_id, day, tvl, vol, tx, vol_day=None, tx_day=None):
    conn.execute(sa.insert(db.pool_snapshots).values(pool_id=pool_id, chain_id=chain_id, day=day, tvl_usd=tvl, volume_usd_cum=vol,
                                                     tx_count_cum=tx, volume_usd_day=vol_day, tx_count_day=tx_day))
```
Replace `tests/test_score.py` with:
```python
import math
from datetime import timedelta

import pytest
import sqlalchemy as sa

from bait import db
from bait.steps import score
from tests.conftest import DAY, NOW, insert_hook, insert_pool, insert_project, insert_snap

END = DAY - timedelta(days=1)          # window_end: the run on DAY reports closed UTC days through END


def daily(conn, pid):
    return conn.execute(sa.select(db.project_daily).where(db.project_daily.c.project_id == pid)).one()


def series(conn, pool, chain, vols: dict, tvl=0.0, tx: dict | None = None):
    """One row per day: vols = {day: volume}, tx = {day: swaps} (absent = unknown); volume_usd_cum is the running sum."""
    cum = 0.0
    for d in sorted(vols):
        cum += vols[d]
        insert_snap(conn, pool, chain, d, tvl, cum, None, vol_day=vols[d], tx_day=(tx or {}).get(d))


def test_score_hand_computed(conn, ctx):
    # One pool: $5K a day the week before last, $10K a day last week, 100 swaps every day, TVL 5000. The running day (DAY)
    # has a partial row that must never be read.
    p = insert_project(conn, "P", first_seen=NOW - timedelta(days=3))
    insert_hook(conn, "0xp", 8453, project_id=p, name="P")
    insert_pool(conn, "0xpool1", 8453, "0xp", created=NOW - timedelta(days=60))
    vols = {END - timedelta(days=i): (10_000.0 if i < 7 else 5_000.0) for i in range(14)}
    series(conn, "0xpool1", 8453, vols, tvl=5000, tx={d: 100 for d in vols})
    insert_snap(conn, "0xpool1", 8453, DAY, 1, 999_999_999, None, vol_day=1_234_567.0, tx_day=9_999)    # still running
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert (d.window_end, d.vol_24h, d.vol_7d, d.vol_7d_prev) == (END, 10_000, 70_000, 35_000)
    assert (d.swaps_24h, d.swaps_7d, d.tvl_usd, d.pools_n) == (100, 700, 5000, 1)
    assert d.score_trending == pytest.approx(math.log1p(70_000) * 2.0)
    assert d.is_new is False           # pipeline first saw it 3d ago, but its pool is 60d old: not new
    assert d.is_spam is False
    assert out == {"projects": 1, "with_pools": 1, "new": 0, "trending": 1, "spam": 0, "wash": 0, "wash_7d": 0}


def test_score_not_new_when_old_or_tiny(conn, ctx):
    old = insert_project(conn, "Old", first_seen=NOW - timedelta(days=30))
    insert_hook(conn, "0xo", 1, project_id=old)
    insert_pool(conn, "0xpo", 1, "0xo", created=NOW - timedelta(days=30))
    insert_snap(conn, "0xpo", 1, END, 5_000_000, 1_000_000, 50, vol_day=0.0, tx_day=0)
    tiny = insert_project(conn, "Tiny", first_seen=NOW - timedelta(days=1), slug="tiny")
    insert_hook(conn, "0xt", 1, project_id=tiny)
    insert_pool(conn, "0xpt", 1, "0xt", created=NOW - timedelta(days=1))          # born on END
    insert_snap(conn, "0xpt", 1, END, 900, 3_000, 9, vol_day=3_000.0, tx_day=9)
    score.run(conn, ctx)
    assert daily(conn, old).is_new is False
    t = daily(conn, tiny)
    assert t.is_new is False and t.is_spam is False     # TVL 900 >= 100 so not spam, but not "new" either
    assert (t.vol_7d, t.swaps_7d, t.score_trending) == (3_000, 9, 0.0)   # days before the pool existed count as 0 swaps


def test_score_cold_start_sums_known_days_never_all_time(conn, ctx):
    p = insert_project(conn, "Cold", first_seen=NOW - timedelta(days=100))
    insert_hook(conn, "0xc", 1, project_id=p)
    insert_pool(conn, "0xpc", 1, "0xc", created=NOW - timedelta(days=100))
    insert_snap(conn, "0xpc", 1, END - timedelta(days=1), 10_000, 9_000_000, 90_000)     # first night: all-time cumulative only
    insert_snap(conn, "0xpc", 1, END, 10_000, 9_020_000, 90_100, vol_day=20_000.0, tx_day=100)
    score.run(conn, ctx)
    d = daily(conn, p)
    assert (d.vol_7d, d.vol_7d_prev, d.vol_24h) == (20_000, 0, 20_000)
    assert d.swaps_7d is None and d.swaps_24h == 100     # six days of the week unknown: no 7-day count
    assert d.score_trending == 0.0                       # no prior week to grow from


def test_score_spam_and_no_pools(conn, ctx):
    p = insert_project(conn, "Ghost", first_seen=NOW)
    insert_hook(conn, "0xg", 1, project_id=p)
    s = insert_project(conn, "Dust", first_seen=NOW, slug="dust")
    insert_hook(conn, "0xd", 1, project_id=s)
    insert_pool(conn, "0xpd", 1, "0xd", created=NOW)
    insert_snap(conn, "0xpd", 1, END, 12, 40, 3)
    out = score.run(conn, ctx)
    assert daily(conn, p).is_spam is True and daily(conn, p).pools_n == 0
    assert daily(conn, s).is_spam is True
    assert out["spam"] == 2
    # idempotent: re-run replaces, no duplicate rows
    score.run(conn, ctx)
    assert conn.execute(sa.select(sa.func.count()).select_from(db.project_daily)).scalar_one() == 2


def test_young_launch_is_new_not_trending(conn, ctx):
    p = insert_project(conn, "L", first_seen=NOW - timedelta(days=1))
    insert_hook(conn, "0xl", 8453, project_id=p, name="L")
    insert_pool(conn, "0xlp", 8453, "0xl", created=NOW - timedelta(days=3))
    days = [END - timedelta(days=2), END - timedelta(days=1), END]
    series(conn, "0xlp", 8453, dict(zip(days, (0.0, 0.0, 500_000.0))), tvl=5000, tx=dict(zip(days, (0, 0, 900))))
    score.run(conn, ctx)
    d = daily(conn, p)
    assert d.is_new is True and d.score_trending == 0.0 and d.vol_7d == 500_000 and d.swaps_7d == 900


def test_wash_rule_reads_persisted_pool_stats(conn, ctx):
    """The 24h rule reads pool_stats(day) from the database, so a score-only run judges like the nightly one."""
    p = insert_project(conn, "Washy", first_seen=NOW - timedelta(days=60))
    insert_hook(conn, "0xw", 8453, project_id=p, name="Washy")
    insert_pool(conn, "0xwp", 8453, "0xw", created=NOW - timedelta(days=60))
    insert_snap(conn, "0xwp", 8453, END, 0, 9_000_000, 0, vol_day=8_000_000.0)
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xwp", chain_id=8453, day=DAY, vol_24h=5_000_000, traders_24h=6))
    out = score.run(conn, ctx)
    d = daily(conn, p)
    assert d.is_spam and d.score_trending == 0.0 and out["wash"] == 1


def _onchain_project(conn, name, pool, wallets, vol_per_day, tvl=50_000):
    p = insert_project(conn, name, first_seen=NOW - timedelta(days=60))
    insert_hook(conn, f"0x{name}", 10, project_id=p, name=name)
    insert_pool(conn, pool, 10, f"0x{name}", created=NOW - timedelta(days=60))
    series(conn, pool, 10, {END - timedelta(days=i): float(vol_per_day) for i in range(14)}, tvl=tvl,
           tx={END - timedelta(days=i): 2 for i in range(14)})
    for i in range(7):
        d = END - timedelta(days=i)
        conn.execute(sa.insert(db.pool_daily_onchain).values(chain_id=10, pool_id=pool, day=d, swaps=2, wallets=1, volume_usd=vol_per_day))
        for w in range(wallets):
            conn.execute(sa.insert(db.pool_wallet_day).values(chain_id=10, pool_id=pool, day=d, address=f"0xw{w}", swaps=1))
    return p


def test_wash_7d_from_indexer_one_wallet_hundreds_of_millions(conn, ctx):
    """10/8 case: Optimism 0x69af...7fff moved $436M in 7 days from one wallet. Same wallets every day count once."""
    wash = _onchain_project(conn, "Desk", "0xdp", wallets=1, vol_per_day=60_000_000)
    busy = _onchain_project(conn, "Busy", "0xbp", wallets=11, vol_per_day=60_000_000, tvl=50_000_000)   # 11 wallets: not wash
    small = _onchain_project(conn, "Small", "0xsp", wallets=1, vol_per_day=100_000)       # $700K < $1M: not wash
    _optimism_ready(conn)
    out = score.run(conn, ctx)
    assert daily(conn, wash).is_spam and daily(conn, wash).score_trending == 0.0
    assert not daily(conn, busy).is_spam and not daily(conn, small).is_spam
    assert out["wash_7d"] == 1


def test_is_wash_7d_thresholds():
    from bait.rules import is_wash_7d
    assert is_wash_7d(1_000_000, 10, tvl=1_000, symmetric=False)
    assert not is_wash_7d(999_999, 1, tvl=1, symmetric=False) and not is_wash_7d(5_000_000, 11, tvl=1, symmetric=False)


def _optimism_ready(conn, swap_time=NOW):
    conn.execute(sa.insert(db.indexer_state).values(chain_id=10, initialize_block=1, swap_block=1, head_block=1, backfilled=True,
                                                    updated_at=NOW, swap_block_time=swap_time))


def test_wash_7d_ignores_a_chain_whose_swaps_are_not_caught_up(conn, ctx):
    wash = _onchain_project(conn, "Desk", "0xdp", wallets=1, vol_per_day=60_000_000)
    _optimism_ready(conn, swap_time=NOW - timedelta(days=2))
    out = score.run(conn, ctx)
    assert out["wash_7d"] == 0 and not daily(conn, wash).is_spam


def test_held_back_pool_with_null_volume_is_left_out_of_both_weeks(conn, ctx):
    # Two pools of one project. The normal one did $1,000 in each week. The other's on-chain volume became unknown during
    # this week (onchain wrote NULL from that day on): it must not read as $0 this week next to its $5,000 last week.
    p = insert_project(conn, "Mixed", first_seen=NOW - timedelta(days=60))
    insert_hook(conn, "0xm", 8453, project_id=p, name="Mixed")
    for pool, prev_week, cur in (("0xnormal", 1_000.0, 1_000.0), ("0xheld", 5_000.0, None)):
        insert_pool(conn, pool, 8453, "0xm", created=NOW - timedelta(days=60))
        insert_snap(conn, pool, 8453, END - timedelta(days=7), 0, prev_week, 10, vol_day=prev_week, tx_day=10)
        insert_snap(conn, pool, 8453, END, 0, None if cur is None else prev_week + cur, 20, vol_day=cur, tx_day=10)
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xheld", chain_id=8453, day=DAY, vol_24h=None, traders_24h=3))
    score.run(conn, ctx)
    d = daily(conn, p)
    assert (d.vol_7d, d.vol_7d_prev) == (1_000, 1_000)


def test_swaps_are_null_unless_every_day_of_the_window_is_known(conn, ctx):
    p = insert_project(conn, "Gap", first_seen=NOW - timedelta(days=60), slug="gap")
    insert_hook(conn, "0xg", 8453, project_id=p, name="Gap")
    insert_pool(conn, "0xgp", 8453, "0xg", created=NOW - timedelta(days=60))
    insert_pool(conn, "0xquiet", 8453, "0xg", created=NOW - timedelta(days=60))
    week = {END - timedelta(days=i): 1_000.0 for i in range(7)}
    series(conn, "0xgp", 8453, week, tx={d: 3_000 for d in week if d != END - timedelta(days=3)})       # one day unknown
    insert_snap(conn, "0xquiet", 8453, END - timedelta(days=9), 10, 0.0, 0, vol_day=0.0)               # no volume this week: adds 0
    score.run(conn, ctx)
    d = daily(conn, p)
    assert (d.swaps_7d, d.swaps_24h, d.vol_7d) == (None, 3_000, 7_000)       # never one day presented as seven
    conn.execute(sa.update(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == "0xgp",
                                                    db.pool_snapshots.c.day == END - timedelta(days=3)).values(tx_count_day=3_000))
    score.run(conn, ctx)
    assert daily(conn, p).swaps_7d == 21_000


def test_score_for_a_day_reads_stored_rows_only_so_a_rerun_writes_the_same_rows(conn, ctx):
    p = insert_project(conn, "P", first_seen=NOW - timedelta(days=60))
    insert_hook(conn, "0xp", 8453, project_id=p, name="P")
    insert_pool(conn, "0xpool1", 8453, "0xp", created=NOW - timedelta(days=60))
    series(conn, "0xpool1", 8453, {END - timedelta(days=i): 1_000.0 * (i + 1) for i in range(14)}, tvl=9_000,
           tx={END - timedelta(days=i): 5 for i in range(14)})
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xpool1", chain_id=8453, day=DAY, vol_24h=1_000.0, traders_24h=40))
    score.run(conn, ctx)
    first = dict(daily(conn, p)._mapping)
    ctx.now = NOW + timedelta(days=5)                                    # package 9 re-scores days later
    ctx.extras["keyless_pool_stats"] = {("0xpool1", 8453): (9e9, 1)}     # a stale in-run hand-off is never read
    score.run(conn, ctx)
    assert dict(daily(conn, p)._mapping) == first
```
(The Task 1 schema test moves along with the file: append it again at the end, unchanged.)
Replace `tests/test_snapshot_keyless.py` with:
```python
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from bait import db, keyless as K
from bait.steps import score, snapshot


def _pool(addr, hook, vol24, buyers, created, daily):
    return {"net": "base", "attrs": {"address": addr, "name": "ETH / USDC 0.3%", "pool_created_at": created,
                                     "reserve_in_usd": "2000000", "volume_usd": {"h24": str(vol24)},
                                     "transactions": {"h24": {"buys": 300, "sells": 200, "buyers": buyers, "sellers": 0}}},
            "key": {"currency0": "0x" + "0" * 40, "currency1": "0x" + "1" * 40, "fee": 3000, "tickSpacing": 60, "hooks": hook},
            "daily": daily}


def _t(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _run_keyless(conn, ctx, monkeypatch, found):
    monkeypatch.setenv("KEYLESS", "1")
    monkeypatch.setattr(K, "collect", lambda *a, **k: {("base", p["attrs"]["address"]): p for p in found})
    for name in ("resolve_hooks", "resolve_via_logs", "fetch_ohlcv"):
        monkeypatch.setattr(K, name, lambda *a, **k: None)
    monkeypatch.setattr(K, "code_hashes", lambda *a, **k: {})
    return snapshot.run(conn, ctx)


def _rows(conn, pool_id):
    return {r.day: r for r in conn.execute(sa.select(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == pool_id))}


def test_keyless_rows_feed_scoring_and_wash_is_excluded(conn, ctx, monkeypatch):
    day, end = ctx.day, ctx.day - timedelta(days=1)
    # candles day-14 .. day: $10K a day for the closed week day-7 .. day-1 (and the running day), $5K the week before
    daily = [(_t(day - timedelta(days=i)), 10_000.0 if i <= 7 else 5_000.0) for i in range(14, -1, -1)]
    real = _pool("0x" + "a" * 64, "0x" + "b" * 40, 10_000, 400, "2026-01-01T00:00:00Z", daily)
    wash = _pool("0x" + "c" * 64, "0x" + "d" * 40, 5_000_000, 5, "2026-01-01T00:00:00Z", daily)
    out = _run_keyless(conn, ctx, monkeypatch, [real, wash])
    assert out["keyless_pools"] == 2 and out["errors"] == 0 and out["estimated_pools"] == 0
    hooks = {r.address: r for r in conn.execute(sa.select(db.hooks))}
    assert hooks["0x" + "b" * 40].name is None          # unlisted: grouping names it (bytecode family or address)
    p = conn.execute(sa.select(db.pools).where(db.pools.c.pool_id == real["attrs"]["address"])).one()
    assert p.token0["symbol"] == "ETH" and p.token1["symbol"] == "USDC" and p.fee == 3000
    st = conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.pool_id == real["attrs"]["address"])).one()
    assert (st.buys_24h, st.sells_24h, st.buyers_24h, st.sellers_24h, st.traders_24h) == (300, 200, 400, 0, 400)
    assert (st.vol_24h, st.vol_24h_rolling, st.wallets_24h, db.as_utc(st.window_end)) == (10_000, 10_000, None, ctx.now)
    e = _rows(conn, real["attrs"]["address"])[end]
    assert (e.volume_usd_day, e.tx_count_day, e.estimated) == (10_000.0, 500, False)

    # one project per hook, then score
    for i, addr in enumerate(["0x" + "b" * 40, "0x" + "d" * 40]):
        pid = conn.execute(sa.insert(db.projects).values(slug=f"p{i}", name=f"P{i}", first_seen_at=ctx.now - timedelta(days=60))).inserted_primary_key[0]
        conn.execute(sa.update(db.hooks).values(project_id=pid).where(db.hooks.c.address == addr))
    res = score.run(conn, ctx)
    rows = {r.project_id: r for r in conn.execute(sa.select(db.project_daily).where(db.project_daily.c.day == day))}
    real_row, wash_row = rows[1], rows[2]
    assert (real_row.vol_7d, real_row.vol_7d_prev, real_row.vol_24h) == (70_000, 35_000, 10_000)
    assert (real_row.swaps_24h, real_row.swaps_7d) == (500, None)      # one known day of swaps is not a 7-day count
    assert real_row.score_trending > 0 and not real_row.is_spam
    assert wash_row.is_spam and wash_row.score_trending == 0 and res["wash"] == 1


def test_keyless_rerun_clears_a_stale_onchain_volume_label(conn, ctx, monkeypatch):
    real = _pool("0x" + "a" * 64, "0x" + "b" * 40, 10_000, 400, "2026-01-01T00:00:00Z", [(_t(ctx.day), 10_000.0)])
    # the onchain step ran earlier today and labelled the row; a same-day snapshot rerun rewrites vol_24h from keyless
    conn.execute(sa.insert(db.pool_stats).values(pool_id=real["attrs"]["address"], chain_id=8453, day=ctx.day, vol_24h=1.0,
                                                 traders_24h=1, vol_source="onchain"))
    _run_keyless(conn, ctx, monkeypatch, [real])
    st = conn.execute(sa.select(db.pool_stats).where(db.pool_stats.c.pool_id == real["attrs"]["address"])).one()
    assert (st.vol_24h, st.vol_source) == (10_000.0, None)


def test_pool_past_the_ohlcv_cap_writes_only_the_closed_day_as_an_estimate(conn, ctx, monkeypatch):
    end, pid = ctx.day - timedelta(days=1), "0x" + "a" * 64
    for i in range(1, 4):           # three earlier days from nights when it was inside the cap
        conn.execute(sa.insert(db.pool_snapshots).values(pool_id=pid, chain_id=8453, day=end - timedelta(days=i), tvl_usd=1.0,
                                                         volume_usd_cum=1000.0 * (4 - i), volume_usd_day=1000.0,
                                                         tx_count_cum=30 * (4 - i), tx_count_day=30, estimated=False))
    out = _run_keyless(conn, ctx, monkeypatch, [_pool(pid, "0x" + "b" * 40, 7_000, 400, "2026-01-01T00:00:00Z", None)])
    s = _rows(conn, pid)
    assert sorted(s) == [end - timedelta(days=3), end - timedelta(days=2), end - timedelta(days=1), end]   # no running-day row
    assert all(s[end - timedelta(days=i)].volume_usd_day == 1000.0 for i in range(1, 4))                   # history untouched
    assert (s[end].volume_usd_day, s[end].volume_usd_cum, s[end].estimated) == (7_000.0, 10_000.0, True)
    assert (s[end].tx_count_day, s[end].tx_count_cum) == (500, 590)          # 300 buys + 200 sells on top of 90 known
    assert out["estimated_pools"] == 1


def test_a_candle_row_for_the_closed_day_is_never_replaced_by_an_estimate(conn, ctx, monkeypatch):
    end, pid = ctx.day - timedelta(days=1), "0x" + "a" * 64
    conn.execute(sa.insert(db.pool_snapshots).values(pool_id=pid, chain_id=8453, day=end, tvl_usd=1.0, volume_usd_cum=5.0,
                                                     volume_usd_day=5.0, estimated=False))
    capped = _pool(pid, "0x" + "b" * 40, 7_000, 400, "2026-01-01T00:00:00Z", None)
    assert _run_keyless(conn, ctx, monkeypatch, [capped])["estimated_pools"] == 0
    _run_keyless(conn, ctx, monkeypatch, [capped])                           # same-day rerun
    r = conn.execute(sa.select(db.pool_snapshots).where(db.pool_snapshots.c.pool_id == pid)).one()
    assert (r.volume_usd_day, r.estimated, r.tx_count_day, r.tx_count_cum) == (5.0, False, 500, 500)


def test_candles_fill_days_without_trades_with_zero_and_never_touch_earlier_days(conn, ctx, monkeypatch):
    end, pid = ctx.day - timedelta(days=1), "0x" + "a" * 64
    conn.execute(sa.insert(db.pool_snapshots).values(pool_id=pid, chain_id=8453, day=end - timedelta(days=5), tvl_usd=1.0,
                                                     volume_usd_cum=9.0, volume_usd_day=9.0, estimated=False))
    candles = [(_t(end - timedelta(days=2)), 300.0), (_t(end), 200.0), (_t(ctx.day), 50.0)]
    _run_keyless(conn, ctx, monkeypatch, [_pool(pid, "0x" + "b" * 40, 4_000, 400, "2026-01-01T00:00:00Z", candles)])
    s = {d: (r.volume_usd_day, r.estimated) for d, r in _rows(conn, pid).items()}
    assert s == {end - timedelta(days=5): (9.0, False), end - timedelta(days=2): (300.0, False),
                 end - timedelta(days=1): (0.0, False), end: (200.0, False), ctx.day: (50.0, False)}
```
`tests/test_onchain.py`: delete line 63 (`ctx.extras["keyless_pool_stats"] = ...`) and line 90 (`assert ctx.extras["keyless_pool_stats"]...`); change the comment at line 92 to `# score reads the on-chain series: 7d = REF-6..REF, prev = REF-13..REF-7` (the asserted numbers do not change).
- [ ] **Step 2:** `tests/test_score.py tests/test_snapshot_keyless.py` → FAIL (`window_end` None; `KeyError: 'estimated_pools'`; `vol_7d` 80,000 vs 70,000).
- [ ] **Step 3: Implement.** Replace `pipeline/bait/steps/score.py` with:
```python
"""Step 6 (score): project_daily(day) from closed UTC days.

One window convention (DESIGN.md "Windows"). The run on `day` reports the UTC days that are over: window_end = day - 1;
"24h" = the day window_end; "7d" = window_end-6 .. window_end; "prev 7d" = window_end-13 .. window_end-7. Windows sum the
per-day columns of pool_snapshots (volume_usd_day, tx_count_day); the row for `day` itself is still running and is
never read.
- Volume: a day without a row adds $0 (the pool was not listed, or did not trade). A pool whose cumulative volume at
  window_end is NULL (the onchain step could not measure it) is left out of every volume window, not read as $0.
- Swaps: a pool's count is known when every day of the window is (days before the pool was created count as 0; a pool
  with no volume in the window adds 0). Otherwise swaps_7d / swaps_24h are NULL, never a 7-day number built from one day.
Reads stored rows only, so `python -m bait run --steps score --day <d>` writes the same rows however often it runs.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa

from ..db import (as_utc, hooks, pool_daily_onchain, pool_snapshots, pool_stats, pool_wallet_day, pools, project_daily,
                  projects, upsert)
from ..rules import is_new, is_spam, is_wash_7d, score_trending
from . import RunContext, log
from .onchain import ready_chains

_D = timedelta(days=1)


def window_end(day: date) -> date:
    """The last closed UTC day the run on `day` reports."""
    return day - _D


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    day = ctx.day
    end = window_end(day)
    w24, w7, wp = (end, end), (end - 6 * _D, end), (end - 13 * _D, end - 7 * _D)
    hook_to_project = {(r.chain_id, r.address): r.project_id for r in conn.execute(
        sa.select(hooks.c.chain_id, hooks.c.address, hooks.c.project_id).where(hooks.c.project_id.is_not(None)))}
    pool_project = {}
    for p in conn.execute(sa.select(pools.c.pool_id, pools.c.chain_id, pools.c.hook_address, pools.c.created_at)):
        pid = hook_to_project.get((p.chain_id, p.hook_address))
        if pid:
            pool_project[(p.pool_id, p.chain_id)] = (pid, as_utc(p.created_at).date() if p.created_at else None)
    by_pool: dict[tuple, dict] = {}
    for s in conn.execute(sa.select(pool_snapshots).where(pool_snapshots.c.day >= wp[0], pool_snapshots.c.day <= end)):
        by_pool.setdefault((s.pool_id, s.chain_id), {})[s.day] = s

    agg: dict[int, dict] = {}
    unknown_vol: set[tuple] = set()   # cumulative volume NULL at window_end (onchain held it back): not $0
    pool_tvl: dict[tuple, float | None] = {}
    for key, rows in by_pool.items():
        if key not in pool_project:
            continue
        pid, created = pool_project[key]
        a = agg.setdefault(pid, _empty())
        cur = rows[max(rows)]          # latest row on/before window_end
        a["pools"] += 1
        a["tvl"] += cur.tvl_usd or 0.0
        a["max_tvl"] = max(a["max_tvl"], cur.tvl_usd or 0.0)
        a["stats"].append((cur.tvl_usd or 0.0, cur.tx_count_cum or 0))
        pool_tvl[key] = cur.tvl_usd
        quiet: dict[str, bool] = {}
        if cur.volume_usd_cum is None:
            unknown_vol.add(key)
        else:
            for name, w in (("vol24", w24), ("vol7", w7), ("vol7p", wp)):
                v = _vol(rows, w)
                a[name] += v
                quiet[name] = v == 0
        for name, w, vol_name in (("tx24", w24, "vol24"), ("tx7", w7, "vol7")):
            n, known = _swaps(rows, w, created)
            a[name] += n
            if not known and not quiet.get(vol_name, False):
                a[name + "_known"] = False

    # 24h wash rule, per pool from pool_stats(day); a project is wash-like when wash pools carry most of its 24h volume
    # (summing wallets across a merged family would let two wash pools pass as one busy project).
    from ..keyless import is_wash
    per: dict[int, list[float]] = {}
    for r in conn.execute(sa.select(pool_stats).where(pool_stats.c.day == day)):
        key = (r.pool_id, r.chain_id)
        if key in pool_project and r.vol_24h is not None and key not in unknown_vol:
            t = per.setdefault(pool_project[key][0], [0.0, 0.0])
            t[0] += r.vol_24h
            if is_wash(r.vol_24h, r.traders_24h or 0):
                t[1] += r.vol_24h
    wash = {pid for pid, (total, washed) in per.items() if total > 0 and washed / total >= 0.5}
    wash7 = _wash_7d(conn, end, pool_project, set(ready_chains(conn, ctx)), unknown_vol, pool_tvl)
    wash |= wash7

    first_seen = {r.id: as_utc(r.first_seen_at) for r in conn.execute(sa.select(projects.c.id, projects.c.first_seen_at))}
    # A project's age is its earliest pool when known; on a first run every project was "first seen" today.
    for (pid, created) in pool_project.values():
        if created is not None and pid in first_seen:
            c = datetime.combine(created, datetime.min.time(), tzinfo=timezone.utc)
            if c < first_seen[pid]:
                first_seen[pid] = c
    rows = []
    for pid, fs in first_seen.items():
        a = agg.get(pid) or _empty()
        young = fs > datetime.combine(day - 7 * _D, datetime.min.time(), tzinfo=timezone.utc)
        rows.append({
            "project_id": pid, "day": day, "window_end": end, "tvl_usd": a["tvl"], "pools_n": a["pools"],
            "vol_24h": a["vol24"], "vol_7d": a["vol7"], "vol_7d_prev": a["vol7p"],
            "swaps_24h": a["tx24"] if a["tx24_known"] else None, "swaps_7d": a["tx7"] if a["tx7_known"] else None,
            "score_trending": 0.0 if (pid in wash or young) else score_trending(a["vol7"], a["vol7p"]),
            # the known days are a lower bound of the 7-day count, so ">= 10 of them" still proves "swaps_7d >= 10"
            "is_new": pid not in wash and is_new(fs, day, a["max_tvl"], a["tx7"]),
            "is_spam": pid in wash or is_spam(a["stats"]),
        })
    for i in range(0, len(rows), 500):
        upsert(conn, project_daily, rows[i:i + 500], keys=["project_id", "day"])
    trending = sum(1 for r in rows if r["score_trending"] > 0)
    return {"projects": len(rows), "with_pools": len(agg), "new": sum(r["is_new"] for r in rows),
            "trending": trending, "spam": sum(r["is_spam"] for r in rows), "wash": len(wash), "wash_7d": len(wash7)}


def _empty() -> dict:
    return {"tvl": 0.0, "max_tvl": 0.0, "pools": 0, "vol24": 0.0, "vol7": 0.0, "vol7p": 0.0, "tx24": 0, "tx7": 0,
            "tx24_known": True, "tx7_known": True, "stats": []}


def _vol(rows: dict, w: tuple[date, date]) -> float:
    """Sum of volume_usd_day over the window; a missing day or a NULL value adds $0."""
    return sum((r.volume_usd_day or 0.0) for d, r in rows.items() if w[0] <= d <= w[1])


def _swaps(rows: dict, w: tuple[date, date], created: date | None) -> tuple[int, bool]:
    """Sum of the known tx_count_day over the window, and whether every day is known (a day before the pool was created
    counts as 0, known)."""
    total, known, d = 0, True, w[0]
    while d <= w[1]:
        r = rows.get(d)
        if r is not None and r.tx_count_day is not None:
            total += r.tx_count_day
        elif created is None or d >= created:
            known = False
        d += _D
    return total, known


def _wash_7d(conn, end: date, pool_project: dict, chains: set[int], skip: set[tuple] | frozenset, tvl: dict) -> set[int]:
    """Projects whose 7-day on-chain volume (end-6 .. end) is mostly in wash pools (rules.is_wash_7d), judged per pool.
    Only chains whose swap cursor is caught up (onchain.ready_chains). skip: pools whose volume is unknown (NULL).
    tvl: latest TVL per pool, for the corroboration."""
    if not chains:
        return set()
    start = end - 6 * _D
    vol = {(r.pool_id, r.chain_id): r.v or 0.0 for r in conn.execute(
        sa.select(pool_daily_onchain.c.pool_id, pool_daily_onchain.c.chain_id, sa.func.sum(pool_daily_onchain.c.volume_usd).label("v"))
        .where(pool_daily_onchain.c.day >= start, pool_daily_onchain.c.day <= end, pool_daily_onchain.c.chain_id.in_(chains))
        .group_by(pool_daily_onchain.c.pool_id, pool_daily_onchain.c.chain_id))}
    if not vol:
        return set()
    wallets = {(r.pool_id, r.chain_id): r.n for r in conn.execute(
        sa.select(pool_wallet_day.c.pool_id, pool_wallet_day.c.chain_id, sa.func.count(sa.distinct(pool_wallet_day.c.address)).label("n"))
        .where(pool_wallet_day.c.day >= start, pool_wallet_day.c.day <= end, pool_wallet_day.c.chain_id.in_(chains))
        .group_by(pool_wallet_day.c.pool_id, pool_wallet_day.c.chain_id))}
    per: dict[int, list[float]] = {}
    for key, v in vol.items():
        if key not in pool_project or key in skip:
            continue
        t = per.setdefault(pool_project[key][0], [0.0, 0.0])
        t[0] += v
        if is_wash_7d(v, wallets.get(key, 0), tvl.get(key), False):
            t[1] += v
    out = {pid for pid, (total, washed) in per.items() if total > 0 and washed / total >= 0.5}
    if out:
        log.info("score: %d projects flagged as 7-day wash (>= $1M through <= 10 wallets, corroborated)", len(out))
    return out
```
`snapshot.py`: add `date` to the `datetime` import and `pool_snapshots` is already imported. Constants under the imports:
```python
SERIES_DAYS = 15          # candles per pool (fetch_ohlcv limit=15): the 14 closed days score reads plus the running day
SNAP_KEYS = ["pool_id", "chain_id", "day"]
BASE_LOOKBACK_DAYS = 30   # how far back a legacy cumulative is continued from
```
Replace `_keyless` from its docstring down to the line before `if new_hooks:` with:
```python
def _keyless(conn, ctx: RunContext, chain_ids: list[int], out: dict) -> None:
    """Chains with no subgraph (or no GRAPH_API_KEY): GeckoTerminal + on-chain hook resolution. See keyless.py.

    Every value goes to the UTC day it belongs to (score.py: windows end the day before the run). Candles give each day's
    volume. A pool past the OHLCV cap only has GeckoTerminal's rolling 24h volume: it is written as yesterday's, marked
    estimated, and no other day is touched. Rolling buys + sells become yesterday's swap count (an estimate too)."""
    from .. import keyless as K
    nets = [net for net, (cid, *_rest) in K.CHAINS.items() if cid in chain_ids]
    log.info("snapshot keyless chains=%s", ",".join(K.CHAINS[n][1] for n in nets))
    http = keyless_http(ctx)
    found = K.collect(http, int(os.environ.get("KEYLESS_PAGES", "4")), int(os.environ.get("KEYLESS_NEW_PAGES", "2")), nets=nets)
    K.resolve_hooks(http, found)
    K.resolve_via_logs(http, found)
    hooked = [p for p in found.values() if p.get("key") and int(p["key"]["hooks"], 16) != 0]
    K.fetch_ohlcv(http, hooked, int(os.environ.get("KEYLESS_OHLCV_CAP", "70")))

    end = ctx.day - timedelta(days=1)
    known = {(r.address, r.chain_id) for r in conn.execute(sa.select(hooks.c.address, hooks.c.chain_id))}
    new_hooks, pool_rows, snap_rows, stat_rows, capped, swaps = {}, [], [], [], [], {}
    for p in hooked:
        a, k = p["attrs"], p["key"]
        cid = K.CHAINS[p["net"]][0]
        pid, hook = a["address"].lower(), k["hooks"].lower()
        if (hook, cid) not in known and (hook, cid) not in new_hooks:
            new_hooks[(hook, cid)] = {"address": hook, "chain_id": cid, "name": None, "net": p["net"],
                                      "description": "", "flags": flags_from_address(hook), "properties": {}, "hooklisted": False,
                                      "verified_source": None,
                                      "audit_url": None, "deployer": None, "first_seen_at": ctx.now}
        sym0, sym1 = _symbols(a.get("name", ""))
        pool_rows.append({"pool_id": pid, "chain_id": cid, "hook_address": hook,
                          "token0": {"id": k["currency0"], "symbol": sym0}, "token1": {"id": k["currency1"], "symbol": sym1},
                          "fee": k["fee"], "tick_spacing": k["tickSpacing"],
                          "created_at": _ts(a.get("pool_created_at"))})
        tvl = float(a.get("reserve_in_usd") or 0)
        tx = a.get("transactions", {}).get("h24", {})
        counts = {f"{n}_24h": _int(tx.get(n)) for n in ("buys", "sells", "buyers", "sellers")}
        vol24 = float(a["volume_usd"]["h24"] or 0)
        # vol_source=None (keyless): a same-day rerun must not leave the onchain step's label on a keyless number.
        # traders_24h = buyers + sellers: an upper bound of distinct wallets (one that bought and sold counts twice).
        stat_rows.append({"pool_id": pid, "chain_id": cid, "day": ctx.day, "vol_24h": vol24, "vol_24h_rolling": vol24,
                          "traders_24h": (counts["buyers_24h"] or 0) + (counts["sellers_24h"] or 0), "wallets_24h": None,
                          "window_end": ctx.now, "vol_source": None, **counts})
        swaps[(pid, cid)] = (counts["buys_24h"] or 0) + (counts["sells_24h"] or 0)
        if p.get("daily"):
            snap_rows += _candle_rows(pid, cid, p["daily"], tvl, ctx.day)
        else:
            capped.append((pid, cid, tvl, vol24))
```
Then keep the `if new_hooks:` block and the `pools` upsert loop unchanged, and replace everything after them (the old `snap_rows` / `blank` / `stat_rows` code through the final log line) with:
```python
    vol_cols = ["tvl_usd", "volume_usd_cum", "volume_usd_day", "estimated"]
    for i in range(0, len(snap_rows), 500):
        upsert(conn, pool_snapshots, snap_rows[i:i + 500], keys=SNAP_KEYS, update=vol_cols)
    before = _before(conn, list(swaps), end)
    est = _carry_forward(capped, end, before)
    for i in range(0, len(est), 500):
        upsert(conn, pool_snapshots, est[i:i + 500], keys=SNAP_KEYS, update=vol_cols)
    _rolling_swaps(conn, swaps, end, before)
    for i in range(0, len(stat_rows), 500):
        upsert(conn, pool_stats, stat_rows[i:i + 500], keys=SNAP_KEYS)
    out["keyless_chains"] = len(nets)
    out["keyless_pools"] = len(pool_rows)
    out["estimated_pools"] = len(est)
    out["pools"] += len(pool_rows)
    log.info("snapshot keyless hooked_pools=%d unlisted_hooks=%d estimated=%d gt_calls=%d", len(pool_rows), len(new_hooks),
             len(est), http.gt_calls)


def _candle_rows(pid: str, cid: int, candles, tvl: float, day: date) -> list[dict]:
    """Daily candles (UTC days) -> one row per day from the first candle (at most SERIES_DAYS back) through `day`, the
    running day: that day's candle volume, $0 on a day with no candle (no trades). Days before the first candle are never
    written. volume_usd_cum (legacy) is the running sum from the first row."""
    vols = {datetime.fromtimestamp(t, tz=timezone.utc).date(): float(v) for t, v in candles}
    d = max(min(vols), day - timedelta(days=SERIES_DAYS - 1))
    rows, cum = [], 0.0
    while d <= day:
        v = vols.get(d, 0.0)
        cum += v
        rows.append({"pool_id": pid, "chain_id": cid, "day": d, "tvl_usd": tvl, "volume_usd_cum": cum, "volume_usd_day": v,
                     "estimated": False})
        d += timedelta(days=1)
    return rows


def _before(conn, keys: list[tuple[str, int]], end: date) -> dict[tuple[str, int], tuple]:
    """Per pool: (last volume_usd_cum before `end`, last tx_count_cum before `end`, whether `end` holds a candle row).
    estimated NULL (a row from before 0012) is not a candle row: it is replaced."""
    want, out = set(keys), {}
    pids = sorted({p for p, _ in keys})
    for i in range(0, len(pids), 500):
        q = (sa.select(pool_snapshots.c.pool_id, pool_snapshots.c.chain_id, pool_snapshots.c.day, pool_snapshots.c.volume_usd_cum,
                       pool_snapshots.c.tx_count_cum, pool_snapshots.c.estimated)
             .where(pool_snapshots.c.pool_id.in_(pids[i:i + 500]), pool_snapshots.c.day <= end,
                    pool_snapshots.c.day >= end - timedelta(days=BASE_LOOKBACK_DAYS))
             .order_by(pool_snapshots.c.day))
        for r in conn.execute(q):
            k = (r.pool_id, r.chain_id)
            if k not in want:
                continue
            vol, txs, candle = out.get(k, (None, None, False))
            if r.day < end:
                out[k] = (r.volume_usd_cum if r.volume_usd_cum is not None else vol,
                          r.tx_count_cum if r.tx_count_cum is not None else txs, candle)
            else:
                out[k] = (vol, txs, r.estimated is False)
    return out


def _carry_forward(capped: list[tuple[str, int, float, float]], end: date, before: dict) -> list[dict]:
    """Rows for pools past the OHLCV cap: GeckoTerminal's rolling 24h volume (it ends at run time, about 6 h into today) as
    `end`'s volume, estimated. Only `end` is written, so a pool that leaves the cap never zeroes its history, and a candle
    row already there wins. volume_usd_cum (legacy) continues from the last known value."""
    rows = []
    for pid, cid, tvl, vol24 in capped:
        base, _, candle = before.get((pid, cid), (None, None, False))
        if not candle:
            rows.append({"pool_id": pid, "chain_id": cid, "day": end, "tvl_usd": tvl, "volume_usd_cum": (base or 0.0) + vol24,
                         "volume_usd_day": vol24, "estimated": True})
    return rows


def _rolling_swaps(conn, swaps: dict[tuple[str, int], int], end: date, before: dict) -> None:
    """GeckoTerminal's rolling 24h buys + sells -> `end`'s tx_count_day (an estimate, like the rolling volume; the onchain
    step replaces it with exact counts on ready chains). tx_count_cum (legacy; is_spam's lifetime count) continues from the
    last known value. Only existing `end` rows are touched."""
    params = [{"p": pid, "c": cid, "n": n, "cum": (before.get((pid, cid), (None, None, False))[1] or 0) + n}
              for (pid, cid), n in swaps.items()]
    if params:
        conn.execute(sa.update(pool_snapshots)
                     .where(pool_snapshots.c.pool_id == sa.bindparam("p"), pool_snapshots.c.chain_id == sa.bindparam("c"),
                            pool_snapshots.c.day == end)
                     .values(tx_count_day=sa.bindparam("n"), tx_count_cum=sa.bindparam("cum")), params)
```
`onchain.py` (the hand-off no longer has a reader): in `_usage` delete `extras_stats = ctx.extras.get("keyless_pool_stats")` and the `extras_stats` argument of the `_usage_chunk` call; in `_usage_chunk` delete the `extras_stats` parameter and the two `if extras_stats ...:` blocks (lines 310-311 and 314-315).
- [ ] **Step 4:** full suite → **B+16 passed, S skipped**. `grep -rn keyless_pool_stats pipeline/bait` prints nothing.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/steps/score.py pipeline/bait/steps/snapshot.py pipeline/bait/steps/onchain.py pipeline/tests/conftest.py pipeline/tests/test_score.py pipeline/tests/test_snapshot_keyless.py pipeline/tests/test_onchain.py
git commit -m "score, snapshot: windows are closed UTC days from per-day columns; swaps NULL unless every day is known; the OHLCV cap never zeroes history

GeckoTerminal's cumulative was yesterday's plus today's partial candle, so 24h was a partial
day and 7d six days and six hours. Pools outside the cap rewrote day-1 as 0 every night,
and tx_count_cum held one day's count, so swaps_7d was a 24-hour number."
```

### Task 7: Wallet counts: distinct where known, a lower bound for the rules

**Files:** Modify `pipeline/bait/steps/score.py` (imports, the 24h loop, `run` rows, new `_wallets`); `pipeline/bait/steps/activity.py:1-6`, `:14`, `:28-30`, `:54-60`, `:67-76`, new `distinct_wallets`; `pipeline/bait/keyless.py:26` (import), `:67-69` (delete), `:170-171` (delete), `:431-441`, `:460` (`build`). Test `tests/test_score.py`, `tests/test_activity.py`, `tests/test_postgres.py`.
**Interfaces:** `score._wallets(conn, day, end, ready, pool_project) -> {project_id: (n, exact)}`. `project_daily.wallets_24h`, `wallets_exact`. `activity.TRADES_PER_CALL = 300`; `pool_stats.wallets_24h` set when the fetch returned fewer (every trade of the last 24 h). `keyless.is_wash` and its constants are deleted; callers use `rules.is_wash_24h` with `rules.wallets_lower_bound`.

- [ ] **Step 1: Failing tests.** Append to `tests/test_score.py`:
```python
def test_project_wallets_distinct_on_ready_chains_upper_bound_elsewhere(conn, ctx):
    # StablePair-like: six GeckoTerminal pools whose buyers + sellers sum to 1,142 (534+511+45+31+12+9)
    sp = insert_project(conn, "StablePair", first_seen=NOW - timedelta(days=60), slug="stablepair")
    insert_hook(conn, "0xsp", 1, project_id=sp, name="StablePair")
    for i, (b, s) in enumerate([(300, 234), (300, 211), (25, 20), (16, 15), (6, 6), (5, 4)]):
        pool = f"0xsp{i}"
        insert_pool(conn, pool, 1, "0xsp", created=NOW - timedelta(days=60))
        insert_snap(conn, pool, 1, END, 1_000, 10.0, 1, vol_day=10.0, tx_day=1)
        conn.execute(sa.insert(db.pool_stats).values(pool_id=pool, chain_id=1, day=DAY, vol_24h=10.0, traders_24h=b + s,
                                                     buyers_24h=b, sellers_24h=s))
    solo = insert_project(conn, "Solo", first_seen=NOW - timedelta(days=60), slug="solo")
    insert_hook(conn, "0xso", 1, project_id=solo, name="Solo")
    insert_pool(conn, "0xsop", 1, "0xso", created=NOW - timedelta(days=60))
    insert_snap(conn, "0xsop", 1, END, 1_000, 10.0, 1, vol_day=10.0, tx_day=1)
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xsop", chain_id=1, day=DAY, vol_24h=10.0, traders_24h=70, wallets_24h=40))
    desk = _onchain_project(conn, "Desk", "0xdp", wallets=2, vol_per_day=100.0)
    insert_pool(conn, "0xdp2", 10, "0xDesk", created=NOW - timedelta(days=60))
    for w in ("0xw0", "0xw1"):                     # the same two wallets in a second pool count once
        conn.execute(sa.insert(db.pool_wallet_day).values(chain_id=10, pool_id="0xdp2", day=END, address=w, swaps=1))
    _optimism_ready(conn)
    score.run(conn, ctx)
    assert (daily(conn, sp).wallets_24h, daily(conn, sp).wallets_exact) == (1_142, False)
    assert (daily(conn, solo).wallets_24h, daily(conn, solo).wallets_exact) == (40, True)
    assert (daily(conn, desk).wallets_24h, daily(conn, desk).wallets_exact) == (2, True)


def test_24h_wash_uses_a_wallet_lower_bound_not_buyers_plus_sellers(conn, ctx):
    p = insert_project(conn, "Ouro", first_seen=NOW - timedelta(days=60), slug="ouro")
    insert_hook(conn, "0xou", 4663, project_id=p, name="Ouro")
    insert_pool(conn, "0xoup", 4663, "0xou", created=NOW - timedelta(days=60))
    insert_snap(conn, "0xoup", 4663, END, 1_000, 1.0, 1, vol_day=900_000.0, tx_day=500)
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xoup", chain_id=4663, day=DAY, vol_24h=900_000.0, traders_24h=26,
                                                 buyers_24h=13, sellers_24h=13))
    out = score.run(conn, ctx)
    assert out["wash"] == 1                        # 13 buyers + 13 sellers is at least 13 wallets, not 26 > 25
```
Append to `tests/test_activity.py`:
```python
def test_activity_records_distinct_wallets_only_when_it_saw_every_trade_of_the_day(conn, ctx):
    http = _seed(conn)
    ctx.extras["keyless_http"] = http
    out = activity.run(conn, ctx)
    st = {r.pool_id: r.wallets_24h for r in conn.execute(sa.select(db.pool_stats))}
    assert (st[POOL_A], st[POOL_B], st[POOL_W], out["wallets_distinct"]) == (25, 3, None, 2)   # A: 26 trades, 25 wallets
    http.responses[f"/networks/base/pools/{POOL_A}/trades"] = {"data": [
        _trade(_w(i % 7), 10, "buy", "2026-10-06T01:00:00Z") for i in range(300)]}             # the cap: maybe not the whole day
    conn.execute(sa.update(db.pool_stats).values(wallets_24h=None))
    activity.run(conn, ctx)
    assert conn.execute(sa.select(db.pool_stats.c.wallets_24h).where(db.pool_stats.c.pool_id == POOL_A)).scalar() is None
```
Append to `tests/test_postgres.py`:
```python
def test_score_counts_a_wallet_once_across_a_projects_pools_on_postgres(pg_engine):
    from bait.steps import score
    end = DAY - timedelta(days=1)
    with pg_engine.begin() as c:
        pid = c.execute(sa.insert(db.projects).values(slug="desk", name="Desk", norm_name="desk",
                                                      first_seen_at=NOW - timedelta(days=60))).inserted_primary_key[0]
        c.execute(sa.insert(db.hooks).values(address="0xdesk", chain_id=10, project_id=pid, name="Desk", first_seen_at=NOW,
                                             flags={}, properties={}))
        for pool in ("0xp1", "0xp2"):
            c.execute(sa.insert(db.pools).values(pool_id=pool, chain_id=10, hook_address="0xdesk", created_at=NOW - timedelta(days=60)))
            c.execute(sa.insert(db.pool_snapshots).values(pool_id=pool, chain_id=10, day=end, tvl_usd=1.0, volume_usd_cum=1.0,
                                                          volume_usd_day=1.0, tx_count_day=1))
            for w in ("0xw0", "0xw1"):
                c.execute(sa.insert(db.pool_wallet_day).values(chain_id=10, pool_id=pool, day=end, address=w, swaps=1))
        c.execute(sa.insert(db.indexer_state).values(chain_id=10, initialize_block=1, swap_block=1, head_block=1, backfilled=True,
                                                     updated_at=NOW, swap_block_time=NOW))
    with pg_engine.begin() as c:
        score.run(c, _ctx())
    with pg_engine.connect() as c:
        r = c.execute(sa.select(db.project_daily)).one()
    assert (r.wallets_24h, r.wallets_exact, r.window_end) == (2, True, end)
```
- [ ] **Step 2:** `tests/test_score.py tests/test_activity.py -k "wallets or lower_bound or distinct"` → FAIL (`(None, None) != (1142, False)`; `out["wash"] == 0`; `KeyError: 'wallets_distinct'`). Postgres `-k counts_a_wallet_once` → FAIL (`wallets_24h` None).
- [ ] **Step 3: Implement.** `score.py`: rules import → `from ..rules import is_new, is_spam, is_wash_24h, is_wash_7d, score_trending, wallets_lower_bound`. Replace the 24h block's `from ..keyless import is_wash` line (delete it) and its test with `if is_wash_24h(r.vol_24h, wallets_lower_bound(r.wallets_24h, r.buyers_24h, r.sellers_24h, r.traders_24h)):`. Replace the `wash7 = ...` line with:
```python
    ready = ready_chains(conn, ctx)
    wash7 = _wash_7d(conn, end, pool_project, set(ready), unknown_vol, pool_tvl)
    wallets = _wallets(conn, day, end, ready, pool_project)
```
In the row dict, after `"swaps_7d": ...` add `"wallets_24h": wallets.get(pid, (None, None))[0], "wallets_exact": wallets.get(pid, (None, None))[1],`. New function after `_swaps`:
```python
def _wallets(conn, day: date, end: date, ready: list[int], pool_project: dict) -> dict[int, tuple[int, bool]]:
    """Wallets per project for the 24h figure, and whether the number is a distinct count across the project's pools.
    Ready chains: distinct addresses on `end` from pool_wallet_day, one union over the project's pools. Other pools:
    pool_stats(day).wallets_24h (distinct within the pool, rolling 24 h), else traders_24h (buyers + sellers). Adding
    pools, or buyers to sellers, can count a wallet twice: then the number is an upper bound (exact False)."""
    parts: dict[int, list[tuple[int, bool]]] = {}
    if ready:
        q = (sa.select(hooks.c.project_id, sa.func.count(sa.distinct(pool_wallet_day.c.address)).label("n"))
             .select_from(pool_wallet_day
                          .join(pools, (pools.c.pool_id == pool_wallet_day.c.pool_id) & (pools.c.chain_id == pool_wallet_day.c.chain_id))
                          .join(hooks, (hooks.c.address == pools.c.hook_address) & (hooks.c.chain_id == pools.c.chain_id)))
             .where(pool_wallet_day.c.day == end, pool_wallet_day.c.chain_id.in_(ready), hooks.c.project_id.is_not(None))
             .group_by(hooks.c.project_id))
        for r in conn.execute(q):
            parts[r.project_id] = [(r.n, True)]
    for r in conn.execute(sa.select(pool_stats).where(pool_stats.c.day == day)):
        key = (r.pool_id, r.chain_id)
        if key not in pool_project or r.chain_id in ready:
            continue
        n, distinct = (r.wallets_24h, True) if r.wallets_24h is not None else (r.traders_24h, False)
        if n:
            parts.setdefault(pool_project[key][0], []).append((n, distinct))
    return {pid: (sum(n for n, _ in ps), len(ps) == 1 and ps[0][1]) for pid, ps in parts.items()}
```
`activity.py`: docstring item 1 → "today's pool_stats joined to pools, 24h-wash pools excluded (rules.is_wash_24h on a wallet lower bound), top ACTIVITY_POOLS by 24h volume."; append to item 2: "When the call returned fewer than 300 trades it holds every trade of the last 24 h, so pool_stats.wallets_24h gets the exact distinct count." Imports: `from ..rules import is_wash_24h, wallets_lower_bound`. Under `RETAIN_DAYS`: `TRADES_PER_CALL = 300   # GeckoTerminal /trades returns at most this many, all from the last 24 h`. `out` gains `"wallets_distinct": 0`. After `upsert(conn, pool_top_wallets, rows, ...)`:
```python
                if len(trades) < TRADES_PER_CALL:   # every trade of the last 24 h: an exact distinct count for this pool
                    conn.execute(sa.update(pool_stats).where(pool_stats.c.pool_id == pid, pool_stats.c.chain_id == cid,
                                                             pool_stats.c.day == ctx.day).values(wallets_24h=distinct_wallets(trades)))
                    out["wallets_distinct"] += 1
```
Replace `select_pools`:
```python
def select_pools(conn, ctx: RunContext, limit: int) -> list[tuple[str, int]]:
    q = (sa.select(pool_stats)
         .join(pools, (pools.c.pool_id == pool_stats.c.pool_id) & (pools.c.chain_id == pool_stats.c.chain_id))
         .where(pool_stats.c.day == ctx.day))
    if ctx.chains:
        q = q.where(pool_stats.c.chain_id.in_(ctx.chains))
    rows = [r for r in conn.execute(q)
            if not is_wash_24h(r.vol_24h, wallets_lower_bound(r.wallets_24h, r.buyers_24h, r.sellers_24h, r.traders_24h))]
    rows.sort(key=lambda r: -(r.vol_24h or 0.0))
    return [(r.pool_id, r.chain_id) for r in rows[:max(limit, 0)]]


def distinct_wallets(trades: list[dict]) -> int:
    return len({str(t.get("tx_from_address") or "").strip().lower() for t in trades} - {""})
```
`keyless.py`: add `is_wash_24h, wallets_lower_bound` to the `from .rules import ...` line; delete `WASH_MIN_VOL_24H`, `WASH_MAX_TRADERS_24H` with their comment (lines 67-69) and `is_wash` (170-171). In `build`, the pool dict gains `"walletsMin24h": wallets_lower_bound(None, int(tx.get("buyers", 0)), int(tx.get("sellers", 0)), None),` and line 460 becomes `washed = sum(x["vol24h"] for x in pools_ if is_wash_24h(x["vol24h"], x["walletsMin24h"]))`.
- [ ] **Step 4:** full suite → **B+19 passed, S+1 skipped**; Postgres → **P+1 passed**.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/steps/score.py pipeline/bait/steps/activity.py pipeline/bait/keyless.py pipeline/tests/test_score.py pipeline/tests/test_activity.py pipeline/tests/test_postgres.py
git commit -m "wallets: distinct across a project's pools on ready chains, exact per pool when the trade fetch saw the whole day, else an upper bound; wash rules use a lower bound

traders_24h is buyers + sellers, so a wallet that bought and sold counted twice and
StablePair's six pools summed to 1,142 'wallets'."
```

### Task 8: Wash is its own flag, with reasons; spam is only spam

**Files:** Modify `pipeline/bait/steps/score.py` (imports, the 24h block and `wash7` lines, `_wash_7d` → `_wash` + `_onchain_7d`, the row dict, the return). Test `tests/test_score.py`, `tests/test_snapshot_keyless.py`.
**Interfaces:** `project_daily.is_wash`, `wash_reason` (comma list in `WASH_REASONS` order), `is_spam = rules.is_spam(...)` only. Wash: `score_trending = 0`, `is_new = False`. Counts `wash`, `wash_24h`, `wash_7d`, `wash_vol_tvl`, `wash_symmetric`.

- [ ] **Step 1: Failing tests.** In `tests/test_score.py`: `test_score_hand_computed` last line → `assert out == {"projects": 1, "with_pools": 1, "new": 0, "trending": 1, "spam": 0, "wash": 0, "wash_24h": 0, "wash_7d": 0, "wash_vol_tvl": 0, "wash_symmetric": 0}`; `test_wash_rule_reads_persisted_pool_stats` last line → `assert d.is_wash and not d.is_spam and d.wash_reason == "24h" and d.score_trending == 0.0 and out["wash"] == 1`; in `test_wash_7d_from_indexer_one_wallet_hundreds_of_millions` replace the two `is_spam` lines with
```python
    w = daily(conn, wash)
    assert w.is_wash and not w.is_spam and w.wash_reason == "7d,vol_tvl" and w.score_trending == 0.0
    assert not daily(conn, busy).is_wash and not daily(conn, small).is_wash
```
in `test_wash_7d_ignores_a_chain_whose_swaps_are_not_caught_up` → `assert out["wash_7d"] == 0 and not daily(conn, wash).is_wash`. In `tests/test_snapshot_keyless.py` the last line → `assert wash_row.is_wash and not wash_row.is_spam and wash_row.score_trending == 0 and res["wash"] == 1`. Append to `tests/test_score.py`:
```python
def test_wash_reasons_on_the_live_cases(conn, ctx):
    """10/9 audit cases: OUROBOROS/CRCL on Robinhood is flagged with its reasons; the Base launchpad family 0x0f48 and a
    private RFQ hook are not; dust stays spam and is not called wash."""
    def keyless_project(name, chain, tvl, vol_day, buyers, sellers, rolling=None):
        p = insert_project(conn, name, first_seen=NOW - timedelta(days=60), slug=name.lower())
        insert_hook(conn, f"0x{name.lower()}", chain, project_id=p, name=name)
        pool = f"0x{name.lower()}p"
        insert_pool(conn, pool, chain, f"0x{name.lower()}", created=NOW - timedelta(days=60))
        series(conn, pool, chain, {END - timedelta(days=i): vol_day for i in range(7)}, tvl=tvl)
        conn.execute(sa.insert(db.pool_stats).values(pool_id=pool, chain_id=chain, day=DAY, vol_24h=vol_day, vol_24h_rolling=rolling,
                                                     traders_24h=buyers + sellers, buyers_24h=buyers, sellers_24h=sellers))
        return p, pool
    ouro, ouro_pool = keyless_project("Ouroboros", 4663, tvl=10_000, vol_day=1_237_000.0, buyers=13, sellers=13, rolling=1_237_000.0)
    for w in range(10):       # symmetric 10/10 bots: $1M of $1.237M
        conn.execute(sa.insert(db.pool_top_wallets).values(pool_id=ouro_pool, chain_id=4663, day=DAY, address=f"0xbot{w}",
                                                           volume_usd=100_000.0, trades=10, buys=5, sells=5))
    launch, _ = keyless_project("Launchpad", 8453, tvl=200_000, vol_day=9_857_000.0, buyers=15_000, sellers=15_000)   # 345x
    rfq = _onchain_project(conn, "Rfq", "0xrfq", wallets=4, vol_per_day=714_000, tvl=2_000_000)                       # 2.5x
    _optimism_ready(conn)
    dust = insert_project(conn, "Dust", first_seen=NOW, slug="dust")
    insert_hook(conn, "0xdust", 1, project_id=dust)
    insert_pool(conn, "0xdustp", 1, "0xdust", created=NOW)
    insert_snap(conn, "0xdustp", 1, END, 12, 40, 3)
    out = score.run(conn, ctx)
    o = daily(conn, ouro)
    assert o.is_wash and o.wash_reason == "24h,vol_tvl,symmetric" and not o.is_spam and o.score_trending == 0 and not o.is_new
    assert not daily(conn, launch).is_wash and not daily(conn, rfq).is_wash
    assert daily(conn, dust).is_spam and not daily(conn, dust).is_wash
    assert (out["wash"], out["wash_24h"], out["wash_7d"], out["wash_vol_tvl"], out["wash_symmetric"]) == (1, 1, 0, 1, 1)
```
- [ ] **Step 2:** `tests/test_score.py tests/test_snapshot_keyless.py` → FAIL (`KeyError: 'wash_24h'`, `is_wash` False).
- [ ] **Step 3: Implement** in `score.py`. Imports: `pool_top_wallets` added to the `..db` import; rules import → `from ..rules import (WASH_REASONS, WASH_SHARE, is_new, is_spam, is_symmetric, is_wash_7d, is_wash_24h, is_wash_vol_tvl, score_trending, wallets_lower_bound)`. Next to `pool_tvl` add `pool_vol7: dict[tuple, float] = {}`; in the volume loop after `a[name] += v` add `if name == "vol7": pool_vol7[key] = v`. Replace everything from the `# 24h wash rule` comment through `wash |= wash7` with:
```python
    ready = ready_chains(conn, ctx)
    reasons = _wash(conn, day, end, pool_project, ready, unknown_vol, pool_vol7, pool_tvl)
    wallets = _wallets(conn, day, end, ready, pool_project)
```
In the row loop, first line `wash = pid in reasons`; `"score_trending": 0.0 if (wash or young) else ...`; `"is_new": not wash and is_new(...)`; `"is_spam": is_spam(a["stats"]),   # true spam only: no pools, or every pool tiny and idle`; add `"is_wash": wash, "wash_reason": ",".join(reasons.get(pid, [])) or None,`. Replace the `return` with:
```python
    counts = {f"wash_{r}": sum(r in rs for rs in reasons.values()) for r in WASH_REASONS}
    if reasons:
        log.info("score: %d wash-like projects (%s)", len(reasons), " ".join(f"{k}={v}" for k, v in counts.items()))
    return {"projects": len(rows), "with_pools": len(agg), "new": sum(r["is_new"] for r in rows),
            "trending": trending, "spam": sum(r["is_spam"] for r in rows), "wash": len(reasons), **counts}
```
Replace `_wash_7d` with:
```python
def _wash(conn, day: date, end: date, pool_project: dict, ready: list[int], unknown: set[tuple], vol7: dict,
          tvl: dict) -> dict[int, list[str]]:
    """Wash reasons per project (DESIGN.md "Wash rules"). Each rule judges pools; a project gets the reason when the
    pools it flags carry >= WASH_SHARE of the project's volume in that rule's window. Pools with unknown volume are never
    judged. Wallet thresholds compare a lower bound (rules.wallets_lower_bound), never buyers + sellers.
    vol7 / tvl: the pool's closed 7-day volume and latest TVL from the snapshots."""
    stats = {(r.pool_id, r.chain_id): r for r in conn.execute(sa.select(pool_stats).where(pool_stats.c.day == day))}
    lb = {k: wallets_lower_bound(r.wallets_24h, r.buyers_24h, r.sellers_24h, r.traders_24h) for k, r in stats.items()}
    top: dict[tuple, list] = {}
    for r in conn.execute(sa.select(pool_top_wallets.c.pool_id, pool_top_wallets.c.chain_id, pool_top_wallets.c.volume_usd,
                                    pool_top_wallets.c.buys, pool_top_wallets.c.sells).where(pool_top_wallets.c.day == day)):
        top.setdefault((r.pool_id, r.chain_id), []).append((r.volume_usd or 0.0, r.buys or 0, r.sells or 0))
    sym = {k: is_symmetric(top.get(k, []), r.vol_24h_rolling) for k, r in stats.items()}
    vol_on, wallets_7d = _onchain_7d(conn, end, ready)
    judged = {
        "24h": {k: (r.vol_24h or 0.0, is_wash_24h(r.vol_24h, lb[k])) for k, r in stats.items()},
        "7d": {k: (v, is_wash_7d(v, wallets_7d.get(k, 0), tvl.get(k), sym.get(k, False))) for k, v in vol_on.items()},
        "vol_tvl": {k: (v, is_wash_vol_tvl(v, tvl.get(k), wallets_7d[k] if k in wallets_7d else lb.get(k)))
                    for k, v in vol7.items()},
        "symmetric": {k: (r.vol_24h_rolling or 0.0, sym[k]) for k, r in stats.items()},
    }
    reasons: dict[int, list[str]] = {}
    for reason in WASH_REASONS:
        share: dict[int, list[float]] = {}
        for key, (v, hit) in judged[reason].items():
            if key not in pool_project or key in unknown or not v:
                continue
            t = share.setdefault(pool_project[key][0], [0.0, 0.0])
            t[0] += v
            t[1] += v if hit else 0.0
        for pid, (total, washed) in share.items():
            if washed / total >= WASH_SHARE:
                reasons.setdefault(pid, []).append(reason)
    return reasons


def _onchain_7d(conn, end: date, chains: list[int]) -> tuple[dict, dict]:
    """Per pool on ready chains (onchain.ready_chains: a stuck swap cursor would judge partial volume): on-chain volume and
    distinct wallets over end-6 .. end."""
    if not chains:
        return {}, {}
    start = end - 6 * _D
    vol = {(r.pool_id, r.chain_id): r.v or 0.0 for r in conn.execute(
        sa.select(pool_daily_onchain.c.pool_id, pool_daily_onchain.c.chain_id, sa.func.sum(pool_daily_onchain.c.volume_usd).label("v"))
        .where(pool_daily_onchain.c.day >= start, pool_daily_onchain.c.day <= end, pool_daily_onchain.c.chain_id.in_(chains))
        .group_by(pool_daily_onchain.c.pool_id, pool_daily_onchain.c.chain_id))}
    wallets = {(r.pool_id, r.chain_id): r.n for r in conn.execute(
        sa.select(pool_wallet_day.c.pool_id, pool_wallet_day.c.chain_id, sa.func.count(sa.distinct(pool_wallet_day.c.address)).label("n"))
        .where(pool_wallet_day.c.day >= start, pool_wallet_day.c.day <= end, pool_wallet_day.c.chain_id.in_(chains))
        .group_by(pool_wallet_day.c.pool_id, pool_wallet_day.c.chain_id))}
    return vol, wallets
```
Delete the now-unused `wash`, `wash7` names. Add a docstring line to the module: "Wash (rules.WASH_REASONS) is its own flag: listed in All with a badge, never ranked; is_spam is only rules.is_spam."
- [ ] **Step 4:** full suite → **B+20 passed, S+1 skipped**.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/steps/score.py pipeline/tests/test_score.py pipeline/tests/test_snapshot_keyless.py
git commit -m "score: is_wash with reasons (24h, 7d, vol_tvl, symmetric), separate from is_spam

Both wash rules folded into is_spam, so wash projects 404ed although DESIGN shows them in
All with a red badge."
```

### Task 9: The 24h rule needs the same corroboration (owner may drop this task)

**Files:** Modify `pipeline/bait/steps/score.py` (`_wash`, the `"24h"` entry; import `corroborated`). Test `tests/test_score.py`, `tests/test_snapshot_keyless.py`.
**Interfaces:** A 24h-wash pool also needs `corroborated(pool 7d volume, TVL, symmetric)`. Why: with the lower bound (Task 7) the 24h rule flags private / RFQ hooks (≤ 25 wallets, real volume, real liquidity) that Task 3 spared in the 7-day rule. Dropping this task leaves the 24h rule wallet-only; nothing else depends on it.

- [ ] **Step 1: Failing test.** Append to `tests/test_score.py`:
```python
def test_24h_rule_spares_a_private_rfq_hook_with_real_liquidity(conn, ctx):
    """Four wallets, $714K a day, $2M TVL: few wallets alone is not wash."""
    rfq = _onchain_project(conn, "Rfq", "0xrfq", wallets=4, vol_per_day=714_000, tvl=2_000_000)
    _optimism_ready(conn)
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xrfq", chain_id=10, day=DAY, vol_24h=714_000.0, traders_24h=4,
                                                 wallets_24h=4, vol_source="onchain"))
    score.run(conn, ctx)
    assert not daily(conn, rfq).is_wash
```
Fixtures that relied on a wallet-only 24h rule now give the pool a TVL the volume dwarfs: in `test_wash_rule_reads_persisted_pool_stats` change `insert_snap(conn, "0xwp", 8453, END, 0, ...)` to `insert_snap(conn, "0xwp", 8453, END, 10_000, ...)` and its last line to `assert d.is_wash and not d.is_spam and d.wash_reason == "24h,vol_tvl" and d.score_trending == 0.0 and out["wash"] == 1`; in `tests/test_snapshot_keyless.py` give `_pool` a `reserve="2000000"` parameter used for `reserve_in_usd`, and build the wash pool with `reserve="1000"` (its $70K week is 70x its TVL).
- [ ] **Step 2:** `-k spares_a_private_rfq` → FAIL (`is_wash` True: 714K >= 250K through 4 wallets).
- [ ] **Step 3: Implement.** Add `corroborated` to the rules import; the `"24h"` entry of `judged` becomes
```python
        "24h": {k: (r.vol_24h or 0.0, is_wash_24h(r.vol_24h, lb[k]) and corroborated(vol7.get(k), tvl.get(k), sym[k]))
                for k, r in stats.items()},
```
and the `_wash` docstring gains: "Every few-wallet rule also needs rules.corroborated (volume far above a known TVL, or symmetric traders)."
- [ ] **Step 4:** full suite → **B+21 passed, S+1 skipped**.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/steps/score.py pipeline/tests/test_score.py pipeline/tests/test_snapshot_keyless.py
git commit -m "score: the 24h wash rule needs the same corroboration as the 7-day rule (volume far above a known TVL, or symmetric traders)"
```

### Task 10: Blurb and digest use the same windows

**Files:** Modify `pipeline/bait/steps/signals.py:71-72` (`select_targets`), `:125-135` (`_blurb_ctx`); `pipeline/bait/steps/digest.py:1-31`. Test `tests/test_signals_digest.py`.
**Interfaces:** The blurb context's `metrics` carry `windows` (text naming the closed days), `vol_24h`, `swaps_24h`, `swaps_7d` (None = unknown), `wallets_24h`, `wallets_24h_kind`. Wash projects are never blurb targets. The digest body gains `window_end`; wash projects are left out of `new`, `trending` and `totals`.

- [ ] **Step 1: Failing tests** (append to `tests/test_signals_digest.py`)
```python
def test_blurb_context_names_its_windows_and_never_a_made_up_swap_count(conn, ctx):
    hot = insert_project(conn, "Hot", NOW - timedelta(days=40))
    insert_hook(conn, f"0x{hot}", 1, project_id=hot)
    conn.execute(sa.insert(db.project_daily).values(
        project_id=hot, day=DAY, window_end=DAY - timedelta(days=1), tvl_usd=1e6, vol_24h=10_000.0, vol_7d=70_000.0,
        vol_7d_prev=35_000.0, swaps_24h=500, swaps_7d=None, wallets_24h=1_142, wallets_exact=False, pools_n=6,
        score_trending=30.0, is_new=False, is_spam=False))
    washy = insert_project(conn, "Washy", NOW - timedelta(days=40), slug="washy")
    insert_hook(conn, f"0x{washy}", 1, project_id=washy)
    conn.execute(sa.insert(db.project_daily).values(project_id=washy, day=DAY, tvl_usd=1.0, vol_7d=9e8, vol_7d_prev=1e6, pools_n=1,
                                                    score_trending=40.0, is_wash=True, wash_reason="24h"))
    seen = []

    class Capture(FakeClaude):
        def why_blurb(self, c):
            seen.append(c)
            return super().why_blurb(c)
    ctx.clients.update(claude=Capture())
    assert [p.slug for p, _ in signals.select_targets(conn, ctx)] == ["hot"]       # wash-like volume gets no blurb
    signals.run(conn, ctx)
    m = seen[0]["metrics"]
    assert m["windows"].startswith("closed UTC days ending 2026-10-05")
    assert (m["vol_24h"], m["swaps_24h"], m["swaps_7d"]) == (10_000.0, 500, None)
    assert (m["wallets_24h"], m["wallets_24h_kind"]) == (1_142, "upper bound (buyers + sellers per pool)")


def test_digest_leaves_wash_out_of_the_week_and_names_the_window(conn, ctx):
    a = insert_project(conn, "Real", NOW - timedelta(days=40), slug="real")
    b = insert_project(conn, "Washy", NOW - timedelta(days=40), slug="washy")
    conn.execute(sa.insert(db.project_daily).values(project_id=a, day=DAY, tvl_usd=100.0, vol_7d=50_000.0, vol_7d_prev=20_000.0,
                                                    pools_n=1, score_trending=12.0))
    conn.execute(sa.insert(db.project_daily).values(project_id=b, day=DAY, tvl_usd=1.0, vol_7d=9e8, vol_7d_prev=1e6, pools_n=1,
                                                    score_trending=0.0, is_wash=True, wash_reason="7d"))
    ctx.force_digest = True
    digest.run(conn, ctx)
    body = conn.execute(sa.select(db.digests.c.body)).scalar_one()
    assert body["window_end"] == "2026-10-05"
    assert body["totals"] == {"projects": 1, "tvl_usd": 100.0, "vol_7d": 50_000.0}
    assert [c["slug"] for c in body["trending"]] == ["real"]
```
- [ ] **Step 2:** `tests/test_signals_digest.py -k "windows or wash_out"` → FAIL (targets `['washy', 'hot']`; `KeyError: 'window_end'`).
- [ ] **Step 3: Implement.** `signals.py` `select_targets`: `.where(project_daily.c.day == ctx.day, project_daily.c.is_spam.is_(False), project_daily.c.is_wash.is_(False))` and its docstring "...; spam and wash-like volume excluded." `_blurb_ctx` metrics:
```python
        "metrics": {
            "windows": f"closed UTC days ending {daily.window_end}: 24h = that day; 7d = it and the 6 days before; "
                       "prev_7d = the 7 days before those. A null count is unknown: do not quote it.",
            "tvl_usd": daily.tvl_usd, "vol_24h": daily.vol_24h, "vol_7d": daily.vol_7d, "vol_7d_prev": daily.vol_7d_prev,
            "swaps_24h": daily.swaps_24h, "swaps_7d": daily.swaps_7d, "wallets_24h": daily.wallets_24h,
            "wallets_24h_kind": None if daily.wallets_24h is None else (
                "distinct" if daily.wallets_exact else "upper bound (buyers + sellers per pool)"),
            "pools_n": daily.pools_n, "is_new": daily.is_new},
```
`digest.py`: docstring → `"""Step 8 (digest): Monday (or --force-digest) -> digests(week_of=day): the week's New + Trending over the closed days ending day - 1; wash-like projects are left out."""`; `from datetime import timedelta`; after `rows = ...`: `live = [r for r in rows if not r.is_wash]   # listed in All, never counted in the week`; use `live` for `trending`, `new` and the three `totals`; `body` gains `"window_end": (ctx.day - timedelta(days=1)).isoformat(),` after `week_of`.
- [ ] **Step 4:** full suite → **B+23 passed, S+1 skipped**; Postgres → **P+1 passed**.
- [ ] **Step 5: Commit**
```bash
git add pipeline/bait/steps/signals.py pipeline/bait/steps/digest.py pipeline/tests/test_signals_digest.py
git commit -m "signals, digest: the blurb gets the window and NULL counts as unknown; wash-like projects get no blurb and stay out of the digest"
```

### Task 11: Web data layer reads the new columns

**Precondition:** package 6 is on this branch (`git merge origin/main` again; `git merge-base --is-ancestor origin/fix/p6-ux HEAD`). If not, stop and report: Tasks 12-13 edit package 6's files.
**Files:** Modify `web/src/db/schema.ts` (`poolSnapshots`, `projectDaily`, `poolStats`), `web/src/lib/types.ts` (`PoolView`, `Usage`, `Metrics`, `ProjectSummary`, `RowView`), `web/src/db/queries.ts:46`, `:156`, `:340-341`, `:349-563` (`loadFromDb`), `:565-651` (`loadPools`), `:653-679` (`loadSeries`), `web/src/lib/present.ts` (`toRow`), `web/src/fixtures/sample.ts` (`build`).
**Interfaces:** `Usage.vol24h` / `tx24h` / `wallets24h` / `walletsExact` from `project_daily` (closed day); `Usage.vol24hRolling` = sum of `pool_stats.vol_24h_rolling`; `Metrics.swaps7d: number | null`; `ProjectSummary.isWash`, `washReason`; `RowView.isWash`; `PoolView.tx7d: number | null`, `walletsExact`. Every snapshot read uses `end = latest - 1`. `getLeaders` drops wash projects; `getProject` still 404s spam only.

- [ ] **Step 1: Types and schema.** `schema.ts`, `poolSnapshots` after `txCountCum`:
```ts
    volumeUsdDay: doublePrecision("volume_usd_day"), // that UTC day's volume; NULL = unknown
    txCountDay: bigint("tx_count_day", { mode: "number" }), // that UTC day's swaps; NULL = unknown
    estimated: boolean("estimated"),
```
`projectDaily` after `isSpam`:
```ts
    windowEnd: date("window_end"), // the last closed UTC day the row reports (day - 1)
    vol24h: doublePrecision("vol_24h"),
    swaps24h: bigint("swaps_24h", { mode: "number" }), // NULL = not every day known (swaps7d likewise)
    wallets24h: integer("wallets_24h"),
    walletsExact: boolean("wallets_exact"), // true = distinct across the project's pools; false = upper bound
    isWash: boolean("is_wash").notNull().default(false),
    washReason: text("wash_reason"), // comma list: 24h, 7d, vol_tvl, symmetric
```
`poolStats` after `sellers24h`:
```ts
    vol24hRolling: doublePrecision("vol_24h_rolling"), // GeckoTerminal h24 at the run (rolling)
    wallets24h: integer("wallets_24h"), // distinct; NULL = unknown (traders24h is buyers + sellers)
    windowEnd: timestamp("window_end", { withTimezone: true }),
```
`types.ts`: in `PoolView` replace the `tx7d`, `vol24h`, `wallets24h` members with
```ts
  /** swaps over the 7 closed UTC days; null unless all 7 are known */
  tx7d: number | null;
  /** volume of the closed UTC day before the latest run; null when unknown */
  vol24h: number | null;
  /** distinct wallets when walletsExact, else buyers + sellers (an upper bound); latest pool_stats day */
  wallets24h: number | null;
  walletsExact: boolean;
```
replace `Usage` with
```ts
/** Usage per project. Volume, swaps and wallets cover the closed UTC day before the latest run; buys/sells are
 * GeckoTerminal's rolling 24 h. null = no data (table empty, missing, not every day known). */
export interface Usage {
  vol24h: number | null;
  /** GeckoTerminal's rolling 24 h at the run (sum of pool_stats.vol_24h_rolling); only the pace line uses it */
  vol24hRolling: number | null;
  /** swaps on the closed day; null unless every pool's day is known */
  tx24h: number | null;
  /** distinct across the project's pools when walletsExact, else an upper bound (pools added, buyers + sellers) */
  wallets24h: number | null;
  walletsExact: boolean | null;
  buys24h: number | null;
  sells24h: number | null;
  buyers24h: number | null;
  sellers24h: number | null;
  /** distinct smart_money wallets among pool_top_wallets, latest day */
  smartWallets: number | null;
}
```
`Metrics.swaps7d` → `swaps7d: number | null; // swaps over the 7 closed UTC days; null unless every day is known`. `ProjectSummary`: the `isSpam` doc → `/** DESIGN.md spam filter: no pools, or all pools under $100 TVL and < 10 txs lifetime. Hidden everywhere (404). */`, and after it
```ts
  /** wash-like volume: listed in All and on its page with a badge, never in Trending, New, movers or Leaders */
  isWash: boolean;
  /** comma list of reasons: 24h, 7d, vol_tvl, symmetric */
  washReason: string | null;
```
`RowView`: `isWash: boolean;` after `isNew`. `present.ts` `toRow`: `isWash: p.isWash,` after `isNew: p.isNew,`.
- [ ] **Step 2: Queries.** `queries.ts`:
  - line 46 → `const EMPTY_USAGE: Usage = { vol24h: null, vol24hRolling: null, tx24h: null, wallets24h: null, walletsExact: null, buys24h: null, sells24h: null, buyers24h: null, sellers24h: null, smartWallets: null };`
  - `getProject`: `if (!detail || detail.isSpam) return null;` gains the comment `// spam 404s; a wash-like project keeps its page (Detail shows why)`.
  - `getLeaders` first line → `const all = (await loadAll()).filter((p) => !p.isSpam && !p.isWash); // wash-like volume never ranks`.
  - Delete `type DayUsageRow`; `StatsUsageRow` → `type StatsUsageRow = { project_id: number; rolling: unknown; buys: unknown; sells: unknown; buyers: unknown; sellers: unknown };`.
  - `loadFromDb`: replace `const prev1 = latest ? shiftDay(latest, -1) : null;` with
```ts
  // The run on `latest` reports closed UTC days through `end` (project_daily.window_end); the snapshot row for `latest` is
  // the running day and is never read.
  const end = latest ? shiftDay(latest, -1) : null;
```
    the destructuring → `const [daily, hookRows, signalRows, receiptRows, topPools, statsUsage, smartUsage] = await Promise.all([`; the top-pool query condition `latest ?` → `end ?` and `s.day = ${latest}` → `s.day = ${end}`; delete the whole `// 24h volume and txs from consecutive daily snapshots.` element (`latest && prev1 ? safe("usage:snapshots", ...) : null,`); in the `usage:pool_stats` SQL replace `sum(ps.traders_24h)::float8 as wallets,` with `sum(ps.vol_24h_rolling)::float8 as rolling,`; delete `const dayBy = ...` and `const du = dayBy?.get(p.id);`; `out.degraded = statsUsage === null || smartUsage === null;`; the `usage` object →
```ts
    const usage: Usage = {
      ...EMPTY_USAGE,
      // the closed day `end`, from score: one definition for the list, the page, the blurb and the digest
      vol24h: d?.vol24h ?? null,
      tx24h: d?.swaps24h ?? null,
      wallets24h: d?.wallets24h ?? null,
      walletsExact: d?.walletsExact ?? null,
      vol24hRolling: su ? num(su.rolling) : null,
      buys24h: su ? num(su.buys) : null,
      sells24h: su ? num(su.sells) : null,
      buyers24h: su ? num(su.buyers) : null,
      sellers24h: su ? num(su.sellers) : null,
      smartWallets: smartBy ? (smartBy.get(p.id) ?? 0) : null,
    };
```
    `swaps7d: d?.swaps7d ?? 0,` → `swaps7d: d?.swaps7d ?? null,`; after `isSpam: ...` add `isWash: d?.isWash ?? false,` and `washReason: d?.washReason ?? null,`.
  - `PoolRow`: `tx7d: unknown;` stays; `PoolStatRow` → `type PoolStatRow = { pool_id: string; chain_id: number; wallets_24h: unknown; traders_24h: unknown };`. In `loadPools` replace `const prev7 = ...` and `const prev1 = ...` with `const end = shiftDay(latest, -1);` and `const from7 = shiftDay(end, -6);`; the pools SQL →
```ts
    db.execute<PoolRow>(sql`
      select p.pool_id, p.chain_id, p.hook_address, p.token0, p.token1, p.fee, p.tick_spacing, p.created_at,
             s.tvl_usd,
             -- closed UTC days: 7d = from7..end, 24h = end. No row reads as 0; NULL cumulative (unknown on-chain volume) stays NULL
             case when s.pool_id is not null and s.volume_usd_cum is null then null else coalesce(w.vol7d, 0) end as vol7d,
             w.tx7d,
             case when s.volume_usd_cum is null then null else s.volume_usd_day end as vol24
      from ${t.pools} p
      join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
      left join ${t.poolSnapshots} s on s.pool_id = p.pool_id and s.chain_id = p.chain_id and s.day = ${end}
      left join lateral (
        select sum(x.volume_usd_day) as vol7d,
               case when count(x.tx_count_day) = 7 then sum(x.tx_count_day) end as tx7d   -- a 7-day count only with all 7 days
        from ${t.poolSnapshots} x
        where x.pool_id = p.pool_id and x.chain_id = p.chain_id and x.day between ${from7} and ${end}
      ) w on true
      where h.project_id = ${projectId} and p.chain_id = any(${KNOWN_CHAINS})
      order by s.tvl_usd desc nulls last
      limit 50`),
```
    the stats SQL selects `ps.pool_id, ps.chain_id, ps.wallets_24h, ps.traders_24h`; in the mapping replace `const snapVol = num(r.vol24);` with `const vol24 = num(r.vol24);` and set
```ts
        tx7d: num(r.tx7d),
        vol24h: vol24 == null ? null : Math.max(0, vol24),
        // distinct when pool_stats has it (on-chain, or a trade fetch that saw the whole day); else buyers + sellers
        wallets24h: st ? (num(st.wallets_24h) ?? num(st.traders_24h)) : null,
        walletsExact: st != null && num(st.wallets_24h) != null,
```
  - `loadSeries`: doc → `/** Daily volume for the 14 closed UTC days ending the day before \`latest\`, oldest first. Days without snapshots are 0; a day whose volume is unknown in every pool (NULL daily volume) is null, a gap, never 0. */`; body →
```ts
  if (!latest) return [];
  const end = shiftDay(latest, -1);
  const from = shiftDay(end, -13);
  const rows = await db.execute<{ day: string; v: unknown }>(sql`
    select s.day::text as day,
           sum(case when s.volume_usd_day is null then null else greatest(s.volume_usd_day, 0) end)::float8 as v
    from ${t.poolSnapshots} s
    join ${t.pools} p on p.pool_id = s.pool_id and p.chain_id = s.chain_id
    join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
    where h.project_id = ${projectId} and s.day >= ${from} and s.day <= ${end}
    group by s.day
    order by s.day`);
  const byDay = new Map(rowsOf<{ day: string; v: unknown }>(rows).map((r) => [String(r.day).slice(0, 10), num(r.v)]));
  if (byDay.size < 2) return [];
  const out: (number | null)[] = [];
  for (let i = 13; i >= 0; i--) {
    const day = shiftDay(end, -i);
    out.push(byDay.has(day) ? (byDay.get(day) ?? null) : 0);
  }
  return out;
```
- [ ] **Step 3: Fixtures.** `sample.ts` `build()`: in the pool object add `walletsExact: true,` after `wallets24h: t.w,`; in `usage` add `vol24hRolling: raw.vol24,` and `walletsExact: true,`; after `isSpam: false,` add `isWash: false,` and `washReason: null,`.
- [ ] **Step 4: Verify.** `cd web && npm test && npx tsc --noEmit && npm run build` → tests unchanged in number and passing, no type errors, build ok. Postgres smoke (SQL the unit tests cannot reach). Seed, from `pipeline/`:
```bash
PG_TEST_DIR=/tmp/bait-pg-p8 PG_TEST_PORT=5560 eval "$(scripts/pg-test-cluster.sh start)" && .venv/bin/python - <<'PY'
import datetime as dt, os
import sqlalchemy as sa
from bait import db
e = db.make_engine(os.environ["TEST_DATABASE_URL"]); db.metadata.drop_all(e); db.create_all(e)
D = dt.date(2026, 10, 6); E = D - dt.timedelta(days=1); now = dt.datetime(2026, 10, 6, 6, tzinfo=dt.timezone.utc)
hook, pool = "0x" + "a" * 40, "0x" + "b" * 64
with e.begin() as c:
    pid = c.execute(sa.insert(db.projects).values(slug="smoke", name="Smoke", first_seen_at=now - dt.timedelta(days=60))).inserted_primary_key[0]
    c.execute(sa.insert(db.hooks).values(address=hook, chain_id=8453, name="Smoke", project_id=pid, first_seen_at=now, flags={}, properties={}))
    c.execute(sa.insert(db.pools).values(pool_id=pool, chain_id=8453, hook_address=hook, token0={"id": "0x0", "symbol": "ETH"},
                                         token1={"id": "0x1", "symbol": "USDC"}, fee=3000, tick_spacing=60, created_at=now - dt.timedelta(days=60)))
    for i in range(14):
        c.execute(sa.insert(db.pool_snapshots).values(pool_id=pool, chain_id=8453, day=E - dt.timedelta(days=i), tvl_usd=5000.0,
                                                      volume_usd_cum=1000.0 * (14 - i), volume_usd_day=1000.0, tx_count_day=10, estimated=False))
    c.execute(sa.insert(db.pool_stats).values(pool_id=pool, chain_id=8453, day=D, vol_24h=1500.0, vol_24h_rolling=1500.0,
                                              traders_24h=30, buyers_24h=20, sellers_24h=10))
    c.execute(sa.insert(db.project_daily).values(project_id=pid, day=D, window_end=E, tvl_usd=5000.0, vol_24h=1000.0, vol_7d=7000.0,
                                                 vol_7d_prev=7000.0, swaps_24h=10, swaps_7d=70, pools_n=1, score_trending=0.0,
                                                 wallets_24h=30, wallets_exact=False, is_wash=True, wash_reason="24h"))
print("seeded")
PY
```
then create `web/smoke-p8.ts` (never committed):
```ts
import { getLeaders, getProject, getRows } from "./src/db/queries";

(async () => {
  const p = await getProject("smoke");
  const x = p?.pools[0];
  console.log(JSON.stringify({
    usage: p && { vol24h: p.usage.vol24h, vol24hRolling: p.usage.vol24hRolling, tx24h: p.usage.tx24h, wallets24h: p.usage.wallets24h, walletsExact: p.usage.walletsExact },
    swaps7d: p?.metrics.swaps7d, isWash: p?.isWash, washReason: p?.washReason,
    pool: x && { vol24h: x.vol24h, vol7d: x.vol7d, tx7d: x.tx7d, wallets24h: x.wallets24h, walletsExact: x.walletsExact },
    series: p?.series, rows: (await getRows()).map((r) => [r.slug, r.isWash]), leaders: (await getLeaders()).volume.length,
  }));
  process.exit(0);
})();
```
and run `cd web && DATABASE_URL='postgres://postgres@localhost:5560/postgres?host=/tmp/bait-pg-p8' npx tsx smoke-p8.ts` → exactly
`{"usage":{"vol24h":1000,"vol24hRolling":1500,"tx24h":10,"wallets24h":30,"walletsExact":false},"swaps7d":70,"isWash":true,"washReason":"24h","pool":{"vol24h":1000,"vol7d":7000,"tx7d":70,"wallets24h":30,"walletsExact":false},"series":[1000,1000,1000,1000,1000,1000,1000,1000,1000,1000,1000,1000,1000,1000],"rows":[["smoke",true]],"leaders":0}`.
(If postgres.js cannot reach the socket: `pg_ctl -D /tmp/bait-pg-p8 -o "-p 5560 -k /tmp/bait-pg-p8 -c listen_addresses=localhost" restart` and use `postgres://postgres@localhost:5560/postgres`.) Then `rm web/smoke-p8.ts`; keep the cluster running for Task 13.
- [ ] **Step 5: Commit**
```bash
git add web/src/db/schema.ts web/src/lib/types.ts web/src/db/queries.ts web/src/lib/present.ts web/src/fixtures/sample.ts
git commit -m "web: read closed-day volume, swaps and wallets from project_daily, the rolling 24h separately; wash projects listed, never on Leaders"
```

### Task 12: Feed and presentation rules

**Files:** Modify `web/src/lib/feed.ts` (`weekDelta` doc, `FeedItem`, `trendingOrder`, `applyTab`, `movers`), `web/src/lib/present.ts` (imports, `pace`, `rowLabel`, new `walletsText`, `washLine`). Test `web/src/lib/ux.test.ts`.
**Interfaces:** `walletsText(n, exact) -> string` ("≤ 1,142" for an upper bound); `washLine(reason) -> string`; `pace(vol24hRolling, vol7d)` unchanged math, documented as rolling 24 h against the closed week's daily average. Wash rows never in Trending, New or movers.

- [ ] **Step 1: Failing tests** (append to `web/src/lib/ux.test.ts`)
```ts
import { applyTab, movers, weekDelta } from "./feed";
import { pace, walletsText, washLine } from "./present";
import type { RowView } from "./types";

const row = (over: Partial<RowView>): RowView => ({
  slug: "x", name: "X", whatLine: "", color: "#000", category: "other", secondaryCategories: [], chains: [8453],
  firstSeenAt: "2026-10-01T00:00:00Z", isNew: false, isWash: false, scoreTrending: 0, vol7d: 0, vol7dPrev: 0, vol24h: null,
  tvlUsd: 0, routable: "unknown", audited: false, hooks: [], ...over,
});

test("wash rows: in All with the rest, never in Trending, New or movers", () => {
  const rows = [
    row({ slug: "real", scoreTrending: 5, vol7d: 50_000, vol7dPrev: 20_000 }),
    row({ slug: "wash", isWash: true, scoreTrending: 9, vol7d: 9e8, isNew: true }),
    row({ slug: "fresh", isNew: true, vol7d: 2_000 }),
  ];
  assert.deepEqual(applyTab(rows, { tab: "trending" }).map((r) => r.slug), ["real"]);
  assert.deepEqual(applyTab(rows, { tab: "new" }).map((r) => r.slug), ["fresh"]);
  assert.deepEqual(applyTab(rows, { tab: "all" }).map((r) => r.slug), ["wash", "real", "fresh"]);
  assert.ok(!movers(rows).some((r) => r.slug === "wash"));
});
test("weekDelta compares two closed weeks: flat reads 0, a tiny prior week is no basis", () => {
  assert.equal(weekDelta({ vol7d: 70_000, vol7dPrev: 70_000 }), 0);
  assert.equal(weekDelta({ vol7d: 70_000, vol7dPrev: 999 }), null);
});
test("pace: rolling 24h against the closed week's daily average", () => {
  assert.deepEqual([pace(10_000, 70_000), pace(5_000, 70_000), pace(null, 70_000), pace(30_000, 70_000)], [100, 50, 0, 100]);
});
test("walletsText marks an upper bound", () => {
  assert.deepEqual([walletsText(1142, false), walletsText(30, true), walletsText(30, null), walletsText(null, false)], ["≤ 1,142", "30", "30", NONE]);
});
test("washLine names every reason", () => {
  assert.equal(washLine("24h,vol_tvl"), "Wash-like volume: $250K+ in a day through 25 or fewer wallets; 7-day volume over 300x its liquidity from fewer than 50 wallets.");
  assert.equal(washLine(null), "Wash-like volume.");
});
```
- [ ] **Step 2:** `cd web && npm test` → FAIL (`walletsText` and `washLine` are not exported; the wash row ranks first in Trending).
- [ ] **Step 3: Implement.** `feed.ts`: `weekDelta` doc → `/** Week-over-week change in percent: vol7d (the 7 closed UTC days ending the day before the run) against vol7dPrev (the 7 before), or null when the earlier week is under $1K. */`; `FeedItem` adds `"isWash"` to its `Pick`; `trendingOrder` filter → `.filter((p) => !p.isWash && p.vol7d >= TRENDING_MIN_VOL && p.scoreTrending > 0)`; New tab → `rows = all.filter((p) => p.isNew && !p.isWash).sort(...)`; `movers` filter → `.filter((p) => !p.isWash && ((p.isNew && p.vol7d > 0) || trending.has(p.slug)))`. `applyTab` doc → `/** Rows for a tab + filters. \`all\` must already exclude spam; wash-like rows are listed in All and In Uniswap app only. Adds the 1-based trending rank. */`. `present.ts`: add `int, NONE` to the `./format` import; `pace` doc → `/** The rolling 24 h (GeckoTerminal, at the run) against the daily average of the last 7 closed UTC days, 0..100. */`; `rowLabel`'s parameter type → `Pick<RowView, "name" | "category" | "chains" | "isNew" | "vol7d" | "vol7dPrev"> & { isWash?: boolean }` and its branch → `if (r.isWash) parts.push("wash-like volume"); else if (r.isNew) parts.push("new"); else if (d != null) ...`; append:
```ts
/** "1,142" for a distinct count, "≤ 1,142" for an upper bound (buyers + sellers, or pools added together). */
export function walletsText(n: number | null, exact: boolean | null): string {
  if (n == null) return NONE;
  return exact === false ? `≤ ${int(n)}` : int(n);
}

// Keep in step with pipeline/bait/rules.py (WASH_REASONS and the thresholds).
const WASH_TEXT: Record<string, string> = {
  "24h": "$250K+ in a day through 25 or fewer wallets",
  "7d": "$1M+ in 7 days through 10 or fewer wallets",
  vol_tvl: "7-day volume over 300x its liquidity from fewer than 50 wallets",
  symmetric: "its top wallets buy and sell in equal counts and carry most of the volume",
};

export function washLine(reason: string | null): string {
  const parts = (reason ?? "").split(",").map((r) => WASH_TEXT[r.trim()]).filter(Boolean);
  return parts.length ? `Wash-like volume: ${parts.join("; ")}.` : "Wash-like volume.";
}
```
- [ ] **Step 4:** `npm test && npx tsc --noEmit && npm run build` → +5 tests, all pass; no type errors; build ok.
- [ ] **Step 5: Commit**
```bash
git add web/src/lib/feed.ts web/src/lib/present.ts web/src/lib/ux.test.ts
git commit -m "web: wash rows never in Trending, New or movers; wallets text marks an upper bound; pace is the rolling 24h against the closed week"
```

### Task 13: Badge and honest labels in the components

**Files:** Modify `web/src/components/Detail.tsx` (imports, `stats`, badges, stat render, `Bars` text, pools table, `events`), `web/src/components/ProjectList.tsx` (`Delta`), `web/src/components/TradePanel.tsx` (pace line, wallets), `web/src/app/leaders/page.tsx` (wallet labels).
**Interfaces:** Red `b-r` badge "Wash-like volume" with `washLine` as its title and as the first Activity event; "Swaps 24h" = `usage.tx24h`; wallet figures through `walletsText`; daily bars end yesterday; the pace line reads "Last 24h vs daily avg".

- [ ] **Step 1: Edit `Detail.tsx`.** Import `walletsText, washLine` from `@/lib/present`. Replace the `swaps` const and the `stats` array with:
```tsx
  const stats: Array<{ k: string; v: string; cls?: string; title?: string }> = [
    { k: "7d volume", v: usd(p.metrics.vol7d) },
    { k: "vs last week", v: d == null ? (p.isNew ? "new" : NONE) : fmtDelta(d), cls: d == null ? undefined : d >= 0 ? "up" : "down" },
    { k: "TVL", v: usd(p.metrics.tvlUsd) },
    { k: "Pools", v: int(p.metrics.poolsN || p.pools.length) },
    {
      k: "Wallets 24h",
      v: walletsText(u.wallets24h, u.walletsExact),
      title: u.walletsExact === false ? "Upper bound: buyers + sellers per pool, so a wallet that bought and sold counts twice." : undefined,
    },
    { k: "Swaps 24h", v: int(u.tx24h) },
  ];
```
(the stat for "Swaps 24h" no longer adds GeckoTerminal's rolling buys + sells: one definition, the closed day). In the badges `div`, first child: `{p.isWash && <span className="b b-r" title={washLine(p.washReason)}>Wash-like volume</span>}`. Stat render: `<div key={x.k} className={s.stat} title={x.title}>`. In `Bars`, the tooltip `n - 1 - i === 0 ? "Today" : \`${n - 1 - i}d ago\`` → `n - 1 - i === 0 ? "Yesterday" : \`${n - i}d ago\``, and the right-hand axis label `today` → `yesterday`. Pools table: `<td className="num">{int(x.wallets24h)}</td>` → `<td className="num">{walletsText(x.wallets24h, x.walletsExact)}</td>`; stacked row `{int(x.wallets24h)} wallets` → `{walletsText(x.wallets24h, x.walletsExact)} wallets`. In `events`, first statement after `const out: Ev[] = [];`: `if (p.isWash) out.push({ cls: "b-r", label: "Wash-like", text: washLine(p.washReason) });`; the "Top pool" text `from ${int(busy.wallets24h)} wallets` → `from ${walletsText(busy.wallets24h, busy.walletsExact)} wallets`.
- [ ] **Step 2: Edit the rest.** `ProjectList.tsx` `Delta`, first line: `if (r.isWash) return <span className="b b-r" title="Wash-like volume: listed here, never ranked.">Wash</span>;`. `TradePanel.tsx`: import `walletsText` with `pace`; `const share = pace(p.usage.vol24hRolling, p.metrics.vol7d);`; the label `Today vs daily avg` → `Last 24h vs daily avg`; its value `p.usage.vol24h == null ? NONE : \`${share}%\`` → `p.usage.vol24hRolling == null ? NONE : \`${share}%\``; the wallets span `${int(u.wallets24h)} wallets` → `${walletsText(u.wallets24h, u.walletsExact)} wallets`. `leaders/page.tsx`: totals label `Wallets 24h` → `Wallets 24h (upper bound)` (projects are added together); the board `note="24h"` → `note="24h, distinct where known"`.
- [ ] **Step 3: Verify.** `npm test && npx tsc --noEmit && npm run build` → pass. Page smoke against Task 11's seeded cluster:
```bash
cd web && export DATABASE_URL='postgres://postgres@localhost:5560/postgres?host=/tmp/bait-pg-p8'
npm run build && (npx next start -p 3999 > /tmp/bait-p8-web.log 2>&1 &)
for i in $(seq 30); do curl -sf localhost:3999/p/smoke > /tmp/bait-p8-smoke.html && break; sleep 1; done
grep -c 'Wash-like volume' /tmp/bait-p8-smoke.html; grep -c '≤ 30' /tmp/bait-p8-smoke.html; grep -c 'Last 24h vs daily avg' /tmp/bait-p8-smoke.html
curl -s localhost:3999/leaders | grep -c '>Smoke<'
pkill -f "next start -p 3999"; unset DATABASE_URL
```
→ three counts of at least 1 (badge and event; the "≤ 30" wallets stat; the pace label), then `0` (the wash project is not on Leaders). With Playwright browsers installed, `npm run e2e` → passes (fixtures carry no wash project). Stop the cluster: `cd ../pipeline && PG_TEST_DIR=/tmp/bait-pg-p8 scripts/pg-test-cluster.sh stop`.
- [ ] **Step 4: Commit**
```bash
git add web/src/components/Detail.tsx web/src/components/ProjectList.tsx web/src/components/TradePanel.tsx web/src/app/leaders/page.tsx
git commit -m "web: red wash-like badge with its reasons, upper-bound wallets marked, swaps 24h is the closed day, bars end yesterday, pace reads the rolling 24h"
```

### Task 14: Docs

**Files:** `DESIGN.md:55-58` (schema), `:78-84` (Scoring), `:180-181` (wash rule line in "Update 2026-10-08"), `:251-265` (Data issues backlog), `:278-287` ("Wash rules, status"); `pipeline/README.md:41-46` (steps table), `:113-122` (onchain item 3), `:133-139` (Scoring), `:202` (env); `web/README.md:51`.

- [ ] **Step 1: Edit.**
  - `DESIGN.md` schema block: `pool_snapshots(pool_id, chain_id, day, tvl_usd, volume_usd_day, tx_count_day, estimated, volume_usd_cum, tx_count_cum, ...)` and `project_daily(project_id, day, window_end, tvl_usd, vol_24h, vol_7d, vol_7d_prev, swaps_24h, swaps_7d, wallets_24h, wallets_exact, pools_n, score_trending, is_new, is_spam, is_wash, wash_reason, ...)`.
  - `DESIGN.md` Scoring: insert first a "Windows" paragraph with the table from this plan's "The window convention"; New → "swaps_7d >= 10 counted over the known days (a lower bound); swaps_7d is NULL unless every day is known"; Spam → "no pools, or all pools under $100 TVL and < 10 swaps lifetime -> hidden everywhere (404). Wash-like volume is not spam: see Wash rules."
  - `DESIGN.md:180-181` → "**Wash rules:** see "Wash rules (package 8)". Wash-like projects are excluded from Trending, New and Leaders, shown in All and on their page with a red badge and the reason."
  - Replace "## Wash rules, status 2026-10-09" with "## Wash rules (package 8, 2026-10-09)": the four reasons with thresholds (24h: >= $250K through <= 25 wallets; 7d: >= $1M through <= 10 distinct wallets on ready chains; vol_tvl: 7-day volume > 300x a known TVL with fewer than 50 wallets and >= $10K; symmetric: the top 10 wallets each with equal buys and sells carry > 50% of the rolling 24h, >= $10K), the corroboration (24h and 7d also need volume > 50x a known TVL or symmetric traders; Task 9), "wallets are a lower bound: distinct when known, else max(buyers, sellers)", the share rule (flagged pools carry >= 50% of the project's volume in that window), the live cases from Review Focus 5, and keep the dormant all-time and Robinhood lines.
  - Data issues backlog, add: "Keyless candle rows older than 15 days were overwritten with the old zero seed every night until package 8, and rows before 0012 carry no per-day values. Re-scoring 14 past days (package 9) needs a one-off GeckoTerminal OHLCV pull with limit 30 for the keyless pools first." and "Remove `volume_usd_cum` / `tx_count_cum` once package 9 has re-scored: windows read only the per-day columns."
  - `pipeline/README.md` steps table: snapshot writes "pool_snapshots(per UTC day: candles; pools past the OHLCV cap only yesterday, estimated; subgraph at day - 1), pool_stats(day) incl. vol_24h_rolling"; activity adds "pool_stats.wallets_24h when the fetch saw every trade of the day"; score reads "pool_snapshots (window_end-13..window_end), pool_stats(day), pool_top_wallets(day), pool_daily_onchain / pool_wallet_day (ready chains)" and writes "project_daily(day) incl. window_end, vol_24h, swaps, wallets, is_wash / wash_reason".
  - `pipeline/README.md` onchain item 3: the same three replacements as Task 4's docstring.
  - `pipeline/README.md` Scoring → the window convention in two sentences; "Volume windows sum `volume_usd_day` (a missing day adds $0; a pool whose cumulative is NULL is left out of every window). `swaps_24h` / `swaps_7d` are NULL unless every day is known. `wallets_24h` is distinct across the project's pools on ready chains (`wallets_exact`), else an upper bound. `python -m bait run --steps score --day <d>` reads stored rows only and is repeatable." Keep the `score_trending` formula line.
  - `pipeline/README.md:202` after `KEYLESS_OHLCV_CAP`: "(pools past it get yesterday's volume from the rolling 24h, marked `estimated`)".
  - `web/README.md:51`, replace the sentence about 24h volume and txs with: "24h volume, swaps and wallets are the closed UTC day before the latest run (`project_daily.vol_24h`, `swaps_24h`, `wallets_24h`; `wallets_exact = false` renders as "≤", an upper bound); 7d windows are the 7 closed days ending there. `project_daily.is_spam` hides a project everywhere; `is_wash` keeps it in All and on its page with a red badge, out of Trending, New, movers and Leaders."
- [ ] **Step 2: Final run.** Pipeline → **B+23 passed, S+1 skipped**; Postgres → **P+1 passed**; `cd web && npm test && npx tsc --noEmit && npm run build` → pass.
- [ ] **Step 3: Commit**
```bash
git add DESIGN.md pipeline/README.md web/README.md
git commit -m "docs: one window convention, per-day snapshot columns, honest swap and wallet counts, wash rules as their own flag"
```

## Final report (the implementer returns this)

For each task: commit SHA, test counts before and after, anything that deviated from the plan and why. Say whether Task 9 was kept. Then `git log --oneline origin/main..HEAD` and `git diff --stat origin/main..HEAD`. Do not push.

Rollout note for the orchestrator: deploy the pipeline first and let it add the columns (`python -m bait init-db`, or `scripts/apply-migrations.sh`, or the next cron run) BEFORE the web deploy. The web selects the new `project_daily` columns and renders empty (its `safe()` fallback) until they exist. The first night after the deploy: keyless pools inside the OHLCV cap and on-chain pools get a full 15-day per-day series; pools past the cap start with one day (yesterday), so their 7-day volume builds up over a week; keyless `swaps_7d` is NULL for 7 nights (on-chain projects get exact counts at once); subgraph pools, if `GRAPH_API_KEY` is ever set, get one 48-hour delta on their first night. Every existing project_daily row keeps its old `is_spam` (which still includes wash) until package 9 re-scores it.
