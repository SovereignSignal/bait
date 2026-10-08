// One query surface for the pages. With DATABASE_URL set, reads Postgres through
// Drizzle; without it, serves src/fixtures/sample.ts when BAIT_FIXTURES=1 and
// renders empty otherwise. Both paths produce the same view types and run through
// the same derivation (routable, flag labels) and the same tab rules (lib/feed.ts).
//
// Every read goes through safe(): a missing table or a bad query renders empty, never 500.
// The activity tables (pool_stats, pool_top_wallets, wallet_labels) are newer than the rest
// and may be missing or empty in production, so each has its own safe() and degrades to null.
import { and, desc, eq, inArray, sql } from "drizzle-orm";
import { getDb, type Db } from "./client";
import * as t from "./schema";
import { SAMPLE_DIGESTS, SAMPLE_PROJECTS, SAMPLE_WALLET_LABELS } from "@/fixtures/sample";
import { flagLabels, projectRoutable } from "@/lib/routable";
import { CHAINS, knownChain, nansenCovers } from "@/lib/chains";
import { applyTab } from "@/lib/feed";
import { longDate, usd } from "@/lib/format";
import { toRow } from "@/lib/present";
import type {
  Category,
  ChainId,
  DigestItem,
  DigestView,
  FeedQuery,
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

const EMPTY_USAGE: Usage = { vol24h: null, tx24h: null, wallets24h: null, buys24h: null, sells24h: null, buyers24h: null, sellers24h: null, smartWallets: null };

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

function rowOf(p: ProjectDetail): RowView {
  return toRow(p, p.hooks.map((h) => h.address));
}

// ---------- public API ----------

/** Every non-spam project as a light row (the terminal list, movers, search). */
export async function getRows(): Promise<RowView[]> {
  const all = await loadAll();
  return all.filter((p) => !p.isSpam).map(rowOf);
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
  const db = getDb();
  let detail: ProjectDetail | null;
  if (!db) {
    const p = usingFixtures() ? SAMPLE_PROJECTS.find((x) => x.slug === slug) : undefined;
    detail = p ? derive(p) : null;
  } else {
    const rows = await safe("getProject", () => cached(`project:${slug}`, () => loadFromDb(db, { slug, full: true })), []);
    detail = rows[0] ?? null;
  }
  if (!detail) return null;
  const rows = await getRows();
  const rank = applyTab(rows, { tab: "trending" }).find((x) => x.slug === slug)?.rank;
  return { ...detail, rank };
}

export async function getAllSlugs(): Promise<string[]> {
  return (await getRows()).map((p) => p.slug);
}

/** Usage leaderboards across hook projects. */
export async function getLeaders(n = 10): Promise<LeadersView> {
  const all = (await loadAll()).filter((p) => !p.isSpam);
  const active = all.filter((p) => p.metrics.vol7d > 0);
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
      vol7d: active.reduce((s, p) => s + p.metrics.vol7d, 0),
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
  const [row] = await safe("getDigest", () => db.select().from(t.digests).where(eq(t.digests.weekOf, week)).limit(1), []);
  return row ? toDigest(row) : null;
}

export async function listDigests(limit = 12): Promise<DigestView[]> {
  const db = getDb();
  if (!db) return usingFixtures() ? SAMPLE_DIGESTS.slice(0, limit) : [];
  const rows = await safe("listDigests", () => db.select().from(t.digests).orderBy(desc(t.digests.weekOf)).limit(limit), []);
  return rows.map(toDigest);
}

/** Fixtures are opt-in (BAIT_FIXTURES=1) so a build with no DATABASE_URL prerenders empty pages, never sample figures. */
export function usingFixtures(): boolean {
  return !process.env.DATABASE_URL && process.env.BAIT_FIXTURES === "1";
}

/** Before the pipeline's first run the tables may not exist yet; render empty rather than 500 (and fail the healthcheck). */
async function safe<T>(label: string, fn: () => Promise<T>, fallback: T): Promise<T> {
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

// ---------- one-hour in-process cache for the heavy reads (single replica on Railway) ----------
const TTL_MS = 3_600_000;
const MEMO_MAX = 500;
const memo = new Map<string, { at: number; value: Promise<unknown> }>();
function cached<T>(key: string, fn: () => Promise<T>): Promise<T> {
  const hit = memo.get(key);
  const now = Date.now();
  if (hit && now - hit.at < TTL_MS) return hit.value as Promise<T>;
  if (memo.size >= MEMO_MAX) for (const [k, v] of memo) if (now - v.at >= TTL_MS) memo.delete(k);
  const value = fn().catch((e) => {
    memo.delete(key);
    throw e;
  });
  memo.set(key, { at: now, value });
  return value;
}

// ---------- loading ----------

async function loadAll(): Promise<ProjectDetail[]> {
  const db = getDb();
  if (!db) return usingFixtures() ? SAMPLE_PROJECTS.map(derive) : [];
  return safe("loadAll", () => cached("loadAll", () => loadFromDb(db, { full: false })), []);
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
type Enrichment = { secondary_categories?: Category[]; color?: string; parallel?: { audit_url?: string | null } };

type TopPoolRow = { project_id: number; pool_id: string; chain_id: number; hook_address: string; token0_symbol: string | null; token1_symbol: string | null };
type DayUsageRow = { project_id: number; vol24: unknown; tx24: unknown };
type StatsUsageRow = { project_id: number; wallets: unknown; buys: unknown; sells: unknown; buyers: unknown; sellers: unknown };
type SmartRow = { project_id: number; smart: unknown };

/**
 * Loads projects with their latest project_daily row, hooks, signals, receipts,
 * top pool and 24h usage. `full` adds pools, the daily volume series and top
 * wallets, and is only used for the project page.
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
  const prev1 = latest ? shiftDay(latest, -1) : null;

  const [daily, hookRows, signalRows, receiptRows, topPools, dayUsage, statsUsage, smartUsage] = await Promise.all([
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
          select distinct on (h.project_id) h.project_id, p.pool_id, p.chain_id, p.hook_address,
                 p.token0->>'symbol' as token0_symbol, p.token1->>'symbol' as token1_symbol
          from ${t.pools} p
          join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
          join ${t.poolSnapshots} s on s.pool_id = p.pool_id and s.chain_id = p.chain_id and s.day = ${latest}
          where h.project_id = any(${idArray}) and p.chain_id = any(${KNOWN_CHAINS})
          order by h.project_id, s.tvl_usd desc nulls last`)
      : [],
    // 24h volume and txs from consecutive daily snapshots.
    latest && prev1
      ? safe(
          "usage:snapshots",
          () =>
            db.execute<DayUsageRow>(sql`
              select h.project_id,
                     sum(greatest(s.volume_usd_cum - s1.volume_usd_cum, 0))::float8 as vol24,
                     sum(greatest(s.tx_count_cum - s1.tx_count_cum, 0))::float8 as tx24
              from ${t.pools} p
              join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
              join ${t.poolSnapshots} s on s.pool_id = p.pool_id and s.chain_id = p.chain_id and s.day = ${latest}
              join ${t.poolSnapshots} s1 on s1.pool_id = p.pool_id and s1.chain_id = p.chain_id and s1.day = ${prev1}
              where h.project_id = any(${idArray})
              group by h.project_id`),
          null,
        )
      : null,
    // Wallets and buy/sell counts, latest pool_stats day. Table may be missing: null, not a failure.
    safe(
      "usage:pool_stats",
      () =>
        db.execute<StatsUsageRow>(sql`
          with d as (select max(day) as day from ${t.poolStats})
          select h.project_id,
                 sum(ps.traders_24h)::float8 as wallets,
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
  const dayBy = dayUsage ? new Map(rowsOf<DayUsageRow>(dayUsage).map((r) => [Number(r.project_id), r])) : null;
  const statsBy = statsUsage ? new Map(rowsOf<StatsUsageRow>(statsUsage).map((r) => [Number(r.project_id), r])) : null;
  const smartBy = smartUsage ? new Map(rowsOf<SmartRow>(smartUsage).map((r) => [Number(r.project_id), num(r.smart) ?? 0])) : null;

  const out: ProjectDetail[] = [];
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
    const recs = (receiptsBy.get(p.id) ?? []).map(toReceipt);
    const auditUrls = new Set(hs.map((h) => h.auditUrl).filter(Boolean));
    if (enrichment.parallel?.audit_url) auditUrls.add(enrichment.parallel.audit_url);
    const du = dayBy?.get(p.id);
    const su = statsBy?.get(p.id);

    let pools: PoolView[] = [];
    let series: number[] = [];
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
      vol24h: du ? num(du.vol24) : null,
      tx24h: du ? num(du.tx24) : null,
      wallets24h: su ? num(su.wallets) : null,
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
        scoreTrending: d?.scoreTrending ?? 0,
        color: enrichment.color ?? colorFor(p.id),
        hooks: hs,
        pools,
        series,
        topWallets,
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
type PoolStatRow = { pool_id: string; chain_id: number; vol_24h: unknown; traders_24h: unknown };

async function loadPools(db: Db, projectId: number, latest: string | null): Promise<PoolView[]> {
  if (!latest) return [];
  const prev7 = shiftDay(latest, -7);
  const prev1 = shiftDay(latest, -1);
  const [rows, stats] = await Promise.all([
    db.execute<PoolRow>(sql`
      select p.pool_id, p.chain_id, p.hook_address, p.token0, p.token1, p.fee, p.tick_spacing, p.created_at,
             s.tvl_usd,
             s.volume_usd_cum - coalesce(s7.volume_usd_cum, 0) as vol7d,
             s.tx_count_cum - coalesce(s7.tx_count_cum, 0) as tx7d,
             s.volume_usd_cum - s1.volume_usd_cum as vol24
      from ${t.pools} p
      join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
      left join ${t.poolSnapshots} s on s.pool_id = p.pool_id and s.chain_id = p.chain_id and s.day = ${latest}
      left join ${t.poolSnapshots} s7 on s7.pool_id = p.pool_id and s7.chain_id = p.chain_id and s7.day = ${prev7}
      left join ${t.poolSnapshots} s1 on s1.pool_id = p.pool_id and s1.chain_id = p.chain_id and s1.day = ${prev1}
      where h.project_id = ${projectId} and p.chain_id = any(${KNOWN_CHAINS})
      order by s.tvl_usd desc nulls last
      limit 50`),
    safe<unknown>(
      "pools:pool_stats",
      () =>
        db.execute<PoolStatRow>(sql`
          select ps.pool_id, ps.chain_id, ps.vol_24h, ps.traders_24h
          from ${t.poolStats} ps
          join ${t.pools} p on p.pool_id = ps.pool_id and p.chain_id = ps.chain_id
          join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
          where h.project_id = ${projectId} and ps.day = (select max(day) from ${t.poolStats})`),
      [],
    ),
  ]);
  const statBy = new Map(rowsOf<PoolStatRow>(stats).map((r) => [`${r.chain_id}:${r.pool_id}`, r]));
  return rowsOf<PoolRow>(rows)
    .filter((r) => knownChain(Number(r.chain_id)))
    .map((r) => {
      const st = statBy.get(`${r.chain_id}:${r.pool_id}`);
      const snapVol = num(r.vol24);
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
        tvlUsd: num(r.tvl_usd) ?? 0,
        vol7d: Math.max(0, num(r.vol7d) ?? 0),
        tx7d: Math.max(0, num(r.tx7d) ?? 0),
        vol24h: snapVol != null ? Math.max(0, snapVol) : st ? num(st.vol_24h) : null,
        wallets24h: st ? num(st.traders_24h) : null,
      };
    });
}

/** Daily volume for the last 14 days from cumulative snapshots; zero-filled, oldest first. */
async function loadSeries(db: Db, projectId: number, latest: string | null): Promise<number[]> {
  if (!latest) return [];
  const from = shiftDay(latest, -14);
  const rows = await db.execute<{ day: string; v: unknown }>(sql`
    select d.day::text as day, sum(greatest(d.v, 0))::float8 as v
    from (
      select s.day, s.volume_usd_cum - lag(s.volume_usd_cum) over (partition by s.pool_id, s.chain_id order by s.day) as v
      from ${t.poolSnapshots} s
      join ${t.pools} p on p.pool_id = s.pool_id and p.chain_id = s.chain_id
      join ${t.hooks} h on h.address = p.hook_address and h.chain_id = p.chain_id
      where h.project_id = ${projectId} and s.day >= ${from} and s.day <= ${latest}
    ) d
    where d.v is not null
    group by d.day
    order by d.day`);
  const byDay = new Map(rowsOf<{ day: string; v: unknown }>(rows).map((r) => [String(r.day).slice(0, 10), num(r.v) ?? 0]));
  if (byDay.size < 2) return [];
  const out: number[] = [];
  for (let i = 13; i >= 0; i--) out.push(byDay.get(shiftDay(latest, -i)) ?? 0);
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
