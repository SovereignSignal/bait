-- One row per nightly run: per-step counts, exit code and the deployed commit. Read by /api/health.
CREATE TABLE IF NOT EXISTS run_receipts (
  id           SERIAL PRIMARY KEY,
  day          DATE NOT NULL,
  started_at   TIMESTAMPTZ NOT NULL,
  finished_at  TIMESTAMPTZ NOT NULL,
  exit_code    INTEGER NOT NULL,
  soft_errors  INTEGER NOT NULL DEFAULT 0,
  steps        JSONB NOT NULL,
  code_sha     TEXT,
  notes        TEXT
);
CREATE INDEX IF NOT EXISTS run_receipts_finished_idx ON run_receipts (finished_at DESC);
