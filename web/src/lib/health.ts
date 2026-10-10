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
  through: string | null; // YYYY-MM-DD: the last closed UTC day the nightly figures cover
  note: "run_late" | "run_failed" | "catching_up" | null; // why figures may be behind, when health fails
}

const DAY_MS = 86_400_000;
const FIRST_REAL_DAY = "2020-01-01"; // a repair receipt written before any nightly carries 1970-01-01

/** What a visitor is told about data age. The run dated D scores the closed day D-1, so the figures run through D-1.
 * When health fails, the reason class: a pipeline problem outranks indexer lag, since it is what dates the figures. */
export function freshness(input: HealthInput, now: Date): Freshness {
  const v = evaluateHealth(input, now);
  const day = input.receipt?.day;
  const through = day && day >= FIRST_REAL_DAY ? new Date(Date.parse(`${day}T00:00:00Z`) - DAY_MS).toISOString().slice(0, 10) : null;
  const has = (...prefixes: string[]) => v.reasons.some((r) => prefixes.some((p) => r.startsWith(p)));
  const note = v.ok
    ? null
    : has("no pipeline run receipt", "last pipeline receipt")
      ? "run_late"
      : has("pipeline run ")
        ? "run_failed"
        : has("chain ", "indexer")
          ? "catching_up"
          : null;
  return { through, note };
}

export const FRESH_NOTE: Record<NonNullable<Freshness["note"]>, string> = {
  run_late: "Nightly update delayed",
  run_failed: "Nightly update incomplete",
  catching_up: "Indexer catching up",
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
