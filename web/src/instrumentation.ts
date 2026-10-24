// Runs once when the server starts. Begins the list load and the freshness read in the background, so the first visitor
// after a deploy finds them loaded (or in flight) instead of waiting several seconds for the whole list. The caches are
// per process (lib/singleton.ts), so this is the same load "/" and GET /api/rows read. Never awaited, never throws.
export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs" || !process.env.DATABASE_URL) return;   // Next skips register() in `next build`
  const [{ getRowSet }, { getFreshness }] = await Promise.all([import("@/db/queries"), import("@/db/health")]);
  void Promise.all([getRowSet(), getFreshness()]).catch(() => {});
}
