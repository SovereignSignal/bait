// One value per server process. Next bundles route handlers apart from pages, so a module-level `const` exists once per
// bundle: on 2026-10-24 "/" and GET /api/rows (which every visit to "/" fetches next) each held their own copy of the
// list cache, and each paid the full list load. Pure: no server imports, unit tested.
const KEY = Symbol.for("bait.singletons");

export function processSingleton<T>(name: string, init: () => T): T {
  const g = globalThis as typeof globalThis & { [KEY]?: Map<string, unknown> };
  const all = (g[KEY] ??= new Map<string, unknown>());
  if (!all.has(name)) all.set(name, init());
  return all.get(name) as T;
}
