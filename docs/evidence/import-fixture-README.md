# Historical swap import fixture v1

[`pipeline/fixtures/bunni-historical-import-v1.json`](../../pipeline/fixtures/bunni-historical-import-v1.json) is a deterministic conversion of the [frozen proposed evidence bundle](bunni-historical-materiality-2025.proposed.json). It adds no new source requests or production changes. The manifest is data for an importer; its presence alone does not import evidence, change scores, or establish current dormancy.

- Schema: `schema_version: 1`, `kind: historical_swap_evidence`.
- Coverage: `partial_observed_events`; `historical_total_usd` remains **null**.
- Observations: **19 positive custom Swap events**, each from a distinct transaction, plus **one known zero-amount PoolManager Swap observation** from the same transaction/pool as the priced custom event.
- Valuation: one conservative historical contribution of **$262.4545875**; **18 custom observations remain unpriced**. The standard observation also has `valuation: null`.
- Exclusions: **eight event IDs** with reasons: five from the known exploit transaction, three at or after its block. Excluded IDs are disjoint from all imported observation IDs.
- No project ID, project-name exception, asserted qualification flag, fabricated historical total, or current coverage claim is carried into the manifest.

Source bundle SHA-256: `53b7b70c657fd42298885fbd7e32ac2fc225596614ecd5f9a6b841ad92d0eeaf`.

Import fixture SHA-256: `42e7edad6476cceed690ae430cef14fbca96a7be9a6c7a95a4e7648ba991bf6a`.

## Provenance and validation boundary

Each observation preserves raw emitter, topics, data, block hash/number, transaction hash and log index. Its `source.sha256` uses `kind: canonical_log_json`: SHA-256 of the UTF-8 encoding of `json.dumps(raw_log, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`. This is a normalized log-content digest, **not** the hash of an HTTP response. `original_response_sha256` separately identifies the saved source response; URL and observation time are retained. A source hash binds reviewed bytes but does not prove that an arbitrary submitted log exists on chain.

The single valuation embeds the original historical quote response and its SHA-256, request URL, token identities, decimals, prices, quote timestamps and confidence. The importer must recompute the accepted ABI amounts, match currencies to the pool metadata, check the historical quote evidence, and calculate the lower bound; it must not accept the supplied lower bound as an unexplained assertion. The reviewed source material supports the content, while importer validation checks consistency. A manifest cannot authorize itself: review must bind the frozen file digest outside the file.

## Counting and conflicts

The fixture supplies the known PoolManager event as an independent observation, preserving its original identity. Its two token amounts are zero, so it contributes **no positive swap count or USD amount**. The root import/scoring design conservatively groups observations by `(chain_id, pool_id, transaction_hash)`, counts a positive transaction once, and takes the maximum supported USD contribution for that transaction. The custom and standard representations therefore cannot double count this example.

This grouping intentionally **undercounts multiple real positive swaps in the same pool and transaction**. It is a conservative historical qualification measure, not an exact general trade count or volume total. No unobserved aliases are invented for the other 18 transactions. Unique raw identity is `(chain_id, transaction_hash, log_index)`; an identical repeated observation is a no-op, while changed payloads, changed block hashes, conflicting pool/hook assignment, or overlap with exclusions must reject the import rather than silently merge or overwrite.

The event schema adapter is selected by the supported event signature, with raw-log validation; it must not grant a project-name or hook-address exemption. Accepted historical evidence must remain separate from recent daily aggregates, current zero-activity coverage and wash controls.

## Conversion checks performed

Offline checks passed for: 20 unique raw observation IDs; 19 positive custom observations; 19 transaction groups; exactly one valuation; 18 unpriced custom observations; eight disjoint exclusions; every canonical-log digest; embedded quote response digest; zero standard amounts; and recomputed one-event value of $262.4545875. The original bundle verifier separately checks the PoolKey and event signature hashes. No network, production import, score recomputation, or current-coverage assertion occurred during conversion.
