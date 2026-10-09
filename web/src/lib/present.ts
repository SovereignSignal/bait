// Presentation helpers shared by server pages, client components and the OG image. Pure.
import { CHAINS } from "./chains";
import { weekDelta } from "./feed";
import { int } from "./format";
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

/** "▲ 26%" / "▼ 8%" / null when there is no base week. */
export function deltaText(r: { vol7d: number; vol7dPrev: number }): { text: string; up: boolean } | null {
  const d = weekDelta(r);
  if (d == null) return null;
  return { text: `${d >= 0 ? "▲" : "▼"} ${Math.abs(Math.round(d))}%`, up: d >= 0 };
}

const HOOK_SEARCH_CAP = 8;

/** ProjectSummary -> the light row the client list ships with. */
export function toRow(p: ProjectSummary, hookAddresses: string[]): RowView {
  return {
    slug: p.slug,
    name: p.name,
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
export function pace(vol24h: number | null, vol7d: number): number {
  if (vol24h == null || vol7d <= 0) return 0;
  return Math.min(100, Math.round(((vol24h * 7) / vol7d) * 100));
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
  vol_tvl: "7-day volume over 300x its liquidity from fewer than 50 wallets",
  no_liquidity: "$1M+ in 7 days through a pool with $1K or less of liquidity",
  symmetric: "its top wallets buy and sell in equal counts and carry most of the volume",
};

export function washLine(reason: string | null): string {
  const parts = (reason ?? "").split(",").map((r) => WASH_TEXT[r.trim()]).filter(Boolean);
  return parts.length ? `Wash-like volume: ${parts.join("; ")}.` : "Wash-like volume.";
}
