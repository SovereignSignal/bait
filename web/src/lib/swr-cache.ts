// Stale-while-revalidate memo for a single replica. Pure: no server imports, unit tested.
type Entry<T> = { at: number; value: Promise<T>; refreshing: boolean; ttl?: number };
/** Per-call lifetime: a number, or a function of the loaded value (short-lived degraded or error results). */
export type Ttl<T> = number | ((v: T) => number);

export function createSwrCache<T>(opts: { ttlMs: number; max: number; now?: () => number }) {
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
          hit.refreshing = true;
          fn().then(
            (v) => {
              // Skip if the entry was invalidated or evicted while the refresh ran.
              if (map.get(key) === hit) map.set(key, { at: now(), value: Promise.resolve(v), refreshing: false, ttl: ttlOf(v) });
            },
            () => { hit.refreshing = false; },           // keep serving the stale value
          );
        }
        return hit.value;
      }
      while (map.size >= opts.max) {
        const oldest = map.keys().next().value;
        if (oldest === undefined) break;
        map.delete(oldest);
      }
      const entry: Entry<T> = { at: t, value: undefined as unknown as Promise<T>, refreshing: false };
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
    size: () => map.size,
  };
}
