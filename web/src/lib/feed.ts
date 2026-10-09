// Tab / filter / ranking rules shared by the server (which project "/" selects)
// and the client list (search, watchlist). Pure: no server-only imports.
import { CHAINS, parseChainParam } from "./chains";
import { CATEGORIES, isCategory, type Category, type ChainId, type Eligibility, type FeedQuery, type RowView, type Tab } from "./types";

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

/** Week-over-week change in percent, or null when last week is too small to compare. */
export function weekDelta(r: { vol7d: number; vol7dPrev: number }): number | null {
  return r.vol7dPrev >= 1000 ? (r.vol7d / r.vol7dPrev - 1) * 100 : null;
}

/** The "In Uniswap app" tab: at least one pool our rule says the app will route to. Audit status is a separate badge. */
export function inApp(r: { routable: Eligibility }): boolean {
  return r.routable === "eligible";
}

type FeedItem = Pick<RowView, "slug" | "isNew" | "firstSeenAt" | "scoreTrending" | "vol7d" | "tvlUsd" | "chains" | "category" | "secondaryCategories" | "routable" | "audited">;

function matches(p: FeedItem, q: FeedQuery): boolean {
  if (q.chain && !p.chains.includes(q.chain)) return false;
  if (q.category && p.category !== q.category && !p.secondaryCategories.includes(q.category)) return false;
  return true;
}

/** Trending order over every row (not the filtered view), so ranks stay stable across filters. */
export function trendingOrder<T extends FeedItem>(all: T[]): T[] {
  return [...all]
    .filter((p) => p.vol7d >= TRENDING_MIN_VOL && p.scoreTrending > 0)
    .sort((a, b) => b.scoreTrending - a.scoreTrending || b.tvlUsd - a.tvlUsd)
    .slice(0, TRENDING_TOP_N);
}

/** Rows for a tab + filters. `all` must already exclude spam. Adds the 1-based trending rank. */
export function applyTab<T extends FeedItem>(all: T[], q: FeedQuery): Array<T & { rank?: number }> {
  const trending = trendingOrder(all);
  const rankOf = new Map(trending.map((p, i) => [p.slug, i + 1]));
  let rows: T[];
  if (q.tab === "new") rows = all.filter((p) => p.isNew).sort((a, b) => +new Date(b.firstSeenAt) - +new Date(a.firstSeenAt));
  else if (q.tab === "trending") rows = trending;
  else if (q.tab === "verified") rows = all.filter(inApp).sort((a, b) => b.vol7d - a.vol7d);
  else rows = [...all].sort((a, b) => b.vol7d - a.vol7d);
  return rows.filter((p) => matches(p, q)).map((p) => ({ ...p, rank: rankOf.get(p.slug) }));
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
    if (p.isNew) return (p.vol24h ?? p.vol7d / 7) * 7;
    const d = weekDelta(p);
    return p.vol7d * (d != null && d > 0 ? 1 + d / 100 : 1);
  };
  return all
    .filter((p) => (p.isNew && p.vol7d > 0) || trending.has(p.slug))
    .sort((a, b) => key(b) - key(a))
    .slice(0, n);
}

/** Client search: name substring or hook address prefix. */
export function searchRows<T extends Pick<RowView, "name" | "hooks">>(rows: T[], q: string): T[] {
  const s = q.trim().toLowerCase();
  if (!s) return rows;
  return rows.filter((p) => p.name.toLowerCase().includes(s) || (s.startsWith("0x") && p.hooks.some((h) => h.startsWith(s))));
}
