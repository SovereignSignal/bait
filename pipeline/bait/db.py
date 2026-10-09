"""SQLAlchemy Core metadata mirroring db/migrations/0001_init.sql.

JSONB on Postgres, JSON on SQLite so tests run in memory. Upsert helper picks the dialect.
"""
from __future__ import annotations

import logging
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.types import JSON, TypeDecorator

from .config import database_url


class JSONType(TypeDecorator):
    """JSONB on Postgres, plain JSON elsewhere."""
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.JSONB())
        return dialect.type_descriptor(JSON())


TS = sa.DateTime(timezone=True)
metadata = sa.MetaData()

projects = sa.Table(
    "projects", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("slug", sa.Text, nullable=False, unique=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("norm_name", sa.Text, index=True),
    sa.Column("category", sa.Text),
    sa.Column("what_line", sa.Text),
    sa.Column("why_blurb", sa.Text),
    sa.Column("website", sa.Text),
    sa.Column("github", sa.Text),
    sa.Column("x_handle", sa.Text),
    sa.Column("enrichment", JSONType),
    sa.Column("enriched_at", TS),
    sa.Column("blurb_at", TS),
    sa.Column("first_seen_at", TS, nullable=False),
    sa.Column("is_factory", sa.Boolean, nullable=False, server_default=sa.false()),
)

hooks = sa.Table(
    "hooks", metadata,
    sa.Column("address", sa.Text, primary_key=True),
    sa.Column("chain_id", sa.Integer, primary_key=True),
    sa.Column("name", sa.Text),
    sa.Column("description", sa.Text),
    sa.Column("flags", JSONType),
    sa.Column("properties", JSONType),
    sa.Column("verified_source", sa.Boolean),
    sa.Column("audit_url", sa.Text),
    sa.Column("hooklisted", sa.Boolean),       # in the Uniswap hooklist as of the last ingest; NULL = not yet evaluated
    sa.Column("deployer", sa.Text),
    sa.Column("code_hash", sa.Text, index=True),
    sa.Column("contract_name", sa.Text),       # verified-source name (Sourcify/Blockscout/Routescan), for unlisted hooks
    sa.Column("name_checked_at", TS),          # last contract-name lookup; misses are retried after 14 days
    sa.Column("regroup", sa.Boolean, nullable=False, server_default=sa.false()),  # ingest named it / code_hash landed; group clears
    sa.Column("is_proxy", sa.Boolean),         # from the bytecode (EIP-1967 slot / EIP-1167 clone) or explorer; NULL = unknown
    sa.Column("proxy_kind", sa.Text),          # eip1167 | eip1967 | explorer | unknown; an eip1167 clone's hash is evidence
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), index=True),
    sa.Column("first_seen_at", TS, nullable=False),
)

pools = sa.Table(
    "pools", metadata,
    sa.Column("pool_id", sa.Text, primary_key=True),
    sa.Column("chain_id", sa.Integer, primary_key=True),
    sa.Column("hook_address", sa.Text, nullable=False),
    sa.Column("token0", JSONType),
    sa.Column("token1", JSONType),
    sa.Column("fee", sa.Integer),
    sa.Column("tick_spacing", sa.Integer),
    sa.Column("created_at", TS),
    sa.Index("pools_hook_idx", "chain_id", "hook_address"),
)

pool_snapshots = sa.Table(
    "pool_snapshots", metadata,
    sa.Column("pool_id", sa.Text, primary_key=True),
    sa.Column("chain_id", sa.Integer, primary_key=True),
    sa.Column("day", sa.Date, primary_key=True),
    sa.Column("tvl_usd", sa.Float),
    sa.Column("volume_usd_cum", sa.Float),
    sa.Column("tx_count_cum", sa.BigInteger),
    sa.Index("pool_snapshots_day_idx", "day"),
)

# Per-pool 24h activity from the keyless source; scoring reads it for the wash rule.
pool_stats = sa.Table(
    "pool_stats", metadata,
    sa.Column("pool_id", sa.Text, primary_key=True),
    sa.Column("chain_id", sa.Integer, primary_key=True),
    sa.Column("day", sa.Date, primary_key=True),
    sa.Column("vol_24h", sa.Float),
    sa.Column("traders_24h", sa.Integer),
    sa.Column("buys_24h", sa.Integer),
    sa.Column("sells_24h", sa.Integer),
    sa.Column("buyers_24h", sa.Integer),
    sa.Column("sellers_24h", sa.Integer),
)

# Who trades each hooked pool: top wallets by volume from the last ~300 trades (step "activity"). Kept 14 days.
pool_top_wallets = sa.Table(
    "pool_top_wallets", metadata,
    sa.Column("pool_id", sa.Text, primary_key=True),
    sa.Column("chain_id", sa.Integer, primary_key=True),
    sa.Column("day", sa.Date, primary_key=True),
    sa.Column("address", sa.Text, primary_key=True),
    sa.Column("volume_usd", sa.Float),
    sa.Column("trades", sa.Integer),
    sa.Column("buys", sa.Integer),
    sa.Column("sells", sa.Integer),
    sa.Column("last_trade_at", TS),
)

# Wallet labels from the retired Nansen lane (credits exhausted 2026-10-08). Frozen: the web still reads existing rows;
# the pipeline no longer writes here. Delete the table when the web stops reading it.
wallet_labels = sa.Table(
    "wallet_labels", metadata,
    sa.Column("address", sa.Text, primary_key=True),
    sa.Column("chain_id", sa.Integer, primary_key=True),
    sa.Column("labels", JSONType),
    sa.Column("smart_money", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("source", sa.Text),
    sa.Column("as_of", TS, nullable=False),
)

project_daily = sa.Table(
    "project_daily", metadata,
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), primary_key=True),
    sa.Column("day", sa.Date, primary_key=True),
    sa.Column("tvl_usd", sa.Float),
    sa.Column("vol_7d", sa.Float),
    sa.Column("vol_7d_prev", sa.Float),
    sa.Column("swaps_7d", sa.BigInteger),
    sa.Column("pools_n", sa.Integer),
    sa.Column("score_trending", sa.Float),
    sa.Column("is_new", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("is_spam", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Index("project_daily_day_score_idx", "day", "score_trending"),
)

signals = sa.Table(
    "signals", metadata,
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), primary_key=True),
    sa.Column("kind", sa.Text, primary_key=True),
    sa.Column("value", JSONType),
    sa.Column("as_of", TS, nullable=False),
)

receipts = sa.Table(
    "receipts", metadata,
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), primary_key=True),
    sa.Column("url", sa.Text, primary_key=True),
    sa.Column("title", sa.Text),
    sa.Column("source", sa.Text),
    sa.Column("published_at", TS),
)

digests = sa.Table(
    "digests", metadata,
    sa.Column("week_of", sa.Date, primary_key=True),
    sa.Column("body", JSONType, nullable=False),
)

# Old project slugs -> current project (db/migrations/0011_grouping.sql). Written by the group step on every rename,
# merge and regroup; the web redirects /p/<old_slug> through it.
project_aliases = sa.Table(
    "project_aliases", metadata,
    sa.Column("old_slug", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("created_at", TS, nullable=False),
    sa.Index("project_aliases_project_idx", "project_id"),
)

# --- spend ---
# One row per paid API call (Anthropic, Parallel, Exa). Written on its own connection by
# costs.Meter so it survives step rollbacks. Budgets in costs.py read it.
api_spend = sa.Table(
    "api_spend", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", TS, nullable=False, index=True),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("step", sa.Text),
    sa.Column("units", sa.Integer),
    sa.Column("usd", sa.Float, nullable=False),
    sa.Column("detail", sa.Text),
)
# --- /spend ---

# --- radar ---
# Dev radar (step "radar", db/migrations/0005_radar.sql): what is being built before it has volume. One row per URL.
# kind: registry_submission | repo | hackathon | incubator | audit | launch_post. score is the Claude relevance (0-1)
# for web results; rows under 0.5 are kept only so the URL is not re-classified and the web app hides them.
# GitHub rows have score null. Rows not seen for 90 days are deleted.
RADAR_KINDS = ("registry_submission", "repo", "hackathon", "incubator", "audit", "launch_post")

radar_items = sa.Table(
    "radar_items", metadata,
    sa.Column("url", sa.Text, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("title", sa.Text),
    sa.Column("summary", sa.Text),
    sa.Column("source", sa.Text),
    sa.Column("repo", sa.Text),
    sa.Column("stars", sa.Integer),
    sa.Column("chain_id", sa.Integer),
    sa.Column("hook_address", sa.Text),
    sa.Column("project_slug", sa.Text),
    sa.Column("published_at", TS),
    sa.Column("first_seen", TS, nullable=False),
    sa.Column("last_seen", TS, nullable=False),
    sa.Column("score", sa.Float),
    sa.Index("radar_items_kind_published_idx", "kind", "published_at"),
)
# --- /radar ---

# --- indexer ---
# On-chain v4 indexer (python -m bait.indexer, db/migrations/0004_indexer.sql). Hooked pools from PoolManager Initialize
# logs, per-pool daily swaps/wallets/volume from Swap logs (wallet = tx.from). All hex lowercase. pool_wallet_day is kept
# 30 days; pool_daily_onchain.wallets = count of its pool_wallet_day rows. Cursors in indexer_state are the NEXT block to
# scan (exclusive end of what is done). The nightly "onchain" step copies these into hooks/pools/pool_snapshots/pool_stats.
v4_pools = sa.Table(
    "v4_pools", metadata,
    sa.Column("chain_id", sa.Integer, primary_key=True),
    sa.Column("pool_id", sa.Text, primary_key=True),
    sa.Column("hook_address", sa.Text, nullable=False),
    sa.Column("currency0", sa.Text, nullable=False),
    sa.Column("currency1", sa.Text, nullable=False),
    sa.Column("fee", sa.Integer),
    sa.Column("tick_spacing", sa.Integer),
    sa.Column("sqrt_price_x96", sa.Text),
    sa.Column("tick", sa.Integer),
    sa.Column("block_number", sa.BigInteger),
    sa.Column("block_time", TS),
    sa.Column("tx_hash", sa.Text),
    sa.Index("v4_pools_hook_idx", "chain_id", "hook_address"),
)

pool_daily_onchain = sa.Table(
    "pool_daily_onchain", metadata,
    sa.Column("chain_id", sa.Integer, primary_key=True),
    sa.Column("pool_id", sa.Text, primary_key=True),
    sa.Column("day", sa.Date, primary_key=True),
    sa.Column("swaps", sa.Integer, nullable=False, server_default="0"),
    sa.Column("wallets", sa.Integer, nullable=False, server_default="0"),
    sa.Column("volume_usd", sa.Float, nullable=False, server_default="0"),
    sa.Index("pool_daily_onchain_day_idx", "day"),
)

pool_wallet_day = sa.Table(
    "pool_wallet_day", metadata,
    sa.Column("chain_id", sa.Integer, primary_key=True),
    sa.Column("pool_id", sa.Text, primary_key=True),
    sa.Column("day", sa.Date, primary_key=True),
    sa.Column("address", sa.Text, primary_key=True),
    sa.Column("swaps", sa.Integer, nullable=False, server_default="0"),
    sa.Index("pool_wallet_day_day_idx", "day"),
)

indexer_state = sa.Table(
    "indexer_state", metadata,
    sa.Column("chain_id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("initialize_block", sa.BigInteger),
    sa.Column("swap_block", sa.BigInteger),
    sa.Column("head_block", sa.BigInteger),
    sa.Column("backfilled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("updated_at", TS),
)
# --- /indexer ---

run_receipts = sa.Table(
    "run_receipts", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("day", sa.Date, nullable=False),
    sa.Column("started_at", TS, nullable=False),
    sa.Column("finished_at", TS, nullable=False),
    sa.Column("exit_code", sa.Integer, nullable=False),
    sa.Column("soft_errors", sa.Integer, nullable=False, server_default="0"),
    sa.Column("steps", JSONType, nullable=False),
    sa.Column("code_sha", sa.Text),
    sa.Column("notes", sa.Text),
)


def write_receipt(engine: sa.Engine, **fields) -> None:
    """One row per run, on its own connection, after everything else; a failed step must not lose it."""
    try:
        with engine.begin() as c:
            c.execute(sa.insert(run_receipts).values(**fields))
    except Exception:  # the receipt is for monitoring; never turn a finished run into a crash
        logging.getLogger("bait").exception("receipt write failed")


def make_engine(url: str | None = None) -> sa.Engine:
    url = url or database_url()
    kw: dict[str, Any] = {"future": True}
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url == "sqlite://":
            kw["poolclass"] = sa.pool.StaticPool
    return sa.create_engine(url, **kw)


def create_all(engine: sa.Engine) -> None:
    metadata.create_all(engine)


# Columns added to tables that already exist in production. create_all never alters an existing table, so each new
# column is listed here and added by add_missing_columns() right after create_all. (table, column, SQL type).
ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("pool_stats", "buys_24h", "integer"),
    ("pool_stats", "sells_24h", "integer"),
    ("pool_stats", "buyers_24h", "integer"),
    ("pool_stats", "sellers_24h", "integer"),
    ("hooks", "contract_name", "text"),
    ("hooks", "name_checked_at", "timestamptz"),
    ("hooks", "hooklisted", "boolean"),
    ("hooks", "regroup", "boolean NOT NULL DEFAULT false"),
    ("hooks", "is_proxy", "boolean"),
    ("hooks", "proxy_kind", "text"),
]


# Indexes the web's hot queries need on tables that already exist in production. (name, table, column SQL).
ADDITIVE_INDEXES: list[tuple[str, str, str]] = [
    ("v4_pools_block_time_idx", "v4_pools", "block_time DESC NULLS LAST, block_number DESC"),
    ("pool_stats_day_idx", "pool_stats", "day"),
    ("pool_top_wallets_day_idx", "pool_top_wallets", "day"),
]


def add_missing_indexes(engine: sa.Engine, indexes: Iterable[tuple[str, str, str]] = ADDITIVE_INDEXES) -> list[str]:
    """CREATE INDEX IF NOT EXISTS for each (both dialects support it). Tables that do not exist are skipped."""
    done: list[str] = []
    with engine.begin() as conn:
        insp = sa.inspect(conn)
        for name, table, cols in indexes:
            if not insp.has_table(table):
                continue
            if conn.dialect.name == "sqlite":
                cols = cols.replace(" NULLS LAST", "")  # SQLite has no NULLS LAST in index definitions
            conn.execute(sa.text(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({cols})'))
            done.append(name)
    return done


def backfill_address_flags(conn: sa.Connection) -> int:
    """Hooks discovered on-chain were stored with flags={}; derive them from the address bits. Idempotent."""
    from .rules import flags_from_address
    rows = conn.execute(sa.select(hooks.c.address, hooks.c.chain_id, hooks.c.flags)).all()
    todo = [r for r in rows if not r.flags]
    for r in todo:
        conn.execute(sa.update(hooks).values(flags=flags_from_address(r.address))
                     .where(hooks.c.address == r.address, hooks.c.chain_id == r.chain_id))
    return len(todo)


def add_missing_columns(engine: sa.Engine, columns: Iterable[tuple[str, str, str]] = ADDITIVE_COLUMNS) -> list[str]:
    """Idempotent additive migration. Postgres: ADD COLUMN IF NOT EXISTS. SQLite: PRAGMA table_info, then ALTER only
    the missing ones. Tables that do not exist are skipped (create_all owns them). Returns "table.column" added
    (on Postgres: attempted)."""
    added: list[str] = []
    with engine.begin() as conn:
        insp = sa.inspect(conn)
        for table, column, sqltype in columns:
            if not insp.has_table(table):
                continue
            if conn.dialect.name == "postgresql":
                conn.execute(sa.text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column}" {sqltype}'))
                added.append(f"{table}.{column}")
                continue
            have = {r[1] for r in conn.execute(sa.text(f'PRAGMA table_info("{table}")'))}
            if column not in have:
                conn.execute(sa.text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sqltype}'))
                added.append(f"{table}.{column}")
    return added


def savepoint(conn: sa.Connection):
    """Per-item isolation inside a step transaction. Postgres aborts the whole transaction on the first
    statement error, so each item's writes run in a SAVEPOINT that rolls back alone. SQLite: no-op (pysqlite's
    savepoint support is unreliable and the test suite runs there)."""
    return conn.begin_nested() if conn.dialect.name == "postgresql" else nullcontext()


RUN_LOCK_KEY = "bait-run"


def try_run_lock(engine: sa.Engine) -> sa.Connection | None:
    """One nightly run at a time. Postgres: session-level advisory lock held by the returned connection; close it
    to release. Returns None when another runner holds it. Other dialects: no lock, returns an open connection.

    The lock connection comes from a throwaway NullPool engine: a pooled connection would go back to the pool on
    close() with its session (and so the lock) still alive."""
    if engine.dialect.name != "postgresql":
        return engine.connect()
    conn = sa.create_engine(engine.url, poolclass=sa.pool.NullPool, future=True).connect()
    got = conn.execute(sa.text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": RUN_LOCK_KEY}).scalar_one()
    if got:
        return conn
    conn.close()
    return None


def upsert(conn: sa.Connection, table: sa.Table, rows: Sequence[dict], keys: Iterable[str],
           update: Iterable[str] | None = None) -> int:
    """INSERT ... ON CONFLICT (keys) DO UPDATE SET <update cols>. update=None means all non-key cols.
    Empty update -> DO NOTHING. Returns number of rows sent."""
    if not rows:
        return 0
    keys = list(keys)
    ins = postgresql.insert if conn.dialect.name == "postgresql" else sqlite.insert
    if update is None:
        update = [c for c in rows[0].keys() if c not in keys]
    update = list(update)
    stmt = ins(table).values(rows)
    if update:
        stmt = stmt.on_conflict_do_update(
            index_elements=keys, set_={c: getattr(stmt.excluded, c) for c in update})
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=keys)
    conn.execute(stmt)
    return len(rows)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; treat naive as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
