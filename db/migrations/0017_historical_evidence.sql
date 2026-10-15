-- Retained lower-bound evidence; independent of recent metrics and retained when a pool is pruned.
-- The importer validates current identity; scoring rechecks pool/hook membership before using a row.
CREATE TABLE IF NOT EXISTS historical_swap_evidence (
    chain_id integer NOT NULL,
    transaction_hash text NOT NULL,
    log_index integer NOT NULL,
    pool_id text NOT NULL,
    hook_address text NOT NULL,
    block_number bigint NOT NULL,
    block_hash text NOT NULL,
    block_timestamp timestamptz NOT NULL,
    event_kind text NOT NULL,
    positive boolean NOT NULL,
    usd_lower_bound numeric(38,18),
    evidence jsonb NOT NULL,
    evidence_sha256 text NOT NULL,
    bundle_sha256 text NOT NULL,
    imported_at timestamptz NOT NULL,
    PRIMARY KEY (chain_id, transaction_hash, log_index),
    CONSTRAINT historical_evidence_chain_positive CHECK (chain_id > 0),
    CONSTRAINT historical_evidence_log_nonnegative CHECK (log_index >= 0),
    CONSTRAINT historical_evidence_block_nonnegative CHECK (block_number >= 0),
    CONSTRAINT historical_evidence_usd_nonnegative CHECK (usd_lower_bound IS NULL OR usd_lower_bound >= 0),
    CONSTRAINT historical_evidence_priced_positive CHECK (usd_lower_bound IS NULL OR positive),
    CONSTRAINT historical_evidence_physical_log_unique UNIQUE (chain_id, block_hash, log_index)
);
CREATE INDEX IF NOT EXISTS historical_swap_evidence_pool_time_idx
    ON historical_swap_evidence (chain_id, pool_id, block_timestamp);
