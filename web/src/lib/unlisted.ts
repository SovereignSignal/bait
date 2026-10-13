// Projects nobody has named (no hooklist entry, no verified source) arrive as "Unlisted hook 0x7bc2…2aec".
// Show their busiest pair instead so the feed reads as something: "WETH/USDC hook · 0x7bc2…2aec". A family (one
// launchpad's per-token hooks, shared bytecode) says so, since its busiest pair is one token of many.

const UNLISTED = /^Unlisted hook( family)? (0x[0-9a-fA-F]{4}…(?:[0-9a-fA-F]{4})?)$/;

export function isUnlisted(name: string): boolean {
  return UNLISTED.test(name);
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
