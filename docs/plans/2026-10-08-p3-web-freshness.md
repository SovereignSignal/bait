# Package 3: Web freshness and safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The web never stalls a visitor on a cache refill, never serves an empty build-time feed after a deploy, never spends upstream quota on made-up pool ids, hides what the pipeline flagged, and tells Railway the truth about its own health.

**Architecture:** Extract the in-process cache into a pure, testable stale-while-revalidate module with a hard size cap and a cheap "new day" probe that invalidates it minutes after the nightly run. Make the two prerendered routes dynamic. Gate the trades proxy on known pools with a token bucket. Add `/api/ready`. Add the three missing indexes through the same additive mechanism the pipeline already uses. Everything else is a one-file change.

**Tech Stack:** Next.js 16 App Router, Drizzle, `node:test` via `tsx` (`npm test`), `tsc --noEmit`, `next build`. Pipeline side: SQLAlchemy Core for the index helper.

**Spec:** Audit findings Sov Medium: "Hourly cache stall" (High), "Feed and digest after deploy", "Hidden projects reachable", "GeckoTerminal quota burn", "Cache growth from random URLs", "Degraded data cached for 1 h", "Symbol lookup blocks render", "Missing index", Low: headers, OG, week validation, receipts-only-when-full. Astra M7, M8.

## Global Constraints

- Web checks: `cd web && npm test && npx tsc --noEmit && npm run build`. Pipeline: `cd pipeline && .venv/bin/python -m pytest -q`.
- Never call a paid API. No secrets, hostnames or absolute paths in files or commits. Do not push.
- Commit after every task; append these trailer lines verbatim to every commit message:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01A2FHLwk9NeThmyZ1JbHe3o`
- Do not touch `derive`, `toHook`, `projectRoutable`, `TradePanel.tsx`, `Detail.tsx` badges (package 2 owns them), `pipeline/bait/indexer/`, or any CSS.
- Add `npm test` entries for every new `*.test.ts` file (the `test` script lists files explicitly in `web/package.json`).

## Review Focus

1. Cache expiry under load: the request that finds an expired entry must get the stale value immediately while exactly one refresh runs. (Task 1)
2. The first request to `/feed.xml` after a fresh Docker build with a database must contain items and the production origin. (Task 2)
3. A well-formed but unknown pool id on `/api/trades` must not reach GeckoTerminal. (Task 4)
4. A spam-flagged project must return 404 on `/p/<slug>` and never appear in `/api/launches`. (Task 3)
5. A database outage must make `/api/ready` return 503 while `/` still renders. (Task 3)

---

### Task 1: Stale-while-revalidate cache with a hard cap and a new-day probe

**Files:**
- Create: `web/src/lib/swr-cache.ts`, `web/src/lib/swr-cache.test.ts`
- Modify: `web/src/db/queries.ts` (replace `TTL_MS`/`MEMO_MAX`/`memo`/`cached` with the new module; add `refreshIfNewDay`)
- Modify: `web/package.json` (add the test file)

**Interfaces:**
- Produces: `createSwrCache<T>({ ttlMs, max, now? })` returning `{ get(key, fn): Promise<T>; invalidate(prefix?: string): void; size(): number }`. Within `ttlMs`: cached. Past `ttlMs`: return the stale value at once and start one background refresh; a refresh failure keeps the stale value. A never-cached key awaits `fn()`; a rejected first load is not stored. When `size() >= max`, the oldest entry is evicted (insertion order).
- Produces: `refreshIfNewDay(db)` in `queries.ts`: at most once per 60 s, `select max(day) from project_daily`; when the value differs from the last one seen, `cache.invalidate()`.

- [ ] **Step 1: Failing test**

`web/src/lib/swr-cache.test.ts`:

```ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { createSwrCache } from "./swr-cache";

const tick = () => new Promise((r) => setImmediate(r));

test("fresh hits are shared, stale hits are served while one refresh runs", async () => {
  let t = 1000;
  let calls = 0;
  const c = createSwrCache<number>({ ttlMs: 100, max: 10, now: () => t });
  const load = async () => ++calls;
  assert.equal(await c.get("k", load), 1);
  assert.equal(await c.get("k", load), 1);
  assert.equal(calls, 1);
  t += 150;                                          // expired
  const a = c.get("k", load);
  const b = c.get("k", load);
  assert.equal(await a, 1);                          // stale value, immediately
  assert.equal(await b, 1);
  await tick();
  assert.equal(calls, 2);                            // exactly one refresh
  assert.equal(await c.get("k", load), 2);           // refreshed value now current
});

test("a failed refresh keeps the stale value; a failed first load is not cached", async () => {
  let t = 0;
  const c = createSwrCache<string>({ ttlMs: 10, max: 10, now: () => t });
  assert.equal(await c.get("a", async () => "v1"), "v1");
  t = 50;
  assert.equal(await c.get("a", async () => { throw new Error("db down"); }), "v1");
  await tick();
  assert.equal(await c.get("a", async () => "v2"), "v1");   // still stale-served, refresh in flight or done
  await assert.rejects(c.get("b", async () => { throw new Error("nope"); }));
  assert.equal(c.size(), 1);
});

test("hard cap evicts the oldest entry; invalidate clears by prefix", async () => {
  const c = createSwrCache<number>({ ttlMs: 1000, max: 2 });
  await c.get("x1", async () => 1);
  await c.get("x2", async () => 2);
  await c.get("y1", async () => 3);
  assert.equal(c.size(), 2);
  c.invalidate("x");
  assert.equal(c.size(), 1);
  c.invalidate();
  assert.equal(c.size(), 0);
});
```

Add `src/lib/swr-cache.test.ts` to the `test` script in `package.json`. Run `npm test` → FAIL.

- [ ] **Step 2: Implement**

`web/src/lib/swr-cache.ts`:

```ts
// Stale-while-revalidate memo for a single replica. Pure: no server imports, unit tested.
type Entry<T> = { at: number; value: Promise<T>; refreshing: boolean };

export function createSwrCache<T>(opts: { ttlMs: number; max: number; now?: () => number }) {
  const now = opts.now ?? Date.now;
  const map = new Map<string, Entry<T>>();
  return {
    get(key: string, fn: () => Promise<T>): Promise<T> {
      const t = now();
      const hit = map.get(key);
      if (hit) {
        if (t - hit.at < opts.ttlMs) return hit.value;
        if (!hit.refreshing) {
          hit.refreshing = true;
          fn().then(
            (v) => map.set(key, { at: now(), value: Promise.resolve(v), refreshing: false }),
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
      const value = fn().catch((e) => { map.delete(key); throw e; });
      map.set(key, { at: t, value, refreshing: false });
      return value;
    },
    invalidate(prefix?: string) {
      if (!prefix) return map.clear();
      for (const k of [...map.keys()]) if (k.startsWith(prefix)) map.delete(k);
    },
    size: () => map.size,
  };
}
```

In `queries.ts`: delete `TTL_MS`, `MEMO_MAX`, `memo`, `cached`; add

```ts
import { createSwrCache } from "@/lib/swr-cache";
const cache = createSwrCache<unknown>({ ttlMs: 3_600_000, max: 400 });
function cached<T>(key: string, fn: () => Promise<T>): Promise<T> { return cache.get(key, fn) as Promise<T>; }

let lastDaySeen: string | null = null;
let lastProbe = 0;
/** Cheap probe: when the pipeline has scored a new day, drop every cached read so the site refreshes within a minute. */
async function refreshIfNewDay(db: NonNullable<ReturnType<typeof getDb>>): Promise<void> {
  const t = Date.now();
  if (t - lastProbe < 60_000) return;
  lastProbe = t;
  const r = await safe("probe:maxday", async () => (await db.execute<{ d: string | null }>(sql`select max(day)::text as d from project_daily`)) as unknown, null);
  const d = ((Array.isArray(r) ? r[0] : (r as { rows?: unknown[] } | null)?.rows?.[0]) as { d?: string | null } | undefined)?.d ?? null;
  if (d && d !== lastDaySeen) { if (lastDaySeen !== null) cache.invalidate(); lastDaySeen = d; }
}
```

Call `await refreshIfNewDay(db)` at the top of `loadAll()` and `getProject()` (after the `db` null check). Keep `safe(...)` wrapping as today. In `getProject`, do not store a memo entry for an unknown slug: load through `cached` only after `getAllSlugs()` (already exists, cached) confirms the slug; otherwise return `null` directly.

Run `npm test && npx tsc --noEmit` → PASS.

- [ ] **Step 3: Commit**

```bash
git add web/src/lib/swr-cache.ts web/src/lib/swr-cache.test.ts web/src/db/queries.ts web/package.json
git commit -m "web: stale-while-revalidate cache with a hard cap; invalidate within a minute of a new scored day"
```

---

### Task 2: Feed and week render at request time; feed capped and XML-safe

**Files:**
- Modify: `web/src/app/feed.xml/route.ts`, `web/src/app/week/page.tsx`
- Create: `web/src/lib/xml.ts`, `web/src/lib/xml.test.ts`

- [ ] **Step 1: Failing test**

`web/src/lib/xml.test.ts`:

```ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { escXml } from "./xml";

test("escXml escapes markup and strips XML-illegal control characters", () => {
  assert.equal(escXml(`a<b>&"c"'`), "a&lt;b&gt;&amp;&quot;c&quot;&apos;");
  assert.equal(escXml("x\u0000y\u0008z\u000Bq\u001F"), "xyzq");
  assert.equal(escXml("tab\tnl\nok"), "tab\tnl\nok");
});
```

Add it to the `test` script. Run → FAIL.

- [ ] **Step 2: Implement**

`web/src/lib/xml.ts`:

```ts
/** RSS text: escape markup and drop characters XML 1.0 forbids (one bad name must not break the whole feed). */
export function escXml(s: string): string {
  return s
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, "")
    .replace(/[<>&'"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", "'": "&apos;", '"': "&quot;" })[c] as string);
}
```

`feed.xml/route.ts`: replace `export const revalidate = 3600;` with `export const dynamic = "force-dynamic";`, use `escXml` instead of the local `esc`, cap `fresh` to the first 50 items, and return with `headers: { "Content-Type": "application/rss+xml; charset=utf-8", "Cache-Control": "public, max-age=3600" }`. Keep `SITE` as is (the env is set on Railway at runtime; dynamic rendering reads it).

`week/page.tsx`: replace `export const revalidate = 3600;` with `export const dynamic = "force-dynamic";`.

Run `npm test && npx tsc --noEmit && npm run build` → PASS; the route table must now show `ƒ /feed.xml` and `ƒ /week`.

- [ ] **Step 3: Commit**

```bash
git add web/src/lib/xml.ts web/src/lib/xml.test.ts web/src/app/feed.xml/route.ts web/src/app/week/page.tsx web/package.json
git commit -m "web: feed and week render at request time (no more empty localhost feed after a deploy); feed capped at 50 and XML-safe"
```

---

### Task 3: `/api/ready`, hidden projects stay hidden, degraded reads are not cached

**Files:**
- Create: `web/src/app/api/ready/route.ts`
- Modify: `web/src/db/queries.ts` (`getProject`, the usage queries' `safe` fallbacks), `web/src/db/radar.ts` (`getLaunches` spam filter)
- Modify: `web/src/app/p/[slug]/page.tsx` (throw on DB error instead of `notFound()`)

- [ ] **Step 1: `/api/ready`**

```ts
// Liveness for Railway's healthcheck: can we reach Postgres? Nothing about data freshness (that is /api/health).
import { sql } from "drizzle-orm";
import { getDb } from "@/db/client";

export const dynamic = "force-dynamic";

export async function GET() {
  const db = getDb();
  if (!db) return Response.json({ ok: false, reason: "no DATABASE_URL" }, { status: 503, headers: { "Cache-Control": "no-store" } });
  try {
    await db.execute(sql`select 1`);
    return Response.json({ ok: true }, { headers: { "Cache-Control": "no-store" } });
  } catch (e) {
    return Response.json({ ok: false, reason: (e as Error).message.split("\n")[0] }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}
```

- [ ] **Step 2: Spam stays hidden**

In `queries.ts` `getProject`: after loading `detail`, find how `isSpam` is exposed on the loaded row (`loadFromDb` reads `project_daily.is_spam`; if `ProjectDetail` lacks the field, add `isSpam: boolean` to the type and set it in the builder). Return `null` when `detail.isSpam` is true. In `radar.ts` `getLaunches`: join `project_daily` for the latest day and set `projectSlug`/`projectName` to `null` when `is_spam` is true (keep the launch row itself).

- [ ] **Step 3: Degraded reads are not cached for an hour**

In `queries.ts`, the usage sub-queries wrapped in `safe(..., null)` inside `loadFromDb`: when any of them falls back, mark the result `degraded = true` and, in `loadAll`/`getProject`, store it with a short TTL. Implement by giving the cache module an optional per-call `ttlMs` override: `get(key, fn, ttlMs?)` (add the parameter to `createSwrCache.get` and a test line: `await c.get("short", async () => 1, 5)` expires at `t + 5`). Use 60 s for degraded results.

In `p/[slug]/page.tsx`: distinguish "not found" from "database error": `getProject` returns `null` for unknown and throws for a DB failure (remove the `safe` wrapper around the `cached(...)` call in `getProject` only; keep `safe` on `loadAll`). A thrown error renders Next's error boundary and ISR keeps the previous good page instead of caching a 404.

Run `npm test && npx tsc --noEmit && npm run build` → PASS.

- [ ] **Step 4: Commit**

```bash
git add web
git commit -m "web: /api/ready for the healthcheck; spam projects 404 and never link from launches; degraded reads cached 60 s, DB errors do not become cached 404s"
```

---

### Task 4: Trades proxy accepts only known pools, with a hard LRU and a token bucket

**Files:**
- Modify: `web/src/app/api/trades/[chain]/[pool]/route.ts`
- Create: `web/src/lib/token-bucket.ts`, `web/src/lib/token-bucket.test.ts`
- Modify: `web/src/db/queries.ts` (export `knownPoolKeys(): Promise<Set<string>>`, cached 10 min)

- [ ] **Step 1: Failing test**

```ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { createTokenBucket } from "./token-bucket";

test("token bucket allows capacity then refills over time", () => {
  let t = 0;
  const b = createTokenBucket({ capacity: 3, refillPerSec: 1, now: () => t });
  assert.deepEqual([b.take(), b.take(), b.take(), b.take()], [true, true, true, false]);
  t = 2000;
  assert.deepEqual([b.take(), b.take(), b.take()], [true, true, false]);
});
```

- [ ] **Step 2: Implement**

```ts
export function createTokenBucket(opts: { capacity: number; refillPerSec: number; now?: () => number }) {
  const now = opts.now ?? Date.now;
  let tokens = opts.capacity;
  let last = now();
  return {
    take(): boolean {
      const t = now();
      tokens = Math.min(opts.capacity, tokens + ((t - last) / 1000) * opts.refillPerSec);
      last = t;
      if (tokens < 1) return false;
      tokens -= 1;
      return true;
    },
  };
}
```

`queries.ts`: `export function knownPoolKeys(): Promise<Set<string>>` → `cached("poolkeys", async () => new Set(rows.map(r => `${r.chainId}:${r.poolId.toLowerCase()}`)), 600_000)` reading `select pool_id, chain_id from pools` (and `v4_pools` if the indexer's table exists; `safe` to an empty set).

Trades route: after the regex check, `if (!(await knownPoolKeys()).has(key)) return json({ trades: [], status: "unknown_pool" }, 404, "no-store")`. Replace the local `memo` with `createSwrCache<Payload>({ ttlMs: 120_000, max: 500 })` (import from `@/lib/swr-cache`), keep the 30 s error TTL via the per-call `ttlMs` override from Task 3. Add `const gtBucket = createTokenBucket({ capacity: 20, refillPerSec: 20 / 60 })`; when `take()` is false, return the stale cached value if any, else `json({ trades: [], status: "rate_limited" }, 503, "no-store")`. Add `status: "ok" | "unknown_pool" | "rate_limited" | "upstream_error" | "empty"` to `Payload` and set it in `load()`. Change `json()` to take `(p, status = 200, cacheControl = "public, max-age=60, s-maxage=120")`. `LiveTrades.tsx` reads `trades` only; leave it.

Run `npm test && npx tsc --noEmit && npm run build` → PASS.

- [ ] **Step 3: Commit**

```bash
git add web
git commit -m "trades proxy: only pools we know, 20/min GeckoTerminal budget, hard LRU, explicit status instead of a silent empty list"
```

---

### Task 5: Indexes the queries need

**Files:**
- Create: `db/migrations/0008_indexes.sql`
- Modify: `pipeline/bait/db.py` (`ADDITIVE_INDEXES` + `add_missing_indexes`), `pipeline/bait/__main__.py` and `pipeline/bait/indexer/__main__.py` (call it after `add_missing_columns`; note the indexer file is otherwise off limits, this is one call)
- Test: `pipeline/tests/test_cli.py`

- [ ] **Step 1: Failing test**

```python
def test_additive_indexes_are_created_idempotently(tmp_path):
    from bait import db
    e = db.make_engine(f"sqlite:///{tmp_path}/i.sqlite")
    db.create_all(e)
    assert db.add_missing_indexes(e) == [n for n, *_ in db.ADDITIVE_INDEXES]
    assert db.add_missing_indexes(e) == [n for n, *_ in db.ADDITIVE_INDEXES]   # IF NOT EXISTS: same list, no error
    with e.connect() as c:
        names = {r[1] for r in c.exec_driver_sql("PRAGMA index_list('v4_pools')")}
    assert "v4_pools_block_time_idx" in names
```

- [ ] **Step 2: Implement**

`db.py`:

```python
# Indexes the web's hot queries need on tables that already exist in production. (name, table, column SQL).
ADDITIVE_INDEXES: list[tuple[str, str, str]] = [
    ("v4_pools_block_time_idx", "v4_pools", "block_time DESC NULLS LAST, block_number DESC"),
    ("pool_stats_day_idx", "pool_stats", "day"),
    ("pool_top_wallets_day_idx", "pool_top_wallets", "day"),
]


def add_missing_indexes(engine: sa.Engine, indexes=ADDITIVE_INDEXES) -> list[str]:
    """CREATE INDEX IF NOT EXISTS for each (both dialects support it). Tables that do not exist are skipped."""
    done = []
    with engine.begin() as conn:
        insp = sa.inspect(conn)
        for name, table, cols in indexes:
            if not insp.has_table(table):
                continue
            if conn.dialect.name == "sqlite":
                cols = cols.replace(" NULLS LAST", "")          # SQLite has no NULLS LAST in index definitions
            conn.execute(sa.text(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({cols})'))
            done.append(name)
    return done
```

`0008_indexes.sql`:

```sql
CREATE INDEX IF NOT EXISTS v4_pools_block_time_idx ON v4_pools (block_time DESC NULLS LAST, block_number DESC);
CREATE INDEX IF NOT EXISTS pool_stats_day_idx ON pool_stats (day);
CREATE INDEX IF NOT EXISTS pool_top_wallets_day_idx ON pool_top_wallets (day);
```

Call `db.add_missing_indexes(engine)` right after `db.add_missing_columns(engine)` in both entrypoints. Run the pipeline suite → PASS.

- [ ] **Step 3: Commit**

```bash
git add db/migrations/0008_indexes.sql pipeline
git commit -m "db: additive indexes for the launches sort and the max(day) subqueries"
```

---

### Task 6: Symbols never hold a render for long; headers; OG and week hygiene

**Files:**
- Modify: `web/src/lib/symbols.ts` (`resolveSymbols(refs, { limit = 60, waitMs = 1500 })`), `web/src/lib/symbols.test.ts`
- Modify: `web/next.config.ts`
- Modify: `web/src/app/p/[slug]/opengraph-image.tsx`, `web/src/app/week/[date]/page.tsx`

- [ ] **Step 1: Symbols**

Change `resolveSymbols` to race the resolution against `waitMs`: start `fromLlama` then `fromChain` as one promise, `await Promise.race([work, sleep(waitMs)])`, and return the lookup from whatever is in the cache; the work keeps running and fills the cache for the next request. Dedupe in-flight work per key with a module-level `Map<string, Promise<void>>`. Add a test to `symbols.test.ts` with a fake `fetchJson` (check how the existing tests inject it; if they do not, add an injectable `deps` parameter) asserting that a resolver that never settles returns within ~1.6 s with `null` symbols and no throw.

- [ ] **Step 2: Headers**

`next.config.ts`:

```ts
const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  async headers() {
    return [{
      source: "/:path*",
      headers: [
        { key: "X-Content-Type-Options", value: "nosniff" },
        { key: "X-Frame-Options", value: "DENY" },
        { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
      ],
    }];
  },
};
```

(No CSP in this package: it needs an inline-script nonce strategy; leave a one-line comment saying so.)

- [ ] **Step 3: OG and week**

`opengraph-image.tsx`: when `getProject(slug)` is null, `return new Response("Not found", { status: 404 })`; on success add `headers: { "Cache-Control": "public, max-age=3600" }` to the `ImageResponse` options.

`week/[date]/page.tsx`: validate before any DB call: `/^\d{4}-\d{2}-\d{2}$/` AND `new Date(date + "T00:00:00Z").toISOString().startsWith(date)` AND `getUTCDay() === 1` (Monday); otherwise `notFound()` in both `generateMetadata` and the page.

Run `npm test && npx tsc --noEmit && npm run build` → PASS.

- [ ] **Step 4: Commit**

```bash
git add web
git commit -m "web: symbol lookups bounded to 1.5 s per render; security headers; OG 404s for unknown slugs; week dates validated before the DB"
```

---

### Task 7: Receipts only on full loads; README truth

**Files:**
- Modify: `web/src/db/queries.ts:322-326` (load receipts only when `full`)
- Modify: `README.md`, `web/README.md` (replace "ISR 1h" with what is true: `/p/[slug]` and `/week/[date]` are ISR 1 h; `/`, `/leaders`, `/building`, `/feed.xml`, `/week` render per request behind a 1 h stale-while-revalidate in-process cache that refreshes within a minute of a new scored day; `/api/ready` is the Railway healthcheck)

- [ ] **Step 1: Edit, run all checks, commit**

```bash
git add web README.md
git commit -m "web: receipts only on full loads; READMEs describe the real caching"
```

---

## Final report (the implementer returns this)

Per task: SHA, test counts before and after (web, pipeline), deviations. Then `git log --oneline fix/p2-trust-signals..HEAD`, `git diff --stat fix/p2-trust-signals..HEAD`, and the `npm run build` route table (must show `ƒ /feed.xml`, `ƒ /week`, `ƒ /api/ready`). Do not push.
