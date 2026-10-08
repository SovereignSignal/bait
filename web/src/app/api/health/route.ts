// Freshness check for the daily GitHub Actions alert (.github/workflows/health.yml).
// 200 when the nightly pipeline scored today (or yesterday before 08:00 UTC) and the indexer reported within 2 hours;
// 503 with reasons otherwise. Never throws.
import { sql } from "drizzle-orm";
import { getDb } from "@/db/client";
import { safe } from "@/db/queries";

export const dynamic = "force-dynamic";

const INDEXER_MAX_AGE_MS = 2 * 3600_000;

export async function GET() {
  const db = getDb();
  const now = new Date();
  if (!db) return Response.json({ ok: false, reasons: ["no DATABASE_URL"] }, { status: 503 });
  const [pipe, idx] = await Promise.all([
    safe("health:pipeline", async () => (await db.execute<{ d: string | null }>(sql`select max(day)::text as d from project_daily`)) as unknown, null),
    safe("health:indexer", async () => (await db.execute<{ t: string | null }>(sql`select max(updated_at) as t from indexer_state`)) as unknown, null),
  ]);
  const first = (r: unknown) => (Array.isArray(r) ? r[0] : (r as { rows?: unknown[] } | null)?.rows?.[0]) as Record<string, unknown> | undefined;
  const lastDay = (first(pipe)?.d as string | null) ?? null;
  const idxRaw = first(idx)?.t as string | Date | null | undefined;
  const indexerAt = idxRaw ? new Date(idxRaw) : null;

  const today = now.toISOString().slice(0, 10);
  const yesterday = new Date(now.getTime() - 86_400_000).toISOString().slice(0, 10);
  const graceForToday = now.getUTCHours() < 8;   // nightly runs at 06:00 UTC
  const reasons: string[] = [];
  if (!lastDay) reasons.push("pipeline has never scored a day");
  else if (lastDay !== today && !(graceForToday && lastDay === yesterday)) reasons.push(`pipeline last scored ${lastDay}, expected ${today}`);
  if (!indexerAt) reasons.push("indexer has never reported");
  else if (now.getTime() - indexerAt.getTime() > INDEXER_MAX_AGE_MS)
    reasons.push(`indexer last reported ${indexerAt.toISOString()} (over 2h ago)`);

  const ok = reasons.length === 0;
  return Response.json({ ok, checked_at: now.toISOString(), pipeline_last_day: lastDay, indexer_updated_at: indexerAt?.toISOString() ?? null, reasons },
    { status: ok ? 200 : 503, headers: { "Cache-Control": "no-store" } });
}
