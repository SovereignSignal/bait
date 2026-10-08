// Sample radar items and launches, served only with BAIT_FIXTURES=1 and no DATABASE_URL.
// Radar rows are shaped like real Oct 2026 hooklist submissions and GitHub repos; the web rows (hackathon, incubator,
// audit, launch) are illustrative and link to example.com. Launch rows are generated relative to the request time so
// the "Just launched" feed looks live; hooks reuse fixture projects so the project-name path renders.
import type { LaunchView, RadarItemView } from "@/lib/types";
import { SAMPLE_NOW } from "./sample";

const H = 3_600_000;
const D = 24 * H;
const at = (ms: number) => new Date(SAMPLE_NOW - ms).toISOString();

function item(x: Partial<RadarItemView> & Pick<RadarItemView, "url" | "kind" | "title">, agoMs: number): RadarItemView {
  return {
    summary: null,
    source: null,
    repo: null,
    stars: null,
    chainId: null,
    hookAddress: null,
    projectSlug: null,
    publishedAt: at(agoMs),
    lastSeen: at(6 * H),
    ...x,
  };
}

export const SAMPLE_RADAR: RadarItemView[] = [
  item({ url: "https://github.com/Uniswap/hooklist/issues/10521", kind: "registry_submission", title: "WorkLaunchHook3", source: "github.com/Uniswap/hooklist",
    summary: "Open. Immutable fee hook for a single WORK/IMD pool with a 900-second anti-snipe fee that slides to a 1% standing fee.", chainId: 4663,
    hookAddress: "0x3cc31dbb63ad4575ea86cdab57bcc5c06fe22044" }, 5 * H),
  item({ url: "https://github.com/Uniswap/hooklist/issues/10518", kind: "registry_submission", title: "OHMVAULT VaultHook", source: "github.com/Uniswap/hooklist",
    summary: "Open, PR in review. Flat 5% swap fee; 40% goes to a locked full-range position, 60% to the treasury.", chainId: 1,
    hookAddress: "0x450c7eb41cb3c98690f8411dfe0fbe690b22f0c8" }, 8 * H),
  item({ url: "https://github.com/Uniswap/hooklist/issues/10516", kind: "registry_submission", title: "Claus", source: "github.com/Uniswap/hooklist",
    summary: "Merged registry submission.", chainId: 1, hookAddress: "0x37bfb8ac7c960e558657871d41ca70e07e7dbfff", projectSlug: "claus" }, 16 * H),
  item({ url: "https://github.com/Uniswap/hooklist/issues/10490", kind: "registry_submission", title: "ZoraCoinHook", source: "github.com/Uniswap/hooklist",
    summary: "Open. Creator-coin hook on Zora that routes swap fees to the coin's creator.", chainId: 7777777 }, 2 * D),
  item({ url: "https://github.com/hookwright/untoll-hook", kind: "repo", title: "hookwright/untoll-hook", source: "github", repo: "hookwright/untoll-hook", stars: 1,
    summary: "A Uniswap v4 hook where the bonding curve is the pool: graduate in place on the same poolId." }, 1 * D),
  item({ url: "https://github.com/chaosxcode/hookguard", kind: "repo", title: "chaosxcode/hookguard", source: "github", repo: "chaosxcode/hookguard", stars: 2,
    summary: "Risk scanner for Uniswap v4 hooks: CI-native and bytecode-aware." }, 3 * D),
  item({ url: "https://github.com/Hookr-fun/hookr-contracts", kind: "repo", title: "Hookr-fun/hookr-contracts", source: "github", repo: "Hookr-fun/hookr-contracts", stars: 4,
    summary: "Hookr smart contracts: a Uniswap v4 hook launchpad on Robinhood Chain.", projectSlug: "hookrmodularhookv6" }, 2 * D),
  item({ url: "https://example.com/sample/ethglobal-showcase-twamm-hook", kind: "hackathon", title: "TWAMM-lite: a v4 hook for streaming orders", source: "example.com",
    summary: "Hackathon project that splits large orders into time-weighted swaps through a v4 hook." }, 6 * D),
  item({ url: "https://example.com/sample/uhi-cohort-demo-day", kind: "incubator", title: "Hook Incubator cohort demo day recap", source: "example.com",
    summary: "Incubator cohort demos covering LVR-aware fees, limit orders and a lending-backed liquidity hook." }, 9 * D),
  item({ url: "https://example.com/sample/v4-hook-audit-report", kind: "audit", title: "Security review: dynamic-fee hook", source: "example.com",
    summary: "Audit report on a dynamic-fee v4 hook with two medium findings, both fixed before deployment." }, 4 * D),
  item({ url: "https://example.com/sample/launch-announcement", kind: "launch_post", title: "Our v4 hook is live on Base", source: "example.com",
    summary: "Launch post for a v4 hook that rebates LPs from MEV auction proceeds on Base.", chainId: 8453 }, 1 * D),
];

// [chainId, hook, project slug, project name, currency0, currency1, symbol0, symbol1, fee, minutes ago]
type Seed = [number, string, string | null, string | null, string, string, string | null, string | null, number, number];
const ZERO = "0x0000000000000000000000000000000000000000";
const SEEDS: Seed[] = [
  [4663, "0xb3ca29cf721380cee8b8e4755f3865ebc68fe8cc", "hookrmodularhookv6", "HookrModularHookV6", ZERO, "0x9a8f2b1c3d4e5f60718293a4b5c6d7e8f9012345", null, null, 0x800000, 2],
  [8453, "0xd60d6b218116cfd801e28f78d011a203d2b068cc", "clanker-dynamic-fee-hook-v2-base", "Clanker Dynamic Fee Hook v2 (Base)", ZERO, "0x1f2e3d4c5b6a79880716253443526170f8e9dacb", null, "CLNK", 0x800000, 7],
  [1, "0x37bfb8ac7c960e558657871d41ca70e07e7dbfff", "claus", "Claus", ZERO, "0x2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e", null, "CLAUS", 0x800000, 19],
  [7777777, "0x7a1b2c3d4e5f60718293a4b5c6d7e8f901234cc0", null, null, ZERO, "0x4200000000000000000000000000000000000777", null, null, 10000, 33],
  [130, "0x5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f2080", null, null, "0x078d782b760474a361dda0af3839290b0ef57ad6", "0x8f187aa05619a017077f5308904739877ce9ea21", "USDC", "UNI", 3000, 58],
  [480, "0x0c1d2e3f405162738495a6b7c8d9e0f1a2b30044", null, null, ZERO, "0x2cfc85d8e48f8eab294be644d9e25c3030863003", null, "WLD", 0x800000, 95],
  [42161, "0x4440854b2d02c57a0dc5c58b7a884562d875c0c4", "uniswapv4kemhook", "UniswapV4KEMHook", "0xaf88d065e77c8cc2239327c5edb3a432268e5831", "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", "USDC", "USDT", 100, 140],
  [143, "0x9d8c7b6a5f4e3d2c1b0a99887766554433221080", null, null, ZERO, "0x754704bc059f8c67012fed69bc8a327a5aafb603", null, null, 500, 260],
];

export function sampleLaunches(now = Date.now()): LaunchView[] {
  return SEEDS.map(([chainId, hook, projectSlug, projectName, currency0, currency1, symbol0, symbol1, fee, mins], i) => ({
    chainId,
    poolId: `0x${(i + 1).toString(16).padStart(2, "0")}${"ab".repeat(31)}`,
    hook,
    projectSlug,
    projectName,
    currency0,
    currency1,
    symbol0,
    symbol1,
    fee,
    blockTime: new Date(now - mins * 60_000).toISOString(),
    txHash: `0x${(i + 7).toString(16).padStart(2, "0")}${"cd".repeat(31)}`,
  }));
}
