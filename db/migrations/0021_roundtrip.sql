-- Volume of a pool's swaps that came and went inside one transaction: at least two swaps whose amounts net to under 1%
-- of their gross on both tokens (pipeline/bait/indexer/core.py roundtrip_usd). The score step's `roundtrip` wash rule
-- flags a pool whose week is at least half such volume. 0 on rows written before this; the indexer fills it going forward.
ALTER TABLE pool_daily_onchain ADD COLUMN IF NOT EXISTS roundtrip_usd double precision NOT NULL DEFAULT 0;
