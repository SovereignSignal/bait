# Proposed production operations — not executed

The code and local tests are separate from permission to change production. These are the exact bounded operations
for a later approval. No deployment, production migration/import/rescore, or cache restart was performed in this task.
Use the final reviewed PR SHA. Reconfirm identities, latest run day and row preconditions immediately before acting;
if they differ from this October 15 capture, return the new plan for review rather than silently widening its scope.

## Bunni historical evidence

- Frozen manifest: `pipeline/fixtures/bunni-historical-import-v1.json`.
- SHA-256: `42e7edad6476cceed690ae430cef14fbca96a7be9a6c7a95a4e7648ba991bf6a`.
- Intended current mapping: project **1505**, Ethereum hook `0x000052423c1db6b7ff8641b85a7eefc7b2791888`, pool
  `0xd9f673912e1da331c9e56c5f0dbc7273c0eb684617939a375ec5e227c62d6707`.
- Expected first import: **20 observations**, 19 positive pool transactions, one **$262.4545875 known contribution**,
  18 unpriced custom observations and a zero-amount standard counterpart. Eight excluded IDs remain excluded.
  The complete historical USD total stays null. This does not establish current inactivity or organic demand.

1. Review migration `0017_historical_evidence.sql` and the frozen manifest. Apply only migration 0017 using the normal
   migration runner after confirming earlier migrations, including 0016, are already applied. This creates one ledger
   and its indexes/constraints. It does not touch existing usage, coverage or score rows.
2. From `pipeline/`, with the separately approved production connection already supplied through `DATABASE_URL`, preview:

   ```sh
   python -m bait historical-evidence import fixtures/bunni-historical-import-v1.json --sha256 42e7edad6476cceed690ae430cef14fbca96a7be9a6c7a95a4e7648ba991bf6a --dry-run
   ```

   Require resolved `project_ids=[1505]`, the expected pool, `would_insert=20`, `unchanged=0`, the expected lower-bound
   contribution and `scored=false`. A previous exact import may instead return `would_insert=0, unchanged=20`;
   investigate any other state before writing. Preview is read-only and does not bootstrap schema.
3. After approval of those exact preconditions, repeat that command with `--apply` instead of `--dry-run`. It takes
   the existing pipeline run lock and commits only new immutable evidence rows. Repeat preview: it must now report
   zero new rows and twenty unchanged. Save the JSON result and manifest digest with the operational record.
4. Separately approve a targeted score update of **only `project_daily(project_id=1505, day=2026-10-14)`**, the captured
   existing run day. If the current latest day has changed, review a new explicit date; do not create a missing day.
   Under the existing pipeline run lock, export the full old target row to an immutable external backup, preserving
   nulls and types, before the score transaction. Use the bounded API:

   ```python
   # Inside an approved transaction, with the pipeline run lock held and old row backed up:
   score.run(conn, RunContext(day=date(2026, 10, 14), now=datetime.now(timezone.utc)), project_ids={1505})
   ```

   This explicitly scoped call rejects an empty set or missing project/day rows and writes no other project/day rows.
   Never omit `project_ids`: `None` retains normal all-project scoring. Expected classification is
   `has_historical_usage=true`, `is_spam=false`, `activity_status='unknown'`, `is_new=false`, `score_trending=0`.
   Missing recent volume/swap windows become null under the candidate rules; they must not gain the historical
   19 transactions or $262.4545875. Existing wash evidence must remain governed by the unchanged wash controls.
   Verify the complete before/after row and that all other scored rows are unchanged before committing.
5. Verify All, name/address search, watchlist and `/p/bunnihook` against a fresh query cache; confirm exclusion from
   New/Trending and no dormant claim. A same-day rescore need not change the cache's day key. Wait for the existing
   cache expiry or separately approve a cache/process refresh; do not infer failure or restart production implicitly.

### Bunni rollback boundaries

Data rollback removes only the rows originally owned by this manifest; exact replays do not transfer ownership:

```sh
python -m bait historical-evidence rollback --sha256 42e7edad6476cceed690ae430cef14fbca96a7be9a6c7a95a4e7648ba991bf6a --dry-run
python -m bait historical-evidence rollback --sha256 42e7edad6476cceed690ae430cef14fbca96a7be9a6c7a95a4e7648ba991bf6a --apply --expected-rows 20
```

Review the preview and count before the write. This does **not** restore the scored row: restore the separately
exported project/day backup in an approved locked transaction, or separately approve its targeted recomputation.
Do not use a broad day-wide restore for this one-project change.

Schema rollback requires rolling back the dependent scorer/importer code first and exporting needed evidence.
`db/rollback/0017_historical_evidence.sql` atomically drops only the evidence table and deletes only its migration
marker. The normal migration runner can reapply it. It never restores scores or deletes raw usage/coverage.

## Aegis candle correction — still separate

The existing proposal remains unapplied: Ink pool
`0xfff506f639ae6cbe6c4743ddfbac69169bca6f1a38ff6997c6f35ff2f8bd1805`, October 13 UTC,
estimated daily USD `9412.7449754386` versus accepted closed-day candle `54040.612305176204`, preserving 761 swaps.
After fresh source/row checks and backup approval, replace that daily value and estimate flag, synchronize October 14
closed-day pool stats, and target only the approved existing project 1881/day score. Keep raw USD, TVL, cumulative
columns and other projects unchanged. The new code's normal-run refresh is not a claim that production was repaired.
