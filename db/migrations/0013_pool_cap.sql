-- Package 5b (per-hook pool cap). Additive; also applied at startup by db.create_all / db.add_missing_columns.
-- A pool inserted when it first swapped, after its hook had passed INDEXER_POOLS_PER_HOOK (indexer/core.py PoolResolver).
ALTER TABLE v4_pools ADD COLUMN IF NOT EXISTS sampled boolean NOT NULL DEFAULT false;
-- Hooks past the cap. pools_total = every pool initialized through the hook, stored in v4_pools or not.
-- first_block: oldest pool of the hook that is not a row (the lookup floor); last_block: newest Initialize block counted.
CREATE TABLE IF NOT EXISTS hook_pool_counts (
  chain_id     integer NOT NULL,
  hook_address text    NOT NULL,
  pools_total  bigint  NOT NULL DEFAULT 0,
  first_block  bigint,
  last_block   bigint,
  last_seen    timestamptz,
  PRIMARY KEY (chain_id, hook_address)
);
-- Every pool initialized through the hook, from the indexer (nightly step onchain). NULL = no indexer rows.
ALTER TABLE hooks ADD COLUMN IF NOT EXISTS pools_total bigint;
