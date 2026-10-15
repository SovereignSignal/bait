// Service liveness and reported-window indexer coverage are separate verdicts.
export interface HealthReceipt {
  day: string;
  exitCode: number;
  softErrors: number;
  codeSha: string | null;
}
export interface HealthChain {
  chainId: number;
  backfilled: boolean;
  headBlock: number | null;
  swapBlock: number | null;
  updatedAt: Date | null;
  swapBlockTime?: Date | null;
  pendingPools?: number | null;
  pendingBeforeThrough?: number | null;
  oldestPending?: Date | null;
}
export interface HealthInput {
  receipt: HealthReceipt | null;
  chains: HealthChain[];
  /** Actual latest scored window, not the date of an otherwise successful run. */
  reportedThrough?: string | null;
}
const DEADLINE_HOUR_UTC = 8;
const INDEXER_MAX_AGE_MS = 2 * 3600_000;
const SWAP_LAG_WARN_BLOCKS = 50_000;
const SOFT_ERRORS_FAIL = 10;
const DAY_MS = 86_400_000;
const FIRST_REAL_DAY = "2020-01-01";
export interface Freshness {
  through: string | null;
  note: "run_late" | "run_failed" | "catching_up" | "coverage_unknown" | null;
  partialChains?: number[];
}
function reportedThrough(input: HealthInput): string | null {
  if (input.reportedThrough !== undefined) return input.reportedThrough;
  const day = input.receipt?.day;
  if (!day || day < FIRST_REAL_DAY || !Number.isFinite(Date.parse(`${day}T00:00:00Z`))) return null;
  return new Date(Date.parse(`${day}T00:00:00Z`) - DAY_MS).toISOString().slice(0, 10);
}
export interface Coverage {
  status: "ready" | "partial" | "unknown";
  through: string | null;
  chains: Array<{ chainId: number; status: "ready" | "partial" | "unknown"; reasons: string[] }>;
}
/** Coverage of the reported closed day on the observed chains only. This does not certify USD pricing,
 * organic demand, contract safety, every pool ever created, or correctness of a provider's fallback data. */
export function evaluateCoverage(input: HealthInput, now: Date): Coverage {
  const through = reportedThrough(input);
  const boundary = through ? Date.parse(`${through}T00:00:00Z`) + DAY_MS : NaN;
  const chains: Coverage["chains"] = input.chains.map(c => {
    const reasons: string[] = [];
    let partial = false;
    if (!c.backfilled) { reasons.push("initial backfill incomplete"); partial = true; }
    if (c.pendingBeforeThrough == null) reasons.push("pending-history coverage unavailable");
    else if (c.pendingBeforeThrough > 0) { reasons.push(`${c.pendingBeforeThrough} unresolved pool identifiers owe swaps before the reported day's end`); partial = true; }
    if (!c.swapBlockTime || !Number.isFinite(c.swapBlockTime.getTime())) reasons.push("swap cursor time unavailable");
    else if (Number.isFinite(boundary) && c.swapBlockTime.getTime() < boundary) { reasons.push("swap cursor has not covered the reported day's end"); partial = true; }
    if (!c.updatedAt || !Number.isFinite(c.updatedAt.getTime())) reasons.push("indexer freshness unavailable");
    else if (now.getTime() - c.updatedAt.getTime() > INDEXER_MAX_AGE_MS) { reasons.push("indexer report is stale"); partial = true; }
    if (!Number.isFinite(boundary)) reasons.push("scored window unavailable");
    return { chainId: c.chainId, status: partial ? "partial" : reasons.length ? "unknown" : "ready", reasons };
  });
  const status = chains.some(c => c.status === "partial") ? "partial"
    : !chains.length || !Number.isFinite(boundary) || chains.some(c => c.status === "unknown") ? "unknown" : "ready";
  return { status, through, chains };
}
export function freshness(input: HealthInput, now: Date): Freshness {
  const v = evaluateHealth(input, now);
  const coverage = evaluateCoverage(input, now);
  const has = (...prefixes: string[]) => v.reasons.some(r => prefixes.some(p => r.startsWith(p)));
  // The site-wide chip answers "is the nightly data trustworthy right now"; a chain that is still backfilling or has
  // unknown coverage is a per-project matter (partialChains), shown only on projects with pools on that chain.
  const note = v.ok ? null
    : has("no pipeline run receipt", "last pipeline receipt") ? "run_late"
    : has("pipeline run ") ? "run_failed"
    : has("chain ", "indexer") ? "catching_up"
    : coverage.status === "unknown" ? "coverage_unknown" : null;
  return { through: coverage.through, note, partialChains: coverage.chains.filter(c => c.status !== "ready").map(c => c.chainId) };
}
export const FRESH_NOTE: Record<NonNullable<Freshness["note"]>, string> = {
  run_late: "Nightly update delayed",
  run_failed: "Nightly update incomplete",
  catching_up: "Indexed history incomplete",
  coverage_unknown: "Data coverage unavailable",
};
export function evaluateHealth(input: HealthInput, now: Date): { ok: boolean; reasons: string[]; warnings: string[] } {
  const reasons: string[] = [];
  const warnings: string[] = [];
  const today = now.toISOString().slice(0, 10);
  const yesterday = new Date(now.getTime() - DAY_MS).toISOString().slice(0, 10);
  const expected = now.getUTCHours() < DEADLINE_HOUR_UTC ? yesterday : today;
  const r = input.receipt;
  if (!r) reasons.push("no pipeline run receipt");
  else {
    if (r.day < expected) reasons.push(`last pipeline receipt is for ${r.day}, expected ${expected}`);
    if (r.exitCode !== 0) reasons.push(`pipeline run for ${r.day} exited ${r.exitCode}`);
    if (r.softErrors >= SOFT_ERRORS_FAIL) reasons.push(`pipeline run for ${r.day} had ${r.softErrors} soft errors`);
    else if (r.softErrors > 0) warnings.push(`pipeline run for ${r.day} had ${r.softErrors} soft errors`);
  }
  if (input.chains.length === 0) reasons.push("indexer has never reported");
  for (const c of input.chains) {
    if (!c.updatedAt || !Number.isFinite(c.updatedAt.getTime())) reasons.push(`chain ${c.chainId} has never reported`);
    else if (now.getTime() - c.updatedAt.getTime() > INDEXER_MAX_AGE_MS)
      reasons.push(`chain ${c.chainId} last reported ${c.updatedAt.toISOString()} (over 2h ago)`);
    if (c.backfilled && c.headBlock != null && c.swapBlock != null && c.headBlock - c.swapBlock > SWAP_LAG_WARN_BLOCKS)
      warnings.push(`chain ${c.chainId} swap sync is ${c.headBlock - c.swapBlock} blocks behind head`);
  }
  const coverage = evaluateCoverage(input, now);
  if (coverage.status !== "ready") warnings.push(`reported-window indexer coverage is ${coverage.status}; service health does not certify complete data`);
  return { ok: reasons.length === 0, reasons, warnings };
}
