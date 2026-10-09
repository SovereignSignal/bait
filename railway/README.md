# Deploying Bait on Railway

Live setup (configured 2026-10-08 through the Railway CLI and GraphQL API):

| Service | Source | Settings |
|---|---|---|
| `bait` (web) | repo `SovereignSignal/bait`, branch `main` | Root Directory `/web`, Dockerfile `Dockerfile`, healthcheck `/` (120 s), restart on failure (5), watch `/web/**`, domain `bait-production-2706.up.railway.app` |
| `pipeline` | same repo | Root Directory `/pipeline`, Dockerfile `Dockerfile`, cron `0 6 * * *` (06:00 UTC), restart never, watch `/pipeline/**` |
| `Postgres` | Railway database | none |

Variables: `DATABASE_URL=${{Postgres.DATABASE_URL}}` on both code services; `NEXT_PUBLIC_SITE_URL=https://${{RAILWAY_PUBLIC_DOMAIN}}` on `bait`.

Railway has deprecated config-as-code (`railway.json`), and setting a config file path is rejected, so these settings
live on the services. To recreate them from the CLI:

```sh
railway api 'mutation { serviceInstanceUpdate(serviceId: "<id>", environmentId: "<env>",
  input: { rootDirectory: "/web", dockerfilePath: "Dockerfile", healthcheckPath: "/", healthcheckTimeout: 120,
           restartPolicyType: ON_FAILURE, restartPolicyMaxRetries: 5, watchPatterns: ["/web/**"] }) }'
railway api 'mutation { serviceInstanceUpdate(serviceId: "<id>", environmentId: "<env>",
  input: { rootDirectory: "/pipeline", dockerfilePath: "Dockerfile", cronSchedule: "0 6 * * *",
           restartPolicyType: NEVER, watchPatterns: ["/pipeline/**"] }) }'
railway api 'mutation { serviceInstanceDeployV2(serviceId: "<id>", environmentId: "<env>") }'   # deploy latest main
```

Gotchas found on the first deploy:
- A service created from the repo deploys at once, before a root directory is set; Railpack then fails with
  "could not determine how to build the app". Set the root directory, then deploy again.
- `railway add --repo` failed with "You do not have access to this resource"; `serviceCreate` through
  `railway api` with `source: { repo: "SovereignSignal/bait" }` worked.
- There is no Dockerfile builder enum; Railway uses the Dockerfile in the root directory automatically.

No API keys are required. Chains without `GRAPH_API_KEY` use the keyless source (GeckoTerminal + public RPC), about
35 minutes per run because of the GeckoTerminal free-tier limit. Tables are created on first run.

Optional keys, each one switches on a layer: `GRAPH_API_KEY` (subgraph usage, faster nightly run), `ANTHROPIC_API_KEY`
(what-it-does and why-it-matters blurbs), `EXA_API_KEY` (receipt links),
`GITHUB_TOKEN`, `PARALLEL_API_KEY`. Tuning: `KEYLESS_PAGES` (4), `KEYLESS_OHLCV_CAP` (70), `KEYLESS_SPACING` (10 s),
`KEYLESS=0` to disable the keyless source.

## Adding the `indexer` service (on-chain v4 indexer)

The `indexer` service exists in the Railway project; the steps below are how to recreate it. `python -m bait.indexer` is a long-running process (one loop every `INDEXER_INTERVAL`, 60 s) that
writes `v4_pools`, `pool_daily_onchain`, `pool_wallet_day` and `indexer_state`; the nightly `pipeline` run copies them
into the feed tables (step `onchain`). Same image as `pipeline`, different start command, always on.

| Setting | Value |
|---|---|
| Source | repo `SovereignSignal/bait`, branch `main` |
| Root Directory | `/pipeline` |
| Dockerfile | `Dockerfile` (the pipeline one) |
| Start command | `python -m bait.indexer` (overrides the Dockerfile `CMD python -m bait run`) |
| Restart policy | `ALWAYS` |
| Cron | none |
| Watch paths | `/pipeline/**` |
| Variables | `DATABASE_URL=${{Postgres.DATABASE_URL}}`, `HYPERSYNC_API_TOKEN=<Envio token>` (set in the dashboard, never in git) |

```sh
railway api 'mutation { serviceCreate(input: { name: "indexer", projectId: "<project>", source: { repo: "SovereignSignal/bait" } }) { id } }'
railway api 'mutation { serviceInstanceUpdate(serviceId: "<id>", environmentId: "<env>",
  input: { rootDirectory: "/pipeline", dockerfilePath: "Dockerfile", startCommand: "python -m bait.indexer",
           restartPolicyType: ALWAYS, watchPatterns: ["/pipeline/**"] }) }'
railway variables --service indexer --set 'DATABASE_URL=${{Postgres.DATABASE_URL}}'
railway variables --service indexer --set "HYPERSYNC_API_TOKEN=$HYPERSYNC_API_TOKEN"   # from your shell, not a file
railway api 'mutation { serviceInstanceDeployV2(serviceId: "<id>", environmentId: "<env>") }'
```

As with the other services, the first deploy fires before the root directory is set and fails; deploy again after the
update. Tables are created by the indexer on start (`metadata.create_all`), or apply `db/migrations/0004_indexer.sql`.

Without `HYPERSYNC_API_TOKEN` the service still runs, in a degraded mode it logs at startup: new Initialize logs only, over
public RPCs on 10 chains, no history and no swaps. With the token it backfills every hooked pool from block 0 on 16
chains and the last 14 days of swaps, then follows the heads. Watch the logs for the per-loop line
`heartbeat loop=N mode=hypersync new_pools=K base:lag=<initialize>/<swap>,new=..,swaps=..`; lags fall to near 0 once a chain
is caught up, and `indexer_state.backfilled` flips to true per chain. Optional: `INDEXER_CHAINS` (subset),
`INDEXER_MAX_PAGES` (40 HyperSync pages per chain per loop).

Deploying the package-5 indexer over an older one: the indexer must be stopped before this deploy (the orchestrator
removes the running deployment first). Otherwise the old process (no lock, no compare-and-set) runs next to the new one,
and every page the new process wins is duplicated until the old process exits. Expect the first start to block
`v4_pools` inserts briefly while it builds `v4_pools_chain_block_idx`.

Schema changes reach production through the indexer: it runs `add_missing_columns` on start and redeploys on every
`pipeline/bait/db.py` change, so new columns (package 8: `0012_windows.sql`) appear about a minute after a merge. In that
minute the web can serve empty lists (its `safe()` fallback, not cached) and recovers on its own.

