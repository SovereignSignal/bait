"""SQLAlchemy Core metadata mirroring db/migrations/0001_init.sql.

JSONB on Postgres, JSON on SQLite so tests run in memory. Upsert helper picks the dialect.
"""
from __future__ import annotations

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
    sa.Column("deployer", sa.Text),
    sa.Column("code_hash", sa.Text, index=True),
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
