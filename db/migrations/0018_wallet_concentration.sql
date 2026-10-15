-- Wallet concentration over the seven-day window, from the indexer's exact per-wallet swap counts (pool_wallet_day).
-- NULL = unknown: a chain of the project is not ready, or its indexed history does not cover the window.
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS wallets_7d integer;
-- The busiest wallet's share of the project's swaps in the window (0..1). Of swaps, not volume: wallet volume is sampled.
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS top_wallet_share_7d double precision;
ALTER TABLE IF EXISTS project_daily_p9_backup ADD COLUMN IF NOT EXISTS wallets_7d integer;
ALTER TABLE IF EXISTS project_daily_p9_backup ADD COLUMN IF NOT EXISTS top_wallet_share_7d double precision;
