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
  nansen: boolean;
  blockscout: string | null;
  subgraphId: string | null;
}

export const CHAINS: Record<ChainId, ChainInfo> = {
  1: { id: 1, slug: "ethereum", name: "Ethereum", color: "#627eea", nansen: true, blockscout: "https://eth.blockscout.com", subgraphId: "DiYPVdygkfjDWhbxGSqAQxwBKmfKnkWQojqeM2rkLb3G" },
  8453: { id: 8453, slug: "base", name: "Base", color: "#0052ff", nansen: true, blockscout: "https://base.blockscout.com", subgraphId: "Gqm2b5J85n1bhCyDMpGbtbVn4935EvvdyHdHrx3dibyj" },
  130: { id: 130, slug: "unichain", name: "Unichain", color: "#f3f3f3", lightDot: true, nansen: false, blockscout: "https://unichain.blockscout.com", subgraphId: "aa3YpPCxatg4LaBbLFuv2iBC8Jvs9u3hwt5GTpS4Kit" },
  4663: { id: 4663, slug: "robinhood", name: "Robinhood", color: "#00c805", nansen: true, blockscout: "https://robinhoodchain.blockscout.com", subgraphId: null },
  56: { id: 56, slug: "bnb", name: "BNB", color: "#f0b90b", nansen: true, blockscout: null, subgraphId: "EAq1nJKgjnuKH6Gj4RFjCW7LcL7E2uipbncdwV7TTWkX" },
  42161: { id: 42161, slug: "arbitrum", name: "Arbitrum", color: "#28a0f0", nansen: true, blockscout: "https://arbitrum.blockscout.com", subgraphId: "G5TsTKNi8yhPSV7kycaE23oWbqv9zzNqR49FoEQjzq1r" },
  43114: { id: 43114, slug: "avalanche", name: "Avalanche", color: "#e84142", nansen: true, blockscout: null, subgraphId: "49JxRo9FGxWpSf5Y5GKQPj5NUpX2HhpoZHpGzNEWQZjq" },
  137: { id: 137, slug: "polygon", name: "Polygon", color: "#8247e5", nansen: true, blockscout: "https://polygon.blockscout.com", subgraphId: "2CB2uQxcDKWDenagn2z17KQVCtfwSx5eXYuvqTciRTJu" },
  57073: { id: 57073, slug: "ink", name: "Ink", color: "#7132f5", nansen: false, blockscout: "https://explorer.inkonchain.com", subgraphId: null },
  10: { id: 10, slug: "optimism", name: "Optimism", color: "#ff0420", nansen: true, blockscout: "https://optimism.blockscout.com", subgraphId: "6RBtsmGUYfeLeZsYyxyKSUiaA6WpuC69shMEQ1Cfuj9u" },
};

/** Display order: by hook count on hooklist, roughly. */
export const CHAIN_ORDER: ChainId[] = [1, 8453, 130, 4663, 56, 42161, 43114, 137, 57073, 10];

export const CHAIN_SLUGS: Record<ChainId, string> = Object.fromEntries(
  Object.values(CHAINS).map((c) => [c.id, c.slug]),
) as Record<ChainId, string>;

export function chainSlug(chainId: ChainId): string {
  return CHAINS[chainId].slug;
}

export function chainName(chainId: ChainId): string {
  return CHAINS[chainId]?.name ?? `Chain ${chainId}`;
}

export function isChainId(n: number): n is ChainId {
  return n in CHAINS;
}

export function parseChainParam(s: string | undefined): ChainId | undefined {
  if (!s) return undefined;
  const byId = Number(s);
  if (Number.isInteger(byId) && isChainId(byId)) return byId;
  const bySlug = Object.values(CHAINS).find((c) => c.slug === s.toLowerCase());
  return bySlug?.id;
}

export function nansenCovers(chains: ChainId[]): boolean {
  return chains.some((c) => CHAINS[c]?.nansen);
}

/** https://app.uniswap.org/explore/pools/{chainSlug}/{poolId}  (v4 poolId is bytes32) */
export function poolDeepLink(chainId: ChainId, poolId: string): string {
  return `https://app.uniswap.org/explore/pools/${chainSlug(chainId)}/${poolId}`;
}

export function explorerAddressUrl(chainId: ChainId, address: string): string | null {
  const base = CHAINS[chainId]?.blockscout;
  return base ? `${base}/address/${address}` : null;
}
