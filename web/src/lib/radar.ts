// Pure helpers for the dev radar (/building) and the "Just launched" feed. No server-only imports.
import { nativeSymbol } from "./chains";
import { feeLabel, shortAddr } from "./format";
import { RADAR_KINDS, type LaunchView, type RadarItemView, type RadarKind } from "./types";

export const ZERO = "0x0000000000000000000000000000000000000000";
const DYNAMIC_FEE_FLAG = 0x800000;

export const RADAR_LABEL: Record<RadarKind, string> = Object.fromEntries(RADAR_KINDS) as Record<RadarKind, string>;

/** fomo-style pill class per kind (globals.css badges + Radar.module.css .bc). */
export const RADAR_PILL: Record<RadarKind, string> = {
  registry_submission: "b-v",
  repo: "b-n",
  hackathon: "b-a",
  incubator: "b-p",
  audit: "b-g",
  launch_post: "bc",
};

/** LP fee from PoolKey: the 0x800000 bit marks a dynamic-fee pool. */
export function isDynamicFee(fee: number | null | undefined): boolean {
  return fee != null && (fee & DYNAMIC_FEE_FLAG) !== 0;
}

export function launchFee(fee: number | null | undefined): string {
  if (fee == null) return "–";
  return isDynamicFee(fee) ? "dynamic" : feeLabel(fee);
}

function currency(addr: string, symbol: string | null, chainId: number): string {
  if (symbol) return symbol;
  return addr.toLowerCase() === ZERO ? nativeSymbol(chainId) : shortAddr(addr);
}

/** "ETH / 0x1234…abcd": symbols when the pools table has them, native for 0x0, else short addresses. */
export function launchPair(l: Pick<LaunchView, "chainId" | "currency0" | "currency1" | "symbol0" | "symbol1">): string {
  return `${currency(l.currency0, l.symbol0, l.chainId)} / ${currency(l.currency1, l.symbol1, l.chainId)}`;
}

/** Project name when the hook belongs to one, else the short hook address. */
export function launchLabel(l: Pick<LaunchView, "hook" | "projectName">): string {
  return l.projectName || shortAddr(l.hook);
}

/** Items grouped in RADAR_KINDS order, each group newest first, empty groups dropped. */
export function groupRadar(items: RadarItemView[], kind?: RadarKind): Array<[RadarKind, RadarItemView[]]> {
  const ts = (x: RadarItemView) => Date.parse(x.publishedAt ?? x.lastSeen) || 0;
  return RADAR_KINDS.filter(([k]) => !kind || k === kind)
    .map(([k]) => [k, items.filter((x) => x.kind === k).sort((a, b) => ts(b) - ts(a))] as [RadarKind, RadarItemView[]])
    .filter(([, xs]) => xs.length > 0);
}

export function radarHref(kind?: RadarKind): string {
  return kind ? `/building?kind=${kind}` : "/building";
}

/** One row per project (or unnamed hook), newest first; `more` counts the older pools folded in. */
export function collapseLaunches<T extends { projectSlug: string | null; hook: string }>(ls: T[]): Array<{ l: T; more: number }> {
  const seen = new Map<string, { l: T; more: number }>();
  for (const l of ls) {
    const k = l.projectSlug ?? l.hook.toLowerCase();
    const e = seen.get(k);
    if (e) e.more += 1;
    else seen.set(k, { l, more: 0 });
  }
  return [...seen.values()];
}
