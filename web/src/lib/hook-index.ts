// Client loader for GET /api/hooks (slug -> hook addresses), shared by every list on the page. One request per page
// load; a failed request is forgotten so the next call tries again.
import type { HookIndex } from "./types";

let load: Promise<HookIndex> | null = null;

export function loadHookIndex(fetcher: typeof fetch = fetch): Promise<HookIndex> {
  load ??= fetcher("/api/hooks")
    .then((r) => (r.ok ? (r.json() as Promise<HookIndex>) : Promise.reject(new Error(`/api/hooks HTTP ${r.status}`))))
    .catch((e) => {
      load = null;
      throw e;
    });
  return load;
}

/** Tests only. */
export function resetHookIndex(): void {
  load = null;
}
