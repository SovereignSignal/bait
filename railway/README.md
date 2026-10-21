# Deploying Bait on Railway

Live setup (configured 2026-10-08 through the Railway CLI and GraphQL API):

| Service | Source | Settings |
|---|---|---|
| `bait` (web) | repo `SovereignSignal/bait`, branch `main` | Root Directory `/web`, Dockerfile `Dockerfile`, healthcheck `/api/ready` (60 s; 503 until the schema the build reads exists, so a deploy against an unmigrated database fails and the previous build keeps serving), restart on failure (5), watch `/web/**`, domain `bait-production-2706.up.railway.app` |
| `pipeline` | same repo | Root Directory `/pipeline`, Dockerfile `Dockerfile`, cron `0 6 * * *` (06:00 UTC), restart never, watch `/pipeline/**`. Start command is a wrapper: `python -m bait run` when the UTC hour is 05 to 07, otherwise `python -m bait repair all --since 14 --pools 2800 --stop-at 04:30 --yes`, so a manual `deploymentRestart` outside that window runs a repair, not the nightly (checked 2026-10-15) |
| `Postgres` | Railway database | none |

Variables: `DATABASE_URL=${{Postgres.DATABASE_URL}}` on both code services; `NEXT_PUBLIC_SITE_URL=https://${{RAILWAY_PUBLIC_DOMAIN}}` on `bait`.

Railway has deprecated config-as-code (`railway.json`), and setting a config file path is rejected, so these settings
live on the services. To recreate them from the CLI:

```sh
railway api 'mutation { serviceInstanceUpdate(serviceId: "<id>", environmentId: "<env>",
  input: { rootDirectory: "/web", dockerfilePath: "Dockerfile", healthcheckPath: "/api/ready", healthcheckTimeout: 60,
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
is caught up, and `indexer_state.backfilled` flips to true per chain (once `pool_lookup_queue` owes nothing from before the
block where the swap cursor reached the head; until then the indexer logs `backfill completes once the lookup queue ...`). A
chain more than 100K blocks behind also logs `swap catch-up: block A -> B (+N in P pages), L behind, lookup queue Q ids`
every loop. Optional: `INDEXER_CHAINS` (subset), `INDEXER_MAX_PAGES` (40 HyperSync pages per chain per loop).

Deploying the package-5 indexer over an older one: the indexer must be stopped before this deploy (the orchestrator
removes the running deployment first). Otherwise the old process (no lock, no compare-and-set) runs next to the new one,
and every page the new process wins is duplicated until the old process exits. Expect the first start to block
`v4_pools` inserts briefly while it builds `v4_pools_chain_block_idx`.

Schema changes reach production through the indexer: it runs `add_missing_columns` on start and redeploys on every
`pipeline/bait/db.py` change, so new columns (package 8: `0012_windows.sql`) appear about a minute after a merge. In that
minute the web can serve empty lists (its `safe()` fallback, not cached) and recovers on its own.


## Trimming `v4_pools` (package 5b, one time)

Deploy the package-5b indexer first: on start it creates `hook_pool_counts` and adds `v4_pools.sampled` and
`hooks.pools_total` (constant defaults, so no table rewrite), then caps new pools per hook. Existing excess is then trimmed
by `python -m bait.indexer prune-pools`, run as a temporary start command on the `indexer` service (same image, same
`DATABASE_URL`; `--keep` defaults to `INDEXER_POOLS_PER_HOOK`, 2000). Set the restart policy to `NEVER` while it is
overridden: with `ALWAYS` the one-shot command would run again every time it exits. Indexing pauses while the override is
deployed; the old container may still hold a chain's lock for a few seconds during the swap, which `--lock-wait` (600 s)
covers. The nightly `pipeline` (06:00 UTC) must not run while the prune does: its copy can re-create a `pools` row for a
row the prune is deleting. Check that no `pipeline` run is in progress, and finish before 06:00 UTC.

```sh
# 1. dry run: per-hook lines "prune-pools dry run chain_id= hook= rows= keep= delete= pools_rows= est=" and a total
railway api 'mutation { serviceInstanceUpdate(serviceId: "<indexer id>", environmentId: "<env>",
  input: { startCommand: "python -m bait.indexer prune-pools --dry-run", restartPolicyType: NEVER }) }'
railway api 'mutation { serviceInstanceDeployV2(serviceId: "<indexer id>", environmentId: "<env>") }'
railway logs --service indexer      # read the dry run; nothing was changed

# 2. prune: 5,000 rows per transaction per chain under bait-indexer:<chain_id>; logs pg_database_size before and after
railway api 'mutation { serviceInstanceUpdate(serviceId: "<indexer id>", environmentId: "<env>",
  input: { startCommand: "python -m bait.indexer prune-pools --yes", restartPolicyType: NEVER }) }'
railway api 'mutation { serviceInstanceDeployV2(serviceId: "<indexer id>", environmentId: "<env>") }'
railway logs --service indexer      # ends with "prune-pools done: ... database <before> -> <after>"; exit 3 = a chain was
                                    # locked, exit 4 = stopped early by SIGTERM (rerun --yes)

# 3. back to the indexer
railway api 'mutation { serviceInstanceUpdate(serviceId: "<indexer id>", environmentId: "<env>",
  input: { startCommand: "python -m bait.indexer", restartPolicyType: ALWAYS }) }'
railway api 'mutation { serviceInstanceDeployV2(serviceId: "<indexer id>", environmentId: "<env>") }'
```

The prune does not VACUUM. Railway Postgres autovacuum marks the deleted rows' space reusable, so the database stops
growing into new disk until that space is used again, but `pg_database_size` and the volume do not shrink on their own;
giving the space back to the volume needs `VACUUM FULL v4_pools, pools` (an exclusive lock for its duration) or a
dump and restore. The deletes write WAL as they go; it is recycled at checkpoints (bounded by `max_wal_size`), so watch the
volume during the run rather than expecting it to fall. Rerunnable: a second `--yes` right away finds nothing to delete. Later, it can delete a few rows the first run kept: the
newest-N window per hook moves as sampled rows arrive. SIGTERM (a redeploy) stops it between batches.

## One-off: key every hooked pool on Base (0022)

Base reports `partial` while ~80K pool ids owe swaps to `pool_lookup_queue`: each unknown id on a swap page costs a
HyperSync scan, and on the free tier the queue never drains. `v4_pool_keys` ends that: once every hooked pool below the
Initialize cursor has a key or a row, a swap page decides from Postgres and queues nothing. The live Initialize phase
keys new pools itself; history needs one walk of Base's Initialize logs (~9M hooked pools, 1 to 2 GB of rows):

1. Get a second Envio token for the scan and set `HYPERSYNC_API_TOKEN_SCAN` on the `indexer` service (the live indexer
   keeps `HYPERSYNC_API_TOKEN`; the free tier is ~20 queries a minute and the indexer needs all of it).
2. Deploy a release with 0022 first; the indexer creates the table and columns on start.
3. Add a temporary service from the same image (or reuse `pipeline` outside 05:00 to 08:00 UTC) with start command
   `python -m bait.indexer scan-pool-keys --chains base --dry-run`, read the log line (pages, hooked pools seen), then
   switch to `--yes --stop-at 04:30`. It is resumable (`indexer_state.keys_block`); redeploy as often as needed.
4. When the log says `complete=True`, remove the temporary service or start command. The next heartbeat shows
   `keyed=N` for Base and the queue stops growing; `pool_lookup_queue` rows settle as before and `backfilled` flips once
   none owe swaps. Delete `HYPERSYNC_API_TOKEN_SCAN` afterwards.

If Base's `v4_pools` is pruned again later (`prune-pools`), rerun the scan: a pruned row has no key.

## One-off repairs (package 9)

Repairs run inside the `pipeline` service (Postgres has no public endpoint): set its start command to the repair
command, then restart the service's deployment (`deploymentRestart`), which runs the start command at once (checked
2026-10-09). Wait for the process to exit, then clear the start command so the image `CMD` (`python -m bait run`) is
back. Start with `python -m bait repair all --since 14 --dry-run`; paid steps (`repair prose`) run only after the
owner's go. See `pipeline/README.md` "Repair".

- A repair still running at 06:00 UTC makes Railway skip that night's cron run. Repairs stop at `--stop-at` (05:30 by
  default) and refuse to start between it and 06:30, but check the deployment has exited before 06:00.
- Revert any `SPEND_DAILY_USD` / `SPEND_MONTHLY_USD` override made for a repair, and clear the start command, before
  06:00 UTC, or the nightly runs with them.
