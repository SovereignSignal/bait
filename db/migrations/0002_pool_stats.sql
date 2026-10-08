-- Per-pool 24h activity from the keyless source (GeckoTerminal); scoring reads it for the wash rule.
-- The pipeline also creates this table itself (metadata.create_all) on every run.
CREATE TABLE IF NOT EXISTS pool_stats (
    pool_id     text    NOT NULL,
    chain_id    integer NOT NULL,
    day         date    NOT NULL,
    vol_24h     double precision,
    traders_24h integer,
    PRIMARY KEY (pool_id, chain_id, day)
);
