// Reads behind /api/health and the freshness line: the latest run receipt and every chain's indexer_state row.
// Never throws: a failed read is reported in queryReasons.
import { desc, sql } from "drizzle-orm";
import { runReceipts } from "@/db/schema";
import { freshness, type Freshness, type HealthChain, type HealthReceipt } from "@/lib/health";
import { getDb, type Db } from "./client";
import { cached } from "./queries";

type IndexerRow = {
  chain_id: number | string;
  backfilled: boolean | null;
  head_block: number | string | null;
  swap_block: number | string | null;
  updated_at: string | Date | null;
};

export type StepStatus = Record<string, "ok" | { failed: string }>;

const num = (v: number | string | null): number | null => (v == null ? null : Number(v));

export async function readHealth(db: Db): Promise<{ receipt: HealthReceipt | null; stepStatus: StepStatus; chains: HealthChain[]; queryReasons: string[] }> {
  const queryReasons: string[] = [];
  let receipt: HealthReceipt | null = null;
  let stepStatus: StepStatus = {};
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
  return { receipt, stepStatus, chains, queryReasons };
}

/** The freshness line for pages; null without a database (fixtures, build) or when a read failed. */
export async function getFreshness(): Promise<Freshness | null> {
  const db = getDb();
  if (!db) return null;
  // cached a minute: dynamic pages render it on every request, and a chip should clear soon after health recovers
  return cached("freshness", async () => {
    const { receipt, chains, queryReasons } = await readHealth(db);
    return queryReasons.length ? null : freshness({ receipt, chains }, new Date());
  }, 60_000);
}
