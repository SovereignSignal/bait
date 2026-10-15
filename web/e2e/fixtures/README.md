# Candidate source replay

`representative.json` is local test input, not a production seed or a new source of truth.

The six named records replay published data captured on **2026-10-15** from the retained
`bait-mvp-sources-11941800548` artifact, plus the Clanker Base and Arrakis detail reads at
03:12:35 UTC. Their reported metrics end on **2026-10-13 UTC**. The fixture includes each
HTML source file's SHA-256, exact API row totals, displayed integer counts, registry hook
identities, and pool links. Hook membership is bounded by the captured `/api/hooks`
address index, chain list, registry label, and reported hook count. Same-address deployments
under different labels are excluded, rather than inventing a family merge. Pool USD numbers available only as abbreviated HTML labels
are left `null`; they are not expanded into invented precision. Array `series` is empty
where raw daily values were unavailable.

The recorded public Aegis and Aegis v3 rows remain separate. Their `(chain, pool ID)` sets
do not overlap in this bounded replay. `qa-retired-aegis → aegis` is a synthetic alias
that tests the redirect mechanism; it does not assert that a production merge occurred.

The `QA` records exercise synthetic scoring outcomes: eligible dormant history, incomplete
recent coverage, positive unpriced swaps, never-active dust, missing history, current and historical wash controls,
and revival. These inputs verify presentation of pipeline states. Pipeline tests separately
verify evidence qualification and scoring; browser replay alone does not validate ingestion.

The initial capture had no public Bunni row. Its verified canonical `/p/bunnihook` also
returned 404 in a separate read at 03:19:01 UTC. No retained Bunni fixture
or dormant classification is fabricated. Primary-source historical Bunni swap evidence
must be evaluated separately against the pipeline's accepted usage and coverage rules.

Base had `backfilled=false`. The fixture shows that partial-coverage badge and marks
coverage unknown because the captured old health response lacks newer required fields.

Run each fixture mode from a fresh build of the same candidate commit. Next's generated
ISR pages persist between server processes, so clear the generated build before changing
fixture mode; a different port alone does not isolate that cache.

```sh
rm -rf .next
npm run build
BAIT_E2E_SHA=<exact-built-commit> npm run e2e
rm -rf .next
npm run build
BAIT_E2E_SHA=<exact-built-commit> BAIT_E2E_REPRESENTATIVE=1 npm run e2e
```

`BAIT_E2E_OUTPUT_DIR` selects an evidence directory for JSON results, screenshots, and
failure traces. The config always starts a new local server, clears `DATABASE_URL`, and
uses explicit fixture mode. Browser requests to the live-trades proxy are fulfilled with
empty test data so candidate UI verification does not request provider data or execute trades.
