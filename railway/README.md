# Deploying Bait (repo: hookfeed) on Railway

## Quick path from the GitHub repo

1. New project, then **Deploy from GitHub repo** and pick `hookfeed`. Railway creates one service; rename it `web`.
2. **+ New, Database, PostgreSQL.** Keep the service name `Postgres`.
3. **+ New, GitHub repo** again for the same repo; rename it `pipeline`.
4. For each code service: **Settings, Source, Root Directory** = `/web` or `/pipeline`, and **Settings, Config-as-code,
   Railway Config File** = `/web/railway.json` or `/pipeline/railway.json` (absolute path; Railway does not resolve it
   from the root directory).
5. Variables on both services: `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`. On `web` also
   `NEXT_PUBLIC_SITE_URL` = `https://${{RAILWAY_PUBLIC_DOMAIN}}`, then **Settings, Networking, Generate Domain**.
6. On `pipeline`, open **Deployments** and run it once by hand (cron is 06:00 UTC). No API keys are required: chains
   without `GRAPH_API_KEY` use the keyless source (GeckoTerminal + public RPC), about 35 minutes per run because of the
   GeckoTerminal free-tier limit. Tables are created on first run; no manual migration.
7. Reload the web domain once the pipeline run finishes.

Optional keys, each one switches on a layer: `GRAPH_API_KEY` (subgraph usage, faster nightly run), `ANTHROPIC_API_KEY`
(what-it-does and why-it-matters blurbs), `EXA_API_KEY` (receipt links), `NANSEN_API_KEY` (smart money),
`GITHUB_TOKEN`, `PARALLEL_API_KEY`. Tuning: `KEYLESS_PAGES` (4), `KEYLESS_OHLCV_CAP` (70), `KEYLESS_SPACING` (10 s),
`KEYLESS=0` to disable the keyless source.

The detailed reference follows.

## Reference

One Railway project, one environment (`production`), three services:

| Service | What | Source | Config |
|---|---|---|---|
| `Postgres` | Railway Postgres database. Exposes `DATABASE_URL` (private network) and, if you enable Public Access, `DATABASE_PUBLIC_URL`. | Railway database template | none |
| `pipeline` | Nightly cron job. Root directory `/pipeline`, Dockerfile build, `cronSchedule` `0 6 * * *` (06:00 UTC). Runs `python -m hookfeed run` (the Dockerfile `CMD`) and exits. | this repo | `pipeline/railway.json` |
| `web` | Next.js site. Root directory `/web`, Dockerfile build, public domain, healthcheck on `/`, restart on failure. | this repo | `web/railway.json` |

Service names matter: `${{Postgres.DATABASE_URL}}` below assumes the database service is literally named `Postgres` (Railway's default for the template). If you rename it, change the reference.

## What the config files set

`pipeline/railway.json` and `web/railway.json` are Railway config-as-code
(`$schema` `https://railway.com/railway.schema.json`). Keys used, all from the published schema:

- `build.builder: "DOCKERFILE"`, `build.dockerfilePath: "Dockerfile"`, `build.watchPatterns`
- pipeline: `deploy.cronSchedule: "0 6 * * *"`, `deploy.restartPolicyType: "NEVER"` (a failed night must not loop and burn API credits; the next cron tick is the retry), `deploy.drainingSeconds`
- web: `deploy.healthcheckPath: "/"`, `deploy.healthcheckTimeout: 120`, `deploy.restartPolicyType: "ON_FAILURE"`, `deploy.restartPolicyMaxRetries: 5`, `deploy.numReplicas: 1`, `deploy.overlapSeconds`

Neither file sets `startCommand`; the Dockerfile `CMD` is the start command
(`python -m hookfeed run` and `node server.js`). Config-as-code values override the same
settings in the dashboard, so change them in the file, not in the UI.

Important: Railway does not look for the config file inside the service's Root Directory.
Each service needs **Settings > Config-as-code > Railway Config File** set to the absolute
repo path: `/pipeline/railway.json` and `/web/railway.json` (per the monorepo guide, quoted:
"The Railway Config File does not follow the Root Directory path. You have to specify the
absolute path for the railway.json or railway.toml file"). This one setting is done in the UI
below; I did not find a documented CLI dot-path for it.

## Create the project (CLI)

Install and log in once (`npm i -g @railway/cli` or the install script on the CLI page), then
from the repo root:

```sh
railway login
railway init                       # new project, name it hookfeed
railway add --database postgres    # service "Postgres"; injects DATABASE_URL, PG* vars on itself

railway add --service pipeline
railway add --service web
```

Connect both code services to the GitHub repo and set their root directories. The repo
connection is a dashboard step (service > Settings > Source > Connect Repo), or create the
service from the repo directly with `railway add --repo <owner>/hookfeed` and rename it.
The root directory, cron schedule and healthcheck can be set from the CLI with
`railway environment edit --service-config <service> <dot.path> <value>`:

```sh
railway environment edit --service-config pipeline source.rootDirectory /pipeline
railway environment edit --service-config web      source.rootDirectory /web
# belt and braces; the railway.json files set the same values once the Config File path is set
railway environment edit --service-config pipeline deploy.cronSchedule "0 6 * * *"
railway environment edit --service-config web      deploy.healthcheckPath /
```

Then in the dashboard, for each code service: Settings > Config-as-code > Railway Config File
= `/pipeline/railway.json` resp. `/web/railway.json`.

Equivalent clicks for everything above: `+ New` (or cmd/ctrl+K) > Database > PostgreSQL;
`+ New` > GitHub Repo > hookfeed, twice; each service > Settings > Source > Root Directory;
pipeline > Settings > Cron Schedule `0 6 * * *`; web > Settings > Networking > Generate Domain;
web > Settings > Healthcheck Path `/`.

## Variables

Set with `railway variable set -s <service> KEY=value [KEY2=value2 ...]` or in the service's
Variables tab (RAW Editor accepts `.env` format). Never put values in the repo; `.env.example` at
the repo root lists every key with a comment.

```sh
# pipeline: DATABASE_URL by reference, then the API keys
railway variable set -s pipeline 'DATABASE_URL=${{Postgres.DATABASE_URL}}'
railway variable set -s pipeline \
  GRAPH_API_KEY=... ANTHROPIC_API_KEY=... EXA_API_KEY=... PARALLEL_API_KEY=... \
  NANSEN_API_KEY=... GITHUB_TOKEN=... BLOCKSCOUT_API_KEY=... NANSEN_PREMIUM_LABELS=0

# web: DATABASE_URL by reference, public URL for RSS/OG; BLOCKWORKS_API_KEY is reserved (not read by the app yet)
railway variable set -s web 'DATABASE_URL=${{Postgres.DATABASE_URL}}'
railway variable set -s web 'NEXT_PUBLIC_SITE_URL=https://${{RAILWAY_PUBLIC_DOMAIN}}'
railway variable set -s web BLOCKWORKS_API_KEY=...
```

`${{Postgres.DATABASE_URL}}` is Railway's reference-variable syntax (`${{SERVICE_NAME.VAR}}`);
it resolves to the Postgres service's private-network URL at deploy time, so the two code
services need no hard-coded connection string. Missing optional keys are fine: every pipeline
client is skipped with a log line when its key is unset.

Note the single quotes around any value containing `${{ }}`: your shell would otherwise try to
expand it. Use `--stdin` for secrets you do not want in shell history:
`printf %s "$KEY" | railway variable set -s pipeline ANTHROPIC_API_KEY --stdin`.

## Schema

Railway Postgres starts empty. Apply `db/migrations/*.sql` once from your machine:

```sh
# Postgres service > Settings > Networking > Public Access (TCP proxy) -> DATABASE_PUBLIC_URL
DATABASE_URL="$(railway variable list -s Postgres --kv | sed -n 's/^DATABASE_PUBLIC_URL=//p')" \
  scripts/apply-migrations.sh
```

`railway connect Postgres` opens a psql shell too, if you prefer to paste the file. The web
Dockerfile builds without `DATABASE_URL` on purpose; only the runtime container reads it.

## First deploy and first night

1. Push to the connected branch, or `railway up -s web` / `railway up -s pipeline` from the repo
   root to deploy from your working tree.
2. `web` deploys, Railway probes `/` on `PORT` until it returns 2xx (default healthcheck timeout
   is 300 s; the file sets 120). Open the generated domain; with an empty database the feed
   renders empty, not fixtures (fixtures only kick in when `DATABASE_URL` is unset).
3. `pipeline` builds but, being a cron service, does not run until 06:00 UTC. Run it once by hand
   first: service > Deployments > latest > three-dot menu > Run now (or temporarily clear the
   cron schedule, redeploy, restore it). Watch the logs for `step=ingest ok` and
   `step=snapshot ok` with non-zero counts, then `psql` the counts (`hooks`, `pools`,
   `pool_snapshots`).
4. Leave `cronSchedule` in place. Railway cron is UTC; runs must be at least 5 minutes apart;
   if a previous run is still `Active` the next tick is skipped, so the job must exit cleanly
   (the CLI closes its SQLAlchemy engine and exits 0/1/2).

## Sources (docs.railway.com, read 2026-10-07)

- Config as code (file names, `$schema`, `build.*` / `deploy.*` keys): https://docs.railway.com/reference/config-as-code and the schema itself https://railway.com/railway.schema.json (301 to https://backboard.railway.app/railway.schema.json)
- Cron jobs (`cronSchedule`, UTC, 5-minute minimum, skip-if-still-running, must exit): https://docs.railway.com/reference/cron-jobs
- Monorepo root directory, Config File absolute path, `railway environment edit --service-config <svc> source.rootDirectory <path>`: https://docs.railway.com/guides/monorepo
- PostgreSQL service and its variables (`DATABASE_URL`, `PG*`, `DATABASE_PUBLIC_URL` via Public Access): https://docs.railway.com/guides/postgresql
- Reference variables `${{SERVICE_NAME.VAR}}`, shared variables, RAW editor: https://docs.railway.com/guides/variables and https://docs.railway.com/reference/variables (`RAILWAY_PUBLIC_DOMAIN`, `PORT`)
- Healthchecks (2xx, `PORT`, 300 s default, `healthcheck.railway.app`): https://docs.railway.com/guides/healthchecks
- Dockerfile detection (`Dockerfile` at the root of the source directory, `RAILWAY_DOCKERFILE_PATH`, `ARG` for build-time vars): https://docs.railway.com/guides/dockerfiles
- CLI: https://docs.railway.com/reference/cli-api, https://docs.railway.com/cli/add (`-d`, `-s`, `-r`, `-v`), https://docs.railway.com/cli/variable (`set`, `-s`, `--stdin`, `--skip-deploys`), https://docs.railway.com/cli/environment (`edit --service-config <SERVICE> <PATH> <VALUE>`)
