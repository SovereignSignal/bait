-- Activity layer: per-pool 24h trade counts, top wallets per hooked pool, Nansen wallet labels.
-- Written by the pipeline "activity" step (and snapshot for the pool_stats columns); read by the web app.
-- The pipeline also applies this itself on every run: metadata.create_all for the new tables and
-- db.add_missing_columns() for the pool_stats columns (create_all never alters an existing table).
-- Addresses are stored lowercase.

ALTER TABLE pool_stats ADD COLUMN IF NOT EXISTS buys_24h    integer;
ALTER TABLE pool_stats ADD COLUMN IF NOT EXISTS sells_24h   integer;
ALTER TABLE pool_stats ADD COLUMN IF NOT EXISTS buyers_24h  integer;
ALTER TABLE pool_stats ADD COLUMN IF NOT EXISTS sellers_24h integer;

-- Top 20 wallets by USD volume per pool per day, from GeckoTerminal's last ~300 trades (24h). Kept 14 days.
CREATE TABLE IF NOT EXISTS pool_top_wallets (
    pool_id       text    NOT NULL,
    chain_id      integer NOT NULL,
    day           date    NOT NULL,
    address       text    NOT NULL,
    volume_usd    double precision,
    trades        integer,
    buys          integer,
    sells         integer,
    last_trade_at timestamptz,
    PRIMARY KEY (pool_id, chain_id, day, address)
);

-- Nansen labels per wallet. labels is a JSON list of strings; an empty list means "queried, no labels".
-- Re-queried only after NANSEN_RELABEL_DAYS (default 14).
CREATE TABLE IF NOT EXISTS wallet_labels (
    address     text        NOT NULL,
    chain_id    integer     NOT NULL,
    labels      jsonb,
    smart_money boolean     NOT NULL DEFAULT false,
    source      text,
    as_of       timestamptz NOT NULL,
    PRIMARY KEY (address, chain_id)
);
