// Copy for the live-trades panel. Mirrors the status field of /api/trades.
export type TradesStatus = "ok" | "unknown_pool" | "rate_limited" | "upstream_error" | "empty";

/** The line under "Live trades" when there is nothing to list; null when trades are showing. */
export function tradesNote(status: TradesStatus | undefined, count: number): string | null {
  if (count > 0) return null;
  if (status === "unknown_pool") return "No live trade data for this pool.";
  if (status === "ok" || status === "empty") return "No trades over $10 right now.";
  return "Trade feed unavailable, retrying in 60s.";
}
