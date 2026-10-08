-- On-chain v4 indexer (pipeline/bait/indexer, run as `python -m bait.indexer`, Railway service "indexer").
-- Written by the indexer; read by the nightly "onchain" step and the web app. All hex values lowercase.
-- The indexer and the pipeline also create these tables themselves (metadata.create_all), so applying this file
-- by hand is optional. Mirrored in pipeline/bait/db.py ("# --- indexer ---").

-- Hooked pools only (hooks != 0x0), one row per PoolManager Initialize log.
CREATE TABLE IF NOT EXISTS v4_pools (
    chain_id       integer NOT NULL,
    pool_id        text    NOT NULL,
    hook_address   text    NOT NULL,
    currency0      text    NOT NULL,
    currency1      text    NOT NULL,
    fee            integer,
    tick_spacing   integer,
    sqrt_price_x96 text,
    tick           integer,
    block_number   bigint,
    block_time     timestamptz,
    tx_hash        text,
    PRIMARY KEY (chain_id, pool_id)
);
CREATE INDEX IF NOT EXISTS v4_pools_hook_idx ON v4_pools (chain_id, hook_address);

-- Per pool per UTC day from Swap logs. wallets = count of pool_wallet_day rows for the key (unique tx.from).
-- volume_usd is approximate: each swap is valued at the token's current DefiLlama price, not the price at swap time.
CREATE TABLE IF NOT EXISTS pool_daily_onchain (
    chain_id   integer          NOT NULL,
    pool_id    text             NOT NULL,
    day        date             NOT NULL,
    swaps      integer          NOT NULL DEFAULT 0,
    wallets    integer          NOT NULL DEFAULT 0,
    volume_usd double precision NOT NULL DEFAULT 0,
    PRIMARY KEY (chain_id, pool_id, day)
);
CREATE INDEX IF NOT EXISTS pool_daily_onchain_day_idx ON pool_daily_onchain (day);

-- Swaps per wallet (transaction sender) per pool per day. Kept 30 days.
CREATE TABLE IF NOT EXISTS pool_wallet_day (
    chain_id integer NOT NULL,
    pool_id  text    NOT NULL,
    day      date    NOT NULL,
    address  text    NOT NULL,
    swaps    integer NOT NULL DEFAULT 0,
    PRIMARY KEY (chain_id, pool_id, day, address)
);
CREATE INDEX IF NOT EXISTS pool_wallet_day_day_idx ON pool_wallet_day (day);

-- Per-chain cursors: the NEXT block to scan for Initialize and Swap logs; head_block = last seen archive head.
-- backfilled = Initialize history from block 0 and the 14-day Swap window have both reached the head once.
CREATE TABLE IF NOT EXISTS indexer_state (
    chain_id         integer PRIMARY KEY,
    initialize_block bigint,
    swap_block       bigint,
    head_block       bigint,
    backfilled       boolean NOT NULL DEFAULT false,
    updated_at       timestamptz
);
