# Runbook: importing reviewed historical evidence into production

For migration 0017 and `python -m bait historical-evidence`. Written for the Bunni import
(`pipeline/fixtures/bunni-historical-import-v1.json`, SHA-256
`42e7edad6476cceed690ae430cef14fbca96a7be9a6c7a95a4e7648ba991bf6a`); the steps are the same for any reviewed manifest.
Nothing here has been run against production. Reconfirm identities and row preconditions immediately before acting; if
they differ from the capture that produced the manifest, stop and review rather than widen the scope.

## What the import does and does not do

- Adds immutable rows to `historical_swap_evidence` only. It never writes daily usage, snapshots, indexer state,
  coverage or scores, and it never calls an external service.
- The next nightly `score` run (06:00 UTC) consumes the ledger: a qualifying pool makes `has_historical_usage` true,
  `is_spam` false and the project discoverable in All, search, watchlists and its page, with `activity_status`
  `unknown` while recent coverage is incomplete. It does not enter New or Trending and it does not become `dormant`.
- Expected result for Bunni: 20 observations, 19 positive pool transactions, one known contribution of $262.4545875,
  18 unpriced observations, 8 excluded IDs, historical total null. This is listing evidence, not a safety or
  organic-demand claim.

## Where it runs

Production Postgres has no public endpoint, so the command runs inside the `pipeline` Railway service, the same way
one-off repairs do (`railway/README.md`, "One-off repairs"):

1. Set the `pipeline` service start command to the command below (the service's normal start command is a wrapper that
   runs `python -m bait run` between 05:00 and 08:00 UTC and `python -m bait repair all ...` at any other hour; note
   it down before replacing it).
2. `railway api 'mutation { deploymentRestart(id: "<latest pipeline deployment id>") }'` runs it at once.
3. Read the deployment log, confirm the process exited, then restore the original start command.
4. Do all of this outside 05:30 to 06:30 UTC. `--apply` refuses inside that window (exit 5), and a start command left
   in place at 06:00 UTC means the cron runs the import instead of the nightly: exit 0, no rows, no receipt, "Nightly
   update delayed" on the site and a failed health check at 08:30.

## Steps

1. Review migration `db/migrations/0017_historical_evidence.sql` and the manifest. Apply 0017 with the normal migration
   runner (`scripts/apply-migrations.sh`) after confirming 0016 is already applied. It creates one table and its
   indexes; existing rows are untouched.
2. Preview, from `pipeline/` with the production `DATABASE_URL`:

   ```sh
   python -m bait historical-evidence import fixtures/bunni-historical-import-v1.json \
     --sha256 42e7edad6476cceed690ae430cef14fbca96a7be9a6c7a95a4e7648ba991bf6a --dry-run
   ```

   Require `project_ids=[1505]`, the expected pool, `would_insert=20`, `unchanged=0`, the expected contribution and
   `scored=false`. A previous exact import returns `would_insert=0, unchanged=20`; any other state needs a look before
   writing. Preview is read-only and does not create the table.
3. Apply: the same command with `--apply`. It takes the pipeline run lock and commits only new ledger rows. Preview
   again: `would_insert=0, unchanged=20`. Keep the JSON result and the manifest digest with the change record.
4. Wait for the nightly. After 06:00 UTC, check `/p/bunnihook` (listed, "unknown" status, not in New or Trending) and
   `/api/health`. The web's in-process cache drops within a minute of the new day; a redeploy of the web service clears
   it immediately if needed.
5. Only if the nightly cannot be waited for: a targeted rescore of the existing `project_daily(project_id=1505,
   day=<latest day>)` row, under the run lock, after exporting that row:

   ```python
   score.run(conn, RunContext(day=<latest day>, now=datetime.now(timezone.utc)), project_ids={1505})
   ```

   It rejects an empty set or a missing project/day row and writes no other rows. The web reads only the latest
   scored day, so rescoring an older day changes nothing visible.

## Rollback

Data: `historical-evidence rollback --sha256 <import sha> --dry-run`, then `--apply --expected-rows 20`. It removes only
the rows that import owns; an exact replay never transfers ownership. The next nightly forgets the evidence. It does
not restore an already rescored row; restore the exported row or let the nightly recompute it.

Schema: `db/rollback/0017_historical_evidence.sql` drops the ledger and resets its migration marker. Run it only after
every service is on code without the table (the indexer's `migrate()` recreates it every loop otherwise). Additive
columns from 0016 never need a rollback.

## Aegis Ink candle correction (separate, still unapplied)

Pool `0xfff506f639ae6cbe6c4743ddfbac69169bca6f1a38ff6997c6f35ff2f8bd1805` on Ink, October 13 UTC: estimated daily USD
`9412.7449754386` against the accepted closed-day candle `54040.612305176204`, 761 swaps. The candle refresh shipped in
the pipeline (part 3) repairs such days on its own at up to `ONCHAIN_CANDLE_REFRESH_CAP` pools a night; no manual
production write is planned. Verify after a nightly on the new code that the day reads `estimated=false`.
