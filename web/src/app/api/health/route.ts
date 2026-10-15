// Health for the daily GitHub Actions alert (.github/workflows/health.yml) and the Railway healthcheck.
// Decides from the latest pipeline run receipt and every chain's indexer_state row (see lib/health.ts).
// 200 when healthy, 503 with reasons otherwise. Never throws.
import { getDb } from "@/db/client";
import { readHealth } from "@/db/health";
import { evaluateHealth, evaluateCoverage } from "@/lib/health";

export const dynamic = "force-dynamic";

export async function GET() {
  const db = getDb();
  const now = new Date();
  if (!db) return Response.json({ ok: false, reasons: ["no DATABASE_URL"], warnings: [] }, { status: 503 });

  const { receipt, stepStatus, chains, queryReasons, reportedThrough } = await readHealth(db);
  const input = { receipt, chains, reportedThrough };
  const verdict = evaluateHealth(input, now);
  const coverage = evaluateCoverage(input, now);
  const reasons = [...queryReasons, ...verdict.reasons];
  const ok = reasons.length === 0;
  return Response.json(
    {
      ok,
      checked_at: now.toISOString(),
      coverage: { ...coverage, status: queryReasons.length ? "unknown" : coverage.status,
        scope: "Reported closed-day swap indexing on the observed chains. Does not certify pricing, organic volume, or contract safety." },
      receipt: receipt
        ? { day: receipt.day, exit_code: receipt.exitCode, soft_errors: receipt.softErrors, code_sha: receipt.codeSha, steps: stepStatus }
        : null,
      chains: chains.map((c) => ({
        chain_id: c.chainId,
        backfilled: c.backfilled,
        swap_lag: c.headBlock != null && c.swapBlock != null ? c.headBlock - c.swapBlock : null,
        updated_at: c.updatedAt?.toISOString() ?? null,
        swap_block_time: c.swapBlockTime?.toISOString() ?? null,
        unresolved_pool_ids: c.pendingPools ?? null,
        unresolved_before_reported_day_end: c.pendingBeforeThrough ?? null,
        oldest_unresolved_at: c.oldestPending?.toISOString() ?? null,
      })),
      reasons,
      warnings: verdict.warnings,
    },
    { status: ok ? 200 : 503, headers: { "Cache-Control": "no-store" } },
  );
}
