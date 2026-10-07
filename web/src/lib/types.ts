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

export type Tab = "new" | "trending" | "all";

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
}

export interface DayPoint {
  day: string; // YYYY-MM-DD
  tvlUsd: number;
  vol7d: number;
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
  topPool: { poolId: string; chainId: ChainId; token0Symbol: string | null; token1Symbol: string | null } | null;
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
  history: DayPoint[]; // last 30 days, ascending
}

export interface ContextStats {
  hooksTracked: number;
  hooksDelta7d: number;
  projects: number;
  projectsNew7d: number;
  tvlUsd: number;
  tvlDelta7dPct: number;
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
