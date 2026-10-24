// Live recent trades for one pool, from GeckoTerminal (keyless).
// Never throws: any upstream error, 429 or bad input is an empty list with an explicit status. Only pools Bait knows are proxied.
import { isKnownPool } from "@/db/queries";
import { respondJson } from "@/lib/http";
import { createSwrCache } from "@/lib/swr-cache";
import { createTokenBucket } from "@/lib/token-bucket";
import { CHAINS, parseChainParam } from "@/lib/chains";
import type { ChainId, TradeView } from "@/lib/types";

export const dynamic = "force-dynamic";

const TTL_MS = 120_000;
const ERROR_TTL_MS = 30_000;
// Past this an idle pool's cached list is refetched before answering, not served first: a page opened after hours
// without visitors showed trades "3d" old for a pool that traded that morning.
const MAX_STALE_MS = 600_000;
const MIN_USD = 10;
const LIMIT = 25;
const NO_STORE = "no-store";
const UA = "bait/0.1 (+https://github.com/Uniswap/hooklist; hook discovery feed)";

type Status = "ok" | "unknown_pool" | "rate_limited" | "upstream_error" | "empty";
type Payload = { trades: TradeView[]; status: Status };
type GtTrade = {
  attributes?: {
    block_timestamp?: string;
    kind?: string;
    volume_in_usd?: string | number;
    tx_from_address?: string;
    tx_hash?: string;
  };
};

// In-process stale-while-revalidate cache (single replica): 120 s per pool, 30 s after an error or an empty list,
// served stale for at most 10 min (older only when the refetch fails), hard cap of 500 pools. GeckoTerminal's keyless tier is ~30 calls/min; we spend at most 20.
const cache = createSwrCache<Payload>({ ttlMs: TTL_MS, max: 500, maxStaleMs: MAX_STALE_MS });
const gtBucket = createTokenBucket({ capacity: 20, refillPerSec: 20 / 60 });
class RateLimited extends Error {}

export async function GET(req: Request, { params }: { params: Promise<{ chain: string; pool: string }> }) {
  const { chain, pool } = await params;
  const chainId = parseChainParam(chain);
  const poolId = pool.toLowerCase();
  if (!chainId || !CHAINS[chainId] || !/^0x[0-9a-f]{40,64}$/.test(poolId)) return json(req, { trades: [], status: "unknown_pool" }, 404, NO_STORE);

  const key = `${chainId}:${poolId}`;
  if (!(await isKnownPool(chainId, poolId))) return json(req, { trades: [], status: "unknown_pool" }, 404, NO_STORE);

  try {
    const value = await cache.get(key, () => load(chainId, poolId), (v) => (v.status === "ok" ? TTL_MS : ERROR_TTL_MS));
    return json(req, value);
  } catch (e) {
    if (e instanceof RateLimited) return json(req, { trades: [], status: "rate_limited" }, 503, NO_STORE);
    throw e;
  }
}

async function load(chainId: ChainId, poolId: string): Promise<Payload> {
  try {
    const network = CHAINS[chainId]?.gtNetwork;
    if (!network) return { trades: [], status: "upstream_error" };
    // Checked inside the load, so a stale entry keeps serving when the budget is spent; a first load throws and is not cached.
    if (!gtBucket.take()) throw new RateLimited();
    const res = await fetch(`https://api.geckoterminal.com/api/v2/networks/${network}/pools/${poolId}/trades`, {
      headers: { Accept: "application/json", "User-Agent": UA },
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    if (!res.ok) return { trades: [], status: "upstream_error" };
    const body = (await res.json()) as { data?: GtTrade[] };
    const trades: TradeView[] = (body.data ?? [])
      .map((d) => d.attributes ?? {})
      .map((a) => ({
        ts: String(a.block_timestamp ?? ""),
        kind: a.kind === "sell" ? ("sell" as const) : ("buy" as const),
        usd: Number(a.volume_in_usd ?? 0),
        wallet: String(a.tx_from_address ?? "").toLowerCase(),
        tx: String(a.tx_hash ?? ""),
      }))
      .filter((x) => x.ts && x.tx && Number.isFinite(x.usd) && x.usd >= MIN_USD)
      .sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts))
      .slice(0, LIMIT);
    return { trades, status: trades.length ? "ok" : "empty" };
  } catch (e) {
    if (e instanceof RateLimited) throw e;
    console.error("[trades] upstream failed, serving empty:", (e as Error).message);
    return { trades: [], status: "upstream_error" };
  }
}

function json(req: Request, p: Payload, status = 200, cacheControl = "public, max-age=60, s-maxage=120"): Response {
  return respondJson(req, p, { status, headers: { "Cache-Control": cacheControl } });
}
