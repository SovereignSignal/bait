# Proposed Bunni historical evidence

Observed **2026-10-15** through read-only public sources. [`bunni-historical-materiality-2025.proposed.json`](bunni-historical-materiality-2025.proposed.json) preserves **19 positive custom Swap events in 19 distinct transactions**, all before the known exploit block, for Ethereum pool `0xd9f673912e1da331c9e56c5f0dbc7273c0eb684617939a375ec5e227c62d6707`. Eight later/exploit events are explicitly excluded. The bundle includes raw event data, exact event IDs, source URLs/hashes, token metadata, a paired zero-amount PoolManager event, and one historical quote response.

**This bundle does not itself fix production Bunni. No production data changed, and it does not establish current dormancy.** A separately pinned [v1 import fixture](import-fixture-README.md) now exercises the offline importer and scoring branch locally. The original source bundle stays immutable. The captured production project is **1505 / `bunnihook`**, with no stored Bunni raw daily usage or pool snapshots. Its score zeros therefore do not prove inactivity. Base was incompletely indexed at inspection time.

The pool's token order is USDC `0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48`, USDT `0xdac17f958d2ee523a2206206994597c13d831ec7`; fee 0, tick spacing 1, hook `0x000052423c1db6b7ff8641b85a7eefc7b2791888`. Recomputing the ABI-encoded PoolKey hash matches the pool ID. The [official deployment table](https://docs.bunni.xyz/docs/v2/technical/deployments/) and [official event interface](https://github.com/Bunniapp/bunni-v2/blob/main/src/interfaces/IBunniHook.sol#L46) support the identity and event schema.

The [saved hook log page](https://eth.blockscout.com/api/v2/addresses/0x000052423c1db6b7ff8641b85a7eefc7b2791888/logs) is a bounded sample, not complete history. The cutoff excludes block **23273098** and later, including five events in the [known exploit transaction](https://eth.blockscout.com/tx/0x1c27c4d625429acfc0f97e466eda725fd09ebdc77550e529ba4cbdbc33beb97b); see the [official post-mortem](https://blog.bunni.xyz/posts/exploit-post-mortem/). Events before that block are not automatically cleared of wash concerns.

One accepted swap—[transaction `0xb3f1…e426`](https://eth.blockscout.com/tx/0xb3f10b3d5caa9b808a02cfbfcd9fe7f163dc2566757cbcad3fa61f357022e426), custom log **166**, **2025-09-01 20:49:11 UTC**—has a conservative historically valued contribution of **$262.4545875**. The [free historical-price response](https://coins.llama.fi/prices/historical/1756759751/ethereum:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48,ethereum:0xdac17f958d2ee523a2206206994597c13d831ec7?searchWidth=4h) returns USDC $0.999827 and USDT $1, both with six decimals and source confidence 0.99, at 11 and 12 seconds before that event. The value is the smaller of the input and output USD values, counted once. It is a source-valued contribution, not an exact execution-price guarantee. The other **18 events remain unpriced**, and **historical total USD is null**.

This bounded evidence satisfies the candidate's same-pool historical threshold of at least 10 swaps and $100 priced contribution. It must remain separate from recent metrics and wash controls. [Receipt logs](https://eth.blockscout.com/api/v2/transactions/0xb3f10b3d5caa9b808a02cfbfcd9fe7f163dc2566757cbcad3fa61f357022e426/logs) show the following PoolManager log **167** for the same pool has zero amounts; custom and standard events represent **one swap**. The indexer's 14-day bootstrap would not retrieve these 2025 transactions, and standard-event amounts alone would not recover their actual volume.

The candidate now includes a **generic, idempotent historical-event evidence importer**, with unique event IDs, payload/source hashes, exclusion handling and separate historical qualification. It never replays partial pages into additive raw daily aggregates, changes recent metrics, or moves indexer cursors/coverage markers. Only after the code, migration and exact manifest are reviewed should an approved import and targeted project-score recomputation occur. See the [proposed production operations](proposed-production-operations.md); none have been executed.

Run the standard-library-only offline verifier from any directory:

```sh
python3 /path/to/bait/docs/evidence/verify_bunni_historical_materiality.py
```

It checks the PoolKey and event signature hashes, raw custom-event decoding, 19 unique positive events, eight disjoint exclusions, historical quote hash/timestamps/decimals, the one-event materiality calculation, null unpriced totals, and the paired standard zero-amount event. It makes no network requests or database changes. It does not independently re-fetch sources, verify complete coverage, or certify organic activity. The observed verification passed.

Bundle SHA-256: `53b7b70c657fd42298885fbd7e32ac2fc225596614ecd5f9a6b841ad92d0eeaf`.
