-- Package 5 (indexer safety). Additive; also applied at startup by db.add_missing_columns / add_missing_indexes.
-- Block time the swap cursor has reached (a lower bound): the nightly freshness gate (steps/onchain.py).
ALTER TABLE indexer_state ADD COLUMN IF NOT EXISTS swap_block_time timestamptz;
-- Swaps with no priced side: their USD value is missing, not zero.
ALTER TABLE pool_daily_onchain ADD COLUMN IF NOT EXISTS unpriced_swaps integer NOT NULL DEFAULT 0;
-- Source of pool_stats.vol_24h: 'onchain' | 'hook' (custom accounting) | 'unpriced'. NULL = keyless / subgraph.
ALTER TABLE pool_stats ADD COLUMN IF NOT EXISTS vol_source text;
-- Nightly onchain step: highest v4_pools.block_number already copied into pools, per chain.
CREATE TABLE IF NOT EXISTS onchain_watermarks (chain_id integer PRIMARY KEY, pools_block bigint, updated_at timestamptz);
CREATE INDEX IF NOT EXISTS v4_pools_chain_block_idx ON v4_pools (chain_id, block_number, pool_id);
