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

export type Tab = "trending" | "new" | "verified" | "all";

export const TABS: Array<[Tab, string]> = [
  ["trending", "Trending"],
  ["new", "New"],
  ["verified", "Verified"],
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
  routable: boolean;
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
  routable: boolean;
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
