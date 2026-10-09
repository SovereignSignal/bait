CREATE INDEX IF NOT EXISTS v4_pools_block_time_idx ON v4_pools (block_time DESC NULLS LAST, block_number DESC);
CREATE INDEX IF NOT EXISTS pool_stats_day_idx ON pool_stats (day);
CREATE INDEX IF NOT EXISTS pool_top_wallets_day_idx ON pool_top_wallets (day);
