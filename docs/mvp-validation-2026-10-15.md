# MVP data and presentation checkpoint — October 15, 2026

PR #31 remains a draft. This continuation covers representative validation and historical listing eligibility.
It does not authorize deployment, a production rescore/backfill, trading, paid enrichment, or workflow approval.

## Candidate changes

- Historical listing evidence is separate from the current seven-day window. Retained daily Swap evidence can
  qualify a project after it falls outside fourteen days. Cumulative snapshots are never summed. Registry membership,
  source verification, a name, audit link, or old TVL cannot establish historical use.
- The conservative new historical threshold is ten exact Swap events and $100 of priced usage in one pool. The
  existing $1M volume exemption remains subject to its unverified/wash safeguard. This is a listing rule, not proof
  of organic demand. Estimated rolling fallback values are excluded from historical qualification. A folded
  `hook:<address>` aggregate cannot satisfy the real-pool threshold, even if ten dust pools collectively pass it.
- `activity_status=dormant` additionally requires complete seven-day zero-swap evidence across the mapped pools and
  hooks, an adequate pool census, ready chains, cursor coverage and no owed historical lookups. Missing/stale/unknown
  coverage stays unknown; positive unpriced swaps prove activity. Prior wash findings survive uncertain inactivity
  and are labelled historical. Fresh usable evidence is required to clear a prior finding on revival.
- The frontend consumes the pipeline decision. It no longer resurrects spam from metadata or zero USD volume.
  Retained dormant/unknown-history records remain available in All, search, watchlists and direct details; dormant
  records are excluded from New/Trending. Missing volume/counts remain null, including detail pools whose raw swaps
  are ahead of copied snapshots.
- Migration `0016_historical_usage.sql` covers the daily table and an existing repair backup table. Apply it before
  serving the new web build; old rows remain unclassified until a separately authorized score run. Fresh backup tables
  now also preserve false defaults for the new booleans. Legacy explicit-column writers and restore were checked on
  Postgres. Web readiness returns 503 before the migration and 200 afterward, including on an empty migrated database.
- The onchain step now refreshes a bounded queue of held-back estimated days from explicit closed UTC/USD source
  candles. It preserves the custom-accounting guard, exact swap counts, TVL and historical cumulative values. Missing,
  invalid, rate-limited or absent-day responses leave estimates intact. The default cap is ten pool attempts per run,
  with the existing free client pacing; `ONCHAIN_CANDLE_REFRESH_CAP=0` disables it. Same-day full reruns are idempotent.
  The shared daily-candle parser also rejects a whole malformed response: a missing USD value no longer becomes zero,
  and invalid rows are not dropped into callers that infer no trading on absent dates. Explicit numeric zero remains valid.
- Removed the one-use commit-writing workflow and all encoded transport files. Production baseline/source workflows
  are manual-only and explicitly labelled as baseline captures. No pending workflow was approved.
- Candidate browser checks also cover saved-watchlist hydration and expanded evidence explanations on narrow phones.
  Long source URLs wrap without hiding their content, and missing pool TVL is not described as zero liquidity.
- Project pages render per request from the existing cached data, avoiding a reproduced Next 16.4 first-hit ISR bug
  that duplicated alias redirect headers. This costs a page render per visit; it does not remove the data cache.

## Representative production evidence

Production was read on October 15, including bounded database queries under `BEGIN READ ONLY`, a ten-second statement
timeout and `ROLLBACK`. Each saved query confirms `transaction_read_only=on`. No production records were changed.
The scored window was October 7–13 (run day October 14). Browser/source replay is separate from pipeline correctness.

| Record | Dated evidence and finding | Remaining limit |
|---|---|---|
| Angstrom (`1502`, Ethereum) | Hook `0x0000000aa232009084bd71a5797d089aa4edfad4`. Both sampled October 13 pool candles agree with displayed rounding. Two sampled pools total $174,415,854.69 for October 7–13 versus stored project $175,438,038.56. [Official docs](https://docs.angstrom.xyz/). | The bounded sample does not reconcile the full three-pool project or pricing differences. |
| Aegis (`1881`) / v3 (`2222`, Unichain) | Separate canonical records. Aegis has seven hooks across six chains; v3 Unichain has one hook and 33 initialized pools. Sampled pool sets are disjoint. No Aegis aliases were found in the bounded database query. [Project repository](https://github.com/labs-solo/AEGIS_DFM). | No brand-only merge or fabricated redirect. Full project grouping/totals remain unproven. |
| Aegis Ink USDC/USD₮0 | Pool `0xfff506f639ae6cbe6c4743ddfbac69169bca6f1a38ff6997c6f35ff2f8bd1805`: October 13 snapshot $9,412.744975, **estimated=true**; raw indexer 761 swaps, $54,002.803066, zero unpriced swaps; [matched UTC candle](https://api.geckoterminal.com/api/v2/networks/ink/pools/0xfff506f639ae6cbe6c4743ddfbac69169bca6f1a38ff6997c6f35ff2f8bd1805/ohlcv/day?aggregate=1&limit=20&currency=usd) $54,040.612305. Hook bit 3 correctly blocks raw-USD replacement. The new source-refresh replay replaces the estimated day with the candle, preserving 761 swaps; the isolated seven-day sum increases once by $44,627.8673297376. | Code fix and replay pass; production repair remains unapplied. Per-run provenance cannot prove whether the original estimate came from rolling carry-forward or a previously running candle. Raw indexer USD uses current prices and is not the replacement authority. |
| Hookr V6 (`2538`, Robinhood 4663) | [Official repository](https://github.com/Hookr-fun/hookr-contracts) confirms legacy root `0xb3ca29cf721380cee8b8e4755f3865ebc68fe8cc`. MOW/WETH `0x5291cfd042d8a771a510534ee31090bc4f4dc72724871e8f321aecfd12332a9a`: saved October 7–13 candles $75,385,273.90 match stored project volume. | Concentrated sampled wallet (~$7.1M, 296 trades, 145 buys/151 sells) remains suspicious. Stored `is_wash=false`; provider agreement is not organic-demand validation. A second pool's displayed October 13 zero differs from a $32.36 candle. |
| Clanker Base static v2 (`3023`) | [Official deployment list](https://github.com/clanker-devco/v4-contracts#base-mainnet) confirms hook `0xb429d62f8f3bffb98cdb9569533ea23bf0ba28cc`, chain 8453. Base backfill remains incomplete. | Displayed zeros cannot certify inactivity or complete totals. Source verification does not cure coverage. |
| Arrakis private (`1585`, others separate) | Hook `0xf9527fb5a34ac6fbc579e4fbc3bf292ed57d4880` belongs to `1585` on Arbitrum and separate `2109` on Ethereum; address alone is insufficient identity. [Official docs](https://docs.arrakis.finance/arrakis-pro) describe modular vaults; the published `ArrakisFinance/arrakis-modular` repository URL returned anonymous 404. | Unavailable source link is not proof of absent code. Do not equate private permissions with RFQ or treat missing custom-accounting values as zero. |
| Bunni (`1505`, `bunnihook`) | Canonical `/p/bunnihook` returned 404 at 03:19:01 UTC. Database has fourteen hooks, 632 initialized pools, `is_spam=true`, and **no snapshots or daily usage rows**. [Official deployment table](https://docs.bunni.xyz/docs/v2/technical/deployments/) matches mainnet hook `0x000052423c1db6b7ff8641b85a7eefc7b2791888`. A bounded real-pool proof contains 19 positive events in 19 transactions before exploit block 23273098; eight exploit/later events are excluded. One historical-price cross-check supplies a conservative $262.4545875 contribution, exceeding the $100 threshold without pricing the remaining 18 events. | Historical materiality is independently established but absent from accepted pipeline inputs. The initial swap scan begins fourteen days back, so 2025 history was never imported. Base and other recent coverage remain unproven. No importer or production mutation was added; Bunni discoverability is still blocked pending a reviewed historical input path. |

Bunni's custom event is distinct from the PoolManager Swap event. Two complete receipts show both: the standard event
has zero amounts, while the custom event and transfers carry the amounts. The existing indexer would count the standard
event if scanned; counting both would duplicate one economic swap. PoolKey recomputation confirms Ethereum pool
`0xd9f673912e1da331c9e56c5f0dbc7273c0eb684617939a375ec5e227c62d6707`, USDC/USDT, fee 0, tick spacing 1, and the official hook.
The priced [September 1 swap](https://eth.blockscout.com/tx/0xb3f10b3d5caa9b808a02cfbfcd9fe7f163dc2566757cbcad3fa61f357022e426)
is custom log 166 (standard log 167), input 262.5 USDC and output 262.481915 USDT. The historical quote response observed
at 03:52:14 UTC gives USDC $0.999827 and USDT $1 at 11/12 seconds before the transaction, both confidence 0.99.
`min(262.5 × 0.999827, 262.481915 × 1) = $262.4545875`; the full historical total remains null. These are third-party
historical quotes, not an exact execution-price guarantee. The reviewable [evidence bundle](evidence/bunni-historical-materiality-2025.proposed.json)
contains identifiers, hashes, source URLs and exclusions. Old activity does not establish current dormancy or safety.

### Proposed production work — not performed

- Aegis: after approval, capture fresh row/source preconditions and back up the exact Ink October 13 snapshot,
  October 14 pool stats and scored project rows. Replace only the estimated daily USD with the accepted closed-day
  candle, synchronize the pool's UTC-day stats, and rescore the existing October 14 run under the pipeline lock.
  Preserve 761 swaps, raw USD, TVL and cumulative columns. Verify the single-day delta once; do not assume a full-project
  total before all its inputs are rechecked. The new normal-run refresh is also code only until separately deployed.
- Bunni: add a separately reviewed, idempotent event-evidence path before importing the bounded 19-event proof.
  Deduplicate chain/transaction/log identities and correlate custom/standard events as one swap; keep the eighteen
  unpriced contributions and total USD null, while retaining the one independently priced lower bound. Keep proof
  separate from recent metric and coverage tables. `store.apply_swaps` is additive, has no event-level replay dedupe,
  and must not be used to replay this bundle or rewind the live cursor. Target only the approved existing score day,
  with backup and run lock; expect retained eligibility with unknown activity until complete recent coverage exists.
  This import path is not implemented in this PR, so approval of a production rescore alone cannot fix Bunni.

## Verification and evidence

The original source artifact `11941800548` from run `38666527325` was preserved locally before its expiry on
October 22 at 01:38:20 UTC: 6,008,581 bytes, SHA-256
`4a0fce91a9383ce0de089966d31adc7c64dbb29bd642ddc33944a4e032a27226`.
It includes the earlier production capture at 01:28 UTC. That earlier browser baseline cannot certify this candidate.
Prior rate limits are preserved: eleven successful GeckoTerminal requests and seven HTTP 429 responses. Two bounded
new candle requests succeeded; neither success retroactively changes the prior 429 outcomes.

Candidate tests are separate:

- Pipeline regressions: evidence older than fourteen days, dust, incomplete/stale/short coverage, missing history,
  unpriced swaps, no cumulative/source double counting, revival and historical wash. Disposable Postgres also checks
  migration and repair backup/restore compatibility.
- Web unit/query tests: pipeline decisions, null versus zero, direct pages/search/watchlists, raw/copy disagreement,
  and real migrations/queries against an isolated local schema.
- `web/e2e/fixtures/representative.json`: six dated source replays plus explicitly synthetic lifecycle/redirect
  cases. No invented Bunni retention or Aegis production merge. Desktop/mobile checks cover filtering, navigation,
  errors, overflow, evidence badges and unknown values. Each suite rebuilds the exact candidate commit to clear
  ISR pages from the other fixture dataset. The test server clears `DATABASE_URL` and never reuses a running server.
  See the fixture README for provenance and reproducible commands.

Final test outcomes and the exact tested SHA belong in the PR checkpoint and local evidence manifest. No candidate
success should be inferred from the earlier `e38328f` checks: its CI/MVP workflows were `action_required`, not passes
or failures. No workflow approvals, merge or deployment were performed.

Before release: review the code and migration; resolve Bunni historical ingestion/current coverage, apply and verify
the separately approved Aegis data repair, and review grouping/total reconciliation and suspicious Hookr evidence. A deployment or production data repair
requires a separate decision. The Notion workroom was not updated; this report is provided for parent reconciliation.
