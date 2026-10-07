-- hookfeed schema v1. Mirrors pipeline/hookfeed/db.py (SQLAlchemy Core metadata); keep both in sync.
BEGIN;

CREATE TABLE IF NOT EXISTS projects (
  id            SERIAL PRIMARY KEY,
  slug          TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  norm_name     TEXT,                       -- grouping key (rule 1)
  category      TEXT,                       -- dynamic-fee | launchpad | orders | lp-management | compliance | oracle | mev-lvr | lending | gaming-nft | other
  what_line     TEXT,
  why_blurb     TEXT,
  website       TEXT,
  github        TEXT,
  x_handle      TEXT,
  enrichment    JSONB,
  enriched_at   TIMESTAMPTZ,
  blurb_at      TIMESTAMPTZ,
  first_seen_at TIMESTAMPTZ NOT NULL,
  is_factory    BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS projects_norm_name_idx ON projects (norm_name);

CREATE TABLE IF NOT EXISTS hooks (
  address         TEXT NOT NULL,            -- lowercase hex
  chain_id        INTEGER NOT NULL,
  name            TEXT,
  description     TEXT,
  flags           JSONB,
  properties      JSONB,
  verified_source BOOLEAN,
  audit_url       TEXT,
  deployer        TEXT,
  code_hash       TEXT,                     -- sha256 of deployed bytecode
  project_id      INTEGER REFERENCES projects (id),
  first_seen_at   TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (address, chain_id)
);
CREATE INDEX IF NOT EXISTS hooks_project_id_idx ON hooks (project_id);
CREATE INDEX IF NOT EXISTS hooks_code_hash_idx ON hooks (code_hash);

CREATE TABLE IF NOT EXISTS pools (
  pool_id      TEXT NOT NULL,               -- bytes32 poolId hex
  chain_id     INTEGER NOT NULL,
  hook_address TEXT NOT NULL,
  token0       JSONB,                       -- {id, symbol, name, decimals}
  token1       JSONB,
  fee          INTEGER,                     -- LP fee; 8388608 (0x800000) = dynamic
  tick_spacing INTEGER,
  created_at   TIMESTAMPTZ,
  PRIMARY KEY (pool_id, chain_id)
);
CREATE INDEX IF NOT EXISTS pools_hook_idx ON pools (chain_id, hook_address);

CREATE TABLE IF NOT EXISTS pool_snapshots (
  pool_id        TEXT NOT NULL,
  chain_id       INTEGER NOT NULL,
  day            DATE NOT NULL,
  tvl_usd        DOUBLE PRECISION,
  volume_usd_cum DOUBLE PRECISION,
  tx_count_cum   BIGINT,                    -- swaps + liquidity mods ("txs")
  PRIMARY KEY (pool_id, chain_id, day)
);
CREATE INDEX IF NOT EXISTS pool_snapshots_day_idx ON pool_snapshots (day);

CREATE TABLE IF NOT EXISTS project_daily (
  project_id     INTEGER NOT NULL REFERENCES projects (id),
  day            DATE NOT NULL,
  tvl_usd        DOUBLE PRECISION,
  vol_7d         DOUBLE PRECISION,
  vol_7d_prev    DOUBLE PRECISION,
  swaps_7d       BIGINT,
  pools_n        INTEGER,
  score_trending DOUBLE PRECISION,          -- 0 when vol_7d < $10k
  is_new         BOOLEAN NOT NULL DEFAULT FALSE,
  is_spam        BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (project_id, day)
);
CREATE INDEX IF NOT EXISTS project_daily_day_score_idx ON project_daily (day, score_trending);

CREATE TABLE IF NOT EXISTS signals (
  project_id INTEGER NOT NULL REFERENCES projects (id),
  kind       TEXT NOT NULL,                 -- nansen | github | exa | alive
  value      JSONB,
  as_of      TIMESTAMPTZ NOT NULL,          -- last time value changed
  PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS receipts (
  project_id   INTEGER NOT NULL REFERENCES projects (id),
  url          TEXT NOT NULL,
  title        TEXT,
  source       TEXT,                        -- domain
  published_at TIMESTAMPTZ,
  PRIMARY KEY (project_id, url)
);

CREATE TABLE IF NOT EXISTS digests (
  week_of DATE PRIMARY KEY,                 -- the Monday
  body    JSONB NOT NULL
);

COMMIT;
