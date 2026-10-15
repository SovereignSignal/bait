// Tab / filter / ranking rules shared by the server (which project "/" selects)
// and the client list (search, watchlist). Pure: no server-only imports.
import { CHAINS, parseChainParam } from "./chains";
import { isUnnamed } from "./unlisted";
import { CATEGORIES, CATEGORY_LABEL, isCategory, type Category, type ChainId, type Eligibility, type FeedQuery, type HookIndex, type RowView, type Tab } from "./types";

export const TRENDING_TOP_N = 20;
export const TRENDING_MIN_VOL = 10_000;

export function parseTab(s: string | null | undefined): Tab {
  return s === "new" || s === "all" || s === "verified" ? s : "trending";
}

type Params = { get(k: string): string | null } | Record<string, string | string[] | undefined>;

function read(sp: Params, k: string): string | undefined {
  if (typeof (sp as { get?: unknown }).get === "function") return (sp as { get(k: string): string | null }).get(k) ?? undefined;
  const v = (sp as Record<string, string | string[] | undefined>)[k];
  return Array.isArray(v) ? v[0] : v;
}

/** URLSearchParams (client) or Next's searchParams object (server) -> FeedQuery. */
export function parseFeedQuery(sp: Params): FeedQuery {
  const category = read(sp, "category");
  return { tab: parseTab(read(sp, "tab")), chain: parseChainParam(read(sp, "chain")), category: isCategory(category) ? category : undefined };
}

/** "?tab=new&chain=base" (empty string for the defaults). Trending is the default tab. */
export function feedQs(q: Partial<FeedQuery>): string {
  const p = new URLSearchParams();
  if (q.tab && q.tab !== "trending") p.set("tab", q.tab);
  if (q.chain && CHAINS[q.chain]) p.set("chain", CHAINS[q.chain].slug);
  if (q.category) p.set("category", q.category);
  const s = p.toString();
  return s ? `?${s}` : "";
}

export function feedHref(q: Partial<FeedQuery>): string {
  return `/${feedQs(q)}`;
}

export function projectHref(slug: string, q?: Partial<FeedQuery>): string {
  return `/p/${slug}${q ? feedQs(q) : ""}`;
}

/** Where a tab, category or chain change goes: the feed, or the open project with the list re-filtered beside it. */
export function feedHrefAt(pathname: string | null | undefined, q: Partial<FeedQuery>): string {
  return `${pathname?.startsWith("/p/") ? pathname : "/"}${feedQs(q)}`;
}

export function sameQuery(a: FeedQuery, b: FeedQuery): boolean {
  return feedQs(a) === feedQs(b);
}

/** Week-over-week change in percent: vol7d (the 7 closed UTC days ending the day before the run) against vol7dPrev (the
 * 7 before), or null when the earlier week is under $1K. */
export function weekDelta(r: { vol7d: number | null; vol7dPrev: number | null }): number | null {
  if (r.vol7d == null || r.vol7dPrev == null) return null;   // an unknown week is no basis
  return r.vol7dPrev >= 1000 ? (r.vol7d / r.vol7dPrev - 1) * 100 : null;
}

/** Conservative automatic-routing criteria only. Individual Uniswap approvals and quote availability are not checked. */
export function inApp(r: { routable: Eligibility }): boolean {
  return r.routable === "eligible";
}

type FeedItem = Pick<RowView, "slug" | "name" | "isNew" | "isWash" | "isDormant" | "firstSeenAt" | "scoreTrending" | "vol7d" | "tvlUsd" | "chains" | "category" | "secondaryCategories" | "routable">;

function matches(p: FeedItem, q: FeedQuery): boolean {
  if (q.chain && !p.chains.includes(q.chain)) return false;
  if (q.category && p.category !== q.category && !p.secondaryCategories.includes(q.category)) return false;
  return true;
}

/** Trending order over every row (not the filtered view), so ranks stay stable across filters. */
export function trendingOrder<T extends FeedItem>(all: T[]): T[] {
  return [...all]
    .filter((p) => !p.isWash && !p.isDormant && !isUnnamed(p.name) && p.vol7d != null && p.vol7d >= TRENDING_MIN_VOL && p.scoreTrending > 0)
    .sort((a, b) => b.scoreTrending - a.scoreTrending || b.tvlUsd - a.tvlUsd || a.slug.localeCompare(b.slug))
    .slice(0, TRENDING_TOP_N);
}

/** Largest 7-day volume first; an unknown one (null) last. */
export function byVol7d(a: { vol7d: number | null }, b: { vol7d: number | null }): number {
  return (b.vol7d ?? -1) - (a.vol7d ?? -1);
}

/** Rows for a tab + filters. `all` must already exclude spam; wash-like rows are listed in All only.
 * Adds the 1-based trending rank. */
export function applyTab<T extends FeedItem>(all: T[], q: FeedQuery): Array<T & { rank?: number }> {
  const trending = trendingOrder(all);
  const rankOf = new Map(trending.map((p, i) => [p.slug, i + 1]));
  let rows: T[];
  if (q.tab === "new") rows = all.filter((p) => p.isNew && !p.isWash && !p.isDormant && !isUnnamed(p.name)).sort((a, b) => +new Date(b.firstSeenAt) - +new Date(a.firstSeenAt));
  else if (q.tab === "trending") rows = trending;
  else if (q.tab === "verified") rows = all.filter((p) => inApp(p) && !p.isWash && !p.isDormant).sort(byVol7d);
  else rows = [...all].sort((a, b) => Number(a.isWash) - Number(b.isWash) || Number(!!a.isDormant) - Number(!!b.isDormant) || byVol7d(a, b)); // wash-like rows after the rest
  return rows.filter((p) => matches(p, q)).map((p) => ({ ...p, rank: rankOf.get(p.slug) }));
}

/** Rows the list renders at first and per "Show more": a few screens, not all 2,000+ (DOM, hydration, prefetch). */
export const LIST_PAGE = 100;

/** What the list shows: the watchlist, a search across every project (chain and category still apply), or a tab. */
export function listFor<T extends FeedItem>(all: T[], q: FeedQuery, o: { search: string; watchlist?: string[]; hooks?: HookIndex }): Array<T & { rank?: number }> {
  const list = o.watchlist ? all.filter((r) => o.watchlist!.includes(r.slug)).sort(byVol7d) : applyTab(all, o.search.trim() ? { ...q, tab: "all" } : q);
  return searchRows(list, o.search, o.hooks);
}

/** The list a page server-renders for its URL, so first paint needs no fetch; the full set follows from /api/rows. */
export interface FeedSeed {
  query: FeedQuery;
  /** the first LIST_PAGE rows of the query, ranked */
  rows: RowView[];
  /** rows the query matches in all */
  total: number;
  /** every listed project */
  count: number;
  cats: Category[];
  /** /api/rows cache key (rowsVersion) */
  version: string;
}

export function feedSeed(all: RowView[], q: FeedQuery, version: string): FeedSeed {
  const rows = applyTab(all, q);
  return { query: q, rows: rows.slice(0, LIST_PAGE), total: rows.length, count: all.length, cats: availableCategories(all), version };
}

/** Categories that have at least one row, in canonical order. */
export function availableCategories(rows: Array<Pick<RowView, "category" | "secondaryCategories">>): Category[] {
  return CATEGORIES.filter((c) => rows.some((p) => p.category === c || p.secondaryCategories.includes(c)));
}

/** Chains that have at least one row, in CHAIN_ORDER. */
export function availableChains(rows: Array<Pick<RowView, "chains">>, order: ChainId[]): ChainId[] {
  return order.filter((c) => rows.some((p) => p.chains.includes(c)));
}

/** Movers strip, as in the mock: trending-eligible or new, ranked by (new ? 24h x 7 : 7d boosted by growth), top 10. */
export function movers(all: RowView[], n = 10): RowView[] {
  const trending = new Set(trendingOrder(all).map((p) => p.slug));
  const key = (p: RowView) => {
    const v7 = p.vol7d ?? 0;
    if (p.isNew) return (p.vol24h ?? v7 / 7) * 7;
    const d = weekDelta(p);
    return v7 * (d != null && d > 0 ? 1 + d / 100 : 1);
  };
  return all
    .filter((p) => !p.isWash && !p.isDormant && !isUnnamed(p.name) && ((p.isNew && (p.vol7d ?? 0) > 0) || trending.has(p.slug)))
    .sort((a, b) => key(b) - key(a))
    .slice(0, n);
}

/** Client search: name substring, or hook address prefix once the index (/api/hooks) has loaded. */
export function searchRows<T extends Pick<RowView, "name" | "slug">>(rows: T[], q: string, hooks?: HookIndex): T[] {
  const s = q.trim().toLowerCase();
  if (!s) return rows;
  const byAddress = s.startsWith("0x") && hooks;
  return rows.filter((p) => p.name.toLowerCase().includes(s) || (byAddress && (hooks[p.slug] ?? []).some((h) => h.startsWith(s))));
}

/** Movers for the strip: filtered by chain when one is selected, capped. `rows` is already in mover order. */
export function moversFor<T extends { chains: ChainId[] }>(rows: T[], chain?: ChainId, n = 10): T[] {
  return (chain ? rows.filter((r) => r.chains.includes(chain)) : rows).slice(0, n);
}

/** "No new Launchpad hooks on Base." for a filter that matched nothing. */
export function emptyCopy(q: FeedQuery): string {
  const adj = q.tab === "new" ? "new " : q.tab === "trending" ? "trending " : q.tab === "verified" ? "automatic-routing " : "";
  const cat = q.category ? `${CATEGORY_LABEL[q.category]} ` : "";
  return `No ${adj}${cat}hooks${q.chain && CHAINS[q.chain] ? ` on ${CHAINS[q.chain].name}` : ""}.`;
}
