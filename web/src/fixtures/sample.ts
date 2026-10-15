// Fixture dataset, served only with BAIT_FIXTURES=1 and no DATABASE_URL.
// Built from terminal-sample.json (the Oct 8, 2026 snapshot behind demo/fomo-blend.template.html:
// GeckoTerminal pools, hooks resolved on-chain, Uniswap hooklist). Buy/sell splits and top wallets
// are deterministic sample rows, not live data.
import type {
  Category,
  ChainId,
  DigestView,
  Eligibility,
  HookFlags,
  HookView,
  PoolView,
  ProjectDetail,
  ReceiptView,
  WalletLabel,
  WalletView,
} from "@/lib/types";
import data from "./terminal-sample.json";
import { loadE2eFixtures } from "./e2e";

const NOW = Date.parse("2026-10-08T16:00:00Z");
const DAY = 86_400_000;

interface Raw {
  name: string;
  cat: string;
  chains: string[];
  what: string;
  why?: string;
  rec?: Array<{ title: string; url: string; source: string }>;
  vol7: number;
  prev: number;
  vol24: number;
  tvl: number;
  wallets: number;
  txs: number;
  pools: number;
  hooks: number;
  routable: Eligibility;
  hooklisted: boolean;
  audited: boolean;
  upg: boolean;
  flags: string[];
  first: string;
  series?: number[];
  top: Array<{ pair: string; chain: string; tvl: number; v24: number; w: number; fee: string; pid: string; created: string }>;
  hook: string;
}

const CHAIN_ID: Record<string, ChainId> = {
  ethereum: 1,
  base: 8453,
  unichain: 130,
  robinhood: 4663,
  bnb: 56,
  arbitrum: 42161,
  avalanche: 43114,
  polygon: 137,
  ink: 57073,
  optimism: 10,
};

const FLAG: Record<string, keyof HookFlags> = {
  beforeInit: "beforeInitialize",
  afterInit: "afterInitialize",
  beforeAddLiquidity: "beforeAddLiquidity",
  afterAddLiquidity: "afterAddLiquidity",
  beforeRemoveLiquidity: "beforeRemoveLiquidity",
  afterRemoveLiquidity: "afterRemoveLiquidity",
  beforeSwap: "beforeSwap",
  afterSwap: "afterSwap",
  returnsDelta: "beforeSwapReturnsDelta",
  afterSwapDelta: "afterSwapReturnsDelta",
};

const COLORS = ["#5b2fff", "#ff007a", "#1b8a5a", "#ff6a00", "#2563eb", "#0ea5e9", "#c2410c", "#7c3aed", "#0f766e", "#b45309"];
const LABELS = [["Smart Trader"], ["Fund"], ["DEX Trader", "High Balance"], ["Arbitrage Bot"], ["Token Millionaire"], [], ["Smart LP"], []];

function hash(s: string): number {
  let x = 0;
  for (let i = 0; i < s.length; i++) x = (x * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(x);
}

function slugify(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function feeCode(f: string): number {
  if (f === "dynamic") return 0x800000;
  const n = parseFloat(f);
  return Number.isFinite(n) ? Math.round(n * 10_000) : 0;
}

/** "blog.uniswap.org · 28d" -> source + published date */
function receipt(r: { title: string; url: string; source: string }): ReceiptView {
  const [source, ageTxt] = r.source.split(" · ");
  const days = ageTxt ? parseInt(ageTxt, 10) : NaN;
  return { url: r.url, title: r.title, source, publishedAt: Number.isFinite(days) ? new Date(NOW - days * DAY).toISOString() : "" };
}

function addr(seed: string): string {
  let out = "";
  let x = hash(seed) || 1;
  while (out.length < 40) {
    x = (x * 1103515245 + 12345) % 2147483648;
    out += x.toString(16).padStart(8, "0");
  }
  return "0x" + out.slice(0, 40);
}

/** Sample top wallets for the project's busiest pool: volume tapers, labels rotate. */
function wallets(raw: Raw, chainId: ChainId, h: number): WalletView[] {
  const n = Math.min(8, Math.max(2, Math.round(Math.log10(raw.wallets + 10) * 2.5)));
  const out: WalletView[] = [];
  let left = Math.max(raw.vol24 * 0.6, 0);
  for (let i = 0; i < n; i++) {
    const v = left * (0.34 - i * 0.02);
    left -= v;
    const trades = 3 + ((h >> i) % 40);
    const buys = Math.round(trades * (0.35 + ((h >> (i + 2)) % 50) / 100));
    // one deliberately long label list so the phone layout's overflow test has something to wrap
    const labels = raw.name === "StablePairHook" && i === 0 ? ["Relay Protocol: EOA Bridge Relay Protocol mevrevolutionx.eth"] : LABELS[(h + i) % LABELS.length];
    out.push({
      address: addr(`${raw.name}:${i}`),
      chainId,
      volumeUsd: v,
      trades,
      buys,
      sells: trades - buys,
      lastTradeAt: new Date(NOW - (i * 37 + (h % 30)) * 60_000).toISOString(),
      labels,
      smartMoney: labels.some((l) => /smart|fund/i.test(l)),
    });
  }
  return out;
}

function build(raw: Raw, i: number): ProjectDetail {
  const h = hash(raw.name);
  const chains = raw.chains.map((c) => CHAIN_ID[c]).filter((c): c is ChainId => c != null);
  const hookAddr = raw.hook.toLowerCase();
  const dynamic = raw.top.some((t) => t.fee === "dynamic");
  const flags = Object.fromEntries(raw.flags.map((f) => [FLAG[f], true]).filter(([k]) => k)) as Partial<HookFlags>;
  const firstSeenAt = new Date(raw.first + "T00:00:00Z").toISOString();
  const hooks: HookView[] = chains.map((c) => ({
    address: hookAddr,
    chainId: c,
    name: raw.name,
    description: raw.what,
    flags,
    properties: { dynamicFee: dynamic, upgradeable: raw.upg },
    verifiedSource: true,
    auditUrl: raw.audited ? "https://github.com/Uniswap/hooklist" : null,
    hooklisted: raw.hooklisted,
    deployer: null,
    firstSeenAt,
  }));
  const pools: PoolView[] = raw.top
    .filter((t) => CHAIN_ID[t.chain] != null)
    .map((t) => {
      const [s0, s1] = t.pair.split(" / ");
      return {
        poolId: t.pid,
        chainId: CHAIN_ID[t.chain],
        hookAddress: hookAddr,
        token0: "",
        token1: "",
        token0Symbol: s0 ?? null,
        token1Symbol: s1 ?? null,
        fee: feeCode(t.fee),
        tickSpacing: 60,
        createdAt: new Date(t.created + "T00:00:00Z").toISOString(),
        tvlUsd: t.tvl,
        vol7d: t.v24 * 7,
        tx7d: 0,
        vol24h: t.v24,
        wallets24h: t.w,
        walletsExact: true,
        eligibility: "unknown" as const, // derive() evaluates it
      };
    })
    .sort((a, b) => b.tvlUsd - a.tvlUsd);
  const busiest = [...pools].sort((a, b) => (b.vol24h ?? 0) - (a.vol24h ?? 0))[0];
  const top = pools[0];
  const isNew = (NOW - Date.parse(firstSeenAt)) / DAY <= 7;
  const score = raw.vol7 >= 1e4 && raw.prev >= 1e3 && !isNew ? Math.log1p(raw.vol7) * Math.min(Math.max(raw.vol7 / raw.prev, 0.5), 10) : 0;
  const buyShare = 0.42 + (h % 30) / 100;
  const buys = raw.txs > 0 ? Math.round(raw.txs * buyShare) : null;
  const topWallets = busiest ? wallets(raw, busiest.chainId, h) : [];
  return {
    id: i + 1,
    slug: slugify(raw.name),
    name: raw.name,
    category: raw.cat as Category,
    secondaryCategories: [],
    isFactory: false,
    whatLine: raw.what,
    whyBlurb: raw.why ?? "",
    whyBlurbAt: raw.why ? "2026-10-08T06:20:00.000Z" : null,
    website: null,
    github: null,
    xHandle: null,
    firstSeenAt,
    chains,
    flagLabels: [],
    verified: true,
    hooklisted: raw.hooklisted,
    upgradeable: raw.upg,
    metrics: { tvlUsd: raw.tvl, vol7d: raw.vol7, vol7dPrev: raw.prev, swaps7d: raw.txs * 7, poolsN: raw.pools, hooksN: raw.hooks },
    signals: { smartMoneyLps: null, lastCommitAt: null, lastExaHitAt: null, routable: raw.routable, auditCount: raw.audited ? 1 : 0, auditUrl: raw.audited ? "https://github.com/Uniswap/hooklist" : null },
    receipts: (raw.rec ?? []).map(receipt),
    topPool: top ? { poolId: top.poolId, chainId: top.chainId, token0Symbol: top.token0Symbol, token1Symbol: top.token1Symbol, hookAddress: hookAddr } : null,
    usage: {
      vol24h: raw.vol24,
      vol24hRolling: raw.vol24,
      tx24h: raw.txs,
      wallets24h: raw.wallets,
      walletsExact: true,
      buys24h: buys,
      sells24h: buys == null ? null : raw.txs - buys,
      buyers24h: raw.wallets > 0 ? Math.round(raw.wallets * (buyShare + 0.05)) : null,
      sellers24h: raw.wallets > 0 ? Math.round(raw.wallets * (1.05 - buyShare)) : null,
      smartWallets: topWallets.filter((w) => w.smartMoney).length,
      wallets7d: null,
      topWalletShare7d: null,
    },
    isNew,
    isSpam: false,
    isWash: false,
    washReason: null,
    scoreTrending: score,
    color: COLORS[h % COLORS.length],
    hooks,
    pools,
    series: raw.series ?? [],
    topWallets,
  };
}

const candidateFixtures = loadE2eFixtures();
export const SAMPLE_PROJECTS: ProjectDetail[] = candidateFixtures?.projects ?? (data as Raw[]).map(build);
export const SAMPLE_ALIASES: Record<string, string> = candidateFixtures?.aliases ?? {};
export const SAMPLE_FRESHNESS = candidateFixtures?.freshness ?? null;

/** wallet_labels stand-in: every sample top wallet with a label. Key: `${chainId}:${address}`. */
export const SAMPLE_WALLET_LABELS: Map<string, WalletLabel> = new Map(
  SAMPLE_PROJECTS.flatMap((p) => p.topWallets.filter((w) => w.labels.length).map((w) => [`${w.chainId}:${w.address}`, { labels: w.labels, smartMoney: w.smartMoney }] as const)),
);

export const SAMPLE_DIGESTS: DigestView[] = [
  {
    week: "2026-10-05",
    title: "Week of October 5, 2026",
    intro: "Four new projects cleared the bar. StablePairHook led trending on USDC / USDT.",
    createdAt: "2026-10-05T06:10:00Z",
    items: [
      { slug: "hookrmodularhookv6", name: "HookrModularHookV6", kind: "new", line: "MEV / LVR hook on Robinhood Chain, $63.1M in its first week.", vol7d: 63_129_871 },
      { slug: "claus", name: "Claus", kind: "new", line: "Dynamic-fee hook on Ethereum, $12.0M 7d volume.", vol7d: 11_983_182 },
      { slug: "stablepairhook", name: "StablePairHook", kind: "trending", line: "7d volume $639.7M.", vol7d: 639_691_226 },
      { slug: "fablesrampeth", name: "FablesRampETH", kind: "trending", line: "7d volume $413.1M.", vol7d: 413_102_966 },
      { slug: "angstrom", name: "Angstrom", kind: "trending", line: "7d volume $120.6M, +12%.", vol7d: 120_569_404 },
    ],
  },
  {
    week: "2026-09-28",
    title: "Week of September 28, 2026",
    intro: "Quiet week. No new projects cleared the TVL or swap floor.",
    createdAt: "2026-09-28T06:10:00Z",
    items: [
      { slug: "stablepairhook", name: "StablePairHook", kind: "trending", line: "7d volume $865.6M.", vol7d: 865_569_652 },
      { slug: "fablesrampeth", name: "FablesRampETH", kind: "trending", line: "7d volume $547.5M.", vol7d: 547_526_543 },
    ],
  },
];

export const SAMPLE_NOW = candidateFixtures ? Date.parse(candidateFixtures.observedAt) : NOW;
