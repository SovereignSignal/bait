-- Historical listing eligibility is separate from closed-day activity. Existing rows remain unclassified until scored.
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS has_historical_usage boolean NOT NULL DEFAULT false;
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS activity_status text;
-- A carried prior wash finding is labelled as historical, never presented as a finding about the current seven days.
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS wash_historical boolean NOT NULL DEFAULT false;
ALTER TABLE IF EXISTS project_daily_p9_backup ADD COLUMN IF NOT EXISTS has_historical_usage boolean NOT NULL DEFAULT false;
ALTER TABLE IF EXISTS project_daily_p9_backup ADD COLUMN IF NOT EXISTS activity_status text;
ALTER TABLE IF EXISTS project_daily_p9_backup ADD COLUMN IF NOT EXISTS wash_historical boolean NOT NULL DEFAULT false;
