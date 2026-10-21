-- Every hooked pool's key, stored as a v4_pools row or not (pipeline/bait/db.py v4_pool_keys). A swap of a pool id that is
-- in neither table is a swap of an unhooked pool: no HyperSync lookup, nothing queued. The indexer's Initialize phase
-- writes keys for the pools it does not store; `python -m bait.indexer scan-pool-keys --chains base --yes` backfills the
-- rest and sets indexer_state.keys_complete, which turns the shortcut on for that chain. The services also create this
-- on start (create_all + ADDITIVE_COLUMNS); the file is here so the migration log is complete.
CREATE TABLE IF NOT EXISTS v4_pool_keys (
    chain_id integer NOT NULL,
    pool_id text NOT NULL,
    hook_address text NOT NULL,
    currency0 text NOT NULL,
    currency1 text NOT NULL,
    fee integer,
    tick_spacing integer,
    block_number bigint,
    block_time timestamptz,
    PRIMARY KEY (chain_id, pool_id)
);
ALTER TABLE indexer_state ADD COLUMN IF NOT EXISTS keys_block bigint;
ALTER TABLE indexer_state ADD COLUMN IF NOT EXISTS keys_complete boolean NOT NULL DEFAULT false;
