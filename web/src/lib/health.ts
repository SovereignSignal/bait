// Pure decision function for /api/health. Inputs come from the latest run receipt and every indexer_state row.
export interface HealthReceipt {
  day: string; // YYYY-MM-DD
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
}
export interface HealthInput {
  receipt: HealthReceipt | null;
  chains: HealthChain[];
}

const DEADLINE_HOUR_UTC = 8; // the nightly run starts at 06:00 UTC and must be done by now
const INDEXER_MAX_AGE_MS = 2 * 3600_000;
const SWAP_LAG_WARN_BLOCKS = 50_000;
// Soft errors no longer change the pipeline exit code (a completed run exits 0), so health is where they surface.
const SOFT_ERRORS_FAIL = 10;

export interface Freshness {
  through: string | null; // YYYY-MM-DD of the last nightly run
  note: "catching_up" | "run_late" | null; // why figures may be behind, when health fails
}

/** What a visitor is told about data age: the day of the last nightly run and, when health fails, the reason class. */
export function freshness(input: HealthInput, now: Date): Freshness {
  const v = evaluateHealth(input, now);
  const indexer = v.reasons.some((r) => r.startsWith("chain ") || r.startsWith("indexer"));
  return { through: input.receipt?.day ?? null, note: v.ok ? null : indexer ? "catching_up" : "run_late" };
}

export const FRESH_NOTE: Record<NonNullable<Freshness["note"]>, string> = {
  catching_up: "Indexer catching up",
  run_late: "Nightly update delayed",
};

export function evaluateHealth(input: HealthInput, now: Date): { ok: boolean; reasons: string[]; warnings: string[] } {
  const reasons: string[] = [];
  const warnings: string[] = [];
  const today = now.toISOString().slice(0, 10);
  const yesterday = new Date(now.getTime() - 86_400_000).toISOString().slice(0, 10);
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
    if (!c.updatedAt) reasons.push(`chain ${c.chainId} has never reported`);
    else if (now.getTime() - c.updatedAt.getTime() > INDEXER_MAX_AGE_MS)
      reasons.push(`chain ${c.chainId} last reported ${c.updatedAt.toISOString()} (over 2h ago)`);
    if (c.backfilled && c.headBlock != null && c.swapBlock != null && c.headBlock - c.swapBlock > SWAP_LAG_WARN_BLOCKS)
      warnings.push(`chain ${c.chainId} swap sync is ${c.headBlock - c.swapBlock} blocks behind head`);
  }
  return { ok: reasons.length === 0, reasons, warnings };
}
