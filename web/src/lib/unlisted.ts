// Projects nobody has named (no hooklist entry, no verified source) arrive as "Unlisted hook 0x7bc2…2aec".
// Show their busiest pair instead so the feed reads as something: "WETH/USDC hook · 0x7bc2…2aec".

const UNLISTED = /^Unlisted hook(?: family)? (0x[0-9a-fA-F]{4}…(?:[0-9a-fA-F]{4})?)$/;

export function isUnlisted(name: string): boolean {
  return UNLISTED.test(name);
}

const PAIR_LABEL = /^\S+\/\S+ hook · 0x[0-9a-fA-F]{4}…/;

/** A display name nobody chose: "Unlisted hook 0x…" or the pair label above. Kept out of Trending, New and the movers
 * (no team, site or source to point anyone at); still listed in All and found by search. */
export function isUnnamed(displayName: string): boolean {
  return isUnlisted(displayName) || PAIR_LABEL.test(displayName);
}

export function unlistedLabel(name: string, sym0: string | null, sym1: string | null): string {
  const m = UNLISTED.exec(name);
  if (!m || !sym0 || !sym1) return name;
  return `${sym0}/${sym1} hook · ${m[1]}`;
}

/** Slugs are lowercase words joined by single dashes; anything else cannot be an old slug, so skip the alias query. */
export function isSlugShaped(slug: string): boolean {
  return slug.length <= 120 && /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug);
}
