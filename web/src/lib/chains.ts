import type { ChainId } from "./types";

export interface ChainInfo {
  id: ChainId;
  /** app.uniswap.org URL segment (CHAIN_METADATA[...].urlParam) */
  slug: string;
  name: string;
  /** dot color for the token-style chain pill */
  color: string;
  /** true when the dot needs a border (very light color) */
  lightDot?: boolean;
  blockscout: string | null;
  subgraphId: string | null;
  /** GeckoTerminal network id (api.geckoterminal.com/api/v2/networks/{id}) */
  gtNetwork: string;
  /** block explorer origin for tx / address links */
  explorer: string;
}

export const CHAINS: Record<ChainId, ChainInfo> = {
  1: { id: 1, slug: "ethereum", name: "Ethereum", color: "#627eea", blockscout: "https://eth.blockscout.com", subgraphId: "DiYPVdygkfjDWhbxGSqAQxwBKmfKnkWQojqeM2rkLb3G", gtNetwork: "eth", explorer: "https://etherscan.io" },
  8453: { id: 8453, slug: "base", name: "Base", color: "#0052ff", blockscout: "https://base.blockscout.com", subgraphId: "Gqm2b5J85n1bhCyDMpGbtbVn4935EvvdyHdHrx3dibyj", gtNetwork: "base", explorer: "https://basescan.org" },
  130: { id: 130, slug: "unichain", name: "Unichain", color: "#f50db4", blockscout: "https://unichain.blockscout.com", subgraphId: "aa3YpPCxatg4LaBbLFuv2iBC8Jvs9u3hwt5GTpS4Kit", gtNetwork: "unichain", explorer: "https://uniscan.xyz" },
  4663: { id: 4663, slug: "robinhood", name: "Robinhood", color: "#00c805", blockscout: "https://robinhoodchain.blockscout.com", subgraphId: null, gtNetwork: "robinhood", explorer: "https://robinhoodchain.blockscout.com" },
  56: { id: 56, slug: "bnb", name: "BNB", color: "#f0b90b", blockscout: null, subgraphId: "EAq1nJKgjnuKH6Gj4RFjCW7LcL7E2uipbncdwV7TTWkX", gtNetwork: "bsc", explorer: "https://bscscan.com" },
  42161: { id: 42161, slug: "arbitrum", name: "Arbitrum", color: "#28a0f0", blockscout: "https://arbitrum.blockscout.com", subgraphId: "G5TsTKNi8yhPSV7kycaE23oWbqv9zzNqR49FoEQjzq1r", gtNetwork: "arbitrum", explorer: "https://arbiscan.io" },
  43114: { id: 43114, slug: "avalanche", name: "Avalanche", color: "#e84142", blockscout: null, subgraphId: "49JxRo9FGxWpSf5Y5GKQPj5NUpX2HhpoZHpGzNEWQZjq", gtNetwork: "avax", explorer: "https://snowtrace.io" },
  137: { id: 137, slug: "polygon", name: "Polygon", color: "#8247e5", blockscout: "https://polygon.blockscout.com", subgraphId: "2CB2uQxcDKWDenagn2z17KQVCtfwSx5eXYuvqTciRTJu", gtNetwork: "polygon_pos", explorer: "https://polygonscan.com" },
  57073: { id: 57073, slug: "ink", name: "Ink", color: "#7132f5", blockscout: "https://explorer.inkonchain.com", subgraphId: null, gtNetwork: "ink", explorer: "https://explorer.inkonchain.com" },
  10: { id: 10, slug: "optimism", name: "Optimism", color: "#ff0420", blockscout: "https://optimism.blockscout.com", subgraphId: "6RBtsmGUYfeLeZsYyxyKSUiaA6WpuC69shMEQ1Cfuj9u", gtNetwork: "optimism", explorer: "https://optimistic.etherscan.io" },
};

/** Display order: by hook count on hooklist, roughly. */
export const CHAIN_ORDER: ChainId[] = [1, 8453, 130, 4663, 56, 42161, 43114, 137, 57073, 10];

export const CHAIN_SLUGS: Record<ChainId, string> = Object.fromEntries(
  Object.values(CHAINS).map((c) => [c.id, c.slug]),
) as Record<ChainId, string>;

export function chainSlug(chainId: ChainId): string {
  return CHAINS[chainId]?.slug ?? String(chainId);
}

export function chainColor(chainId: ChainId): string {
  return CHAINS[chainId]?.color ?? "#555";
}

export function chainName(chainId: ChainId): string {
  return CHAINS[chainId]?.name ?? `Chain ${chainId}`;
}

export function isChainId(n: number): n is ChainId {
  return Object.prototype.hasOwnProperty.call(CHAINS, n);
}

export function parseChainParam(s: string | undefined): ChainId | undefined {
  if (!s) return undefined;
  const byId = Number(s);
  if (Number.isInteger(byId) && isChainId(byId)) return byId;
  const bySlug = Object.values(CHAINS).find((c) => c.slug === s.toLowerCase());
  return bySlug?.id;
}

/** https://app.uniswap.org/explore/pools/{chainSlug}/{poolId}  (v4 poolId is bytes32) */
export function poolDeepLink(chainId: ChainId, poolId: string): string {
  return `https://app.uniswap.org/explore/pools/${chainSlug(chainId)}/${poolId}`;
}

export function explorerAddressUrl(chainId: ChainId, address: string): string | null {
  const base = CHAINS[chainId]?.blockscout ?? CHAINS[chainId]?.explorer;
  return base ? `${base}/address/${address}` : null;
}

export function explorerTxUrl(chainId: ChainId, hash: string): string | null {
  const base = CHAINS[chainId]?.explorer;
  return base ? `${base}/tx/${hash}` : null;
}

/** Only the ten chains Bait knows; registry hooks span ~22, so every lookup by id is guarded. */
export function knownChain(id: number): id is ChainId {
  return Object.prototype.hasOwnProperty.call(CHAINS, id);
}

// ---------- any chain id (launch feed, radar) ----------
// The indexer and the registry see chains outside the ten above. Those get a name (when we know it), a neutral dot,
// a native symbol for currency 0x0 and an explorer only where one is verified. Never index CHAINS with these ids.

interface OtherChain {
  name: string;
  native: string;
  explorer: string | null;
}

/** Hooklist chains Bait does not track (hook.chain -> chainId in hooklist.json, 2026-10). */
export const OTHER_CHAINS: Record<number, OtherChain> = {
  7777777: { name: "Zora", native: "ETH", explorer: "https://explorer.zora.energy" },
  480: { name: "World Chain", native: "ETH", explorer: "https://worldscan.org" },
  1868: { name: "Soneium", native: "ETH", explorer: "https://soneium.blockscout.com" },
  42220: { name: "Celo", native: "CELO", explorer: "https://celoscan.io" },
  143: { name: "Monad", native: "MON", explorer: null },
  4326: { name: "MegaETH", native: "ETH", explorer: null },
  196: { name: "X Layer", native: "OKB", explorer: null },
  5042: { name: "Arc", native: "USDC", explorer: null },
  4217: { name: "Tempo", native: "native", explorer: null },
  59144: { name: "Linea", native: "ETH", explorer: "https://lineascan.build" },
  81457: { name: "Blast", native: "ETH", explorer: "https://blastscan.io" },
};

const NATIVE: Record<number, string> = { 56: "BNB", 137: "POL", 43114: "AVAX" };

export const NEUTRAL_DOT = "#676880";

/** Name for any chain id: tracked, known-but-untracked, else "Chain 123". */
export function anyChainName(id: number): string {
  return (knownChain(id) ? CHAINS[id].name : OTHER_CHAINS[id]?.name) ?? `Chain ${id}`;
}

/** Chain dot color; neutral for chains Bait does not track. */
export function anyChainColor(id: number): string {
  return knownChain(id) ? CHAINS[id].color : NEUTRAL_DOT;
}

/** Symbol for currency 0x0 (the chain's native token). */
export function nativeSymbol(id: number): string {
  if (knownChain(id)) return NATIVE[id] ?? "ETH";
  return OTHER_CHAINS[id]?.native ?? "native";
}

export function anyExplorerTxUrl(id: number, hash: string): string | null {
  if (!hash) return null;
  if (knownChain(id)) return explorerTxUrl(id, hash);
  const base = OTHER_CHAINS[id]?.explorer;
  return base ? `${base}/tx/${hash}` : null;
}
