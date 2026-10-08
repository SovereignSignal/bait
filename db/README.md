# db

SQL migrations for bait Postgres. Plain files, applied in order.

```sh
psql "$DATABASE_URL" -f db/migrations/0001_init.sql
```

Each migration is idempotent (`IF NOT EXISTS`) so re-applying is safe. New changes go in a new
`000N_*.sql`; never edit an applied file.

`pipeline/bait/db.py` mirrors this DDL as SQLAlchemy Core metadata (JSONB on Postgres, JSON on
SQLite for tests). When you change one, change the other. For local SQLite dev the pipeline can
create the tables itself: `python -m bait init-db`. Columns added to an existing table also go in
`db.ADDITIVE_COLUMNS` (applied by `db.add_missing_columns()` after `create_all` on every run), because
`create_all` never alters a table that already exists.

Tables: projects, hooks, pools, pool_snapshots, pool_stats, pool_top_wallets, wallet_labels,
project_daily, signals, receipts, digests.
See DESIGN.md "Postgres schema" for field meaning.
