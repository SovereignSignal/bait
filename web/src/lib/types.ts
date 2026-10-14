// View types shared by the DB query layer and the fixture fallback.
// Everything the pages render comes through these shapes.

export type ChainId = 1 | 10 | 56 | 130 | 137 | 4663 | 8453 | 42161 | 43114 | 57073;

export type Category =
  | "dynamic-fee"
  | "launchpad"
  | "orders"
  | "lp-management"
  | "compliance"
  | "oracle"
  | "mev-lvr"
  | "lending"
  | "gaming-nft"
  | "other";

/** Routing eligibility (rules in lib/routable.ts): the app will route / will not route / not enough data to say. */
export type Eligibility = "eligible" | "ineligible" | "unknown";

export type Tab = "trending" | "new" | "verified" | "all";

export const TABS: Array<[Tab, string]> = [
  ["trending", "Trending"],
  ["new", "New"],
  ["verified", "In Uniswap app"],
  ["all", "All"],
];

/** Visible label in the 320 px list column, where the full names plus the star need ~318 px of ~294; the full name stays the accessible name. */
export const TAB_SHORT: Partial<Record<Tab, string>> = { verified: "In app" };

export interface HookFlags {
  beforeInitialize: boolean;
  afterInitialize: boolean;
  beforeAddLiquidity: boolean;
  afterAddLiquidity: boolean;
  beforeRemoveLiquidity: boolean;
  afterRemoveLiquidity: boolean;
  beforeSwap: boolean;
  afterSwap: boolean;
  beforeDonate: boolean;
  afterDonate: boolean;
  beforeSwapReturnsDelta: boolean;
  afterSwapReturnsDelta: boolean;
  afterAddLiquidityReturnsDelta: boolean;
  afterRemoveLiquidityReturnsDelta: boolean;
}

export interface HookProperties {
  dynamicFee: boolean;
  upgradeable: boolean;
  requiresCustomSwapData?: boolean;
  vanillaSwap?: boolean;
  swapAccess?: string;
}

export interface HookView {
  address: string;
  chainId: ChainId;
  name: string;
  description: string | null;
  flags: Partial<HookFlags>;
  properties: Partial<HookProperties>;
  verifiedSource: boolean;
  auditUrl: string | null;
  /** in the Uniswap hooklist as of the last ingest; null = not yet evaluated */
  hooklisted: boolean | null;
  deployer: string | null;
  firstSeenAt: string; // ISO
}

export interface PoolView {
  poolId: string;
  chainId: ChainId;
  hookAddress: string;
  token0: string; // address
  token1: string;
  token0Symbol: string | null;
  token1Symbol: string | null;
  fee: number; // PoolKey.fee; 8388608 (0x800000) means dynamic
  tickSpacing: number;
  createdAt: string; // ISO
  tvlUsd: number | null; // null = unknown (no GeckoTerminal reserve, unpriced tokens), never shown as $0
  /** null when the pool's on-chain volume is unknown (NULL cumulative volume) */
  vol7d: number | null;
  /** swaps over the 7 closed UTC days; null unless all 7 are known */
  tx7d: number | null;
  /** volume of the closed UTC day before the latest run; null when unknown */
  vol24h: number | null;
  /** distinct wallets when walletsExact, else buyers + sellers (an upper bound); latest pool_stats day */
  wallets24h: number | null;
  walletsExact: boolean;
  /** routing eligibility of this pool, evaluated with its own fee (set by derive in db/queries.ts) */
  eligibility: Eligibility;
}

/** Usage per project. Volume, swaps and wallets cover the closed UTC day before the latest run; buys/sells are
 * GeckoTerminal's rolling 24 h. null = no data (table empty, missing, not every day known). */
export interface Usage {
  vol24h: number | null;
  /** GeckoTerminal's rolling 24 h at the run (sum of pool_stats.vol_24h_rolling); only the pace line uses it */
  vol24hRolling: number | null;
  /** swaps on the closed day; null unless every pool's day is known */
  tx24h: number | null;
  /** distinct across the project's pools when walletsExact, else an upper bound (pools added, buyers + sellers) */
  wallets24h: number | null;
  walletsExact: boolean | null;
  buys24h: number | null;
  sells24h: number | null;
  buyers24h: number | null;
  sellers24h: number | null;
  /** distinct smart_money wallets among pool_top_wallets, latest day */
  smartWallets: number | null;
}

export interface WalletView {
  address: string; // lowercase
  chainId: ChainId;
  volumeUsd: number;
  trades: number;
  buys: number | null;
  sells: number | null;
  lastTradeAt: string | null; // ISO
  labels: string[];
  smartMoney: boolean;
}

export interface WalletLabel {
  labels: string[];
  smartMoney: boolean;
}

/** One trade from /api/trades/[chain]/[pool]. */
export interface TradeView {
  ts: string; // ISO
  kind: "buy" | "sell";
  usd: number;
  wallet: string; // lowercase
  tx: string;
  labels?: string[];
  smartMoney?: boolean;
}

export interface ReceiptView {
  url: string;
  title: string;
  source: string; // "x.com", "blog", "docs", ...
  publishedAt: string; // ISO
}

export interface SignalsView {
  /** null = no Nansen coverage on any of the project's chains; tile hidden */
  smartMoneyLps: number | null;
  /** ISO of the last GitHub commit, if any */
  lastCommitAt: string | null;
  /** ISO of the last Exa hit, if any */
  lastExaHitAt: string | null;
  routable: Eligibility;
  auditCount: number;
  /** first published audit link (http/https only) */
  auditUrl?: string | null;
}

export interface Metrics {
  tvlUsd: number;
  /** the 7 closed UTC days; null when every pool's volume is unknown (shown as unknown, never $0) */
  vol7d: number | null;
  vol7dPrev: number | null;
  swaps7d: number | null; // swaps over the 7 closed UTC days; null unless every day is known
  poolsN: number;
  hooksN: number;
}

export interface ProjectSummary {
  id: number;
  slug: string;
  name: string;
  category: Category;
  secondaryCategories: Category[];
  isFactory: boolean;
  whatLine: string;
  whyBlurb: string;
  /** when whyBlurb was written (ISO); it is rewritten only when signals change */
  whyBlurbAt?: string | null;
  website: string | null;
  github: string | null;
  xHandle: string | null;
  firstSeenAt: string; // ISO
  chains: ChainId[];
  /** short flag labels for the well foot, e.g. ["beforeSwap", "returnsDelta"] */
  flagLabels: string[];
  verified: boolean;
  /** at least one of the project's hooks is in the Uniswap hooklist */
  hooklisted: boolean;
  upgradeable: boolean;
  metrics: Metrics;
  signals: SignalsView;
  receipts: ReceiptView[];
  topPool: { poolId: string; chainId: ChainId; token0Symbol: string | null; token1Symbol: string | null; hookAddress: string | null } | null;
  usage: Usage;
  isNew: boolean;
  /** DESIGN.md spam filter: no pools, or all pools under $100 TVL and < 10 txs lifetime, unless $1M+ moved. Hidden everywhere (404). */
  isSpam: boolean;
  /** wash-like volume: listed in All and on its page with a badge, never in Trending, New, movers or Leaders */
  isWash: boolean;
  /** comma list of reasons: 24h, 7d, concentrated, vol_tvl, no_liquidity, symmetric, uniform, unverified */
  washReason: string | null;
  scoreTrending: number;
  /** 1-based trending rank, set when listing the Trending tab */
  rank?: number;
  /** brand color for the logo circle */
  color: string;
}

export interface ProjectDetail extends ProjectSummary {
  hooks: HookView[];
  pools: PoolView[];
  /** daily volume, oldest first (last ~14 days); null = volume unknown that day (a gap); project page only */
  series: (number | null)[];
  /** top wallets across the project's pools, latest pool_top_wallets day; project page only */
  topWallets: WalletView[];
  /** YYYY-MM-DD: the closed UTC day the series, the pools' day volume and the day's wallets/swaps end on (the latest
   * run's day - 1); project page only, absent in fixtures */
  through?: string | null;
}

/** The light row the terminal list, movers and client search work from. */
export interface RowView {
  slug: string;
  name: string;
  whatLine: string;
  color: string;
  category: Category;
  secondaryCategories: Category[];
  chains: ChainId[];
  firstSeenAt: string;
  isNew: boolean;
  isWash: boolean;
  scoreTrending: number;
  /** null = unknown (see Metrics.vol7d) */
  vol7d: number | null;
  vol7dPrev: number | null;
  vol24h: number | null;
  tvlUsd: number;
  routable: Eligibility;
  rank?: number;
}

/** slug -> hook addresses (lowercase, capped), for 0x search. Served by /api/hooks, not shipped with the rows: random hex
 * does not compress and was half of the home page's transfer. */
export type HookIndex = Record<string, string[]>;

export interface LeaderEntry {
  row: RowView;
  value: number;
}

export interface LeadersView {
  totals: { vol7d: number; wallets24h: number | null; projects: number; chains: number };
  wallets: LeaderEntry[];
  volume: LeaderEntry[];
  tvl: LeaderEntry[];
  smartMoney: LeaderEntry[];
}

export interface DigestItem {
  slug: string;
  name: string;
  kind: "new" | "trending";
  line: string;
  /** null = unknown 7-day volume, rendered "–" */
  vol7d: number | null;
}

export interface DigestView {
  week: string; // YYYY-MM-DD (Monday)
  title: string;
  intro: string;
  items: DigestItem[];
  createdAt: string;
}

export interface FeedQuery {
  tab: Tab;
  chain?: ChainId;
  category?: Category;
}

/** Short chip labels, as in the terminal mock. */
export const CATEGORY_SHORT: Record<Category, string> = {
  "dynamic-fee": "Dynamic fee",
  launchpad: "Launchpad",
  orders: "Orders",
  "lp-management": "LP management",
  compliance: "Compliance",
  oracle: "Oracle",
  "mev-lvr": "MEV / LVR",
  lending: "Lending",
  "gaming-nft": "Gaming / NFT",
  other: "Other",
};

export const CATEGORY_LABEL: Record<Category, string> = {
  "dynamic-fee": "Dynamic fee",
  launchpad: "Launchpad",
  orders: "Orders",
  "lp-management": "LP management",
  compliance: "Compliance",
  oracle: "Oracle",
  "mev-lvr": "MEV / LVR",
  lending: "Lending",
  "gaming-nft": "Gaming / NFT",
  other: "Other",
};

export const CATEGORIES = Object.keys(CATEGORY_LABEL) as Category[];

export function isCategory(s: string | undefined): s is Category {
  return !!s && s in CATEGORY_LABEL;
}

// ---------- dev radar (/building) and the launch feed ----------

export type RadarKind = "registry_submission" | "repo" | "hackathon" | "incubator" | "audit" | "launch_post";

/** Display order and pill labels for /building. */
export const RADAR_KINDS: Array<[RadarKind, string]> = [
  ["launch_post", "Launch"],
  ["audit", "Audit"],
  ["hackathon", "Hackathon"],
  ["incubator", "Incubator"],
  ["repo", "Repo"],
  ["registry_submission", "Registry"],
];

export function isRadarKind(s: string | null | undefined): s is RadarKind {
  return RADAR_KINDS.some(([k]) => k === s);
}

/** One radar_items row (score < 0.5 already filtered out). chainId may be any chain, not just the ten. */
export interface RadarItemView {
  url: string;
  kind: RadarKind;
  title: string;
  summary: string | null;
  source: string | null;
  repo: string | null;
  stars: number | null;
  chainId: number | null;
  hookAddress: string | null;
  projectSlug: string | null;
  publishedAt: string | null;
  lastSeen: string;
}

/** A hooked pool initialization from v4_pools (the on-chain indexer). Any chain id. */
export interface LaunchView {
  chainId: number;
  poolId: string;
  hook: string;
  projectSlug: string | null;
  projectName: string | null;
  currency0: string;
  currency1: string;
  symbol0: string | null;
  symbol1: string | null;
  fee: number | null;
  blockTime: string | null;
  txHash: string | null;
}
