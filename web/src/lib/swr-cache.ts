// Stale-while-revalidate memo for a single replica. Pure: no server imports, unit tested.
type Entry<T> = { at: number; value: Promise<T>; refreshing: Promise<T> | null; ttl?: number };
/** Per-call lifetime: a number, or a function of the loaded value (short-lived degraded or error results). */
export type Ttl<T> = number | ((v: T) => number);

/**
 * `maxStaleMs` bounds how old a served value may be: past it, the caller waits for the refresh instead of getting the
 * stale value first (an idle key could otherwise answer with data from days ago). The stale value is still the fallback
 * when that refresh fails.
 */
export function createSwrCache<T>(opts: { ttlMs: number; max: number; maxStaleMs?: number; now?: () => number }) {
  const now = opts.now ?? Date.now;
  const map = new Map<string, Entry<T>>();
  return {
    get(key: string, fn: () => Promise<T>, ttl?: Ttl<T>): Promise<T> {
      const ttlOf = (v: T): number | undefined => (typeof ttl === "function" ? ttl(v) : ttl);
      const t = now();
      const hit = map.get(key);
      if (hit) {
        if (t - hit.at < (hit.ttl ?? opts.ttlMs)) return hit.value;
        if (!hit.refreshing) {
          const refresh = fn().then((v) => {
            // Skip if the entry was invalidated or evicted while the refresh ran.
            if (map.get(key) === hit) map.set(key, { at: now(), value: Promise.resolve(v), refreshing: null, ttl: ttlOf(v) });
            return v;
          });
          hit.refreshing = refresh;
          refresh.catch(() => { hit.refreshing = null; });     // keep serving the stale value
        }
        if (opts.maxStaleMs != null && t - hit.at >= opts.maxStaleMs) {
          const stale = hit.value;
          return hit.refreshing.catch(() => stale);
        }
        return hit.value;
      }
      while (map.size >= opts.max) {
        const oldest = map.keys().next().value;
        if (oldest === undefined) break;
        map.delete(oldest);
      }
      const entry: Entry<T> = { at: t, value: undefined as unknown as Promise<T>, refreshing: null };
      entry.value = fn().then(
        (v) => {
          entry.ttl = ttlOf(v);
          return v;
        },
        (e) => {
          if (map.get(key) === entry) map.delete(key);
          throw e;
        },
      );
      map.set(key, entry);
      return entry.value;
    },
    invalidate(prefix?: string) {
      if (!prefix) return map.clear();
      for (const k of [...map.keys()]) if (k.startsWith(prefix)) map.delete(k);
    },
    /**
     * Marks entries stale without dropping them: the next get starts one refresh and is answered with the old value
     * meanwhile (unlike invalidate, where it waits for a full load). A refresh already running is discarded, so a load
     * that began before the change cannot land as fresh.
     */
    expire(prefix?: string) {
      for (const [k, e] of map) if (!prefix || k.startsWith(prefix)) map.set(k, { at: e.at, value: e.value, refreshing: null, ttl: 0 });
    },
    size: () => map.size,
  };
}
