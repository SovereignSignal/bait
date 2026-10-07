// One query surface for the pages. With DATABASE_URL set, reads Postgres through
// Drizzle; without it, serves src/fixtures/sample.ts. Both paths produce the
// same view types and run through the same derivation (routable, flag labels,
// tab filtering, ranking) so the UI never knows which one it got.
import { and, desc, eq, gte, inArray, sql } from "drizzle-orm";
import { getDb, type Db } from "./client";
import * as t from "./schema";
import { SAMPLE_CONTEXT, SAMPLE_DIGESTS, SAMPLE_PROJECTS } from "@/fixtures/sample";
import { flagLabels, projectRoutable } from "@/lib/routable";
import { nansenCovers } from "@/lib/chains";
import { longDate, usd } from "@/lib/format";
import type {
  Category,
  ChainId,
  ContextStats,
  DayPoint,
  DigestItem,
  DigestView,
  FeedQuery,
  HookView,
  PoolView,
  ProjectDetail,
  ProjectSummary,
  ReceiptView,
} from "@/lib/types";

export const TRENDING_TOP_N = 20;
export const TRENDING_MIN_VOL = 10_000;

// Same markers as pipeline/hookfeed/clients/nansen.py is_smart_money.
const SMART_MONEY_MARKERS = ["smart money", "fund", "smart trader", "smart lp", "smart dex"];

// ---------- derivation shared by both sources ----------

function derive(p: ProjectDetail): ProjectDetail {
  const primary = p.hooks[0];
  const labels = primary ? flagLabels(primary.flags, primary.properties) : [];
  const flagLine = p.isFactory && p.metrics.hooksN > 1 ? [`${p.metrics.hooksN.toLocaleString("en-US")} hooks`, "one per pool"] : labels;
  return {
    ...p,
    flagLabels: flagLine,
    signals: {
      ...p.signals,
      routable: projectRoutable(p.hooks),
      smartMoneyLps: nansenCovers(p.chains) ? (p.signals.smartMoneyLps ?? 0) : null,
    },
  };
}

function matches(p: ProjectSummary, q: FeedQuery): boolean {
  if (q.chain && !p.chains.includes(q.chain)) return false;
  if (q.category && p.category !== q.category && !p.secondaryCategories.includes(q.category)) return false;
  return true;
}

function applyTab(all: ProjectDetail[], q: FeedQuery): ProjectSummary[] {
  const trendingOrder = [...all]
    .filter((p) => !p.isSpam && p.metrics.vol7d >= TRENDING_MIN_VOL && p.scoreTrending > 0)
    .sort((a, b) => b.scoreTrending - a.scoreTrending || b.metrics.tvlUsd - a.metrics.tvlUsd)
    .slice(0, TRENDING_TOP_N);
  const rankOf = new Map(trendingOrder.map((p, i) => [p.id, i + 1]));
  const withRank = (p: ProjectDetail): ProjectSummary => ({ ...p, rank: rankOf.get(p.id) });

  let rows: ProjectDetail[];
  if (q.tab === "new") rows = all.filter((p) => p.isNew && !p.isSpam).sort((a, b) => +new Date(b.firstSeenAt) - +new Date(a.firstSeenAt));
  else if (q.tab === "trending") rows = trendingOrder;
  else rows = all.filter((p) => !p.isSpam).sort((a, b) => b.metrics.vol7d - a.metrics.vol7d);
  return rows.map(withRank).filter((p) => matches(p, q));
}

// ---------- public API ----------

export async function getFeed(q: FeedQuery): Promise<ProjectSummary[]> {
  const all = await loadAll();
  return applyTab(all, q);
}

export async function getProject(slug: string): Promise<ProjectDetail | null> {
  const db = getDb();
  let detail: ProjectDetail | null;
  if (!db) {
    const p = usingFixtures() ? SAMPLE_PROJECTS.find((x) => x.slug === slug) : undefined;
    detail = p ? derive(p) : null;
  } else {
    const rows = await loadFromDb(db, { slug, full: true });
    detail = rows[0] ?? null;
  }
  if (!detail) return null;
  const all = await loadAll();
  const rank = applyTab(all, { tab: "trending" }).find((x) => x.slug === slug)?.rank;
  return { ...detail, rank };
}

export async function getAllSlugs(): Promise<string[]> {
  const all = await loadAll();
  return all.filter((p) => !p.isSpam).map((p) => p.slug);
}

export async function getContext(): Promise<ContextStats> {
  const db = getDb();
  if (!db) return usingFixtures() ? SAMPLE_CONTEXT : EMPTY_CONTEXT;
  return cached("getContext", () => contextFromDb(db));
}

async function contextFromDb(db: Db): Promise<ContextStats> {
  const latest = await latestDay(db);
  const weekAgo = new Date(Date.now() - 7 * 86_400_000);
  const [[h], [hNew], [pr], [prNew]] = await Promise.all([
    db.select({ n: sql<number>`count(*)::int` }).from(t.hooks),
    db.select({ n: sql<number>`count(*)::int` }).from(t.hooks).where(gte(t.hooks.firstSeenAt, weekAgo)),
    db.select({ n: sql<number>`count(*)::int` }).from(t.projects),
    db.select({ n: sql<number>`count(*)::int` }).from(t.projects).where(gte(t.projects.firstSeenAt, weekAgo)),
  ]);
  let tvl = 0;
  let tvlPrev = 0;
  if (latest) {
    const prevDay = shiftDay(latest, -7);
    const [[a], [b]] = await Promise.all([
      db.select({ s: sql<number>`coalesce(sum(${t.projectDaily.tvlUsd}),0)::float` }).from(t.projectDaily).where(eq(t.projectDaily.day, latest)),
      db.select({ s: sql<number>`coalesce(sum(${t.projectDaily.tvlUsd}),0)::float` }).from(t.projectDaily).where(eq(t.projectDaily.day, prevDay)),
    ]);
    tvl = a.s;
    tvlPrev = b.s;
  }
  return {
    hooksTracked: h.n,
    hooksDelta7d: hNew.n,
    projects: pr.n,
    projectsNew7d: prNew.n,
    tvlUsd: tvl,
    tvlDelta7dPct: tvlPrev > 0 ? (tvl / tvlPrev - 1) * 100 : 0,
  };
}

export async function getDigest(week: string): Promise<DigestView | null> {
  const db = getDb();
  if (!db) return usingFixtures() ? (SAMPLE_DIGESTS.find((d) => d.week === week) ?? null) : null;
  const [row] = await db.select().from(t.digests).where(eq(t.digests.weekOf, week)).limit(1);
  return row ? toDigest(row) : null;
}

export async function listDigests(limit = 12): Promise<DigestView[]> {
  const db = getDb();
  if (!db) return usingFixtures() ? SAMPLE_DIGESTS.slice(0, limit) : [];
  const rows = await db.select().from(t.digests).orderBy(desc(t.digests.weekOf)).limit(limit);
  return rows.map(toDigest);
}

/** Fixtures are opt-in (HOOKFEED_FIXTURES=1) so a build with no DATABASE_URL prerenders empty pages, never sample figures. */
export function usingFixtures(): boolean {
  return !process.env.DATABASE_URL && process.env.HOOKFEED_FIXTURES === "1";
}

const EMPTY_CONTEXT: ContextStats = { hooksTracked: 0, hooksDelta7d: 0, projects: 0, projectsNew7d: 0, tvlUsd: 0, tvlDelta7dPct: 0 };

// ---------- one-hour in-process cache for the heavy reads (single replica on Railway) ----------
const TTL_MS = 3_600_000;
const memo = new Map<string, { at: number; value: Promise<unknown> }>();
function cached<T>(key: string, fn: () => Promise<T>): Promise<T> {
  const hit = memo.get(key);
  const now = Date.now();
  if (hit && now - hit.at < TTL_MS) return hit.value as Promise<T>;
  const value = fn().catch((e) => { memo.delete(key); throw e; });
  memo.set(key, { at: now, value });
  return value;
}

// ---------- loading ----------

async function loadAll(): Promise<ProjectDetail[]> {
  const db = getDb();
  if (!db) return usingFixtures() ? SAMPLE_PROJECTS.map(derive) : [];
  return cached("loadAll", () => loadFromDb(db, { full: false }));
}

/** digests.body (pipeline shape) -> DigestView. Title and intro are rendered here, not stored. */
function toDigest(row: typeof t.digests.$inferSelect): DigestView {
  const week = String(row.weekOf);
  const b = row.body;
  const card = (kind: DigestItem["kind"]) => (c: t.DigestCard): DigestItem => ({
    slug: c.slug,
    name: c.name,
    kind,
    line: c.what_line ?? "",
    vol7d: c.vol_7d ?? 0,
  });
  const fresh = (b.new ?? []).map(card("new"));
  const hot = (b.trending ?? []).map(card("trending"));
  const totals = b.totals ?? { projects: 0, tvl_usd: 0, vol_7d: 0 };
  const intro = [
    fresh.length === 0 ? "No new projects cleared the bar." : `${fresh.length} new project${fresh.length === 1 ? "" : "s"} cleared the bar.`,
    hot.length > 0 ? `${hot[0].name} led trending.` : null,
    `${totals.projects.toLocaleString("en-US")} projects with usage, ${usd(totals.tvl_usd)} TVL, ${usd(totals.vol_7d)} 7d volume.`,
  ]
    .filter(Boolean)
    .join(" ");
  return { week, title: `Week of ${longDate(week)}`, intro, items: [...fresh, ...hot], createdAt: b.generated_at ?? `${week}T06:00:00Z` };
}

async function latestDay(db: Db): Promise<string | null> {
  const [row] = await db.select({ day: sql<string | null>`max(${t.projectDaily.day})` }).from(t.projectDaily);
  return row?.day ?? null;
}

function shiftDay(day: string, delta: number): string {
  const d = new Date(day + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + delta);
  return d.toISOString().slice(0, 10);
}

const COLORS = ["#5b2fff", "#ff007a", "#1b5e3f", "#ff6a00", "#2563eb", "#0ea5e9", "#c2410c", "#7c3aed", "#0f766e", "#b45309"];
function colorFor(id: number): string {
  return COLORS[id % COLORS.length];
}

function isSmartMoney(labels: unknown): boolean {
  if (!Array.isArray(labels)) return false;
  return labels.some((l) => typeof l === "string" && SMART_MONEY_MARKERS.some((m) => l.toLowerCase().includes(m)));
}

type NansenValue = { labeled?: Array<{ labels?: unknown }>; smart_money?: boolean };
type GithubValue = { last_commit_at?: string | null };
type ExaValue = { latest?: string | null };
type Enrichment = { secondary_categories?: Category[]; color?: string; parallel?: { audit_url?: string | null } };

type TopPoolRow = { project_id: number; pool_id: string; chain_id: number; token0_symbol: string | null; token1_symbol: string | null };

/**
 * Loads projects with their latest project_daily row, hooks, signals,
 * receipts and top pool. `full` adds the pools table and 30-day history and
 * is only used for the project page.
 */
async function loadFromDb(db: Db, opts: { slug?: string; full: boolean }): Promise<ProjectDetail[]> {
  const latest = await latestDay(db);
  const projectRows = await db
    .select()
    .from(t.projects)
    .where(opts.slug ? eq(t.projects.slug, opts.slug) : undefined);
  if (!projectRows.length) return [];
  const ids = projectRows.map((p) => p.id);
  const idArray = sql.raw(`array[${ids.join(",")}]::int[]`);

  const [daily, hookRows, signalRows, receiptRows, topPools] = await Promise.all([
    latest ? db.select().from(t.projectDaily).where(and(eq(t.projectDaily.day, latest), inArray(t.projectDaily.projectId, ids))) : [],
    db.select().from(t.hooks).where(inArray(t.hooks.projectId, ids)),
    db.select().from(t.signals).where(inArray(t.signals.projectId, ids)),
    db
      .select()
      .from(t.receipts)
      .where(inArray(t.receipts.projectId, ids))
      .orderBy(sql`${t.receipts.publishedAt} desc nulls last`),
    latest
      ? db.execute<TopPoolRow>(sql`
          select distinct on (h.project_id) h.project_id, p.pool_id, p.chain_id,
                 p.token0->>'symbol' as token0_symbol, p.token1->>'symbol' as token1_symbol
          from ${t.pools} p
          join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
          join ${t.poolSnapshots} s on s.pool_id = p.pool_id and s.chain_id = p.chain_id and s.day = ${latest}
          where h.project_id = any(${idArray})
          order by h.project_id, s.tvl_usd desc nulls last`)
      : [],
  ]);

  const dailyBy = new Map(daily.map((d) => [d.projectId, d]));
  const hooksBy = groupBy(hookRows, (h) => h.projectId ?? -1);
  const receiptsBy = groupBy(receiptRows, (r) => r.projectId);
  const topBy = new Map(rowsOf<TopPoolRow>(topPools).map((r) => [r.project_id, r]));
  const sigBy = new Map(signalRows.map((s) => [`${s.projectId}:${s.kind}`, s.value ?? {}]));

  const out: ProjectDetail[] = [];
  for (const p of projectRows) {
    const d = dailyBy.get(p.id);
    const hs = (hooksBy.get(p.id) ?? []).map(toHook);
    const chains = [...new Set(hs.map((h) => h.chainId))];
    const nansen = sigBy.get(`${p.id}:nansen`) as NansenValue | undefined;
    const gh = sigBy.get(`${p.id}:github`) as GithubValue | undefined;
    const exa = sigBy.get(`${p.id}:exa`) as ExaValue | undefined;
    const enrichment = (p.enrichment ?? {}) as Enrichment;
    const top = topBy.get(p.id);
    const recs = (receiptsBy.get(p.id) ?? []).map(toReceipt);
    const auditUrls = new Set(hs.map((h) => h.auditUrl).filter(Boolean));
    if (enrichment.parallel?.audit_url) auditUrls.add(enrichment.parallel.audit_url);

    let pools: PoolView[] = [];
    let history: DayPoint[] = [];
    if (opts.full) {
      [pools, history] = await Promise.all([loadPools(db, p.id, latest), loadHistory(db, p.id)]);
    }

    out.push(
      derive({
        id: p.id,
        slug: p.slug,
        name: p.name,
        category: (p.category as Category | null) ?? "other",
        secondaryCategories: enrichment.secondary_categories ?? [],
        isFactory: p.isFactory,
        whatLine: p.whatLine ?? "",
        whyBlurb: p.whyBlurb ?? "",
        website: p.website,
        github: p.github,
        xHandle: p.xHandle,
        firstSeenAt: p.firstSeenAt.toISOString(),
        chains,
        flagLabels: [],
        verified: hs.some((h) => h.verifiedSource),
        upgradeable: hs.some((h) => !!h.properties.upgradeable),
        metrics: {
          tvlUsd: d?.tvlUsd ?? 0,
          vol7d: d?.vol7d ?? 0,
          vol7dPrev: d?.vol7dPrev ?? 0,
          swaps7d: d?.swaps7d ?? 0,
          poolsN: d?.poolsN ?? 0,
          hooksN: hs.length,
        },
        signals: {
          smartMoneyLps: (nansen?.labeled ?? []).filter((x) => isSmartMoney(x.labels)).length,
          lastCommitAt: gh?.last_commit_at ?? null,
          lastExaHitAt: exa?.latest ?? null,
          routable: false,
          auditCount: auditUrls.size,
        },
        receipts: recs,
        topPool: top ? { poolId: top.pool_id, chainId: top.chain_id as ChainId, token0Symbol: top.token0_symbol, token1Symbol: top.token1_symbol } : null,
        isNew: d?.isNew ?? false,
        isSpam: d?.isSpam ?? true, // no project_daily row yet = nothing to show
        scoreTrending: d?.scoreTrending ?? 0,
        color: enrichment.color ?? colorFor(p.id),
        hooks: hs,
        pools,
        history,
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
  tvl_usd: number | null;
  vol7d: number | null;
  tx7d: number | null;
};

async function loadPools(db: Db, projectId: number, latest: string | null): Promise<PoolView[]> {
  if (!latest) return [];
  const prev = shiftDay(latest, -7);
  const rows = await db.execute<PoolRow>(sql`
    select p.pool_id, p.chain_id, p.hook_address, p.token0, p.token1, p.fee, p.tick_spacing, p.created_at,
           s.tvl_usd,
           s.volume_usd_cum - coalesce(s7.volume_usd_cum, 0) as vol7d,
           s.tx_count_cum - coalesce(s7.tx_count_cum, 0) as tx7d
    from ${t.pools} p
    join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
    left join ${t.poolSnapshots} s on s.pool_id = p.pool_id and s.chain_id = p.chain_id and s.day = ${latest}
    left join ${t.poolSnapshots} s7 on s7.pool_id = p.pool_id and s7.chain_id = p.chain_id and s7.day = ${prev}
    where h.project_id = ${projectId}
    order by s.tvl_usd desc nulls last
    limit 50`);
  return rowsOf<PoolRow>(rows).map((r) => ({
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
    tvlUsd: Number(r.tvl_usd ?? 0),
    vol7d: Math.max(0, Number(r.vol7d ?? 0)),
    tx7d: Math.max(0, Number(r.tx7d ?? 0)),
  }));
}

async function loadHistory(db: Db, projectId: number): Promise<DayPoint[]> {
  const rows = await db
    .select({ day: t.projectDaily.day, tvlUsd: t.projectDaily.tvlUsd, vol7d: t.projectDaily.vol7d })
    .from(t.projectDaily)
    .where(eq(t.projectDaily.projectId, projectId))
    .orderBy(desc(t.projectDaily.day))
    .limit(30);
  return rows.reverse().map((r) => ({ day: String(r.day), tvlUsd: r.tvlUsd ?? 0, vol7d: r.vol7d ?? 0 }));
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
    deployer: h.deployer,
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
