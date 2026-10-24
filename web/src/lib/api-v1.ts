// The documented read API (/api/v1). A stable, snake_case contract over the same project objects the pages render, so
// a reviewer's script reads what the site shows. Pure functions here; the route handlers only fetch, call, and respond.
// Every number is the closed UTC day the page reports (data_day); null means unknown, never zero.
import { CHAIN_SLUGS, isChainId, parseChainParam } from "./chains";
import { TRENDING_MIN_VOL, trendingOrder } from "./feed";
import { toRow } from "./present";
import { isCategory, type Category, type ChainId, type HookView, type PoolView, type ProjectDetail, type ProjectSummary, type ReceiptView, type WalletView } from "./types";

/** A 308 to another API path. The Location is relative on purpose: behind Railway's proxy `req.url` carries the
 *  container's own host (`https://0.0.0.0:8080`), so an absolute URL built from it sent clients somewhere unreachable
 *  (2026-10-21, retired slugs). Page redirects were already relative. */
export function apiRedirect(path: string): Response {
  return new Response(null, { status: 308, headers: { Location: path, "Cache-Control": "no-store", "Access-Control-Allow-Origin": "*", "X-Bait-Api-Version": API_VERSION } });
}

export const API_VERSION = "1";
export const LIST_LIMIT_DEFAULT = 100;
export const LIST_LIMIT_MAX = 500;
export const SORTS = ["vol7d", "trending", "new", "tvl"] as const;
export type Sort = (typeof SORTS)[number];

export interface ListQuery {
  sort: Sort;
  limit: number;
  offset: number;
  chain?: ChainId;
  category?: Category;
}

export type ApiError = { error: { code: string; message: string } };

/** Query parsing never throws: bad values fall back, out-of-range values clamp, so a typo returns data, not a 400. */
export function parseListQuery(sp: URLSearchParams): ListQuery {
  const sortRaw = sp.get("sort") ?? "vol7d";
  const sort: Sort = (SORTS as readonly string[]).includes(sortRaw) ? (sortRaw as Sort) : "vol7d";
  const limit = clampInt(sp.get("limit"), LIST_LIMIT_DEFAULT, 1, LIST_LIMIT_MAX);
  const offset = clampInt(sp.get("offset"), 0, 0, 100_000);
  const chain = parseChainParam(sp.get("chain") ?? undefined);
  const categoryRaw = sp.get("category") ?? undefined;
  const category = isCategory(categoryRaw) ? categoryRaw : undefined;
  return { sort, limit, offset, ...(chain ? { chain } : {}), ...(category ? { category } : {}) };
}

function clampInt(raw: string | null, dflt: number, min: number, max: number): number {
  const n = raw == null || raw === "" ? NaN : Number(raw);
  if (!Number.isFinite(n)) return dflt;
  return Math.min(max, Math.max(min, Math.trunc(n)));
}

function num(n: number | null | undefined): number | null {
  return n == null || !Number.isFinite(n) ? null : n;
}

function listed<T extends ProjectSummary>(all: T[]): T[] {
  return all.filter((p) => !p.isSpam);
}

/** Sorted and filtered, with the trending rank attached when the project has one (the site's own definition). */
export function listProjects<T extends ProjectSummary>(all: T[], q: ListQuery): { total: number; rows: Array<T & { rank?: number }> } {
  const live = listed(all);
  const bySlug = new Map(live.map((p) => [p.slug, p]));
  const trending = trendingOrder(live.map(toRow)).map((r) => bySlug.get(r.slug)).filter((p): p is T => p != null);  // the site's own order
  const rankOf = new Map(trending.map((p, i) => [p.slug, i + 1]));
  let rows: T[];
  const byVol = (a: T, b: T) => (b.metrics.vol7d ?? -1) - (a.metrics.vol7d ?? -1) || a.slug.localeCompare(b.slug);
  if (q.sort === "trending") rows = trending;
  else if (q.sort === "new") rows = live.filter((p) => p.isNew && !p.isWash && !p.isDormant).sort((a, b) => +new Date(b.firstSeenAt) - +new Date(a.firstSeenAt));
  else if (q.sort === "tvl") rows = [...live].sort((a, b) => (b.metrics.tvlUsd ?? -1) - (a.metrics.tvlUsd ?? -1) || a.slug.localeCompare(b.slug));
  else rows = [...live].sort((a, b) => Number(a.isWash) - Number(b.isWash) || Number(!!a.isDormant) - Number(!!b.isDormant) || byVol(a, b));
  rows = rows.filter((p) => (!q.chain || p.chains.includes(q.chain)) && (!q.category || p.category === q.category || p.secondaryCategories.includes(q.category)));
  const total = rows.length;
  return { total, rows: rows.slice(q.offset, q.offset + q.limit).map((p) => ({ ...p, rank: rankOf.get(p.slug) })) };
}

export function isHexAddress(s: string | null | undefined): s is string {
  return typeof s === "string" && /^0x[0-9a-fA-F]{40}$/.test(s);
}

/** Projects that own a hook at this address (one address can be a hook on several chains, so several projects). */
export function findByHook<T extends ProjectSummary & { hooks: HookView[] }>(all: T[], address: string, chain?: ChainId): Array<{ project: T; hooks: HookView[] }> {
  const a = address.toLowerCase();
  const out: Array<{ project: T; hooks: HookView[] }> = [];
  for (const p of listed(all)) {
    const hs = p.hooks.filter((h) => h.address.toLowerCase() === a && (!chain || h.chainId === chain));
    if (hs.length) out.push({ project: p, hooks: hs });
  }
  return out.sort((x, y) => (y.project.metrics.vol7d ?? -1) - (x.project.metrics.vol7d ?? -1));
}

// ---------- serializers ----------

export function projectSummaryV1(p: ProjectSummary & { rank?: number }, site: string) {
  const u = p.usage;
  return {
    slug: p.slug,
    name: p.name,
    url: `${site}/p/${p.slug}`,
    what_line: p.whatLine || null,
    category: p.category,
    secondary_categories: p.secondaryCategories,
    chains: p.chains.map((c) => ({ id: c, slug: CHAIN_SLUGS[c] ?? String(c) })),
    first_seen_at: p.firstSeenAt,
    hooklisted: p.hooklisted,
    is_factory: p.isFactory,
    upgradeable: p.upgradeable,
    routable: p.signals.routable,
    routing_source: p.signals.routingSource ?? "rule",
    audit_url: p.signals.auditUrl ?? null,
    status: {
      is_new: p.isNew,
      is_wash: p.isWash,
      wash_reasons: p.washReason ? p.washReason.split(",").filter(Boolean) : [],
      wash_historical: !!p.washHistorical,
      is_dormant: !!p.isDormant,
      activity_status: p.activityStatus ?? null,
      has_historical_usage: !!p.hasHistoricalUsage,
    },
    metrics: {
      vol_7d_usd: num(p.metrics.vol7d),
      vol_7d_prev_usd: num(p.metrics.vol7dPrev),
      vol_24h_usd: num(u.vol24h),
      tvl_usd: num(p.metrics.tvlUsd),
      swaps_7d: num(p.metrics.swaps7d),
      swaps_24h: num(u.tx24h),
      pools: p.metrics.poolsN,
      hooks: p.metrics.hooksN,
      wallets_24h: num(u.wallets24h),
      wallets_24h_exact: u.walletsExact ?? null,
      wallets_7d: num(u.wallets7d),
      top_wallet_share_7d: num(u.topWalletShare7d),
      deployer_swap_share_7d: num(u.deployerSwapShare7d),
      buys_24h_rolling: num(u.buys24h),
      sells_24h_rolling: num(u.sells24h),
      score_trending: p.scoreTrending,
      rank_trending: p.rank ?? null,
      trending_min_vol_usd: TRENDING_MIN_VOL,
    },
    links: { website: p.website, github: p.github, x: p.xHandle ? `https://x.com/${p.xHandle}` : null },
  };
}

function hookV1(h: HookView) {
  return {
    address: h.address.toLowerCase(),
    chain_id: h.chainId,
    name: h.name,
    description: h.description,
    verified_source: h.verifiedSource,
    audit_url: h.auditUrl,
    hooklisted: h.hooklisted,
    deployer: h.deployer ? h.deployer.toLowerCase() : null,
    deployer_source: h.deployerSource ?? null,
    flags: h.flags,
    properties: h.properties,
    first_seen_at: h.firstSeenAt,
  };
}

function poolV1(x: PoolView) {
  return {
    pool_id: x.poolId,
    chain_id: x.chainId,
    hook_address: x.hookAddress.toLowerCase(),
    token0: { address: x.token0, symbol: x.token0Symbol },
    token1: { address: x.token1, symbol: x.token1Symbol },
    fee: x.fee,
    dynamic_fee: x.fee === 8388608,
    tick_spacing: x.tickSpacing,
    created_at: x.createdAt,
    tvl_usd: num(x.tvlUsd),
    vol_7d_usd: num(x.vol7d),
    vol_24h_usd: num(x.vol24h),
    swaps_7d: num(x.tx7d),
    wallets_24h: num(x.wallets24h),
    wallets_24h_exact: x.walletsExact,
    routable: x.eligibility,
    routing_source: x.routingSource ?? "rule",
  };
}

function walletV1(w: WalletView) {
  return { address: w.address, chain_id: w.chainId, volume_usd: w.volumeUsd, trades: w.trades, buys: w.buys, sells: w.sells, last_trade_at: w.lastTradeAt };
}

function receiptV1(r: ReceiptView) {
  return { title: r.title, url: r.url, source: r.source, published_at: r.publishedAt };
}

/** The newest published date among the receipts the response lists; null when none has one. Read from the same list, so
 *  `last_mention_at` can never name a mention the response does not show (it used to read a stored Exa signal that
 *  outlived its receipts, and even sat on unnamed hooks). */
export function lastMentionAt(receipts: ReceiptView[]): string | null {
  let best: number | null = null;
  for (const r of receipts) {
    const t = r.publishedAt ? Date.parse(r.publishedAt) : NaN;
    if (Number.isFinite(t) && (best == null || t > best)) best = t;
  }
  return best == null ? null : new Date(best).toISOString();
}

export function projectV1(p: ProjectDetail & { rank?: number }, site: string) {
  const days = p.series.length;
  const end = p.through ? Date.parse(`${p.through}T00:00:00Z`) : null;
  return {
    ...projectSummaryV1(p, site),
    data_day: p.through ?? null,
    why_blurb: p.whyBlurb || null,
    why_blurb_at: p.whyBlurbAt ?? null,
    hooks: p.hooks.map(hookV1),
    pools: p.pools.map(poolV1),
    daily_volume_usd: p.series.map((v, i) => ({
      day: end == null ? null : new Date(end - (days - 1 - i) * 86_400_000).toISOString().slice(0, 10),
      vol_usd: num(v),
    })),
    top_wallets: p.topWallets.map(walletV1),
    receipts: p.receipts.map(receiptV1),
    signals: { last_commit_at: p.signals.lastCommitAt, last_mention_at: lastMentionAt(p.receipts), audit_count: p.signals.auditCount },
  };
}

export function chainParam(sp: URLSearchParams): { chain?: ChainId; bad: boolean } {
  const raw = sp.get("chain");
  if (raw == null || raw === "") return { bad: false };
  const chain = parseChainParam(raw);
  return chain && isChainId(chain) ? { chain, bad: false } : { bad: true };
}
