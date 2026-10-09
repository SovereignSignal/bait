// Presentation helpers shared by server pages, client components and the OG image. Pure.
import { CHAINS } from "./chains";
import { weekDelta } from "./feed";
import { clip, fmtDelta, int, usd } from "./format";
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

export type Win = "24h" | "7d";

/** The number a row or mover shows and the window it covers. 24h only when asked for and known. */
export function rowMetric(r: { vol24h: number | null; vol7d: number | null }, use24h: boolean): { v: number | null; win: Win } {
  return use24h && r.vol24h != null ? { v: r.vol24h, win: "24h" } : { v: r.vol7d, win: "7d" };
}

const HOOK_SEARCH_CAP = 8;

/** ProjectSummary -> the light row the client list ships with. */
export function toRow(p: ProjectSummary, hookAddresses: string[]): RowView {
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
    scoreTrending: p.scoreTrending,
    vol7d: p.metrics.vol7d,
    vol7dPrev: p.metrics.vol7dPrev,
    vol24h: p.usage.vol24h,
    tvlUsd: p.metrics.tvlUsd,
    routable: p.signals.routable,
    audited: p.signals.auditCount > 0,
    hooks: [...new Set(hookAddresses.map((a) => a.toLowerCase()))].slice(0, HOOK_SEARCH_CAP),
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
  const parts = [`${rank ? `#${rank} ` : ""}${r.name}`, where, `${usd(m.v)} ${m.win === "7d" ? "7-day" : "24-hour"} volume`];
  const d = weekDelta(r);
  if (r.isWash) parts.push("wash-like volume");
  else if (r.isNew) parts.push("new");
  else if (d != null) parts.push(`${d >= 0 ? "up" : "down"} ${Math.abs(Math.round(d))} percent`);
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
