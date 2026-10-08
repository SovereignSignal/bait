-- Dev radar: hooks being built before they have volume (pipeline step "radar", web route /building).
-- Sources: Uniswap/hooklist issues + PRs (registry_submission), GitHub repo/code search (repo), Exa web results
-- classified by Claude Haiku (hackathon, incubator, audit, launch_post). One row per URL.
-- The pipeline also creates this table itself (metadata.create_all) on every run.
-- score: Claude relevance 0-1 for web results; rows under 0.5 are kept so the URL is not re-classified and are
-- hidden by the web app. GitHub rows have score null. Rows whose last_seen is older than 90 days are deleted.
-- hook_address is lowercase.

CREATE TABLE IF NOT EXISTS radar_items (
    url          text PRIMARY KEY,
    kind         text NOT NULL,
    title        text,
    summary      text,
    source       text,
    repo         text,
    stars        integer,
    chain_id     integer,
    hook_address text,
    project_slug text,
    published_at timestamptz,
    first_seen   timestamptz NOT NULL,
    last_seen    timestamptz NOT NULL,
    score        double precision
);

CREATE INDEX IF NOT EXISTS radar_items_kind_published_idx ON radar_items (kind, published_at);
