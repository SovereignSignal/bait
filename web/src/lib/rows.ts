// The full row set for client search, watchlist and filtering, served by GET /api/rows and fetched once per page load.
// It used to ride in every "/" and "/p/[slug]" RSC payload (2,300 rows, ~0.9 MB per response, link prefetches
// included); now a page ships only the rows it shows (FeedSeed) and the rest arrive here, packed as arrays under one
// key list, since the repeated keys were half of each row's bytes.
import type { RowView } from "./types";

/** What the list, movers and search read. rank is left out: applyTab recomputes it over the full set. */
export const ROW_KEYS = [
  "slug", "name", "whatLine", "color", "category", "secondaryCategories", "chains", "firstSeenAt", "isNew", "isWash",
  "scoreTrending", "vol7d", "vol7dPrev", "vol24h", "tvlUsd", "routable", "isDormant", "activityStatus", "washHistorical",
  "hasHistoricalUsage",
] as const satisfies ReadonlyArray<keyof RowView>;

export interface PackedRows {
  keys: string[];
  rows: unknown[][];
}

export function packRows(rows: RowView[]): PackedRows {
  return { keys: [...ROW_KEYS], rows: rows.map((r) => ROW_KEYS.map((k) => r[k])) };
}

/** Short content hash (FNV-1a, base 36) of a packed set: the cache key a page hands to loadRows. */
export function rowsVersion(p: PackedRows): string {
  const s = JSON.stringify(p);
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 0x01000193);
  return `${(h >>> 0).toString(36)}.${p.rows.length}`;
}

/** Reads the key list the server sent, so a deploy that adds a key does not misalign an open tab's rows. */
export function unpackRows(p: PackedRows): RowView[] {
  if (!Array.isArray(p?.keys) || !Array.isArray(p?.rows)) throw new Error("/api/rows: not a packed row set");
  return p.rows.map((r) => Object.fromEntries(p.keys.map((k, i) => [k, r[i]])) as unknown as RowView);
}

// ---------- client loader ----------

let load: { version: string; rows: Promise<RowView[]> } | null = null;
let loaded: { version: string; rows: RowView[] } | null = null;

/** GET /api/rows?v=<version>, once per version; the version is in the URL so the browser may cache it for a day and
 * still never mix a new page with yesterday's rows. A failed load is forgotten so the next call tries again. */
export function loadRows(version: string, fetcher: typeof fetch = fetch): Promise<RowView[]> {
  if (load?.version === version) return load.rows;
  const rows = fetcher(`/api/rows?v=${encodeURIComponent(version)}`)
    .then((r) => (r.ok ? (r.json() as Promise<PackedRows>) : Promise.reject(new Error(`/api/rows HTTP ${r.status}`))))
    .then(unpackRows)
    .then((x) => {
      loaded = { version, rows: x };
      return x;
    })
    .catch((e) => {
      if (load?.rows === rows) load = null;
      throw e;
    });
  load = { version, rows };
  return rows;
}

/** Rows already here from an earlier page on this visit (any version): a client navigation renders them at once. */
export function peekRows(): RowView[] | undefined {
  return loaded?.rows;
}

/** Tests only. */
export function resetRows(): void {
  load = null;
  loaded = null;
}
