// Fetches Uniswap's published hook routing lists (lib/uniswap-routing.ts) for the server, at most every 6 hours. A failed
// fetch keeps the last good lists; with none yet, routing falls back to the automatic rule alone and says so.
import {
  ALLOWLIST_FILE,
  DENYLIST_FILE,
  LISTS_BASE,
  NO_LISTS,
  listSize,
  parseRoutingList,
  type RoutingLists,
} from "@/lib/uniswap-routing";

const TTL_MS = 6 * 3600_000;
const RETRY_MS = 10 * 60_000;
// The allowlist held 587 hooks on 2026-10-16. Far fewer means the file layout changed and the parse is not trustworthy.
const MIN_ALLOWLISTED = 100;
const UA = "bait/0.1 (+https://github.com/SovereignSignal/bait; hook discovery feed)";

let last: { lists: RoutingLists; at: number } | null = null;
let pending: Promise<RoutingLists> | null = null;
let failedAt = 0;

async function text(file: string): Promise<string> {
  const res = await fetch(LISTS_BASE + file, { headers: { "User-Agent": UA }, cache: "no-store", signal: AbortSignal.timeout(8000) });
  if (!res.ok) throw new Error(`${file}: HTTP ${res.status}`);
  return res.text();
}

async function load(): Promise<RoutingLists> {
  const [a, d] = await Promise.all([text(ALLOWLIST_FILE), text(DENYLIST_FILE)]);
  const allow = parseRoutingList(a, "HOOKS_ADDRESSES_ALLOWLIST");
  const deny = parseRoutingList(d, "HOOKS_ADDRESSES_DENYLIST");
  if (listSize(allow) < MIN_ALLOWLISTED) throw new Error(`allowlist parsed to ${listSize(allow)} hooks`);
  return { allow, deny, checkedAt: new Date().toISOString() };
}

/** The current lists, or the last good ones, or NO_LISTS. Never throws; off in fixture mode and when BAIT_ROUTING_LISTS=0. */
export async function routingLists(): Promise<RoutingLists> {
  if (process.env.BAIT_ROUTING_LISTS === "0" || process.env.BAIT_FIXTURES === "1") return NO_LISTS;
  const now = Date.now();
  if (last && now - last.at < TTL_MS) return last.lists;
  if (!last && now - failedAt < RETRY_MS) return NO_LISTS;
  if (!pending) {
    pending = load()
      .then((lists) => {
        last = { lists, at: Date.now() };
        return lists;
      })
      .catch((e) => {
        failedAt = Date.now();
        console.warn(`[bait] routing lists unavailable: ${e instanceof Error ? e.message : e}`);
        return last?.lists ?? NO_LISTS;
      })
      .finally(() => {
        pending = null;
      });
  }
  // a stale copy answers at once while the refresh runs; only the very first load is waited for
  return last ? last.lists : pending;
}
