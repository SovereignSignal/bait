// Uniswap's published routing lists for v4 hooks (github.com/Uniswap/uniroute-public): an allowlist of hooks approved by
// hand and a denylist of hooks the router must never use. The files are TypeScript; they are read as text and parsed
// here, never executed. Pure: the fetch and its cache live in db/routing-lists.ts.

export type RoutingSource = "allowlist" | "denylist" | "rule";

export interface RoutingLists {
  allow: Map<number, Set<string>>;
  deny: Map<number, Set<string>>;
  /** ISO time the lists were read; null when they could not be (routing then falls back to the rule alone) */
  checkedAt: string | null;
}

export const NO_LISTS: RoutingLists = { allow: new Map(), deny: new Map(), checkedAt: null };

export const LISTS_BASE = "https://raw.githubusercontent.com/Uniswap/uniroute-public/main/src/lib/poolCaching/util/";
export const ALLOWLIST_FILE = "hooksAddressesAllowlist.ts";
export const DENYLIST_FILE = "hooksAddressesDenylist.ts";

// @uniswap/sdk-core ChainId names used as keys in the lists; the files define newer chains locally as CHAIN_ID_<NAME>.
const SDK_CHAIN_IDS: Record<string, number> = {
  MAINNET: 1, OPTIMISM: 10, BNB: 56, UNICHAIN: 130, POLYGON: 137, MONAD: 143, XLAYER: 196, ZKSYNC: 324, WORLDCHAIN: 480,
  SONEIUM: 1868, MEGAETH: 4326, ROBINHOOD: 4663, BASE: 8453, ARBITRUM_ONE: 42161, CELO: 42220, AVALANCHE: 43114,
  INK: 57073, LINEA: 59144, BLAST: 81457, ZORA: 7777777,
};

const ADDRESS = /^0x[0-9a-f]{40}$/;
const ZERO = "0x" + "0".repeat(40);

function stripComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:'"])\/\/.*$/gm, "$1");
}

/** The `{ ... }` body after `name ... = {`, matched by brace depth (type annotations may span lines). */
function objectBody(src: string, name: string): string | null {
  const at = src.search(new RegExp(`\\b${name}\\b[^=]*=\\s*\\{`));
  if (at < 0) return null;
  const open = src.indexOf("{", src.indexOf("=", at));
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(open + 1, i);
  }
  return null;
}

/**
 * Parse one list file into chain id -> hook addresses. Elements may be address literals, constants, arrays of
 * constants, or spreads of those; anything else (function calls, imports from other files) is skipped.
 */
export function parseRoutingList(text: string, mapName: string): Map<number, Set<string>> {
  const src = stripComments(text);
  const strings = new Map<string, string>();
  const arrays = new Map<string, string>();
  const chainConsts = new Map<string, number>();
  for (const m of src.matchAll(/\bconst\s+([A-Za-z0-9_]+)\s*(?::[^=]*?)?=\s*['"](0x[0-9a-fA-F]{40})['"]/g)) strings.set(m[1], m[2].toLowerCase());
  for (const m of src.matchAll(/\bconst\s+([A-Za-z0-9_]+)\s*(?::[^=]*?)?=\s*\[([\s\S]*?)\]\s*;/g)) arrays.set(m[1], m[2]);
  for (const m of src.matchAll(/\bconst\s+(CHAIN_ID_[A-Z0-9_]+)\s*=\s*(\d+)/g)) chainConsts.set(m[1], Number(m[2]));

  const resolve = (items: string, out: Set<string>, seen: Set<string>) => {
    for (const raw of items.split(",")) {
      const tok = raw.trim().replace(/^\.\.\./, "");
      const lit = tok.match(/^['"](0x[0-9a-fA-F]{40})['"]$/);
      if (lit) out.add(lit[1].toLowerCase());
      else if (strings.has(tok)) out.add(strings.get(tok)!);
      else if (arrays.has(tok) && !seen.has(tok)) resolve(arrays.get(tok)!, out, new Set([...seen, tok]));
    }
  };

  const result = new Map<number, Set<string>>();
  const body = objectBody(src, mapName);
  if (!body) return result;
  for (const m of body.matchAll(/\[\s*(?:ChainId\.([A-Z0-9_]+)|(CHAIN_ID_[A-Z0-9_]+))\s*\]\s*:\s*\[([\s\S]*?)\]/g)) {
    const chainId = m[1] ? SDK_CHAIN_IDS[m[1]] : chainConsts.get(m[2]);
    if (!chainId) continue;
    const set = result.get(chainId) ?? new Set<string>();
    resolve(m[3], set, new Set());
    set.delete(ZERO);
    if (set.size) result.set(chainId, set);
  }
  return result;
}

export function listSize(m: Map<number, Set<string>>): number {
  let n = 0;
  for (const s of m.values()) n += s.size;
  return n;
}

/** Which published list names this hook on this chain, if any. The denylist wins. */
export function listedOn(lists: RoutingLists | undefined, chainId: number, address: string): "allowlist" | "denylist" | null {
  if (!lists) return null;
  const a = address.toLowerCase();
  if (!ADDRESS.test(a)) return null;
  if (lists.deny.get(chainId)?.has(a)) return "denylist";
  if (lists.allow.get(chainId)?.has(a)) return "allowlist";
  return null;
}
