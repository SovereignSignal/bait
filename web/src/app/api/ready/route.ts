// Liveness for Railway's healthcheck: can we reach Postgres? Nothing about data freshness (that is /api/health).
import { sql } from "drizzle-orm";
import { getDb } from "@/db/client";

export const dynamic = "force-dynamic";

export async function GET() {
  const db = getDb();
  if (!db) return Response.json({ ok: false, reason: "no DATABASE_URL" }, { status: 503, headers: { "Cache-Control": "no-store" } });
  try {
    await db.execute(sql`select 1`);
    return Response.json({ ok: true }, { headers: { "Cache-Control": "no-store" } });
  } catch (e) {
    return Response.json({ ok: false, reason: (e as Error).message.split("\n")[0] }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}
