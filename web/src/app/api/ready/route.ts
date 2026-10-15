// Railway readiness: Postgres is reachable and has the columns this web release requires.
// LIMIT 0 validates the schema without reading project data. Freshness belongs to /api/health.
import { sql } from "drizzle-orm";
import { getDb } from "@/db/client";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const db = getDb();
    if (!db) return Response.json({ ok: false, reason: "no DATABASE_URL" }, { status: 503, headers: { "Cache-Control": "no-store" } });
    await db.execute(sql`select has_historical_usage, activity_status, wash_historical from project_daily limit 0`);
    return Response.json({ ok: true }, { headers: { "Cache-Control": "no-store" } });
  } catch {
    return Response.json({ ok: false, reason: "database or required schema unavailable" }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}
