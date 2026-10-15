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
project_daily, signals, receipts, digests; on-chain indexer (0004): v4_pools, pool_daily_onchain, pool_wallet_day,
indexer_state; hook_pool_counts (0013); pool_lookup_queue (0015); historical_swap_evidence (0017).
See DESIGN.md "Postgres schema" for field meaning.

The historical evidence CLI requires an already migrated schema; even its preview does not bootstrap tables.
`rollback/0017_historical_evidence.sql` drops only that ledger and resets its migration marker, allowing the normal
migration runner to reapply it. Roll back dependent code and export needed evidence before applying the schema rollback.
It does not restore scored rows. See `../docs/runbooks/historical-evidence.md`.


Deploy order when a release adds columns the web reads (0016 and later): apply the migration to production first,
then merge. The web service's Railway healthcheck is `/api/ready`, which returns 503 until the required columns exist,
so a web build deployed against an unmigrated database fails its healthcheck and the previous build keeps serving; the
pipeline and indexer also add missing columns on their next start, but do not rely on that timing.
