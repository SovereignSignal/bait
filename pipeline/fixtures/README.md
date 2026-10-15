# Pipeline fixtures

`hooklist.sample.json` is 300 entries copied from the public Uniswap v4 hook registry
(https://github.com/Uniswap/hooklist, `hooklist.json`) on 2026-10-07. The tests and `scripts/run-pipeline-local.sh`
read it. It is registry data, not Bait's: refresh it from the source rather than editing entries by hand.

Other files here are captured provider responses or reviewed manifests used by specific tests; each is described
next to the test or in `docs/evidence/`.
