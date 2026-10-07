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
    const client = postgres(url, { max: 5, prepare: false, idle_timeout: 20 });
    db = drizzle(client, { schema });
  }
  return db;
}

export function hasDb(): boolean {
  return !!process.env.DATABASE_URL;
}
