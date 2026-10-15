# Security

Bait is a read-only site over public blockchain and registry data. It holds no user accounts, no funds and no private
keys, and it never executes trades. The attack surface that matters:

- Server-side fetches built from URL input (`/api/trades/[chain]/[pool]`, symbol lookups): they must only reach the
  providers listed in `pipeline/bait/keyless.py` and `web/src/db/queries.ts`, and only for pools Bait already knows.
- Third-party strings rendered on pages (contract names, registry entries, search results, model-written blurbs).
- The pipeline's paid API clients and its spend guard (`pipeline/bait/costs.py`).
- The Postgres connection string and provider keys, which live only in Railway variables and local `.env` files.

## Reporting

Email the maintainer at the address on the GitHub profile of `SovereignSignal`, with "Bait security" in the subject.
Do not open a public issue for anything that could be exploited before it is fixed. Expect an acknowledgement within
a week; there is no bug bounty.

## Scope notes

- Figures that are wrong because a data source lagged or a rule misfired are bugs, not security issues; report them
  as issues with the pool or hook address.
- Hooks listed on Bait are third-party contracts in the Uniswap v4 swap path. Bait does not audit them, and a listing,
  badge or rank is not an endorsement.
