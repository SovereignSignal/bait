# Bait read API (v1)

Read-only JSON over the same figures the site shows. No key. About 600 requests a minute per deployment, then 429 with
`Retry-After`. Responses carry `X-Bait-Data-Day` (the closed UTC day the figures end on), `X-Bait-Api-Version: 1`,
`Cache-Control: public, max-age=300` and `Access-Control-Allow-Origin: *`. `null` means unknown, never zero. Field
names are stable within v1; new fields may be added, and a field whose data source is retired is removed (listed
under [Removed fields](#removed-fields)).

Base URL: `https://bait-production-2706.up.railway.app`

## List projects

```sh
curl -s "https://bait-production-2706.up.railway.app/api/v1/projects?sort=vol7d&limit=5" | jq '.projects[] | {slug, vol: .metrics.vol_7d_usd}'
```

Query: `sort` = `vol7d` (default; wash-like and dormant rows last) | `trending` (the site's Trending order, with
`rank_trending`) | `new` (first seen within 7 days, newest first) | `tvl`; `limit` 1..500 (default 100); `offset`;
`chain` (id or slug, e.g. `8453` or `base`); `category` (`dynamic-fee`, `launchpad`, `orders`, `lp-management`,
`compliance`, `oracle`, `mev-lvr`, `lending`, `gaming-nft`, `other`). Bad values fall back to defaults.

Response: `{ api_version, data_day, query, total, count, projects: [summary] }`.

## One project

```sh
curl -s https://bait-production-2706.up.railway.app/api/v1/projects/angstrom | jq '.project | {name, data_day, hooks: (.hooks|length), top: .metrics.top_wallet_share_7d}'
```

A retired slug (renamed or merged project) answers 308 to the current one. Spam-flagged projects are 404, as on the
site. Response: `{ api_version, project }` where `project` is the summary plus `data_day`, `why_blurb`,
`why_blurb_at`, `hooks[]`, `pools[]`, `daily_volume_usd[]` (`{day, vol_usd}`, oldest first, about 14 days),
`top_wallets[]`, `receipts[]`, `signals`. `top_wallets[]` is up to 10 `{address, chain_id, volume_usd, trades, buys,
sells, last_trade_at}`, by volume across the project's pools, from GeckoTerminal's latest ~300 trades in each of Bait's
busiest pools on the latest day fetched. `receipts[]` are posts about the project found before 2026-10-24 (none are
added now). `signals` is `{last_commit_at, last_mention_at, audit_count}`: the last GitHub commit Bait saw, the newest
`published_at` among the listed receipts (null when there are none), and the number of audit links.

## Look up a hook address

```sh
curl -s "https://bait-production-2706.up.railway.app/api/v1/hooks/0x0000000aa232009084bd71a5797d089aa4edfad4?chain=ethereum" | jq '.matches[] | {slug: .project.slug, hooks}'
```

One address can be a hook on several chains and so belong to several projects; `chain` narrows it. 404 when no listed
project has that hook: Bait lists hooks with at least one pool, so a deployed hook with no pool is not here yet.
Response: `{ api_version, data_day, address, matches: [{ project: summary, hooks: [{address, chain_id, name,
hooklisted, deployer, deployer_source}] }] }`. `deployer` is the wallet that deployed the hook; `deployer_source` is
`hooklist` (the registry names it), `sourcify` or `explorer` (the sender of the hook's creation transaction, from
Sourcify or Blockscout); both are null when it is unknown.

## The summary object

| Field | Meaning |
|---|---|
| `slug`, `name`, `url`, `what_line` | identity; `what_line` is the generated one-line description, null when none yet |
| `category`, `secondary_categories` | see the list above |
| `chains[]` | `{id, slug}` the project's hooks are deployed on |
| `first_seen_at` | first time Bait saw any of its hooks |
| `hooklisted`, `is_factory`, `upgradeable`, `routable` (`eligible` / `ineligible` / `unknown`), `routing_source` (`allowlist` / `denylist` / `rule`), `audit_url` | registry and address-derived facts; `routable` applies Uniswap's published hook allowlist and denylist first, then the automatic rule, and `routing_source` says which decided; `audit_url` is the first published audit link that points at a report (not a repository, code folder or source file) and did not answer 404 or 410 on the last nightly check, while `hooks[].audit_url` is the registry's value as published |
| `status.is_new`, `status.is_wash`, `status.wash_reasons[]`, `status.wash_historical`, `status.is_dormant`, `status.activity_status`, `status.has_historical_usage` | the pipeline's classification; reasons are `24h`, `7d`, `concentrated`, `vol_tvl`, `no_liquidity`, `symmetric`, `uniform`, `unverified`, `lookalike`, `roundtrip` |
| `metrics.vol_7d_usd`, `vol_7d_prev_usd`, `vol_24h_usd`, `tvl_usd`, `swaps_7d`, `swaps_24h`, `pools`, `hooks` | closed UTC days; volume is a USD estimate |
| `metrics.wallets_24h`, `wallets_24h_exact` | distinct wallets on the last closed day; when `exact` is false the number is an upper bound |
| `metrics.wallets_7d`, `top_wallet_share_7d`, `deployer_swap_share_7d` | from the indexer's exact per-wallet swap counts; known only when the indexer has every swap of the project's hooks for the week (a ready chain, or a chain whose swap cursor is past the week and the hook is under the per-hook pool cap; registry hooks on chains Bait does not index are out of scope, as everywhere else); shares are of swaps, not volume |
| `metrics.buys_24h_rolling`, `sells_24h_rolling` | GeckoTerminal's rolling 24h counts |
| `metrics.score_trending`, `rank_trending`, `trending_min_vol_usd` | the site's trending score and rank (null when not ranked) |
| `links.website`, `links.github`, `links.x` | validated links; may be null |

The rules behind every figure are in [`architecture.md`](architecture.md). Nothing here is an endorsement: hooks are
third-party code in the swap path, and a listing, badge or rank says nothing about safety.

## Removed fields

Removed on 2026-10-24 with the retired Nansen wallet labels (collected once in early October 2026, never refreshed):
`metrics.smart_wallets_24h`, `top_wallets[].labels` and `top_wallets[].smart_money`. `api_version` stays 1.
