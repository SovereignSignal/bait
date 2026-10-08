// Live recent trades for one pool, from GeckoTerminal (keyless), with wallet_labels joined.
// Never throws: any upstream error, 429 or bad input is an empty list.
import { getWalletLabels } from "@/db/queries";
import { CHAINS, parseChainParam } from "@/lib/chains";
import type { ChainId, TradeView } from "@/lib/types";

export const dynamic = "force-dynamic";

const TTL_MS = 120_000;
const ERROR_TTL_MS = 30_000;
const MIN_USD = 10;
const LIMIT = 25;
const UA = "bait/0.1 (+https://github.com/Uniswap/hooklist; hook discovery feed)";

type Payload = { trades: TradeView[] };
type GtTrade = {
  attributes?: {
    block_timestamp?: string;
    kind?: string;
    volume_in_usd?: string | number;
    tx_from_address?: string;
    tx_hash?: string;
  };
};

// In-process cache (single replica): 120s per pool, 30s after an error so a 429 is not hammered.
const memo = new Map<string, { until: number; value: Promise<Payload> }>();

export async function GET(_req: Request, { params }: { params: Promise<{ chain: string; pool: string }> }) {
  const { chain, pool } = await params;
  const chainId = parseChainParam(chain);
  const poolId = pool.toLowerCase();
  if (!chainId || !CHAINS[chainId] || !/^0x[0-9a-f]{40,64}$/.test(poolId)) return json({ trades: [] });

  const key = `${chainId}:${poolId}`;
  const now = Date.now();
  const hit = memo.get(key);
  if (hit && hit.until > now) return json(await hit.value);
  if (memo.size > 1000) for (const [k, v] of memo) if (v.until <= now) memo.delete(k);

  const entry = { until: now + TTL_MS, value: load(chainId, poolId) };
  memo.set(key, entry);
  const value = await entry.value;
  if (!value.trades.length) entry.until = now + ERROR_TTL_MS;
  return json(value);
}

async function load(chainId: ChainId, poolId: string): Promise<Payload> {
  try {
    const network = CHAINS[chainId]?.gtNetwork;
    if (!network) return { trades: [] };
    const res = await fetch(`https://api.geckoterminal.com/api/v2/networks/${network}/pools/${poolId}/trades`, {
      headers: { Accept: "application/json", "User-Agent": UA },
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    if (!res.ok) return { trades: [] };
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
    const labels = await getWalletLabels(chainId, trades.map((x) => x.wallet)).catch(() => new Map());
    return {
      trades: trades.map((x) => {
        const l = labels.get(x.wallet);
        return l ? { ...x, labels: l.labels, smartMoney: l.smartMoney } : x;
      }),
    };
  } catch (e) {
    console.error("[trades] upstream failed, serving empty:", (e as Error).message);
    return { trades: [] };
  }
}

function json(p: Payload): Response {
  return Response.json(p, { headers: { "Cache-Control": "public, max-age=60, s-maxage=120" } });
}
