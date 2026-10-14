// Copy for the live-trades panel. Mirrors the status field of /api/trades, plus "timeout" when the browser gave up.
import { int, since, usd } from "./format";

export type TradesStatus = "ok" | "unknown_pool" | "rate_limited" | "upstream_error" | "empty" | "timeout";

const LIVE_MS = 3_600_000;

function newestMs(trades: ReadonlyArray<{ ts: string }>): number {
  return Math.max(...trades.map((t) => Date.parse(t.ts)).filter(Number.isFinite));
}

/** The "Live trades" dot is green only when the feed answered and its newest trade is under an hour old. */
export function liveDotOn(status: TradesStatus | undefined, trades: ReadonlyArray<{ ts: string }>, now: number): boolean {
  if (status !== "ok" || trades.length === 0) return false;
  return now - newestMs(trades) < LIVE_MS;
}

/** The line under "Live trades" when there is nothing to list; null when trades are showing. */
export function tradesNote(status: TradesStatus | undefined, count: number): string | null {
  if (count > 0) return null;
  if (status === "unknown_pool") return "No live trade data for this pool.";
  if (status === "ok" || status === "empty") return "No trades over $10 right now.";
  if (status === "timeout") return "Trade feed did not answer, retrying in 60s.";
  return "Trade feed unavailable, retrying in 60s.";
}

/**
 * Above a list whose newest trade is over an hour old: say so, since the rows' ages alone read like a broken feed
 * next to a big 24h figure. null while the feed is live.
 */
export function quietNote(trades: ReadonlyArray<{ ts: string }>, now: number): string | null {
  if (!trades.length) return null;
  const newest = newestMs(trades);
  if (!Number.isFinite(newest) || now - newest < LIVE_MS) return null;
  return `No trades over $10 in the last hour; the newest is ${since(new Date(newest).toISOString(), now)} old.`;
}

/**
 * The indexer's own figures for the pool (closed UTC day, from pool_stats), shown when the live feed is quiet or has
 * nothing, so the panel never contradicts the stat tiles without saying why. null when the indexer has no day either.
 */
export function indexerLine(pool: { vol24h: number | null; wallets24h: number | null; walletsExact: boolean }): string | null {
  if (pool.vol24h == null || pool.vol24h <= 0) return null;
  const who = pool.wallets24h ? ` from ${pool.walletsExact ? "" : "up to "}${int(pool.wallets24h)} wallet${pool.wallets24h === 1 ? "" : "s"}` : "";
  return `Indexer, last closed day: ${usd(pool.vol24h)}${who} in this pool.`;
}
