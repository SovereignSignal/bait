-- Registry membership is a fact, not a default. NULL = not yet evaluated by ingest.
ALTER TABLE hooks ADD COLUMN IF NOT EXISTS hooklisted BOOLEAN;
-- flags for unlisted hooks are derived from the address bits by the pipeline (db.backfill_address_flags).
