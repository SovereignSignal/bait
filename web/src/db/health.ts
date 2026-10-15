// Reads behind /api/health and the freshness line: the latest run receipt and every chain's indexer_state row.
// Never throws: a failed read is reported in queryReasons.
import { desc, sql } from "drizzle-orm";
import { runReceipts } from "@/db/schema";
import { freshness, type Freshness, type HealthChain, type HealthReceipt } from "@/lib/health";
import { getDb, type Db } from "./client";
import { cached, usingFixtures } from "./queries";
import { SAMPLE_FRESHNESS } from "@/fixtures/sample";

type IndexerRow = {
  chain_id: number | string;
  backfilled: boolean | null;
  head_block: number | string | null;
  swap_block: number | string | null;
  updated_at: string | Date | null;
  swap_block_time: string | Date | null;
  queued: number | string | null;
  owed: number | string | null;
  oldest: string | Date | null;
  reported_through: string | null;
};

export type StepStatus = Record<string, "ok" | { failed: string }>;

const num = (v: number | string | null): number | null => (v == null ? null : Number(v));

export async function readHealth(db: Db): Promise<{ receipt: HealthReceipt | null; stepStatus: StepStatus; chains: HealthChain[]; queryReasons: string[]; reportedThrough: string | null }> {
  const queryReasons: string[] = [];
  let receipt: HealthReceipt | null = null;
  let stepStatus: StepStatus = {};
  let chains: HealthChain[] = [];
  let reportedThrough: string | null = null;
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
      sql`with reporting as (select max(window_end) as through from project_daily),
           pending as (
             select chain_id, count(*) as queued, min(from_time) as oldest,
               count(*) filter (where from_time is null or from_time < ((select through from reporting) + 1)::timestamp at time zone 'UTC') as owed
             from pool_lookup_queue group by chain_id
           )
           select s.chain_id, s.backfilled, s.head_block, s.swap_block, s.updated_at, s.swap_block_time,
             coalesce(q.queued, 0) as queued, coalesce(q.owed, 0) as owed, q.oldest,
             (select through::text from reporting) as reported_through
           from indexer_state s left join pending q using (chain_id) order by s.chain_id`,
    )) as unknown;
    const rows = (Array.isArray(res) ? res : ((res as { rows?: unknown[] }).rows ?? [])) as IndexerRow[];
    reportedThrough = rows[0]?.reported_through ?? null;
    chains = rows.map((x) => ({
      chainId: Number(x.chain_id),
      backfilled: !!x.backfilled,
      headBlock: num(x.head_block),
      swapBlock: num(x.swap_block),
      updatedAt: x.updated_at ? new Date(x.updated_at) : null,
      swapBlockTime: x.swap_block_time ? new Date(x.swap_block_time) : null,
      pendingPools: num(x.queued),
      pendingBeforeThrough: num(x.owed),
      oldestPending: x.oldest ? new Date(x.oldest) : null,
    }));
  } catch (e) {
    console.error("[health] indexer query failed:", (e as Error).message.split("\n")[0]);
    queryReasons.push("indexer_state query failed");
  }
  return { receipt, stepStatus, chains, queryReasons, reportedThrough };
}

/** The freshness line: explicit candidate fixtures may replay captured status; failed DB reads show coverage unavailable. */
export async function getFreshness(): Promise<Freshness | null> {
  const db = getDb();
  if (!db) return usingFixtures() ? SAMPLE_FRESHNESS : null;
  // cached a minute: dynamic pages render it on every request, and a chip should clear soon after health recovers
  return cached("freshness", async () => {
    const { receipt, chains, queryReasons, reportedThrough } = await readHealth(db);
    const value = freshness({ receipt, chains, reportedThrough }, new Date());
    return queryReasons.length ? { ...value, note: "coverage_unknown" as const } : value;
  }, 60_000);
}
