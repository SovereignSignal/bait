-- Deployer attribution. hooks.deployer existed (registry-supplied); the explorer's contract creator now fills it for
-- unlisted hooks, with provenance and a retry stamp. The share is of the project's swaps in the seven-day window made
-- by its known deployer wallets (CREATE2 factories excluded); NULL = no deployer known or coverage unknown.
ALTER TABLE hooks ADD COLUMN IF NOT EXISTS deployer_source text;
ALTER TABLE hooks ADD COLUMN IF NOT EXISTS deployer_checked_at timestamptz;
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS deployer_swap_share_7d double precision;
ALTER TABLE IF EXISTS project_daily_p9_backup ADD COLUMN IF NOT EXISTS deployer_swap_share_7d double precision;
