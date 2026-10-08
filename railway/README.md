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
(what-it-does and why-it-matters blurbs), `EXA_API_KEY` (receipt links), `NANSEN_API_KEY` (smart money),
`GITHUB_TOKEN`, `PARALLEL_API_KEY`. Tuning: `KEYLESS_PAGES` (4), `KEYLESS_OHLCV_CAP` (70), `KEYLESS_SPACING` (10 s),
`KEYLESS=0` to disable the keyless source.
