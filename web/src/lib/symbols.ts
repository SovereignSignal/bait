// Token symbols for pools the indexer found (they arrive with addresses only).
// 1. DefiLlama coins API (keyless, batched): covers majors and anything DefiLlama prices.
// 2. ERC-20 symbol() over a public RPC for the rest (most fresh launches are memecoins DefiLlama does not track).
// Cached in-process: hits for 24h, misses for 6h. Never throws; unresolved stays null and the UI shows a short address.

// DefiLlama chain keys, mirroring pipeline/bait/indexer/chains.py.
import { nativeSymbol } from "./chains";

const LLAMA: Record<number, string> = {
  1: "ethereum", 10: "optimism", 56: "bsc", 130: "unichain", 137: "polygon", 143: "monad", 480: "wc", 1868: "soneium",
  4326: "megaeth", 4663: "robinhood", 8453: "base", 42161: "arbitrum", 42220: "celo", 43114: "avax", 57073: "ink", 7777777: "zora",
};

// Public RPCs that answer eth_call, mirroring pipeline/bait/keyless.py.
const RPC: Record<number, string> = {
  1: "https://ethereum-rpc.publicnode.com", 8453: "https://base-rpc.publicnode.com", 130: "https://mainnet.unichain.org",
  4663: "https://rpc.mainnet.chain.robinhood.com", 56: "https://bsc-rpc.publicnode.com", 42161: "https://arb1.arbitrum.io/rpc",
  43114: "https://api.avax.network/ext/bc/C/rpc", 137: "https://polygon-bor-rpc.publicnode.com", 57073: "https://rpc-gel.inkonchain.com",
  10: "https://mainnet.optimism.io",
};

const ZERO = "0x0000000000000000000000000000000000000000";
const HIT_MS = 24 * 3600_000;
const MISS_MS = 6 * 3600_000;
const cache = new Map<string, { at: number; sym: string | null }>();

export type TokenRef = { chainId: number; address: string };
const key = (t: TokenRef) => `${t.chainId}:${t.address.toLowerCase()}`;

function fresh(k: string): boolean {
  const c = cache.get(k);
  return !!c && Date.now() - c.at < (c.sym ? HIT_MS : MISS_MS);
}

export function cleanSymbol(s: unknown): string | null {
  if (typeof s !== "string") return null;
  const t = s.replace(/[\u0000-\u001f\u007f]/g, "").trim();
  return t && t.length <= 16 ? t : null;
}

/** Decode an eth_call result of symbol(): ABI string (offset, length, bytes) or a right-padded bytes32. */
export function decodeSymbol(hex: string | null | undefined): string | null {
  if (!hex || !hex.startsWith("0x") || hex.length < 66) return null;
  const body = hex.slice(2);
  const bytesToStr = (h: string) => {
    const out: number[] = [];
    for (let i = 0; i + 1 < h.length; i += 2) {
      const b = parseInt(h.slice(i, i + 2), 16);
      if (b === 0) break;
      out.push(b);
    }
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(new Uint8Array(out));
    } catch {
      return null;
    }
  };
  if (body.length >= 192) {
    const offset = parseInt(body.slice(0, 64), 16);
    if (offset === 32) {
      const len = parseInt(body.slice(64, 128), 16);
      if (len > 0 && len <= 64) return cleanSymbol(bytesToStr(body.slice(128, 128 + len * 2)));
    }
  }
  return cleanSymbol(bytesToStr(body.slice(0, 64)));   // bytes32 symbols (MKR-style)
}

async function fetchJson(url: string, init?: RequestInit): Promise<unknown> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 5000);
  try {
    const r = await fetch(url, { ...init, signal: ctl.signal, headers: { "Content-Type": "application/json", Accept: "application/json", "User-Agent": "bait-web/1.0", ...(init?.headers ?? {}) } });
    return r.ok ? await r.json() : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

async function fromLlama(refs: TokenRef[]): Promise<void> {
  const byKey = refs.filter((r) => LLAMA[r.chainId]);
  for (let i = 0; i < byKey.length; i += 50) {
    const chunk = byKey.slice(i, i + 50);
    const ids = chunk.map((r) => `${LLAMA[r.chainId]}:${r.address.toLowerCase()}`).join(",");
    const d = (await fetchJson(`https://coins.llama.fi/prices/current/${ids}`)) as { coins?: Record<string, { symbol?: string }> } | null;
    const coins = d?.coins ?? {};
    for (const r of chunk) {
      const sym = cleanSymbol(coins[`${LLAMA[r.chainId]}:${r.address.toLowerCase()}`]?.symbol);
      if (sym) cache.set(key(r), { at: Date.now(), sym });
    }
  }
}

async function fromChain(refs: TokenRef[]): Promise<void> {
  const byChain = new Map<number, TokenRef[]>();
  for (const r of refs) if (RPC[r.chainId]) byChain.set(r.chainId, [...(byChain.get(r.chainId) ?? []), r]);
  await Promise.all(
    [...byChain.entries()].map(async ([cid, list]) => {
      for (let i = 0; i < list.length; i += 10) {   // some public RPCs reject batches over 10
        const chunk = list.slice(i, i + 10);
        const body = chunk.map((r, j) => ({ jsonrpc: "2.0", id: j, method: "eth_call", params: [{ to: r.address, data: "0x95d89b41" }, "latest"] }));
        const res = (await fetchJson(RPC[cid], { method: "POST", body: JSON.stringify(body) })) as { id: number; result?: string }[] | null;
        const byId = new Map((Array.isArray(res) ? res : []).map((x) => [x.id, x.result]));
        chunk.forEach((r, j) => cache.set(key(r), { at: Date.now(), sym: decodeSymbol(byId.get(j)) }));
      }
    }),
  );
}

/** Lookups in flight, per token: concurrent renders share one resolution instead of each starting their own. */
const inflight = new Map<string, Promise<void>>();

export type ResolveDeps = { llama: (refs: TokenRef[]) => Promise<void>; chain: (refs: TokenRef[]) => Promise<void> };
const REAL_DEPS: ResolveDeps = { llama: fromLlama, chain: fromChain };

/**
 * Resolve symbols for up to `limit` uncached tokens, waiting at most `waitMs` for the lookups. Anything still pending
 * keeps resolving in the background and fills the cache for the next request. Returns a lookup for all requested tokens.
 */
export async function resolveSymbols(
  refs: TokenRef[],
  { limit = 60, waitMs = 1500 }: { limit?: number; waitMs?: number } = {},
  deps: ResolveDeps = REAL_DEPS,
): Promise<(t: TokenRef) => string | null> {
  const want = new Map<string, TokenRef>();
  for (const r of refs) {
    if (!r.address || r.address.toLowerCase() === ZERO) continue;
    const k = key(r);
    if (!fresh(k) && !inflight.has(k) && !want.has(k)) want.set(k, { chainId: r.chainId, address: r.address.toLowerCase() });
  }
  const todo = [...want.values()].slice(0, limit);
  if (todo.length) {
    const work = (async () => {
      try {
        await deps.llama(todo);
        await deps.chain(todo.filter((r) => !cache.get(key(r))?.sym));
      } catch {
        /* never fail a page over symbols */
      }
    })();
    const keys = todo.map(key);
    const done = work.finally(() => keys.forEach((k) => inflight.delete(k)));
    keys.forEach((k) => inflight.set(k, done));
  }
  const pending = Promise.all(refs.map((r) => inflight.get(key(r))).filter((p): p is Promise<void> => !!p));
  let timer: ReturnType<typeof setTimeout> | undefined;
  await Promise.race([pending, new Promise<void>((res) => { timer = setTimeout(res, waitMs); })]);
  clearTimeout(timer);
  // currency 0x0 is the chain's native token: named here, so every caller (pools, unnamed-project labels) gets it
  return (t: TokenRef) => (t.address?.toLowerCase() === ZERO ? nativeSymbol(t.chainId) : (cache.get(key(t))?.sym ?? null));
}
