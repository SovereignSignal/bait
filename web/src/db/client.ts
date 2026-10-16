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
    // the name tells the web's connections apart in pg_stat_activity.
    const client = postgres(url, {
      max: 10,
      prepare: false,
      idle_timeout: 20,
      connect_timeout: 10,
      connection: { application_name: "bait-web", statement_timeout: 30_000 },
    });
    db = drizzle(client, { schema });
  }
  return db;
}

export function hasDb(): boolean {
  return !!process.env.DATABASE_URL;
}
