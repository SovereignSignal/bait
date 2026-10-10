// Health for the daily GitHub Actions alert (.github/workflows/health.yml) and the Railway healthcheck.
// Decides from the latest pipeline run receipt and every chain's indexer_state row (see lib/health.ts).
// 200 when healthy, 503 with reasons otherwise. Never throws.
import { getDb } from "@/db/client";
import { readHealth } from "@/db/health";
import { evaluateHealth } from "@/lib/health";

export const dynamic = "force-dynamic";

export async function GET() {
  const db = getDb();
  const now = new Date();
  if (!db) return Response.json({ ok: false, reasons: ["no DATABASE_URL"], warnings: [] }, { status: 503 });

  const { receipt, stepStatus, chains, queryReasons } = await readHealth(db);
  const verdict = evaluateHealth({ receipt, chains }, now);
  const reasons = [...queryReasons, ...verdict.reasons];
  const ok = reasons.length === 0;
  return Response.json(
    {
      ok,
      checked_at: now.toISOString(),
      receipt: receipt
        ? { day: receipt.day, exit_code: receipt.exitCode, soft_errors: receipt.softErrors, code_sha: receipt.codeSha, steps: stepStatus }
        : null,
      chains: chains.map((c) => ({
        chain_id: c.chainId,
        backfilled: c.backfilled,
        swap_lag: c.headBlock != null && c.swapBlock != null ? c.headBlock - c.swapBlock : null,
        updated_at: c.updatedAt?.toISOString() ?? null,
      })),
      reasons,
      warnings: verdict.warnings,
    },
    { status: ok ? 200 : 503, headers: { "Cache-Control": "no-store" } },
  );
}
