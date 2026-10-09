// Presentation helpers shared by server pages, client components and the OG image. Pure.
import { CHAINS } from "./chains";
import { weekDelta } from "./feed";
import { clip, fmtDelta } from "./format";
import { CATEGORY_LABEL, CATEGORY_SHORT, type ChainId, type ProjectSummary, type RowView } from "./types";

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
export function deltaText(r: { vol7d: number; vol7dPrev: number }): { text: string; up: boolean } | null {
  const d = weekDelta(r);
  return d == null ? null : { text: fmtDelta(d), up: d >= 0 };
}

export type Win = "24h" | "7d";

/** The number a row or mover shows and the window it covers. 24h only when asked for and known. */
export function rowMetric(r: { vol24h: number | null; vol7d: number }, use24h: boolean): { v: number; win: Win } {
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

/** Today's pace against the 7-day run rate, 0..100. */
export function pace(vol24h: number | null, vol7d: number): number {
  if (vol24h == null || vol7d <= 0) return 0;
  return Math.min(100, Math.round(((vol24h * 7) / vol7d) * 100));
}
