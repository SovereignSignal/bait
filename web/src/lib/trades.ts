// Copy for the live-trades panel. Mirrors the status field of /api/trades.
export type TradesStatus = "ok" | "unknown_pool" | "rate_limited" | "upstream_error" | "empty";

const LIVE_MS = 3_600_000;

/** The "Live trades" dot is green only when the feed answered and its newest trade is under an hour old. */
export function liveDotOn(status: TradesStatus | undefined, trades: ReadonlyArray<{ ts: string }>, now: number): boolean {
  if (status !== "ok" || trades.length === 0) return false;
  const newest = Math.max(...trades.map((t) => Date.parse(t.ts)));
  return now - newest < LIVE_MS;
}

/** The line under "Live trades" when there is nothing to list; null when trades are showing. */
export function tradesNote(status: TradesStatus | undefined, count: number): string | null {
  if (count > 0) return null;
  if (status === "unknown_pool") return "No live trade data for this pool.";
  if (status === "ok" || status === "empty") return "No trades over $10 right now.";
  return "Trade feed unavailable, retrying in 60s.";
}
