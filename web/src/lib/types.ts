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
  tvlUsd: number;
  vol7d: number;
  tx7d: number;
  /** null until two daily snapshots (or a pool_stats row) exist */
  vol24h: number | null;
  /** pool_stats.traders_24h, latest day */
  wallets24h: number | null;
  /** routing eligibility of this pool, evaluated with its own fee (set by derive in db/queries.ts) */
  eligibility: Eligibility;
}

/** 24h usage per project. null = no data yet (table empty, missing, or not backfilled). */
export interface Usage {
  vol24h: number | null;
  /** tx_count_cum delta over the last day (swaps + liquidity mods) */
  tx24h: number | null;
  /** sum of pool_stats.traders_24h over the project's pools, latest day */
  wallets24h: number | null;
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
}

export interface Metrics {
  tvlUsd: number;
  vol7d: number;
  vol7dPrev: number;
  swaps7d: number; // Pool.txCount-derived; labelled "Txs" in the UI
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
  /** DESIGN.md spam filter: no pools, or all pools under $100 TVL and < 10 txs lifetime. Hidden from All. */
  isSpam: boolean;
  scoreTrending: number;
  /** 1-based trending rank, set when listing the Trending tab */
  rank?: number;
  /** brand color for the logo circle */
  color: string;
}

export interface ProjectDetail extends ProjectSummary {
  hooks: HookView[];
  pools: PoolView[];
  /** daily volume, oldest first (last ~14 days); filled on the project page only */
  series: number[];
  /** top wallets across the project's pools, latest pool_top_wallets day; project page only */
  topWallets: WalletView[];
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
  scoreTrending: number;
  vol7d: number;
  vol7dPrev: number;
  vol24h: number | null;
  tvlUsd: number;
  routable: Eligibility;
  audited: boolean;
  /** hook addresses (lowercase, capped) for address-prefix search */
  hooks: string[];
  rank?: number;
}

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
  vol7d: number;
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
  "lp-management": "LP mgmt",
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
