-- Verified-source contract names for unlisted hooks (pipeline group step, bait/steps/names.py).
-- The pipeline also adds these itself on every run (db.add_missing_columns).
ALTER TABLE hooks ADD COLUMN IF NOT EXISTS contract_name text;
ALTER TABLE hooks ADD COLUMN IF NOT EXISTS name_checked_at timestamptz;
