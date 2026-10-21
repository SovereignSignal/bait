import { drizzle, type PostgresJsDatabase } from "drizzle-orm/postgres-js";
import postgres from "postgres";
import * as schema from "./schema";

export type Db = PostgresJsDatabase<typeof schema>;

let db: Db | null = null;

/** Lazily built so `next build` works with no DATABASE_URL. */
export function getDb(): Db | null {
  const url = process.env.DATABASE_URL;
  if (!url) return null;
  if (!db) {
    // 10 connections: one project load runs up to 8 queries side by side. Postgres cancels any statement after 30 s, and
    // the name tells the web's connections apart in pg_stat_activity. Connections are recycled after 10 minutes: twice
    // (2026-10-16, 2026-10-21) every page stalled on a container up for days while trivial queries sat in ClientWrite on
    // the Postgres side, which looks like a stuck connection, not a slow query. Closes are logged so the next stall
    // shows whether a connection died or hung.
    const client = postgres(url, {
      max: 10,
      prepare: false,
      idle_timeout: 20,
      max_lifetime: 60 * 10,
      connect_timeout: 10,
      onclose: (id) => console.log(`[db] connection ${id} closed`),
      connection: { application_name: "bait-web", statement_timeout: 30_000 },
    });
    db = drizzle(client, { schema });
  }
  return db;
}

export function hasDb(): boolean {
  return !!process.env.DATABASE_URL;
}
