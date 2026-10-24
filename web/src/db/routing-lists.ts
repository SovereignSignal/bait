// Fetches Uniswap's published hook routing lists (lib/uniswap-routing.ts) for the server, at most every 6 hours.
// Never on a page's critical path: routingLists() answers at once with the last good lists (or none yet) and refreshes
// in the background. 2026-10-16: awaiting Next's patched fetch inside page rendering hung every data page for about 15
// minutes after deploy while API routes, which fetched the same files, answered. Node's own https client is used so Next
// neither intercepts the request nor reclassifies the page.
import { get } from "node:https";
import { processSingleton } from "@/lib/singleton";
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
const TIMEOUT_MS = 8000;
const MAX_BYTES = 2_000_000;
// The allowlist held 587 hooks on 2026-10-16. Far fewer means the file layout changed and the parse is not trustworthy.
const MIN_ALLOWLISTED = 100;
const UA = "bait/0.1 (+https://github.com/SovereignSignal/bait; hook discovery feed)";

// One copy per process (lib/singleton.ts): the list load is shared by every Next bundle, and a refresh started from one
// that had not fetched the lists yet would rebuild every row's routing from the rule alone.
const state = processSingleton("routing-lists", () => ({
  last: null as { lists: RoutingLists; at: number } | null,
  pending: null as Promise<void> | null,
  failedAt: 0,
}));

function text(file: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const req = get(LISTS_BASE + file, { headers: { "User-Agent": UA } }, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        reject(new Error(`${file}: HTTP ${res.statusCode}`));
        return;
      }
      res.setEncoding("utf8");
      let body = "";
      res.on("data", (chunk: string) => {
        body += chunk;
        if (body.length > MAX_BYTES) req.destroy(new Error(`${file}: over ${MAX_BYTES} bytes`));
      });
      res.on("end", () => resolve(body));
      res.on("error", reject);
    });
    req.setTimeout(TIMEOUT_MS, () => req.destroy(new Error(`${file}: timed out`)));
    req.on("error", reject);
  });
}

async function load(): Promise<RoutingLists> {
  const [a, d] = await Promise.all([text(ALLOWLIST_FILE), text(DENYLIST_FILE)]);
  const allow = parseRoutingList(a, "HOOKS_ADDRESSES_ALLOWLIST");
  const deny = parseRoutingList(d, "HOOKS_ADDRESSES_DENYLIST");
  if (listSize(allow) < MIN_ALLOWLISTED) throw new Error(`allowlist parsed to ${listSize(allow)} hooks`);
  return { allow, deny, checkedAt: new Date().toISOString() };
}

/** Off in fixture mode and when BAIT_ROUTING_LISTS=0; routing then uses the automatic rule alone and says so. */
export function routingListsEnabled(): boolean {
  return process.env.BAIT_ROUTING_LISTS !== "0" && process.env.BAIT_FIXTURES !== "1";
}

/** The last good lists, or NO_LISTS; starts a background refresh when they are missing or older than 6 hours. Never
 * waits on the network and never throws. */
export async function routingLists(): Promise<RoutingLists> {
  if (!routingListsEnabled()) return NO_LISTS;
  const now = Date.now();
  const stale = !state.last || now - state.last.at >= TTL_MS;
  if (stale && !state.pending && now - state.failedAt >= RETRY_MS) {
    state.pending = load()
      .then((lists) => {
        state.last = { lists, at: Date.now() };
      })
      .catch((e) => {
        state.failedAt = Date.now();
        console.warn(`[bait] routing lists unavailable: ${e instanceof Error ? e.message : e}`);
      })
      .finally(() => {
        state.pending = null;
      });
  }
  return state.last?.lists ?? NO_LISTS;
}
