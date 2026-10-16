// One query surface for the pages. With DATABASE_URL set, reads Postgres through
// Drizzle; without it, serves src/fixtures/sample.ts when BAIT_FIXTURES=1 and
// renders empty otherwise. Both paths produce the same view types and run through
// the same derivation (routable, flag labels) and the same tab rules (lib/feed.ts).
//
// Every read goes through safe(): a missing table or a bad query renders empty, never 500.
// The activity tables (pool_stats, pool_top_wallets, wallet_labels) are newer than the rest
// and may be missing or empty in production, so each has its own safe() and degrades to null.
import { researchProject } from "@/lib/research";
import { receiptRelevant } from "@/lib/relevance";
import { resolveSymbols } from "@/lib/symbols";
import { createSwrCache, type Ttl } from "@/lib/swr-cache";
import { isSlugShaped, isUnlisted, unlistedLabel } from "@/lib/unlisted";
import { and, desc, eq, inArray, sql } from "drizzle-orm";
import { getDb, type Db } from "./client";
import * as t from "./schema";
import { SAMPLE_ALIASES, SAMPLE_DIGESTS, SAMPLE_PROJECTS, SAMPLE_WALLET_LABELS } from "@/fixtures/sample";
import { eligibility, flagLabels, projectRoutable } from "@/lib/routable";
import { CHAINS, knownChain, nansenCovers } from "@/lib/chains";
import { applyTab } from "@/lib/feed";
import { longDate } from "@/lib/format";
import { auditLinks, digestIntro, digestItem, hookKeys, toRow, withCurrentNames } from "@/lib/present";
import { packRows, rowsVersion, type PackedRows } from "@/lib/rows";
import type {
  Category,
  ChainId,
  DigestItem,
  DigestView,
  FeedQuery,
  HookIndex,
  HookView,
  LeaderEntry,
  LeadersView,
  PoolView,
  ProjectDetail,
  ProjectSummary,
  ReceiptView,
  RowView,
  Usage,
  WalletLabel,
  WalletView,
} from "@/lib/types";

// Same markers as pipeline/bait/clients/nansen.py is_smart_money.
const SMART_MONEY_MARKERS = ["smart money", "fund", "smart trader", "smart lp", "smart dex"];

const KNOWN_CHAINS = sql.raw(`array[${Object.keys(CHAINS).join(",")}]::int[]`);

const EMPTY_USAGE: Usage = { vol24h: null, vol24hRolling: null, tx24h: null, wallets24h: null, walletsExact: null, wallets7d: null, topWalletShare7d: null, deployerSwapShare7d: null, buys24h: null, sells24h: null, buyers24h: null, sellers24h: null, smartWallets: null };

// ---------- derivation shared by both sources ----------

function derive(p: ProjectDetail): ProjectDetail {
  const primary = p.hooks[0];
  const labels = primary ? flagLabels(primary.address, primary.flags, primary.properties) : [];
  const flagLine = p.isFactory && p.metrics.hooksN > 1 ? [`${p.metrics.hooksN.toLocaleString("en-US")} hooks`, "one per pool"] : labels;
  return researchProject({
    ...p,
    flagLabels: flagLine,
    hooklisted: p.hooks.some((h) => h.hooklisted === true),
    pools: p.pools.map((pool) => ({
      ...pool,
      eligibility: eligibility(pool.hookAddress, null, p.hooks.find((h) => h.address.toLowerCase() === pool.hookAddress.toLowerCase() && h.chainId === pool.chainId)?.properties ?? {}, pool.fee),
    })),
    signals: {
      ...p.signals,
      routable: projectRoutable(p.hooks, p.pools),
      smartMoneyLps: nansenCovers(p.chains) ? (p.signals.smartMoneyLps ?? 0) : null,
    },
  });
}

function rowOf(p: ProjectDetail): RowView {
  return toRow(p);
}

// ---------- public API ----------

/** Every listed (non-spam) project with its hooks, for the read API (/api/v1). The same cached load the pages use. */
export async function getAllProjects(): Promise<ProjectDetail[]> {
  return (await loadAll()).filter((p) => !p.isSpam);
}

/** Every non-spam project as a light row (the terminal list, movers, search). */
export async function getRows(): Promise<RowView[]> {
  return (await getRowSet()).rows;
}

type RowSet = { rows: RowView[]; packed: PackedRows; version: string };
// Keyed by the cached loadAll array: every page render and /api/rows call between two loads shares one set and hash.
const rowSets = new WeakMap<ProjectDetail[], RowSet>();

/** The rows, packed for GET /api/rows, and the version a page hands the client to fetch them by. */
export async function getRowSet(): Promise<RowSet> {
  const all = await loadAll();
  let set = rowSets.get(all);
  if (!set) {
    const rows = all.filter((p) => !p.isSpam).map(rowOf);
    const packed = packRows(rows);
    set = { rows, packed, version: rowsVersion(packed) };
    rowSets.set(all, set);
  }
  return set;
}

/** slug -> hook addresses for 0x search (GET /api/hooks): the same projects as getRows. */
export async function getHookIndex(): Promise<HookIndex> {
  const all = await loadAll();
  return Object.fromEntries(all.filter((p) => !p.isSpam).map((p) => [p.slug, hookKeys(p.hooks.map((h) => h.address))]));
}

/** Tab-filtered projects (RSS uses the New tab). */
export async function getFeed(q: FeedQuery): Promise<ProjectSummary[]> {
  const all = (await loadAll()).filter((p) => !p.isSpam);
  const bySlug = new Map(all.map((p) => [p.slug, p]));
  return applyTab(all.map(rowOf), q).flatMap((r) => {
    const p = bySlug.get(r.slug);
    return p ? [{ ...p, rank: r.rank }] : [];
  });
}

export async function getProject(slug: string): Promise<ProjectDetail | null> {
  return (await getProjectPage(slug)).project;
}

/**
 * A project page: the detail and the sidebar rows from one list load. The detail is cached per load generation and pinned
 * to that load's day, and takes its display name from the list row, so the header, the sidebar, its rank and every other
 * page agree even when the snapshot is up to an hour old.
 */
export async function getProjectPage(slug: string): Promise<{ project: ProjectDetail | null; rows: RowView[]; version: string }> {
  const db = getDb();
  const all = await loadAll();
  const live = all.filter((p) => !p.isSpam);
  const { rows, version } = await getRowSet();   // the same set and hash "/" hands the client, from this load
  let detail: ProjectDetail | null;
  if (!db) {
    const p = usingFixtures() ? SAMPLE_PROJECTS.find((x) => x.slug === slug) : undefined;
    detail = p ? derive(p) : null;
  } else {
    // Unknown slugs never reach the cache: random URLs cannot fill it.
    // (An empty list means an empty or failing database: fall through so a failure throws rather than 404s.)
    const base = live.find((p) => p.slug === slug);
    if (live.length && !base) return { project: null, rows, version };
    const gen = (all as Loaded).gen;
    const pin = gen != null ? { latest: (all as Loaded).day ?? null } : {};
    // No safe() here: a database error throws (Next keeps the last good page) instead of becoming a cached 404.
    const loaded = await cached(`project:${gen ?? "-"}:${slug}`, () => loadFromDb(db, { slug, full: true, ...pin }), degradedTtl);
    detail = loaded[0] ?? null;
    // the list row's name and top pool: symbol lookups can differ between two loads, the label must not
    if (detail && base) detail = { ...detail, name: base.name, topPool: base.topPool };
  }
  if (!detail || detail.isSpam) return { project: null, rows, version }; // spam 404s; a wash-like project keeps its page (Detail shows why)
  const rank = applyTab(rows, { tab: "trending" }).find((x) => x.slug === slug)?.rank;
  return { project: { ...detail, rank }, rows, version };
}

/**
 * Whether Bait knows this pool: a scored pool or one of the indexer's v4_pools. The trades proxy only serves these.
 * One primary-key probe per table, cached per pool for ten minutes. Never the whole table: v4_pools runs to millions of
 * rows and loading it into a Set took the web process past the Node heap limit.
 */
export function isKnownPool(chainId: number, poolId: string): Promise<boolean> {
  const id = poolId.toLowerCase();
  const db = getDb();
  if (!db) {
    const known = usingFixtures() && SAMPLE_PROJECTS.some((p) => p.pools.some((x) => x.chainId === chainId && x.poolId.toLowerCase() === id));
    return Promise.resolve(known);
  }
  return poolKeyCache.get(
    `${chainId}:${id}`,
    async () => {
      const [scored, indexed] = await Promise.all([
        safe("poolkey:pools", () => db.select({ one: sql`1` }).from(t.pools).where(and(eq(t.pools.chainId, chainId), eq(t.pools.poolId, id))).limit(1), []),
        safe("poolkey:v4_pools", () => db.select({ one: sql`1` }).from(t.v4Pools).where(and(eq(t.v4Pools.chainId, chainId), eq(t.v4Pools.poolId, id))).limit(1), []),
      ]);
      return scored.length > 0 || indexed.length > 0;
    },
  );
}

/**
 * Where a moved slug lives now: renamed, merged, regrouped, or named since the link was shared. Only aliases the
 * pipeline recorded (project_aliases), never an address-prefix guess.
 */
export async function slugForAlias(slug: string): Promise<string | null> {
  const db = getDb();
  if (!isSlugShaped(slug)) return null;
  if (!db) {
    const to = usingFixtures() ? SAMPLE_ALIASES[slug] : null;
    return to && to !== slug && isSlugShaped(to) ? to : null;
  }
  const rows = await safe(
    "slugForAlias",
    async () =>
      rowsOf<{ slug: string }>(
        await db.execute(sql`select p.slug from project_aliases a join ${t.projects} p on p.id = a.project_id
                             where a.old_slug = ${slug} limit 1`),
      ),
    [],
  );
  const to = rows[0]?.slug;
  return to && to !== slug ? to : null;
}

export async function getAllSlugs(): Promise<string[]> {
  return (await getRows()).map((p) => p.slug);
}

/** Usage leaderboards across hook projects. */
export async function getLeaders(n = 10): Promise<LeadersView> {
  const all = (await loadAll()).filter((p) => !p.isSpam && !p.isWash && !p.isDormant); // wash-like volume never ranks
  const active = all.filter((p) => (p.metrics.vol7d ?? 0) > 0);
  const board = (value: (p: ProjectDetail) => number | null): LeaderEntry[] =>
    all
      .map((p) => ({ p, v: value(p) }))
      .filter((x): x is { p: ProjectDetail; v: number } => x.v != null && x.v > 0)
      .sort((a, b) => b.v - a.v)
      .slice(0, n)
      .map((x) => ({ row: rowOf(x.p), value: x.v }));
  const walletKnown = all.filter((p) => p.usage.wallets24h != null);
  return {
    totals: {
      vol7d: active.reduce((s, p) => s + (p.metrics.vol7d ?? 0), 0),
      wallets24h: walletKnown.length ? walletKnown.reduce((s, p) => s + (p.usage.wallets24h ?? 0), 0) : null,
      projects: active.length,
      chains: new Set(active.flatMap((p) => p.chains)).size,
    },
    wallets: board((p) => p.usage.wallets24h),
    volume: board((p) => p.metrics.vol7d),
    tvl: board((p) => p.metrics.tvlUsd),
    smartMoney: board((p) => p.usage.smartWallets),
  };
}

/** wallet_labels for a set of wallets on one chain. Empty map when the table is missing or empty. */
export async function getWalletLabels(chainId: ChainId, addresses: string[]): Promise<Map<string, WalletLabel>> {
  const addrs = [...new Set(addresses.map((a) => a.toLowerCase()))];
  const out = new Map<string, WalletLabel>();
  if (!addrs.length) return out;
  const db = getDb();
  if (!db) {
    if (!usingFixtures()) return out;
    for (const a of addrs) {
      const l = SAMPLE_WALLET_LABELS.get(`${chainId}:${a}`);
      if (l) out.set(a, l);
    }
    return out;
  }
  const rows = await safe(
    "getWalletLabels",
    () =>
      db
        .select({ address: t.walletLabels.address, labels: t.walletLabels.labels, smartMoney: t.walletLabels.smartMoney })
        .from(t.walletLabels)
        .where(and(eq(t.walletLabels.chainId, chainId), inArray(t.walletLabels.address, addrs))),
    [],
  );
  for (const r of rows) out.set(r.address, toLabel(r.labels, r.smartMoney));
  return out;
}

export async function getDigest(week: string): Promise<DigestView | null> {
  const db = getDb();
  if (!db) return usingFixtures() ? (SAMPLE_DIGESTS.find((d) => d.week === week) ?? null) : null;
  const [[row], names] = await Promise.all([
    safe("getDigest", () => db.select().from(t.digests).where(eq(t.digests.weekOf, week)).limit(1), []),
    currentNames(),
  ]);
  return row ? toDigest(row, names) : null;
}

export async function listDigests(limit = 12): Promise<DigestView[]> {
  const db = getDb();
  if (!db) return usingFixtures() ? SAMPLE_DIGESTS.slice(0, limit) : [];
  const [rows, names] = await Promise.all([
    safe("listDigests", () => db.select().from(t.digests).orderBy(desc(t.digests.weekOf)).limit(limit), []),
    currentNames(),
  ]);
  return rows.map((r) => toDigest(r, names));
}

/** slug -> the display name the list, the project page, the OG image and RSS show (one load, one top-pool rule). */
async function currentNames(): Promise<Map<string, string>> {
  return new Map((await loadAll()).map((p) => [p.slug, p.name]));
}

/** Fixtures are opt-in (BAIT_FIXTURES=1) so a build with no DATABASE_URL prerenders empty pages, never sample figures. */
export function usingFixtures(): boolean {
  return !process.env.DATABASE_URL && process.env.BAIT_FIXTURES === "1";
}

/** Before the pipeline's first run the tables may not exist yet; render empty rather than 500 (and fail the healthcheck). */
export async function safe<T>(label: string, fn: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    // Drizzle wraps the driver error; the cause carries the reason (missing table, connection reset).
    const err = e as Error & { cause?: { message?: string; code?: string } };
    const why = err.cause?.message ? ` (${err.cause.code ?? "cause"}: ${err.cause.message})` : "";
    console.error(`[db] ${label} failed, serving empty: ${err.message.split("\n")[0]}${why}`);
    return fallback;
  }
}

// ---------- one-hour stale-while-revalidate cache for the heavy reads (single replica on Railway) ----------
const cache = createSwrCache<unknown>({ ttlMs: 3_600_000, max: 400 });
// Pool probes get their own cache: a burst of unknown pool ids from /api/trades must never evict loadAll or the leaders.
const poolKeyCache = createSwrCache<boolean>({ ttlMs: 600_000, max: 5_000 });
export function cached<T>(key: string, fn: () => Promise<T>, ttl?: Ttl<T>): Promise<T> {
  return cache.get(key, fn as () => Promise<unknown>, ttl as Ttl<unknown> | undefined) as Promise<T>;
}

/** A load where a usage sub-query fell back to null (outage, or a table that is not there yet) is retried after 60 s, not 1 h. */
type Loaded = ProjectDetail[] & { degraded?: boolean; day?: string | null; gen?: number };
let loads = 0;   // generation stamp: a detail is cached per list load, never mixed with another one
const DEGRADED_TTL_MS = 60_000;
const degradedTtl = (r: Loaded): number => (r.degraded ? DEGRADED_TTL_MS : 3_600_000);

let lastDaySeen: string | null = null;
let lastProbe = 0;
/** Cheap probe: when the pipeline has scored a new day, drop every cached read so the site refreshes within a minute. */
async function refreshIfNewDay(db: NonNullable<ReturnType<typeof getDb>>): Promise<void> {
  const now = Date.now();
  if (now - lastProbe < 60_000) return;
  lastProbe = now;
  const r = await safe("probe:maxday", async () => (await db.execute<{ d: string | null }>(sql`select max(day)::text as d from project_daily`)) as unknown, null);
  const d = ((Array.isArray(r) ? r[0] : (r as { rows?: unknown[] } | null)?.rows?.[0]) as { d?: string | null } | undefined)?.d ?? null;
  if (d && d !== lastDaySeen) {
    if (lastDaySeen !== null) cache.invalidate();
    lastDaySeen = d;
  }
}

// ---------- loading ----------

let lastGen: number | undefined;
async function loadAll(): Promise<ProjectDetail[]> {
  const db = getDb();
  if (!db) return usingFixtures() ? SAMPLE_PROJECTS.map(derive) : [];
  await refreshIfNewDay(db);
  const all: Loaded = await safe("loadAll", () => cached<Loaded>("loadAll", () => loadFromDb(db, { full: false }), degradedTtl), []);
  // a new list load retires the details cached against the old one (they would never be read again)
  if (all.gen != null && all.gen !== lastGen) {
    if (lastGen !== undefined) cache.invalidate(`project:${lastGen}:`);
    lastGen = all.gen;
  }
  return all;
}

/** digests.body (pipeline shape) -> DigestView. Title and intro are rendered here, not stored. */
function toDigest(row: typeof t.digests.$inferSelect, names: ReadonlyMap<string, string>): DigestView {
  const week = String(row.weekOf);
  const b = row.body;
  const card = (kind: DigestItem["kind"]) => (c: t.DigestCard): DigestItem => digestItem(c, kind);
  const fresh = withCurrentNames((b.new ?? []).map(card("new")), names);
  const hot = withCurrentNames((b.trending ?? []).map(card("trending")), names);
  const intro = digestIntro(fresh, hot, b.totals ?? { projects: 0, tvl_usd: 0, vol_7d: 0 });
  return { week, title: `Week of ${longDate(week)}`, intro, items: [...fresh, ...hot], createdAt: b.generated_at ?? `${week}T06:00:00Z` };
}

async function latestDay(db: Db): Promise<string | null> {
  const [row] = await db.select({ day: sql<string | null>`max(${t.projectDaily.day})` }).from(t.projectDaily);
  return row?.day ? String(row.day) : null;
}

function shiftDay(day: string, delta: number): string {
  const d = new Date(day + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + delta);
  return d.toISOString().slice(0, 10);
}

const COLORS = ["#5b2fff", "#ff007a", "#1b8a5a", "#ff6a00", "#2563eb", "#0ea5e9", "#c2410c", "#7c3aed", "#0f766e", "#b45309"];
function colorFor(id: number): string {
  return COLORS[id % COLORS.length];
}

function isSmartMoney(labels: unknown): boolean {
  if (!Array.isArray(labels)) return false;
  return labels.some((l) => typeof l === "string" && SMART_MONEY_MARKERS.some((m) => l.toLowerCase().includes(m)));
}

function toLabel(labels: unknown, smart: boolean | null): WalletLabel {
  const list = Array.isArray(labels) ? labels.filter((l): l is string => typeof l === "string") : [];
  return { labels: list, smartMoney: smart ?? isSmartMoney(list) };
}

/** float8 / int come back as numbers, bigint / numeric as strings; null stays null. */
function num(v: unknown): number | null {
  if (v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

type NansenValue = { labeled?: Array<{ labels?: unknown }>; smart_money?: boolean };
type GithubValue = { last_commit_at?: string | null };
type ExaValue = { latest?: string | null };
type Enrichment = { secondary_categories?: Category[]; color?: string; parallel?: { audit_url?: string | null; team?: string | null } };

type TopPoolRow = {
  project_id: number; pool_id: string; chain_id: number; hook_address: string;
  token0_symbol: string | null; token1_symbol: string | null; token0_id: string | null; token1_id: string | null;
};
type StatsUsageRow = { project_id: number; rolling: unknown; buys: unknown; sells: unknown; buyers: unknown; sellers: unknown };
type SmartRow = { project_id: number; smart: unknown };

/**
 * Loads projects with their latest project_daily row, hooks, signals, receipts,
 * top pool and 24h usage. `full` adds pools, the daily volume series and top
 * wallets, and is only used for the project page.
 */
async function loadFromDb(db: Db, opts: { slug?: string; full: boolean; latest?: string | null }): Promise<Loaded> {
  // a detail load pins the day of the list it sits beside, so a run landing in between cannot split the page
  const latest = opts.latest !== undefined ? opts.latest : await latestDay(db);
  const projectRows = await db
    .select()
    .from(t.projects)
    .where(opts.slug ? eq(t.projects.slug, opts.slug) : undefined);
  if (!projectRows.length) return [];
  const ids = projectRows.map((p) => p.id);
  const idArray = sql.raw(`array[${ids.join(",")}]::int[]`);
  // The run on `latest` reports closed UTC days through `end` (project_daily.window_end); the snapshot row for `latest` is
  // the running day and is never read.
  const end = latest ? shiftDay(latest, -1) : null;

  const [daily, hookRows, signalRows, receiptRows, topPools, statsUsage, smartUsage] = await Promise.all([
    latest ? db.select().from(t.projectDaily).where(and(eq(t.projectDaily.day, latest), inArray(t.projectDaily.projectId, ids))) : [],
    db.select().from(t.hooks).where(inArray(t.hooks.projectId, ids)),
    db.select().from(t.signals).where(inArray(t.signals.projectId, ids)),
    // Receipts only render on the project page; the list load skips them.
    opts.full
      ? db
          .select()
          .from(t.receipts)
          .where(inArray(t.receipts.projectId, ids))
          .orderBy(sql`${t.receipts.publishedAt} desc nulls last`)
      : [],
    latest && end
      ? db.execute<TopPoolRow>(sql`
          with pp as (
            select h.project_id, p.pool_id, p.chain_id, p.hook_address, p.token0, p.token1
            from ${t.pools} p
            join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
            -- 'hook:<address>' is a capped hook's aggregate of pools the indexer does not store: usage, not a pool
            where h.project_id = any(${idArray}) and p.chain_id = any(${KNOWN_CHAINS}) and p.pool_id not like 'hook:%'),
          w as (
            -- as score: 7-day volume over closed days, TVL from the latest row up to the running day (the pool rows' TVL)
            select s.pool_id, s.chain_id,
                   sum(case when s.day between ${shiftDay(end, -6)} and ${end} then greatest(s.volume_usd_day, 0) end) as vol7d,
                   (array_agg(s.tvl_usd order by s.day desc))[1] as tvl
            from ${t.poolSnapshots} s
            join pp on pp.pool_id = s.pool_id and pp.chain_id = s.chain_id
            where s.day between ${shiftDay(end, -13)} and ${latest}
            group by s.pool_id, s.chain_id)
          select distinct on (pp.project_id) pp.project_id, pp.pool_id, pp.chain_id, pp.hook_address,
                 pp.token0->>'symbol' as token0_symbol, pp.token1->>'symbol' as token1_symbol,
                 pp.token0->>'id' as token0_id, pp.token1->>'id' as token1_id
          from pp join w on w.pool_id = pp.pool_id and w.chain_id = pp.chain_id
          -- the same keys as lib/unlisted.ts compareTopPool, ending on the pool id so ties never flip between loads
          order by pp.project_id, w.vol7d desc nulls last, w.tvl desc nulls last, pp.pool_id`)
      : [],
    // Rolling 24h volume and buy/sell counts, latest pool_stats day. Table may be missing: null, not a failure.
    safe(
      "usage:pool_stats",
      () =>
        db.execute<StatsUsageRow>(sql`
          with d as (select max(day) as day from ${t.poolStats})
          select h.project_id,
                 sum(ps.vol_24h_rolling)::float8 as rolling,
                 sum(ps.buys_24h)::float8 as buys,
                 sum(ps.sells_24h)::float8 as sells,
                 sum(ps.buyers_24h)::float8 as buyers,
                 sum(ps.sellers_24h)::float8 as sellers
          from ${t.poolStats} ps
          join d on ps.day = d.day
          join ${t.pools} p on p.pool_id = ps.pool_id and p.chain_id = ps.chain_id
          join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
          where h.project_id = any(${idArray})
          group by h.project_id`),
      null,
    ),
    // Distinct smart-money wallets among the latest top wallets.
    safe(
      "usage:smart_money",
      () =>
        db.execute<SmartRow>(sql`
          with d as (select max(day) as day from ${t.poolTopWallets})
          select h.project_id, count(distinct w.address)::int as smart
          from ${t.poolTopWallets} w
          join d on w.day = d.day
          join ${t.walletLabels} l on l.address = w.address and l.chain_id = w.chain_id and l.smart_money
          join ${t.pools} p on p.pool_id = w.pool_id and p.chain_id = w.chain_id
          join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
          where h.project_id = any(${idArray})
          group by h.project_id`),
      null,
    ),
  ]);

  const dailyBy = new Map(daily.map((d) => [d.projectId, d]));
  const hooksBy = groupBy(hookRows, (h) => h.projectId ?? -1);
  const receiptsBy = groupBy(receiptRows, (r) => r.projectId);
  const topBy = new Map(rowsOf<TopPoolRow>(topPools).map((r) => [Number(r.project_id), r]));
  const sigBy = new Map(signalRows.map((s) => [`${s.projectId}:${s.kind}`, s.value ?? {}]));
  const statsBy = statsUsage ? new Map(rowsOf<StatsUsageRow>(statsUsage).map((r) => [Number(r.project_id), r])) : null;
  const smartBy = smartUsage ? new Map(rowsOf<SmartRow>(smartUsage).map((r) => [Number(r.project_id), num(r.smart) ?? 0])) : null;

  // Unnamed projects show their busiest pair; indexer-only pools need symbols resolved first (cached, never throws).
  const unlistedIds = new Set(projectRows.filter((p) => isUnlisted(p.name)).map((p) => p.id));
  const needSym = rowsOf<TopPoolRow>(topPools).filter((r) => unlistedIds.has(Number(r.project_id)) && knownChain(Number(r.chain_id)));
  const sym = await resolveSymbols(
    needSym.flatMap((r) => [
      ...(r.token0_symbol || !r.token0_id ? [] : [{ chainId: Number(r.chain_id), address: r.token0_id }]),
      ...(r.token1_symbol || !r.token1_id ? [] : [{ chainId: Number(r.chain_id), address: r.token1_id }]),
    ]),
  );
  for (const r of needSym) {
    r.token0_symbol = r.token0_symbol ?? (r.token0_id ? sym({ chainId: Number(r.chain_id), address: r.token0_id }) : null);
    r.token1_symbol = r.token1_symbol ?? (r.token1_id ? sym({ chainId: Number(r.chain_id), address: r.token1_id }) : null);
  }

  const out: Loaded = [];
  out.degraded = statsUsage === null || smartUsage === null;
  out.day = latest;
  out.gen = ++loads;
  for (const p of projectRows) {
    const d = dailyBy.get(p.id);
    // Registry hooks span ~22 chains; Bait covers the ten in CHAINS. Unknown ids would crash every CHAINS[...] lookup.
    const hs = (hooksBy.get(p.id) ?? []).filter((h) => knownChain(h.chainId)).map(toHook);
    const chains = [...new Set(hs.map((h) => h.chainId))];
    const nansen = sigBy.get(`${p.id}:nansen`) as NansenValue | undefined;
    const gh = sigBy.get(`${p.id}:github`) as GithubValue | undefined;
    const exa = sigBy.get(`${p.id}:exa`) as ExaValue | undefined;
    const enrichment = (p.enrichment ?? {}) as Enrichment;
    const top = topBy.get(p.id);
    const recs = (receiptsBy.get(p.id) ?? []).filter((r) => receiptRelevant(p.name, r.title ?? "", r.source ?? "")).map(toReceipt);
    const audits = auditLinks(hs.map((h) => h.auditUrl), enrichment.parallel, isUnlisted(p.name));
    const su = statsBy?.get(p.id);

    let pools: PoolView[] = [];
    let series: (number | null)[] = [];
    let topWallets: WalletView[] = [];
    if (opts.full) {
      [pools, series, topWallets] = await Promise.all([
        safe("pools", () => loadPools(db, p.id, latest), []),
        safe("series", () => loadSeries(db, p.id, latest), []),
        safe("topWallets", () => loadTopWallets(db, p.id), []),
      ]);
    }

    const usage: Usage = {
      ...EMPTY_USAGE,
      // the closed day `end`, from score: one definition for the list, the page, the blurb and the digest
      vol24h: d?.vol24h ?? null,
      tx24h: d?.swaps24h ?? null,
      wallets24h: d?.wallets24h ?? null,
      walletsExact: d?.walletsExact ?? null,
      wallets7d: d?.wallets7d ?? null,
      topWalletShare7d: d?.topWalletShare7d ?? null,
      deployerSwapShare7d: d?.deployerSwapShare7d ?? null,
      vol24hRolling: su ? num(su.rolling) : null,
      buys24h: su ? num(su.buys) : null,
      sells24h: su ? num(su.sells) : null,
      buyers24h: su ? num(su.buyers) : null,
      sellers24h: su ? num(su.sellers) : null,
      smartWallets: smartBy ? (smartBy.get(p.id) ?? 0) : null,
    };

    out.push(
      derive({
        id: p.id,
        slug: p.slug,
        name: unlistedLabel(p.name, top?.token0_symbol ?? null, top?.token1_symbol ?? null),
        category: (p.category as Category | null) ?? "other",
        secondaryCategories: enrichment.secondary_categories ?? [],
        isFactory: p.isFactory,
        whatLine: p.whatLine ?? "",
        whyBlurb: p.whyBlurb ?? "",
        whyBlurbAt: p.blurbAt?.toISOString() ?? null,
        website: p.website,
        github: p.github,
        xHandle: p.xHandle,
        firstSeenAt: p.firstSeenAt.toISOString(),
        chains,
        flagLabels: [],
        verified: hs.some((h) => h.verifiedSource),
        hooklisted: hs.some((h) => h.hooklisted === true),
        upgradeable: hs.some((h) => !!h.properties.upgradeable),
        metrics: {
          tvlUsd: d?.tvlUsd ?? null,
          // NULL = every pool's volume unknown (score): rendered as unknown, never $0
          vol7d: d ? d.vol7d : 0,
          vol7dPrev: d ? d.vol7dPrev : 0,
          swaps7d: d?.swaps7d ?? null,
          poolsN: d?.poolsN ?? 0,
          hooksN: hs.length,
        },
        signals: {
          smartMoneyLps: (nansen?.labeled ?? []).filter((x) => isSmartMoney(x.labels)).length,
          lastCommitAt: gh?.last_commit_at ?? null,
          lastExaHitAt: exa?.latest ?? null,
          routable: "unknown", // derive() computes it from hooks and pools
          auditCount: audits.length,
          auditUrl: audits[0] ?? null,
        },
        receipts: recs,
        topPool:
          top && knownChain(Number(top.chain_id))
            ? {
                poolId: top.pool_id,
                chainId: Number(top.chain_id) as ChainId,
                token0Symbol: top.token0_symbol,
                token1Symbol: top.token1_symbol,
                hookAddress: top.hook_address ?? null,
              }
            : null,
        usage,
        isNew: d?.isNew ?? false,
        isSpam: d?.isSpam ?? true, // no project_daily row yet = nothing to show
        hasHistoricalUsage: d?.hasHistoricalUsage ?? false,
        activityStatus: d?.activityStatus ?? null,
        isWash: d?.isWash ?? false,
        washHistorical: d?.washHistorical ?? false,
        washReason: d?.washReason ?? null,
        scoreTrending: d?.scoreTrending ?? 0,
        color: enrichment.color ?? colorFor(p.id),
        hooks: hs,
        pools,
        series,
        topWallets,
        through: end,
      }),
    );
  }
  return out;
}

type PoolRow = {
  pool_id: string;
  chain_id: number;
  hook_address: string;
  token0: t.TokenJson | null;
  token1: t.TokenJson | null;
  fee: number | null;
  tick_spacing: number | null;
  created_at: string | Date | null;
  tvl_usd: unknown;
  vol7d: unknown;
  tx7d: unknown;
  vol24: unknown;
};
type PoolStatRow = { pool_id: string; chain_id: number; wallets_24h: unknown; traders_24h: unknown };

async function loadPools(db: Db, projectId: number, latest: string | null): Promise<PoolView[]> {
  if (!latest) return [];
  const end = shiftDay(latest, -1);
  const from7 = shiftDay(end, -6);
  const [rows, stats] = await Promise.all([
    db.execute<PoolRow>(sql`
      select p.pool_id, p.chain_id, p.hook_address, p.token0, p.token1, p.fee, p.tick_spacing, p.created_at,
             lt.tvl_usd,
             -- closed UTC days: 7d = from7..end, 24h = end. No observations and unknown on-chain volume stay NULL.
             case when pending_copy.week_incomplete and coalesce(w.vol7d, 0) = 0 then null
                  when (p.created_at at time zone 'UTC')::date > ${end}::date then 0
                  when w.volume_days = 0 or lc.volume_usd_cum is null then null else w.vol7d end as vol7d,
             case when pending_copy.week_incomplete then null else w.tx7d end as tx7d,
             case when pending_copy.day_incomplete and coalesce(s.volume_usd_day, 0) = 0 then null
                  when (p.created_at at time zone 'UTC')::date > ${end}::date then 0
                  when s.pool_id is null or s.volume_usd_cum is null then null else s.volume_usd_day end as vol24
      from ${t.pools} p
      join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
      left join ${t.poolSnapshots} s on s.pool_id = p.pool_id and s.chain_id = p.chain_id and s.day = ${end}
      left join lateral (
        select x.volume_usd_cum from ${t.poolSnapshots} x
        where x.pool_id = p.pool_id and x.chain_id = p.chain_id and x.day between ${shiftDay(end, -13)} and ${end}
        order by x.day desc limit 1
      ) lc on true
      -- TVL as score sums it into the header: the latest row up to the running day, so the rows add up to the header
      left join lateral (
        select x.tvl_usd from ${t.poolSnapshots} x
        where x.pool_id = p.pool_id and x.chain_id = p.chain_id and x.day between ${shiftDay(end, -13)} and ${latest}
        order by x.day desc limit 1
      ) lt on true
      left join lateral (
        select sum(x.volume_usd_day) as vol7d, count(x.volume_usd_day) as volume_days,
               -- as score: a 7-day count only when every day is known; days before the pool existed count as 0.
               -- Zero priced volume cannot turn missing swap counts into zero (unpriced swaps still count).
               case when count(x.tx_count_day)
                         + greatest(0, least(7, coalesce((p.created_at at time zone 'UTC')::date - ${from7}::date, 0))) >= 7
                    then coalesce(sum(x.tx_count_day), 0) end as tx7d
        from ${t.poolSnapshots} x
        where x.pool_id = p.pool_id and x.chain_id = p.chain_id and x.day between ${from7} and ${end}
      ) w on true
      -- New raw swaps invalidate copied zero counts and zero USD windows until the snapshot catches up.
      -- Keep positive measured USD totals as the observed lower bound; never price these swaps here.
      left join lateral (
        select count(*) > 0 as week_incomplete, coalesce(bool_or(raw.day = ${end}), false) as day_incomplete
        from ${t.poolDailyOnchain} raw
        left join ${t.poolSnapshots} copied on copied.pool_id = raw.pool_id and copied.chain_id = raw.chain_id and copied.day = raw.day
        where raw.pool_id = p.pool_id and raw.chain_id = p.chain_id and raw.day between ${from7} and ${end}
          and raw.swaps > 0 and (copied.tx_count_day is null or copied.tx_count_day < raw.swaps)
      ) pending_copy on true
      where h.project_id = ${projectId} and p.chain_id = any(${KNOWN_CHAINS}) and p.pool_id not like 'hook:%'
      -- active pools first, so the 50 rows hold them on a launchpad with hundreds of idle ones
      order by (coalesce(lt.tvl_usd, 0) > 0 or coalesce(s.volume_usd_day, 0) > 0 or coalesce(w.vol7d, 0) > 0 or coalesce(w.tx7d, 0) > 0) desc,
               lt.tvl_usd desc nulls last, s.volume_usd_day desc nulls last, p.pool_id
      limit 50`),
    safe<unknown>(
      "pools:pool_stats",
      () =>
        db.execute<PoolStatRow>(sql`
          select ps.pool_id, ps.chain_id, ps.wallets_24h, ps.traders_24h
          from ${t.poolStats} ps
          join ${t.pools} p on p.pool_id = ps.pool_id and p.chain_id = ps.chain_id
          join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
          where h.project_id = ${projectId} and ps.day = (select max(day) from ${t.poolStats})`),
      [],
    ),
  ]);
  const statBy = new Map(rowsOf<PoolStatRow>(stats).map((r) => [`${r.chain_id}:${r.pool_id}`, r]));
  const out = rowsOf<PoolRow>(rows)
    .filter((r) => knownChain(Number(r.chain_id)))
    .map((r) => {
      const st = statBy.get(`${r.chain_id}:${r.pool_id}`);
      const vol24 = num(r.vol24);
      const vol7 = num(r.vol7d);
      return {
        poolId: String(r.pool_id),
        chainId: Number(r.chain_id) as ChainId,
        hookAddress: String(r.hook_address),
        token0: r.token0?.id ?? "",
        token1: r.token1?.id ?? "",
        token0Symbol: r.token0?.symbol ?? null,
        token1Symbol: r.token1?.symbol ?? null,
        fee: Number(r.fee ?? 0),
        tickSpacing: Number(r.tick_spacing ?? 0),
        createdAt: r.created_at ? new Date(r.created_at).toISOString() : "",
        tvlUsd: num(r.tvl_usd),
        vol7d: vol7 == null ? null : Math.max(0, vol7),
        tx7d: num(r.tx7d),
        vol24h: vol24 == null ? null : Math.max(0, vol24),
        // distinct when pool_stats has it (on-chain, or a trade fetch that saw the whole day); else buyers + sellers
        wallets24h: st ? (num(st.wallets_24h) ?? num(st.traders_24h)) : null,
        walletsExact: st != null && num(st.wallets_24h) != null,
        eligibility: "unknown" as const, // derive() evaluates it against the hook and this pool's fee
      };
    });
  // Pools found only by the indexer have no symbols: fill them (cached; never blocks the page on failure).
  const sym = await resolveSymbols(
    out.flatMap((p) => [
      ...(p.token0Symbol || !p.token0 ? [] : [{ chainId: p.chainId, address: p.token0 }]),
      ...(p.token1Symbol || !p.token1 ? [] : [{ chainId: p.chainId, address: p.token1 }]),
    ]),
  );
  return out.map((p) => ({
    ...p,
    token0Symbol: p.token0Symbol ?? (p.token0 ? sym({ chainId: p.chainId, address: p.token0 }) : null),
    token1Symbol: p.token1Symbol ?? (p.token1 ? sym({ chainId: p.chainId, address: p.token1 }) : null),
  }));
}

/** Daily volume for the 14 closed UTC days ending the day before `latest`, oldest first. Days without snapshots are 0; a
 * day whose volume is unknown in every pool (NULL daily volume) is null, a gap, never 0. */
async function loadSeries(db: Db, projectId: number, latest: string | null): Promise<(number | null)[]> {
  if (!latest) return [];
  const end = shiftDay(latest, -1);
  const from = shiftDay(end, -13);
  const rows = await db.execute<{ day: string; v: unknown }>(sql`
    select s.day::text as day,
           sum(case when s.volume_usd_day is null then null else greatest(s.volume_usd_day, 0) end)::float8 as v
    from ${t.poolSnapshots} s
    join ${t.pools} p on p.pool_id = s.pool_id and p.chain_id = s.chain_id
    join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
    where h.project_id = ${projectId} and s.day >= ${from} and s.day <= ${end}
    group by s.day
    order by s.day`);
  const byDay = new Map(rowsOf<{ day: string; v: unknown }>(rows).map((r) => [String(r.day).slice(0, 10), num(r.v)]));
  if (byDay.size < 2) return [];
  const out: (number | null)[] = [];
  for (let i = 13; i >= 0; i--) {
    const day = shiftDay(end, -i);
    out.push(byDay.has(day) ? (byDay.get(day) ?? null) : 0);
  }
  return out;
}

type TopWalletRow = { address: string; chain_id: number; volume: unknown; trades: unknown; buys: unknown; sells: unknown; last_trade_at: string | Date | null };

/** Top wallets across the project's pools on the latest pool_top_wallets day, labels joined in a second (optional) read. */
async function loadTopWallets(db: Db, projectId: number, n = 10): Promise<WalletView[]> {
  const rows = rowsOf<TopWalletRow>(
    await db.execute<TopWalletRow>(sql`
      with d as (select max(day) as day from ${t.poolTopWallets})
      select w.address, w.chain_id,
             sum(w.volume_usd)::float8 as volume, sum(w.trades)::float8 as trades,
             sum(w.buys)::float8 as buys, sum(w.sells)::float8 as sells,
             max(w.last_trade_at) as last_trade_at
      from ${t.poolTopWallets} w
      join d on w.day = d.day
      join ${t.pools} p on p.pool_id = w.pool_id and p.chain_id = w.chain_id
      join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
      where h.project_id = ${projectId} and w.chain_id = any(${KNOWN_CHAINS})
      group by w.address, w.chain_id
      order by volume desc nulls last
      limit ${n}`),
  ).filter((r) => knownChain(Number(r.chain_id)));
  if (!rows.length) return [];
  const labels = await safe(
    "topWallets:labels",
    () =>
      db
        .select({ address: t.walletLabels.address, chainId: t.walletLabels.chainId, labels: t.walletLabels.labels, smartMoney: t.walletLabels.smartMoney })
        .from(t.walletLabels)
        .where(inArray(t.walletLabels.address, [...new Set(rows.map((r) => r.address))])),
    [],
  );
  const labelBy = new Map(labels.map((l) => [`${l.chainId}:${l.address}`, toLabel(l.labels, l.smartMoney)]));
  return rows.map((r) => {
    const l = labelBy.get(`${r.chain_id}:${r.address}`);
    return {
      address: r.address,
      chainId: Number(r.chain_id) as ChainId,
      volumeUsd: num(r.volume) ?? 0,
      trades: num(r.trades) ?? 0,
      buys: num(r.buys),
      sells: num(r.sells),
      lastTradeAt: r.last_trade_at ? new Date(r.last_trade_at).toISOString() : null,
      labels: l?.labels ?? [],
      smartMoney: l?.smartMoney ?? false,
    };
  });
}

/** postgres.js returns a RowList (array-like); drizzle types it loosely. */
function rowsOf<R>(res: unknown): R[] {
  if (Array.isArray(res)) return res as R[];
  const maybe = res as { rows?: R[] };
  return maybe?.rows ?? [];
}

function toHook(h: typeof t.hooks.$inferSelect): HookView {
  return {
    address: h.address,
    chainId: h.chainId as ChainId,
    name: h.name ?? "Unnamed hook",
    description: h.description,
    flags: (h.flags ?? {}) as HookView["flags"],
    properties: (h.properties ?? {}) as HookView["properties"],
    verifiedSource: h.verifiedSource ?? false,
    auditUrl: h.auditUrl,
    hooklisted: h.hooklisted ?? null,
    deployer: h.deployer,
    deployerSource: h.deployerSource ?? null,
    firstSeenAt: h.firstSeenAt.toISOString(),
  };
}

function toReceipt(r: typeof t.receipts.$inferSelect): ReceiptView {
  return { url: r.url, title: r.title ?? r.url, source: r.source ?? "", publishedAt: r.publishedAt?.toISOString() ?? "" };
}

function groupBy<T, K>(rows: T[], key: (r: T) => K): Map<K, T[]> {
  const m = new Map<K, T[]>();
  for (const r of rows) {
    const k = key(r);
    const arr = m.get(k);
    if (arr) arr.push(r);
    else m.set(k, [r]);
  }
  return m;
}
