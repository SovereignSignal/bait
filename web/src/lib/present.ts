// Presentation helpers shared by server pages, client components and the OG image. Pure.
import { CHAINS } from "./chains";
import { weekDelta } from "./feed";
import { clip, dayLabel, deltaWords, fmtDelta, int, usd } from "./format";
import { CATEGORY_LABEL, CATEGORY_SHORT, type ChainId, type DigestItem, type ProjectSummary, type RowView } from "./types";

export function chainNames(ids: ChainId[], sep = ", "): string {
  return ids.map((c) => CHAINS[c]?.name ?? `Chain ${c}`).join(sep);
}

export function categoryLine(p: Pick<ProjectSummary, "category" | "secondaryCategories" | "isFactory">): string {
  const parts = [CATEGORY_LABEL[p.category] ?? "Other", ...p.secondaryCategories.map((c) => CATEGORY_LABEL[c] ?? c)];
  if (p.isFactory) parts.push("Factory");
  return parts.join(" · ");
}

export function categoryShort(c: RowView["category"]): string {
  return CATEGORY_SHORT[c] ?? "Other";
}

export function sourceLine(p: ProjectSummary): string {
  const parts = [p.verified ? "Verified" : "Unverified"];
  if (p.signals.auditCount > 0) parts.push(`${p.signals.auditCount} audit${p.signals.auditCount === 1 ? "" : "s"}`);
  if (p.upgradeable) parts.push("upgradeable");
  return parts.join(" · ");
}

/** "+26%" / "-8%" / null when there is no base week. */
export function deltaText(r: { vol7d: number | null; vol7dPrev: number | null }): { text: string; up: boolean } | null {
  const d = weekDelta(r);
  return d == null ? null : { text: fmtDelta(d), up: d >= 0 };
}

// "1d", not "24h": the number is one closed UTC day, like "7d" is seven. "24h" reads as the 24 hours before now, which
// is what the Uniswap app's "Past day" shows, and the two differ by up to two days of trading.
export type Win = "1d" | "7d";

/** The number a row or mover shows and the window it covers. The closed day only when asked for and known. */
export function rowMetric(r: { vol24h: number | null; vol7d: number | null }, use24h: boolean): { v: number | null; win: Win } {
  return use24h && r.vol24h != null ? { v: r.vol24h, win: "1d" } : { v: r.vol7d, win: "7d" };
}

const DAY_MS = 86_400_000;

/** The closed UTC day the "24h" figures cover, as a column label ("Oct 12") and the hover note that says how it differs
 * from the Uniswap app. Unknown day: "1d". */
export function dayWindow(through: string | null | undefined): { short: string; note: string } {
  const day = through ? dayLabel(through) : null;
  return {
    short: day ?? "1d",
    note: `${day ? `${day}, 00:00 to 24:00 UTC` : "The last closed UTC day"}. The Uniswap app's "Past day" is the 24 hours before you look, so its number differs.`,
  };
}

/** Bar labels for a daily series that ends on the closed day `end`, oldest first ("Sep 29" .. "Oct 12"); relative
 * ("14d ago" .. "1d ago") when `end` is unknown. Never "yesterday": the run is nightly, so `end` can be two days back. */
export function seriesLabels(n: number, end: string | null | undefined): string[] {
  const last = end ? Date.parse(`${end}T00:00:00Z`) : NaN;
  return Array.from({ length: n }, (_, i) =>
    Number.isNaN(last) ? `${n - i}d ago` : dayLabel(new Date(last - (n - 1 - i) * DAY_MS).toISOString().slice(0, 10)),
  );
}

const HOOK_SEARCH_CAP = 8;

/** The first http(s) URL among published audit links; anything else (javascript:, data:, bare text) is not a link. */
export function firstWebUrl(urls: Iterable<unknown>): string | null {
  for (const u of urls) if (typeof u === "string" && /^https?:\/\//i.test(u)) return u;
  return null;
}

/** Hook addresses for 0x search: lowercase, deduplicated, capped. */
export function hookKeys(addresses: string[]): string[] {
  return [...new Set(addresses.map((a) => a.toLowerCase()))].slice(0, HOOK_SEARCH_CAP);
}

const dollars = (n: number | null): number | null => (n == null ? null : Math.round(n));

/** ProjectSummary -> the light row the client list ships with, 1,600+ of them per page: whole dollars, no hook list. */
export function toRow(p: ProjectSummary): RowView {
  return {
    slug: p.slug,
    name: p.name,
    whatLine: clip(p.whatLine ?? "", 90),
    color: p.color,
    category: p.category,
    secondaryCategories: p.secondaryCategories,
    chains: p.chains,
    firstSeenAt: p.firstSeenAt,
    isNew: p.isNew,
    isWash: p.isWash,
    scoreTrending: Math.round(p.scoreTrending * 1000) / 1000,
    vol7d: dollars(p.metrics.vol7d),
    vol7dPrev: dollars(p.metrics.vol7dPrev),
    vol24h: dollars(p.usage.vol24h),
    tvlUsd: Math.round(p.metrics.tvlUsd),
    routable: p.signals.routable,
    audited: p.signals.auditCount > 0,
    rank: p.rank,
  };
}

/** The rolling 24 h (GeckoTerminal, at the run) against the daily average of the last 7 closed UTC days, 0..100. */
export function pace(vol24h: number | null, vol7d: number | null): number {
  if (vol24h == null || vol7d == null || vol7d <= 0) return 0;
  return Math.min(100, Math.round(((vol24h * 7) / vol7d) * 100));
}

export function rowLabel(r: Pick<RowView, "name" | "category" | "chains" | "isNew" | "vol7d" | "vol7dPrev"> & { isWash?: boolean }, rank: number | undefined, m: { v: number | null; win: Win }): string {
  const where = r.category !== "other" ? `${categoryShort(r.category)} on ${chainNames(r.chains)}` : chainNames(r.chains);
  const parts = [`${rank ? `#${rank} ` : ""}${r.name}`, where, `${usd(m.v)} ${m.win === "7d" ? "7-day" : "1-day"} volume`];
  const d = weekDelta(r);
  if (r.isWash) parts.push("wash-like volume");
  else if (r.isNew) parts.push("new");
  else if (d != null) parts.push(`${d >= 0 ? "up" : "down"} ${deltaWords(d).replace("%", " percent")}`);
  return parts.join(", ");
}

/** "1,142" for a distinct count, "≤ 1,142" for an upper bound (buyers + sellers, or pools added together). */
export function walletsText(n: number | null, exact: boolean | null): string {
  if (n == null) return int(n);
  return exact === false ? `≤ ${int(n)}` : int(n);
}

// Keep in step with pipeline/bait/rules.py (WASH_REASONS and the thresholds).
const WASH_TEXT: Record<string, string> = {
  "24h": "$250K+ in a day through 25 or fewer wallets",
  "7d": "$1M+ in 7 days through 10 or fewer wallets",
  concentrated: "$1M+ in 7 days through 2 or fewer wallets",
  vol_tvl: "7-day volume over 300x its liquidity from fewer than 50 wallets",
  no_liquidity: "$1M+ in 7 days through a pool with $1K or less of liquidity",
  symmetric: "its top wallets buy and sell in equal counts and carry most of the volume",
  uniform: "its top wallets each made one trade of the same size, and the pool averages that size per wallet",
  unverified: "$1M+ moved through a pool that looks idle, with no wallet data to check who moved it",
};

export function washLine(reason: string | null): string {
  const parts = (reason ?? "").split(",").map((r) => WASH_TEXT[r.trim()]).filter(Boolean);
  return parts.length ? `Wash-like volume: ${parts.join("; ")}.` : "Wash-like volume.";
}

/** One digest card (digests.body, pipeline shape) as the week page renders it; an unknown 7-day volume stays null. */
export function digestItem(
  c: { slug: string; name: string; what_line: string | null; vol_7d: number | null },
  kind: DigestItem["kind"],
): DigestItem {
  return { slug: c.slug, name: c.name, kind, line: c.what_line ?? "", vol7d: c.vol_7d ?? null };
}

/** The pool's closed-day volume cell: dollars when known; for a pool whose USD volume is unknown (custom accounting, unpriced swaps)
 * but that swapped this week, the swap count, a lower bound; else the missing glyph. */
export function poolVolume(x: { vol24h: number | null; tx7d: number | null }): { text: string; note?: string } {
  if (x.vol24h != null) return { text: usd(x.vol24h) };
  if ((x.tx7d ?? 0) > 0)
    return {
      text: `${int(x.tx7d)} swap${x.tx7d === 1 ? "" : "s"}/7d`,
      note: "USD volume unknown (a custom-accounting hook or unpriced swaps). At least this many swaps over the last 7 closed days.",
    };
  return { text: usd(null) };
}

/** A pool with any sign of use: TVL, volume in the day or the week, swaps, or wallets. A null volume is unknown (a custom-
 * accounting hook, unpriced swaps), not zero, so it neither proves nor rules out use. */
export function activePool(x: { tvlUsd: number | null; vol24h: number | null; vol7d?: number | null; tx7d?: number | null; wallets24h?: number | null }): boolean {
  return (x.tvlUsd ?? 0) > 0 || (x.vol24h ?? 0) > 0 || (x.vol7d ?? 0) > 0 || (x.tx7d ?? 0) > 0 || (x.wallets24h ?? 0) > 0;
}

/** The note under the pools table. It calls pools idle only when every pool not shown was loaded and looked at: the
 * project total counts pools on chains Bait does not track and past the 50 rows the page loads. */
export function poolsNote(shown: number, active: number, loaded: number, total: number): string | null {
  const all = Math.max(total, loaded);
  const rest = all - shown;
  if (rest <= 0) return null;
  if (active === 0) return `No pool has TVL or volume on record; showing ${shown} of ${int(all)}.`;
  if (active > shown || all > loaded) return `Top ${shown} of ${int(all)} pools.`;
  return `${int(rest)} more pool${rest === 1 ? "" : "s"} with no TVL or volume on record.`;
}
