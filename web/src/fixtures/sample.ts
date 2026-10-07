// Fixture dataset used when DATABASE_URL is unset. The first four projects and
// their figures are lifted from mock.html; the rest are plausible sample rows.
// All numbers are sample figures, not live data.
import type {
  ChainId,
  Category,
  DayPoint,
  DigestView,
  HookFlags,
  HookProperties,
  HookView,
  PoolView,
  ProjectDetail,
  ReceiptView,
  ContextStats,
} from "@/lib/types";

const NOW = new Date("2026-10-07T06:00:00Z").getTime();
const DAY = 86_400_000;
const d = (daysAgo: number) => new Date(NOW - daysAgo * DAY).toISOString();

const F = (on: Array<keyof HookFlags>): Partial<HookFlags> =>
  Object.fromEntries(on.map((k) => [k, true])) as Partial<HookFlags>;
const P = (p: Partial<HookProperties> = {}): Partial<HookProperties> => ({ dynamicFee: false, upgradeable: false, ...p });

/** Deterministic pseudo-random walk so the sparklines are stable across builds. */
function history(seed: number, tvlEnd: number, volEnd: number, growth: number, days = 30): DayPoint[] {
  const out: DayPoint[] = [];
  let x = seed;
  const rnd = () => ((x = (x * 1103515245 + 12345) % 2147483648) / 2147483648 - 0.5);
  let tvl = tvlEnd / (1 + growth);
  let vol = volEnd / (1 + growth * 1.6);
  for (let i = days - 1; i >= 0; i--) {
    const t = (days - 1 - i) / (days - 1);
    const tvlTarget = (tvlEnd / (1 + growth)) * (1 + growth * t);
    const volTarget = (volEnd / (1 + growth * 1.6)) * (1 + growth * 1.6 * t);
    tvl = Math.max(0, tvlTarget * (1 + rnd() * 0.06));
    vol = Math.max(0, volTarget * (1 + rnd() * 0.18));
    out.push({ day: new Date(NOW - i * DAY).toISOString().slice(0, 10), tvlUsd: tvl, vol7d: vol });
  }
  out[out.length - 1] = { ...out[out.length - 1], tvlUsd: tvlEnd, vol7d: volEnd };
  return out;
}

const hook = (
  address: string,
  chainId: ChainId,
  name: string,
  flags: Partial<HookFlags>,
  props: Partial<HookProperties>,
  extra: Partial<HookView> = {},
): HookView => ({
  address,
  chainId,
  name,
  description: null,
  flags,
  properties: props,
  verifiedSource: true,
  auditUrl: null,
  deployer: null,
  firstSeenAt: d(120),
  ...extra,
});

const pool = (
  poolId: string,
  chainId: ChainId,
  hookAddress: string,
  t0: string,
  t1: string,
  fee: number,
  tvlUsd: number,
  vol7d: number,
  tx7d: number,
  ageDays = 90,
): PoolView => ({
  poolId,
  chainId,
  hookAddress,
  token0: "0x" + t0.toLowerCase().padEnd(40, "0").slice(0, 40),
  token1: "0x" + t1.toLowerCase().padEnd(40, "0").slice(0, 40),
  token0Symbol: t0,
  token1Symbol: t1,
  fee,
  tickSpacing: fee >= 0x800000 ? 60 : fee === 100 ? 1 : fee === 500 ? 10 : 60,
  createdAt: d(ageDays),
  tvlUsd,
  vol7d,
  tx7d,
});

const r = (url: string, title: string, source: string, daysAgo: number): ReceiptView => ({
  url,
  title,
  source,
  publishedAt: d(daysAgo),
});

const pid = (n: number) => `0x${n.toString(16).padStart(64, "0")}`;

type Seed = Omit<ProjectDetail, "metrics" | "signals" | "flagLabels" | "verified" | "upgradeable" | "chains" | "topPool" | "isSpam"> & {
  metrics: ProjectDetail["metrics"];
  signals: ProjectDetail["signals"];
};

const BUNNI_HOOK = "0x0010d0d5db05933fa0d9f7038d365e1541a41888";
const ANGSTROM_HOOK = "0x0000000aa232009084bd71a5797d089aa4edfad4";
const EULER_HOOK = "0x5fa4f0a0d2f7e4f1d7b4a0a9c1e3f5a7b9d1c3e5";
const FLAUNCH_HOOK = "0x51bba15255406cfe7099a42183302640ba7dafdc";
const CLANKER_HOOK = "0x34a45c6b61876d739400bd71228cbcbd4f53e8cc";
const DOPPLER_HOOK = "0x9c0fc2b0d4a2a3bb0c5d6e7f8a9b0c1d2e3f4ac0";
const ARRAKIS_HOOK = "0x7a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c040";
const ZORA_HOOK = "0xd61a675f8a0c67a73dc3b54fb7318b4d91409040";
const PREDICATE_HOOK = "0x917f3b1d5a2e4c6b8d0f1a3c5e7b9d1f3a5c7e88";
const SATURN_HOOK = "0x2e4d6c8b0a1f3e5d7c9b1a3f5e7d9c1b3a5f7e9c";

const SEEDS: Seed[] = [
  {
    id: 1,
    slug: "bunni",
    name: "Bunni",
    category: "lp-management",
    secondaryCategories: ["mev-lvr"],
    isFactory: false,
    color: "#5b2fff",
    whatLine:
      "Replaces the curve. LPs pick a liquidity shape and the hook rebalances it, captures LVR with an auction, and surges fees on volatility.",
    whyBlurb:
      "Volume more than doubled after the USDC/USDT pool moved to the am-AMM auction, and LPs are posting fee APRs above vanilla v3 for the same pair. It is the clearest live proof that a hook can out-earn the base curve.",
    website: "https://bunni.xyz",
    github: "https://github.com/timeless-fi/bunni-v2",
    xHandle: "bunni_xyz",
    firstSeenAt: d(210),
    isNew: false,
    scoreTrending: 48.1,
    metrics: { tvlUsd: 212_600_000, vol7d: 184_200_000, vol7dPrev: 86_900_000, swaps7d: 96_310, poolsN: 41, hooksN: 3 },
    signals: { smartMoneyLps: 4, lastCommitAt: d(2), lastExaHitAt: d(3), routable: false, auditCount: 3 },
    hooks: [
      hook(BUNNI_HOOK, 1, "BunniHook", F(["afterInitialize", "beforeAddLiquidity", "beforeSwap", "beforeSwapReturnsDelta"]), P(), {
        description:
          "Bunni's core Uniswap v4 hook that computes all swap math internally using a configurable Liquidity Density Function (LDF), applies dynamic TWAP-based and surge fees with optional am-AMM for MEV/LVR recapture.",
        auditUrl: "https://docs.bunni.xyz/docs/v2/security",
      }),
      hook(BUNNI_HOOK, 8453, "BunniHook", F(["afterInitialize", "beforeAddLiquidity", "beforeSwap", "beforeSwapReturnsDelta"]), P(), {
        auditUrl: "https://docs.bunni.xyz/docs/v2/security",
      }),
      hook(BUNNI_HOOK, 42161, "BunniHook", F(["afterInitialize", "beforeAddLiquidity", "beforeSwap", "beforeSwapReturnsDelta"]), P(), {
        auditUrl: "https://docs.bunni.xyz/docs/v2/security",
      }),
    ],
    pools: [
      pool(pid(0x1001), 1, BUNNI_HOOK, "USDC", "USDT", 0x800000, 98_400_000, 121_300_000, 48_210, 160),
      pool(pid(0x1002), 1, BUNNI_HOOK, "WETH", "USDC", 0x800000, 61_200_000, 44_600_000, 31_040, 200),
      pool(pid(0x1003), 8453, BUNNI_HOOK, "WETH", "USDC", 0x800000, 29_800_000, 12_100_000, 11_920, 150),
      pool(pid(0x1004), 42161, BUNNI_HOOK, "WETH", "USDC", 0x800000, 14_700_000, 5_400_000, 4_120, 140),
      pool(pid(0x1005), 1, BUNNI_HOOK, "wstETH", "WETH", 0x800000, 8_500_000, 800_000, 1_020, 120),
    ],
    history: history(11, 212_600_000, 184_200_000, 1.12),
    receipts: [
      r("https://x.com/bunni_xyz", "Bunni v2 fee APR vs Uniswap v3, 30-day comparison", "x.com", 3),
      r("https://blog.bunni.xyz", "How the am-AMM auction recaptures LVR", "blog", 9),
      r("https://docs.bunni.xyz", "Liquidity Density Functions, explained", "docs", 21),
    ],
  },
  {
    id: 2,
    slug: "angstrom",
    name: "Angstrom",
    category: "mev-lvr",
    secondaryCategories: ["dynamic-fee"],
    isFactory: false,
    color: "#ff007a",
    whatLine:
      "Runs a batch auction before each block's swaps so arbitrage pays LPs instead of searchers. One clearing price per block.",
    whyBlurb:
      "Sorella shipped mainnet with three majors pools and $9M seeded in the first four days. If the LVR math holds, it is the first hook that makes passive ETH/USDC LPing pay again.",
    website: "https://angstrom.xyz",
    github: "https://github.com/SorellaLabs/angstrom",
    xHandle: "SorellaLabs",
    firstSeenAt: d(4),
    isNew: true,
    scoreTrending: 36.4,
    metrics: { tvlUsd: 9_100_000, vol7d: 22_800_000, vol7dPrev: 0, swaps7d: 4_120, poolsN: 3, hooksN: 1 },
    signals: { smartMoneyLps: 2, lastCommitAt: d(0), lastExaHitAt: d(4), routable: false, auditCount: 2 },
    hooks: [
      hook(ANGSTROM_HOOK, 1, "Angstrom", F(["beforeInitialize", "beforeAddLiquidity", "beforeRemoveLiquidity", "beforeSwap", "beforeSwapReturnsDelta"]), P({ dynamicFee: true }), {
        description: "Sorella's Angstrom hook. Batches each block's swaps into a single auction so LVR flows to LPs.",
        auditUrl: "https://github.com/SorellaLabs/angstrom/tree/main/audits",
        firstSeenAt: d(4),
      }),
    ],
    pools: [
      pool(pid(0x2001), 1, ANGSTROM_HOOK, "WETH", "USDC", 0x800000, 5_900_000, 15_200_000, 2_610, 4),
      pool(pid(0x2002), 1, ANGSTROM_HOOK, "WBTC", "USDC", 0x800000, 2_100_000, 5_300_000, 980, 4),
      pool(pid(0x2003), 1, ANGSTROM_HOOK, "WETH", "USDT", 0x800000, 1_100_000, 2_300_000, 530, 3),
    ],
    history: history(22, 9_100_000, 22_800_000, 40).map((p, i) => (i < 26 ? { ...p, tvlUsd: 0, vol7d: 0 } : p)),
    receipts: [
      r("https://x.com/SorellaLabs", "Angstrom is live on mainnet", "x.com", 4),
      r("https://docs.sorellalabs.xyz", "Why LPs lose to arbitrage, and how a pre-block auction fixes it", "docs", 4),
    ],
  },
  {
    id: 3,
    slug: "eulerswap",
    name: "EulerSwap",
    category: "lending",
    secondaryCategories: [],
    isFactory: true,
    color: "#1b5e3f",
    whatLine: "Turns an Euler lending vault into a pool. Idle collateral earns lending yield and backs swaps at the same time.",
    whyBlurb:
      "Largest hook deployer by count and now by volume on Unichain. Market makers are using it to quote stables against borrowed inventory, which is why the stable pairs here trade tighter than the vanilla pools.",
    website: "https://www.euler.finance/eulerswap",
    github: "https://github.com/euler-xyz/euler-swap",
    xHandle: "eulerfinance",
    firstSeenAt: d(150),
    isNew: false,
    scoreTrending: 41.9,
    metrics: { tvlUsd: 388_400_000, vol7d: 141_000_000, vol7dPrev: 86_000_000, swaps7d: 211_904, poolsN: 748, hooksN: 748 },
    signals: { smartMoneyLps: 7, lastCommitAt: d(1), lastExaHitAt: d(2), routable: true, auditCount: 2 },
    hooks: [
      hook(EULER_HOOK, 130, "EulerSwap", F(["beforeInitialize", "beforeAddLiquidity", "beforeSwap", "beforeDonate"]), P(), {
        description: "One EulerSwap hook instance per pool, deployed by EulerSwapFactory. Swaps are backed by Euler vault collateral.",
        auditUrl: "https://docs.euler.finance/security/audits",
      }),
      hook("0x5fa4f0a0d2f7e4f1d7b4a0a9c1e3f5a7b9d1c3e6", 1, "EulerSwap", F(["beforeInitialize", "beforeAddLiquidity", "beforeSwap", "beforeDonate"]), P(), {
        auditUrl: "https://docs.euler.finance/security/audits",
      }),
      hook("0x5fa4f0a0d2f7e4f1d7b4a0a9c1e3f5a7b9d1c3e7", 8453, "EulerSwap", F(["beforeInitialize", "beforeAddLiquidity", "beforeSwap", "beforeDonate"]), P(), {
        auditUrl: "https://docs.euler.finance/security/audits",
      }),
    ],
    pools: [
      pool(pid(0x3001), 130, EULER_HOOK, "USDC", "USDT", 100, 142_000_000, 71_400_000, 98_200, 110),
      pool(pid(0x3002), 1, "0x5fa4f0a0d2f7e4f1d7b4a0a9c1e3f5a7b9d1c3e6", "USDC", "USDT", 100, 96_000_000, 38_100_000, 61_300, 140),
      pool(pid(0x3003), 130, EULER_HOOK, "WETH", "USDC", 500, 58_300_000, 19_900_000, 27_800, 100),
      pool(pid(0x3004), 8453, "0x5fa4f0a0d2f7e4f1d7b4a0a9c1e3f5a7b9d1c3e7", "USDC", "USDbC", 100, 31_100_000, 7_200_000, 14_604, 95),
      pool(pid(0x3005), 1, "0x5fa4f0a0d2f7e4f1d7b4a0a9c1e3f5a7b9d1c3e6", "wstETH", "WETH", 100, 22_000_000, 4_400_000, 10_000, 130),
    ],
    history: history(33, 388_400_000, 141_000_000, 0.64),
    receipts: [
      r("https://x.com/eulerfinance", "EulerSwap passes $1B cumulative volume on Unichain", "x.com", 2),
      r("https://www.euler.finance/blog", "JIT liquidity from lending vaults, explained", "blog", 16),
    ],
  },
  {
    id: 4,
    slug: "flaunch",
    name: "Flaunch",
    category: "launchpad",
    secondaryCategories: [],
    isFactory: false,
    color: "#ff6a00",
    whatLine: "Memecoin launcher where the hook routes swap fees back to the creator and buys back the token on a schedule.",
    whyBlurb:
      "New hook version this week with 1,200 launches in 48 hours. Creators keep the fee stream, which is the pitch against pump-style launchers that keep it for the platform.",
    website: "https://flaunch.gg",
    github: "https://github.com/flayerlabs/flaunchgg-contracts",
    xHandle: "flaunchgg",
    firstSeenAt: d(2),
    isNew: true,
    scoreTrending: 15.7,
    metrics: { tvlUsd: 1_900_000, vol7d: 6_300_000, vol7dPrev: 0, swaps7d: 38_770, poolsN: 1204, hooksN: 2 },
    signals: { smartMoneyLps: 0, lastCommitAt: d(0), lastExaHitAt: d(2), routable: false, auditCount: 0 },
    hooks: [
      hook(FLAUNCH_HOOK, 8453, "PositionManager", F(["beforeInitialize", "afterInitialize", "beforeAddLiquidity", "beforeSwap", "afterSwap", "beforeSwapReturnsDelta", "afterSwapReturnsDelta"]), P({ dynamicFee: true, upgradeable: true }), {
        description: "Flaunch v2 PositionManager. Dynamic fee, creator revenue routing and scheduled buybacks.",
        firstSeenAt: d(2),
      }),
      hook("0x51bba15255406cfe7099a42183302640ba7dafdd", 8453, "FairLaunch", F(["beforeSwap", "beforeSwapReturnsDelta"]), P({ dynamicFee: true }), { firstSeenAt: d(2) }),
    ],
    pools: [
      pool(pid(0x4001), 8453, FLAUNCH_HOOK, "WETH", "FLAY", 0x800000, 410_000, 1_900_000, 9_800, 2),
      pool(pid(0x4002), 8453, FLAUNCH_HOOK, "WETH", "BASED", 0x800000, 120_000, 980_000, 6_200, 1),
      pool(pid(0x4003), 8453, FLAUNCH_HOOK, "WETH", "DEGENX", 0x800000, 88_000, 610_000, 4_100, 1),
    ],
    history: history(44, 1_900_000, 6_300_000, 30).map((p, i) => (i < 28 ? { ...p, tvlUsd: 0, vol7d: 0 } : p)),
    receipts: [r("https://x.com/flaunchgg", "Flaunch v2 hook is live on Base", "x.com", 2)],
  },
  {
    id: 5,
    slug: "clanker",
    name: "Clanker",
    category: "launchpad",
    secondaryCategories: ["dynamic-fee"],
    isFactory: false,
    color: "#111827",
    whatLine: "Agent-driven token launcher on Base. The hook takes a dynamic fee on every swap and splits it between the deployer and the protocol.",
    whyBlurb:
      "Still the default way a bot launches a token on Base, and the v4 hook moved the fee split onchain where anyone can audit it. Volume is lumpy but the launch count has not dropped in three months.",
    website: "https://clanker.world",
    github: "https://github.com/clanker-devco/contracts",
    xHandle: "clanker",
    firstSeenAt: d(190),
    isNew: false,
    scoreTrending: 29.2,
    metrics: { tvlUsd: 14_300_000, vol7d: 48_900_000, vol7dPrev: 44_100_000, swaps7d: 143_200, poolsN: 3_118, hooksN: 4 },
    signals: { smartMoneyLps: 1, lastCommitAt: d(6), lastExaHitAt: d(1), routable: false, auditCount: 1 },
    hooks: [
      hook(CLANKER_HOOK, 8453, "ClankerHookDynamicFee", F(["afterInitialize", "beforeSwap", "afterSwap", "beforeSwapReturnsDelta"]), P({ dynamicFee: true }), {
        description: "Clanker v4 dynamic-fee hook. Fee rises with volatility and is split to deployer and protocol.",
        auditUrl: "https://github.com/clanker-devco/contracts/tree/main/audits",
      }),
      hook("0x34a45c6b61876d739400bd71228cbcbd4f53e8cd", 8453, "ClankerHookStaticFee", F(["afterInitialize", "beforeSwap", "afterSwap", "beforeSwapReturnsDelta"]), P(), {
        auditUrl: "https://github.com/clanker-devco/contracts/tree/main/audits",
      }),
    ],
    pools: [
      pool(pid(0x5001), 8453, CLANKER_HOOK, "WETH", "CLANKER", 0x800000, 6_200_000, 11_300_000, 24_100, 180),
      pool(pid(0x5002), 8453, CLANKER_HOOK, "WETH", "BNKR", 0x800000, 2_900_000, 8_700_000, 19_800, 150),
      pool(pid(0x5003), 8453, CLANKER_HOOK, "WETH", "ANON", 0x800000, 1_400_000, 4_200_000, 12_600, 160),
    ],
    history: history(55, 14_300_000, 48_900_000, 0.11),
    receipts: [
      r("https://x.com/clanker", "Clanker v4 fee split now fully onchain", "x.com", 1),
      r("https://clanker.world/docs", "How dynamic fees work on Clanker pools", "docs", 12),
    ],
  },
  {
    id: 6,
    slug: "doppler",
    name: "Doppler",
    category: "launchpad",
    secondaryCategories: ["dynamic-fee"],
    isFactory: false,
    color: "#0ea5e9",
    whatLine: "Dutch-auction token launches. The hook walks the price down over epochs until buyers show up, then migrates liquidity to a normal pool.",
    whyBlurb:
      "Whetstone's protocol is now under Zora, Pure Markets and a dozen smaller launchers, so a lot of Base token discovery runs through this one hook. The epoch auction is the only launch design here that punishes snipers by construction.",
    website: "https://doppler.lol",
    github: "https://github.com/whetstoneresearch/doppler",
    xHandle: "whetstonedotcc",
    firstSeenAt: d(240),
    isNew: false,
    scoreTrending: 24.8,
    metrics: { tvlUsd: 21_700_000, vol7d: 31_400_000, vol7dPrev: 27_900_000, swaps7d: 71_050, poolsN: 2_406, hooksN: 6 },
    signals: { smartMoneyLps: 3, lastCommitAt: d(1), lastExaHitAt: d(5), routable: false, auditCount: 2 },
    hooks: [
      hook(DOPPLER_HOOK, 8453, "Doppler", F(["beforeInitialize", "afterInitialize", "beforeAddLiquidity", "beforeSwap", "afterSwap", "beforeSwapReturnsDelta"]), P({ dynamicFee: true }), {
        description: "Doppler dutch-auction hook. Price decays per epoch; liquidity migrates on completion.",
        auditUrl: "https://docs.doppler.lol/security",
      }),
      hook("0x9c0fc2b0d4a2a3bb0c5d6e7f8a9b0c1d2e3f4ac1", 130, "Doppler", F(["beforeInitialize", "afterInitialize", "beforeAddLiquidity", "beforeSwap", "afterSwap", "beforeSwapReturnsDelta"]), P({ dynamicFee: true }), {
        auditUrl: "https://docs.doppler.lol/security",
      }),
    ],
    pools: [
      pool(pid(0x6001), 8453, DOPPLER_HOOK, "WETH", "ZORA", 0x800000, 8_900_000, 14_200_000, 30_100, 120),
      pool(pid(0x6002), 8453, DOPPLER_HOOK, "WETH", "PURE", 0x800000, 3_300_000, 6_100_000, 15_300, 60),
      pool(pid(0x6003), 130, "0x9c0fc2b0d4a2a3bb0c5d6e7f8a9b0c1d2e3f4ac1", "WETH", "UNIDOG", 0x800000, 900_000, 2_600_000, 8_900, 30),
    ],
    history: history(66, 21_700_000, 31_400_000, 0.13),
    receipts: [
      r("https://x.com/whetstonedotcc", "Doppler v4 auctions crossed 20,000 launches", "x.com", 5),
      r("https://docs.doppler.lol", "Epoch pricing and why snipers lose", "docs", 18),
    ],
  },
  {
    id: 7,
    slug: "arrakis",
    name: "Arrakis Pro",
    category: "lp-management",
    secondaryCategories: ["mev-lvr"],
    isFactory: false,
    color: "#c2410c",
    whatLine: "Managed liquidity for token issuers. The hook holds the inventory, re-centers the range and filters toxic flow before it hits the pool.",
    whyBlurb:
      "Base's largest hooked TVL outside of lending comes from two issuer vaults that migrated from v3 in September. The pitch is that a treasury can run its own market without a market maker.",
    website: "https://www.arrakis.finance",
    github: "https://github.com/ArrakisFinance",
    xHandle: "ArrakisFinance",
    firstSeenAt: d(130),
    isNew: false,
    scoreTrending: 18.3,
    metrics: { tvlUsd: 74_900_000, vol7d: 19_600_000, vol7dPrev: 17_200_000, swaps7d: 12_480, poolsN: 9, hooksN: 9 },
    signals: { smartMoneyLps: 2, lastCommitAt: d(3), lastExaHitAt: d(8), routable: true, auditCount: 2 },
    hooks: [
      hook(ARRAKIS_HOOK, 8453, "ArrakisHook", F(["beforeInitialize", "beforeAddLiquidity", "beforeRemoveLiquidity", "beforeSwap"]), P(), {
        description: "Arrakis Pro hook deployed by ArrakisHookFactory. External liquidity vault backs the pool.",
        auditUrl: "https://docs.arrakis.finance/security",
      }),
      hook("0x7a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c041", 1, "ArrakisHook", F(["beforeInitialize", "beforeAddLiquidity", "beforeRemoveLiquidity", "beforeSwap"]), P(), {
        auditUrl: "https://docs.arrakis.finance/security",
      }),
    ],
    pools: [
      pool(pid(0x7001), 8453, ARRAKIS_HOOK, "WETH", "AERO", 3000, 38_200_000, 9_900_000, 6_100, 40),
      pool(pid(0x7002), 8453, ARRAKIS_HOOK, "USDC", "cbBTC", 500, 21_400_000, 6_200_000, 3_900, 38),
      pool(pid(0x7003), 1, "0x7a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c041", "WETH", "USDC", 500, 15_300_000, 3_500_000, 2_480, 90),
    ],
    history: history(77, 74_900_000, 19_600_000, 0.14),
    receipts: [
      r("https://x.com/ArrakisFinance", "Two issuer vaults migrate to Uniswap v4 via Arrakis Pro", "x.com", 8),
      r("https://docs.arrakis.finance", "HOT AMM: filtering toxic flow in a hook", "docs", 25),
    ],
  },
  {
    id: 8,
    slug: "zora",
    name: "Zora Coins",
    category: "launchpad",
    secondaryCategories: [],
    isFactory: false,
    color: "#000000",
    whatLine: "Every Zora post is a coin. The hook routes creator rewards and protocol fees out of each swap on a multi-hop path back to ZORA.",
    whyBlurb:
      "Highest swap count of any hooked project this week, almost all of it sub-$100 trades on content coins. It is the volume floor for Base v4 even when nothing else is moving.",
    website: "https://zora.co",
    github: "https://github.com/ourzora/zora-protocol",
    xHandle: "zora",
    firstSeenAt: d(170),
    isNew: false,
    scoreTrending: 22.0,
    metrics: { tvlUsd: 11_800_000, vol7d: 27_100_000, vol7dPrev: 29_400_000, swaps7d: 318_600, poolsN: 48_900, hooksN: 3 },
    signals: { smartMoneyLps: 0, lastCommitAt: d(2), lastExaHitAt: d(2), routable: true, auditCount: 1 },
    hooks: [
      hook(ZORA_HOOK, 8453, "ZoraV4CoinHook", F(["afterInitialize", "beforeSwap", "afterSwap"]), P(), {
        description: "Zora coin hook. Collects creator and protocol rewards on each swap.",
        auditUrl: "https://github.com/ourzora/zora-protocol/tree/main/audits",
      }),
    ],
    pools: [
      pool(pid(0x8001), 8453, ZORA_HOOK, "ZORA", "USDC", 3000, 4_100_000, 7_800_000, 42_000, 160),
      pool(pid(0x8002), 8453, ZORA_HOOK, "WETH", "ZORA", 3000, 3_600_000, 5_900_000, 38_100, 160),
      pool(pid(0x8003), 8453, ZORA_HOOK, "ZORA", "jessepollak", 30000, 420_000, 1_200_000, 21_300, 80),
    ],
    history: history(88, 11_800_000, 27_100_000, -0.08),
    receipts: [
      r("https://x.com/zora", "Content coins pass 300K weekly swaps", "x.com", 2),
      r("https://docs.zora.co", "Zora Coins v4 hook: reward routing", "docs", 40),
    ],
  },
  {
    id: 9,
    slug: "predicate",
    name: "Predicate",
    category: "compliance",
    secondaryCategories: [],
    isFactory: false,
    color: "#2563eb",
    whatLine: "Policy checks before every swap and LP action. Operators attest offchain that the trader passes the pool's rules, and the hook verifies the signature.",
    whyBlurb:
      "First permissioned pool on mainnet with real volume, used by an RWA issuer for its token's only onchain venue. It is the test case for whether compliance hooks get routed at all.",
    website: "https://predicate.io",
    github: "https://github.com/predicatelabs",
    xHandle: "0xPredicate",
    firstSeenAt: d(5),
    isNew: true,
    scoreTrending: 9.6,
    metrics: { tvlUsd: 4_200_000, vol7d: 1_100_000, vol7dPrev: 0, swaps7d: 310, poolsN: 2, hooksN: 2 },
    signals: { smartMoneyLps: 0, lastCommitAt: d(9), lastExaHitAt: d(5), routable: false, auditCount: 1 },
    hooks: [
      hook(PREDICATE_HOOK, 1, "PredicateHook", F(["beforeAddLiquidity", "beforeSwap"]), P(), {
        description: "Predicate compliance hook. beforeSwap verifies an operator attestation for the trader.",
        auditUrl: "https://docs.predicate.io/security",
        firstSeenAt: d(5),
      }),
      hook("0x917f3b1d5a2e4c6b8d0f1a3c5e7b9d1f3a5c7e89", 8453, "PredicateHook", F(["beforeAddLiquidity", "beforeSwap"]), P(), { firstSeenAt: d(5) }),
    ],
    pools: [
      pool(pid(0x9001), 1, PREDICATE_HOOK, "USDC", "rwaUSD", 500, 3_900_000, 1_000_000, 280, 5),
      pool(pid(0x9002), 8453, "0x917f3b1d5a2e4c6b8d0f1a3c5e7b9d1f3a5c7e89", "USDC", "rwaUSD", 500, 300_000, 100_000, 30, 5),
    ],
    history: history(99, 4_200_000, 1_100_000, 10).map((p, i) => (i < 25 ? { ...p, tvlUsd: 0, vol7d: 0 } : p)),
    receipts: [
      r("https://x.com/0xPredicate", "First permissioned v4 pool live on mainnet", "x.com", 5),
      r("https://docs.predicate.io", "How attestation-gated swaps work", "docs", 5),
    ],
  },
  {
    id: 10,
    slug: "saturn",
    name: "Saturn",
    category: "orders",
    secondaryCategories: [],
    isFactory: true,
    color: "#7c3aed",
    whatLine: "Onchain limit orders. Each pool gets its own hook that fills resting orders when the tick crosses them, no keeper needed.",
    whyBlurb:
      "320 hooks and almost no volume, which is the story of most of the long tail. Worth watching because the order book sits inside the pool rather than beside it, but nobody has seeded it yet.",
    website: null,
    github: "https://github.com/saturn-hooks",
    xHandle: null,
    firstSeenAt: d(95),
    isNew: false,
    scoreTrending: 0,
    metrics: { tvlUsd: 61_000, vol7d: 12_400, vol7dPrev: 15_800, swaps7d: 88, poolsN: 320, hooksN: 320 },
    signals: { smartMoneyLps: 0, lastCommitAt: d(44), lastExaHitAt: null, routable: true, auditCount: 0 },
    hooks: [
      hook(SATURN_HOOK, 42161, "SaturnLimitOrder", F(["afterInitialize", "afterSwap"]), P(), {
        description: "Saturn limit order hook, one instance per pool.",
        verifiedSource: false,
      }),
      hook("0x2e4d6c8b0a1f3e5d7c9b1a3f5e7d9c1b3a5f7e9d", 10, "SaturnLimitOrder", F(["afterInitialize", "afterSwap"]), P(), { verifiedSource: false }),
    ],
    pools: [
      pool(pid(0xa001), 42161, SATURN_HOOK, "WETH", "USDC", 500, 41_000, 9_100, 61, 95),
      pool(pid(0xa002), 10, "0x2e4d6c8b0a1f3e5d7c9b1a3f5e7d9c1b3a5f7e9d", "WETH", "OP", 3000, 20_000, 3_300, 27, 90),
    ],
    history: history(110, 61_000, 12_400, -0.2),
    receipts: [],
  },
];

function finish(s: Seed): ProjectDetail {
  const chains = [...new Set(s.hooks.map((h) => h.chainId))].sort(
    (a, b) => s.pools.filter((p) => p.chainId === b).reduce((x, p) => x + p.tvlUsd, 0) - s.pools.filter((p) => p.chainId === a).reduce((x, p) => x + p.tvlUsd, 0),
  );
  const top = [...s.pools].sort((a, b) => b.tvlUsd - a.tvlUsd)[0] ?? null;
  // flagLabels / routable are derived from the hooks; computed in queries via lib/routable.
  return {
    ...s,
    chains,
    flagLabels: [], // filled by queries.ts (keeps the derivation in one place)
    verified: s.hooks.some((h) => h.verifiedSource),
    upgradeable: s.hooks.some((h) => h.properties.upgradeable),
    topPool: top ? { poolId: top.poolId, chainId: top.chainId, token0Symbol: top.token0Symbol, token1Symbol: top.token1Symbol } : null,
    isSpam: s.pools.length === 0 || s.pools.every((p) => p.tvlUsd < 100 && p.tx7d < 10),
    hooks: s.hooks,
  };
}

export const SAMPLE_PROJECTS: ProjectDetail[] = SEEDS.map(finish);

export const SAMPLE_CONTEXT: ContextStats = {
  hooksTracked: 4_942,
  hooksDelta7d: 61,
  projects: 1_640,
  projectsNew7d: 9,
  tvlUsd: 1_210_000_000,
  tvlDelta7dPct: 4.8,
};

export const SAMPLE_DIGESTS: DigestView[] = [
  {
    week: "2026-10-05",
    title: "Week of October 5, 2026",
    intro: "Three new projects cleared the bar this week. Bunni took the top trending slot after the am-AMM switch on USDC/USDT.",
    createdAt: "2026-10-05T06:10:00Z",
    items: [
      { slug: "angstrom", name: "Angstrom", kind: "new", line: "Mainnet launch, $9.1M TVL in four days.", vol7d: 22_800_000 },
      { slug: "flaunch", name: "Flaunch", kind: "new", line: "v2 hook on Base, 1,200 launches in 48 hours.", vol7d: 6_300_000 },
      { slug: "predicate", name: "Predicate", kind: "new", line: "First permissioned pool with real volume.", vol7d: 1_100_000 },
      { slug: "bunni", name: "Bunni", kind: "trending", line: "7d volume $184.2M, +112%.", vol7d: 184_200_000 },
      { slug: "eulerswap", name: "EulerSwap", kind: "trending", line: "7d volume $141.0M, +64%.", vol7d: 141_000_000 },
      { slug: "clanker", name: "Clanker", kind: "trending", line: "7d volume $48.9M, +11%.", vol7d: 48_900_000 },
    ],
  },
  {
    week: "2026-09-28",
    title: "Week of September 28, 2026",
    intro: "Quiet week. Arrakis Pro vaults migrated from v3; no new projects cleared the TVL or swap floor.",
    createdAt: "2026-09-28T06:10:00Z",
    items: [
      { slug: "arrakis", name: "Arrakis Pro", kind: "trending", line: "Two issuer vaults migrated to v4, $74.9M TVL.", vol7d: 17_200_000 },
      { slug: "eulerswap", name: "EulerSwap", kind: "trending", line: "7d volume $86.0M.", vol7d: 86_000_000 },
    ],
  },
];

export const SAMPLE_NOW = NOW;

export type { Category };
