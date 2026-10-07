# db

SQL migrations for hookfeed Postgres. Plain files, applied in order.

```sh
psql "$DATABASE_URL" -f db/migrations/0001_init.sql
```

Each migration is idempotent (`IF NOT EXISTS`) so re-applying is safe. New changes go in a new
`000N_*.sql`; never edit an applied file.

`pipeline/hookfeed/db.py` mirrors this DDL as SQLAlchemy Core metadata (JSONB on Postgres, JSON on
SQLite for tests). When you change one, change the other. For local SQLite dev the pipeline can
create the tables itself: `python -m hookfeed init-db`.

Tables: projects, hooks, pools, pool_snapshots, project_daily, signals, receipts, digests.
See DESIGN.md "Postgres schema" for field meaning.
