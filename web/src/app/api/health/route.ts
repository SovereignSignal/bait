// Health for the daily GitHub Actions alert (.github/workflows/health.yml) and the Railway healthcheck.
// Decides from the latest pipeline run receipt and every chain's indexer_state row (see lib/health.ts).
// 200 when healthy, 503 with reasons otherwise. Never throws.
import { sql, desc } from "drizzle-orm";
import { getDb } from "@/db/client";
import { runReceipts } from "@/db/schema";
import { evaluateHealth, type HealthChain, type HealthReceipt } from "@/lib/health";

export const dynamic = "force-dynamic";

type IndexerRow = {
  chain_id: number | string;
  backfilled: boolean | null;
  head_block: number | string | null;
  swap_block: number | string | null;
  updated_at: string | Date | null;
};

const num = (v: number | string | null): number | null => (v == null ? null : Number(v));

export async function GET() {
  const db = getDb();
  const now = new Date();
  if (!db) return Response.json({ ok: false, reasons: ["no DATABASE_URL"], warnings: [] }, { status: 503 });

  const queryReasons: string[] = [];
  let receipt: HealthReceipt | null = null;
  let stepStatus: Record<string, "ok" | { failed: string }> = {};
  let chains: HealthChain[] = [];
  try {
    const rows = await db.select().from(runReceipts).orderBy(desc(runReceipts.finishedAt)).limit(1);
    const r = rows[0];
    if (r) {
      receipt = { day: r.day, exitCode: r.exitCode, softErrors: r.softErrors, codeSha: r.codeSha };
      // Public endpoint: expose only "ok" or the failing class name per step, never counts' free text or "detail".
      stepStatus = Object.fromEntries(
        Object.entries(r.steps ?? {}).map(([k, v]) => [k, typeof v?.failed === "string" ? { failed: v.failed } : "ok"]),
      );
    }
  } catch (e) {
    console.error("[health] receipt query failed:", (e as Error).message.split("\n")[0]);
    queryReasons.push("run receipt query failed");
  }
  try {
    const res = (await db.execute<IndexerRow>(
      sql`select chain_id, backfilled, head_block, swap_block, updated_at from indexer_state order by chain_id`,
    )) as unknown;
    const rows = (Array.isArray(res) ? res : ((res as { rows?: unknown[] }).rows ?? [])) as IndexerRow[];
    chains = rows.map((x) => ({
      chainId: Number(x.chain_id),
      backfilled: !!x.backfilled,
      headBlock: num(x.head_block),
      swapBlock: num(x.swap_block),
      updatedAt: x.updated_at ? new Date(x.updated_at) : null,
    }));
  } catch (e) {
    console.error("[health] indexer query failed:", (e as Error).message.split("\n")[0]);
    queryReasons.push("indexer_state query failed");
  }

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
