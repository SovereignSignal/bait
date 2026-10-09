// Mirrors db/migrations/0001..0005 (and pipeline/bait/db.py). Column
// names are snake_case in Postgres, camelCase here. Keep the three in sync.
import { pgTable, serial, text, integer, bigint, boolean, jsonb, timestamp, date, doublePrecision, primaryKey, index } from "drizzle-orm/pg-core";

/** Subgraph Token entity as stored on pools.token0 / token1. */
export interface TokenJson {
  id: string;
  symbol?: string | null;
  name?: string | null;
  decimals?: string | number | null;
}

export const projects = pgTable(
  "projects",
  {
    id: serial("id").primaryKey(),
    slug: text("slug").notNull().unique(),
    name: text("name").notNull(),
    normName: text("norm_name"),
    category: text("category"),
    whatLine: text("what_line"),
    whyBlurb: text("why_blurb"),
    website: text("website"),
    github: text("github"),
    xHandle: text("x_handle"),
    enrichment: jsonb("enrichment").$type<Record<string, unknown>>(),
    enrichedAt: timestamp("enriched_at", { withTimezone: true }),
    blurbAt: timestamp("blurb_at", { withTimezone: true }),
    firstSeenAt: timestamp("first_seen_at", { withTimezone: true }).notNull(),
    isFactory: boolean("is_factory").notNull().default(false),
  },
  (t) => [index("projects_norm_name_idx").on(t.normName)],
);

export const hooks = pgTable(
  "hooks",
  {
    address: text("address").notNull(), // lowercase hex
    chainId: integer("chain_id").notNull(),
    name: text("name"),
    description: text("description"),
    flags: jsonb("flags").$type<Record<string, boolean>>(),
    properties: jsonb("properties").$type<Record<string, unknown>>(),
    verifiedSource: boolean("verified_source"),
    auditUrl: text("audit_url"),
    hooklisted: boolean("hooklisted"),
    deployer: text("deployer"),
    codeHash: text("code_hash"),
    projectId: integer("project_id").references(() => projects.id),
    firstSeenAt: timestamp("first_seen_at", { withTimezone: true }).notNull(),
  },
  (t) => [primaryKey({ columns: [t.address, t.chainId] }), index("hooks_project_id_idx").on(t.projectId), index("hooks_code_hash_idx").on(t.codeHash)],
);

export const pools = pgTable(
  "pools",
  {
    poolId: text("pool_id").notNull(), // bytes32 poolId hex
    chainId: integer("chain_id").notNull(),
    hookAddress: text("hook_address").notNull(),
    token0: jsonb("token0").$type<TokenJson>(),
    token1: jsonb("token1").$type<TokenJson>(),
    fee: integer("fee"), // LP fee; 8388608 (0x800000) = dynamic
    tickSpacing: integer("tick_spacing"),
    createdAt: timestamp("created_at", { withTimezone: true }),
  },
  (t) => [primaryKey({ columns: [t.poolId, t.chainId] }), index("pools_hook_idx").on(t.chainId, t.hookAddress)],
);

export const poolSnapshots = pgTable(
  "pool_snapshots",
  {
    poolId: text("pool_id").notNull(),
    chainId: integer("chain_id").notNull(),
    day: date("day").notNull(),
    tvlUsd: doublePrecision("tvl_usd"),
    volumeUsdCum: doublePrecision("volume_usd_cum"),
    txCountCum: bigint("tx_count_cum", { mode: "number" }), // swaps + liquidity mods ("txs")
    volumeUsdDay: doublePrecision("volume_usd_day"), // that UTC day's volume; NULL = unknown
    txCountDay: bigint("tx_count_day", { mode: "number" }), // that UTC day's swaps; NULL = unknown
    estimated: boolean("estimated"),
  },
  (t) => [primaryKey({ columns: [t.poolId, t.chainId, t.day] }), index("pool_snapshots_day_idx").on(t.day)],
);

export const projectDaily = pgTable(
  "project_daily",
  {
    projectId: integer("project_id")
      .notNull()
      .references(() => projects.id),
    day: date("day").notNull(),
    tvlUsd: doublePrecision("tvl_usd"),
    vol7d: doublePrecision("vol_7d"),
    vol7dPrev: doublePrecision("vol_7d_prev"),
    swaps7d: bigint("swaps_7d", { mode: "number" }),
    poolsN: integer("pools_n"),
    scoreTrending: doublePrecision("score_trending"), // 0 when vol_7d < $10k
    isNew: boolean("is_new").notNull().default(false),
    isSpam: boolean("is_spam").notNull().default(false),
    windowEnd: date("window_end"), // the last closed UTC day the row reports (day - 1)
    vol24h: doublePrecision("vol_24h"),
    swaps24h: bigint("swaps_24h", { mode: "number" }), // NULL = not every day known (swaps7d likewise)
    wallets24h: integer("wallets_24h"),
    walletsExact: boolean("wallets_exact"), // true = distinct across the project's pools; false = upper bound
    isWash: boolean("is_wash").notNull().default(false),
    washReason: text("wash_reason"), // comma list: 24h, 7d, vol_tvl, no_liquidity, symmetric
  },
  (t) => [primaryKey({ columns: [t.projectId, t.day] }), index("project_daily_day_score_idx").on(t.day, t.scoreTrending)],
);

/** Per-pool 24h activity (GeckoTerminal). buys/sells/buyers/sellers can be null on rows written before they existed. */
export const poolStats = pgTable(
  "pool_stats",
  {
    poolId: text("pool_id").notNull(),
    chainId: integer("chain_id").notNull(),
    day: date("day").notNull(),
    vol24h: doublePrecision("vol_24h"),
    traders24h: integer("traders_24h"),
    buys24h: integer("buys_24h"),
    sells24h: integer("sells_24h"),
    buyers24h: integer("buyers_24h"),
    sellers24h: integer("sellers_24h"),
    vol24hRolling: doublePrecision("vol_24h_rolling"), // GeckoTerminal h24 at the run (rolling)
    wallets24h: integer("wallets_24h"), // distinct; NULL = unknown (traders24h is buyers + sellers)
    windowEnd: timestamp("window_end", { withTimezone: true }),
  },
  (t) => [primaryKey({ columns: [t.poolId, t.chainId, t.day] })],
);

/** Top wallets per pool per day by volume (pipeline step "activity"). Addresses lowercase. */
export const poolTopWallets = pgTable(
  "pool_top_wallets",
  {
    poolId: text("pool_id").notNull(),
    chainId: integer("chain_id").notNull(),
    day: date("day").notNull(),
    address: text("address").notNull(),
    volumeUsd: doublePrecision("volume_usd"),
    trades: integer("trades"),
    buys: integer("buys"),
    sells: integer("sells"),
    lastTradeAt: timestamp("last_trade_at", { withTimezone: true }),
  },
  (t) => [primaryKey({ columns: [t.poolId, t.chainId, t.day, t.address] })],
);

/** Nansen labels per wallet. An empty list is a real answer ("no labels"). */
export const walletLabels = pgTable(
  "wallet_labels",
  {
    address: text("address").notNull(),
    chainId: integer("chain_id").notNull(),
    labels: jsonb("labels").$type<string[]>(),
    smartMoney: boolean("smart_money"),
    source: text("source"),
    asOf: timestamp("as_of", { withTimezone: true }),
  },
  (t) => [primaryKey({ columns: [t.address, t.chainId] })],
);

/**
 * One row per (project, kind). Value shapes written by the pipeline:
 *   nansen {addresses_checked, labeled: [{address, chain_id, labels: string[]}], smart_money: bool}
 *   github {repo, stars, last_commit_at, commits_30d}
 *   exa    {hits_30d, latest, top: [{url, title}]}
 *   alive  {alive: bool}
 */
export const signals = pgTable(
  "signals",
  {
    projectId: integer("project_id")
      .notNull()
      .references(() => projects.id),
    kind: text("kind").notNull(),
    value: jsonb("value").$type<Record<string, unknown>>(),
    asOf: timestamp("as_of", { withTimezone: true }).notNull(), // last time value changed
  },
  (t) => [primaryKey({ columns: [t.projectId, t.kind] })],
);

export const receipts = pgTable(
  "receipts",
  {
    projectId: integer("project_id")
      .notNull()
      .references(() => projects.id),
    url: text("url").notNull(),
    title: text("title"),
    source: text("source"), // domain
    publishedAt: timestamp("published_at", { withTimezone: true }),
  },
  (t) => [primaryKey({ columns: [t.projectId, t.url] })],
);

/** Digest card as written by pipeline/bait/steps/digest.py. */
export interface DigestCard {
  slug: string;
  name: string;
  category: string | null;
  what_line: string | null;
  tvl_usd: number | null;
  vol_7d: number | null;
  vol_7d_prev: number | null;
  swaps_7d: number | null;
  pools_n: number | null;
  score_trending: number | null;
}

export interface DigestBody {
  week_of: string;
  generated_at: string;
  totals: { projects: number; tvl_usd: number; vol_7d: number };
  new: DigestCard[];
  trending: DigestCard[];
}

export const digests = pgTable("digests", {
  weekOf: date("week_of").primaryKey(), // the Monday
  body: jsonb("body").$type<DigestBody>().notNull(),
});

// ---------- on-chain indexer (db/migrations/0004_indexer.sql) ----------

/** Hooked pools from PoolManager Initialize logs. Any chain id (not only the ten). Hex lowercase. */
export const v4Pools = pgTable(
  "v4_pools",
  {
    chainId: integer("chain_id").notNull(),
    poolId: text("pool_id").notNull(),
    hookAddress: text("hook_address").notNull(),
    currency0: text("currency0").notNull(), // 0x0 = native
    currency1: text("currency1").notNull(),
    fee: integer("fee"), // bit 0x800000 = dynamic
    tickSpacing: integer("tick_spacing"),
    sqrtPriceX96: text("sqrt_price_x96"),
    tick: integer("tick"),
    blockNumber: bigint("block_number", { mode: "number" }),
    blockTime: timestamp("block_time", { withTimezone: true }),
    txHash: text("tx_hash"),
  },
  (t) => [primaryKey({ columns: [t.chainId, t.poolId] }), index("v4_pools_hook_idx").on(t.chainId, t.hookAddress)],
);

/** Per pool per UTC day from Swap logs; volume_usd is approximate (current prices). */
export const poolDailyOnchain = pgTable(
  "pool_daily_onchain",
  {
    chainId: integer("chain_id").notNull(),
    poolId: text("pool_id").notNull(),
    day: date("day").notNull(),
    swaps: integer("swaps").notNull().default(0),
    wallets: integer("wallets").notNull().default(0),
    volumeUsd: doublePrecision("volume_usd").notNull().default(0),
  },
  (t) => [primaryKey({ columns: [t.chainId, t.poolId, t.day] }), index("pool_daily_onchain_day_idx").on(t.day)],
);

// ---------- dev radar (db/migrations/0005_radar.sql, pipeline step "radar") ----------

/** One row per URL. kind: registry_submission | repo | hackathon | incubator | audit | launch_post.
 *  score: Claude relevance for web results (rows < 0.5 are hidden); null for GitHub rows. */
export const radarItems = pgTable(
  "radar_items",
  {
    url: text("url").primaryKey(),
    kind: text("kind").notNull(),
    title: text("title"),
    summary: text("summary"),
    source: text("source"),
    repo: text("repo"),
    stars: integer("stars"),
    chainId: integer("chain_id"),
    hookAddress: text("hook_address"),
    projectSlug: text("project_slug"),
    publishedAt: timestamp("published_at", { withTimezone: true }),
    firstSeen: timestamp("first_seen", { withTimezone: true }).notNull(),
    lastSeen: timestamp("last_seen", { withTimezone: true }).notNull(),
    score: doublePrecision("score"),
  },
  (t) => [index("radar_items_kind_published_idx").on(t.kind, t.publishedAt)],
);

// ---------- run receipts (db/migrations/0009_run_receipts.sql): one row per nightly pipeline run ----------

export const runReceipts = pgTable("run_receipts", {
  id: serial("id").primaryKey(),
  day: date("day").notNull(),
  startedAt: timestamp("started_at", { withTimezone: true }).notNull(),
  finishedAt: timestamp("finished_at", { withTimezone: true }).notNull(),
  exitCode: integer("exit_code").notNull(),
  softErrors: integer("soft_errors").notNull().default(0),
  steps: jsonb("steps").$type<Record<string, Record<string, unknown>>>().notNull(),
  codeSha: text("code_sha"),
  notes: text("notes"),
});
