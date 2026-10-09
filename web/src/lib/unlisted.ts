// Projects nobody has named (no hooklist entry, no verified source) arrive as "Unlisted hook 0x7bc2…2aec".
// Show their busiest pair instead so the feed reads as something: "WETH/USDC hook · 0x7bc2…2aec".

const UNLISTED = /^Unlisted hook(?: family)? (0x[0-9a-fA-F]{4}…(?:[0-9a-fA-F]{4})?)$/;

export function isUnlisted(name: string): boolean {
  return UNLISTED.test(name);
}

export function unlistedLabel(name: string, sym0: string | null, sym1: string | null): string {
  const m = UNLISTED.exec(name);
  if (!m || !sym0 || !sym1) return name;
  return `${sym0}/${sym1} hook · ${m[1]}`;
}

/**
 * Old "unlisted-hook-0x7bc2-2aec" / "unlisted-hook-family-0x4103" slugs, from before the hook was named.
 * Returns a SQL LIKE pattern for the hook address so the page can redirect to the project's current slug.
 */
export function oldUnlistedSlugPattern(slug: string): string | null {
  const one = /^unlisted-hook-(0x[0-9a-f]{4})-([0-9a-f]{4})(?:-\d+)?$/.exec(slug);
  if (one) return `${one[1]}%${one[2]}`;
  const fam = /^unlisted-hook-family-(0x[0-9a-f]{4})(?:-\d+)?$/.exec(slug);
  return fam ? `${fam[1]}%` : null;
}
