// Mirrors db/migrations/0001_init.sql (and pipeline/bait/db.py). Column
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
  },
  (t) => [primaryKey({ columns: [t.projectId, t.day] }), index("project_daily_day_score_idx").on(t.day, t.scoreTrending)],
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
