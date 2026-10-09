-- Package 8 (windows, counts, wash). Additive; also applied at startup by db.add_missing_columns.
-- One window convention: the run on `day` reports closed UTC days. window_end = day - 1; "24h" = window_end;
-- "7d" = window_end-6 .. window_end; "prev 7d" = the 7 days before. Windows sum the per-day snapshot columns.
-- That UTC day's volume / swaps. NULL = unknown, or a row written before 0012.
ALTER TABLE pool_snapshots ADD COLUMN IF NOT EXISTS volume_usd_day double precision;
ALTER TABLE pool_snapshots ADD COLUMN IF NOT EXISTS tx_count_day bigint;
-- true = volume_usd_day from a rolling 24h number or a run-time cumulative; NULL = written before 0012.
ALTER TABLE pool_snapshots ADD COLUMN IF NOT EXISTS estimated boolean;
-- GeckoTerminal volume_usd.h24 at the run (a rolling window); the onchain step never overrides it.
ALTER TABLE pool_stats ADD COLUMN IF NOT EXISTS vol_24h_rolling double precision;
-- Distinct wallets in the row's window; NULL = unknown (traders_24h is buyers + sellers, an upper bound).
ALTER TABLE pool_stats ADD COLUMN IF NOT EXISTS wallets_24h integer;
-- vol_24h and the wallet counts cover the 24 hours before this instant.
ALTER TABLE pool_stats ADD COLUMN IF NOT EXISTS window_end timestamptz;
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS window_end date;
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS vol_24h double precision;
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS swaps_24h bigint;
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS wallets_24h integer;
-- true = distinct across the project's pools; false = an upper bound (pools added, or buyers + sellers).
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS wallets_exact boolean;
-- Wash-like volume, separate from spam: listed in All with a badge, never ranked.
-- Reasons: 24h, 7d, vol_tvl, no_liquidity, symmetric.
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS is_wash boolean NOT NULL DEFAULT false;
ALTER TABLE project_daily ADD COLUMN IF NOT EXISTS wash_reason text;
