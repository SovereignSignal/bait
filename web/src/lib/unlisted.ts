// Projects nobody has named (no hooklist entry, no verified source) arrive as "Unlisted hook 0x7bc2…2aec".
// Show their busiest pair instead so the feed reads as something: "WETH/USDC hook · 0x7bc2…2aec". A family (one
// launchpad's per-token hooks, shared bytecode) says so, since its busiest pair is one token of many.

const UNLISTED = /^Unlisted hook( family)? (0x[0-9a-fA-F]{4}…(?:[0-9a-fA-F]{4})?)$/;

export function isUnlisted(name: string): boolean {
  return UNLISTED.test(name);
}

// "hook family ·" too: a family's pair label must stay out of Trending/New just like its placeholder name.
const PAIR_LABEL = /^\S+\/\S+ hook(?: family)? · 0x[0-9a-fA-F]{4}…/;

/** A display name nobody chose: "Unlisted hook 0x…" or the pair label above. Kept out of Trending, New and the movers
 * (no team, site or source to point anyone at); still listed in All and found by search. */
export function isUnnamed(displayName: string): boolean {
  return isUnlisted(displayName) || PAIR_LABEL.test(displayName);
}

/** The pool an unnamed project is labelled by: most 7-day volume, then most TVL, then the lowest pool id, so every page
 * (list, detail, week, OG, RSS) names a family by the same pair. queries.ts topPools orders by exactly these keys; keep
 * them in step. Unknown (null) sorts below any number. */
export function compareTopPool(
  a: { vol7d: number | null; tvlUsd: number | null; poolId: string },
  b: { vol7d: number | null; tvlUsd: number | null; poolId: string },
): number {
  const desc = (x: number | null, y: number | null) => (y ?? -Infinity) - (x ?? -Infinity) || 0;
  return desc(a.vol7d, b.vol7d) || desc(a.tvlUsd, b.tvlUsd) || (a.poolId < b.poolId ? -1 : a.poolId > b.poolId ? 1 : 0);
}

export function unlistedLabel(name: string, sym0: string | null, sym1: string | null): string {
  const m = UNLISTED.exec(name);
  if (!m || !sym0 || !sym1) return name;
  return `${sym0}/${sym1} hook${m[1] ? " family" : ""} · ${m[2]}`;
}

/** Slugs are lowercase words joined by single dashes; anything else cannot be an old slug, so skip the alias query. */
export function isSlugShaped(slug: string): boolean {
  return slug.length <= 120 && /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug);
}
