// Receipt relevance: keep a link only when its title names the project AND reads as DeFi/Uniswap.
// Exa matches on the name alone, so "Angstrom" returns a fighting-game character and LinkedIn profiles.
// Mirrors pipeline/bait/rules.py receipt_relevant(); keep the two in sync (shared cases in fixtures/relevance_cases.json).

const DOMAIN_WORDS = ["uniswap", "hook", "v4", "liquidity", "swap", "pool", "amm", "dex", "lp", "mev", "fee"];
const BLOCKED_HOSTS = ["linkedin.com", "wikipedia.org", "netlify.app", "facebook.com", "instagram.com", "tiktok.com"];

export function nameTokens(projectName: string): string[] {
  if (/^unlisted hook/i.test(projectName)) return [];
  const words = projectName.replace(/([a-z0-9])([A-Z])/g, "$1 $2").split(/[^A-Za-z0-9]+/).filter(Boolean);
  const norm = projectName.toLowerCase().replace(/[^a-z0-9]/g, "").replace(/hooks?$/, "");
  const first = (words[0] ?? "").toLowerCase();
  const toks = new Set<string>();
  if (norm.length >= 3) toks.add(norm);
  if (first.length >= 4 && !["uniswap", "hook", "dynamic", "static"].includes(first)) toks.add(first);
  return [...toks];
}

export function receiptRelevant(projectName: string, title: string, source: string): boolean {
  const toks = nameTokens(projectName);
  if (!toks.length) return false;
  const host = (source || "").toLowerCase();
  if (BLOCKED_HOSTS.some((b) => host === b || host.endsWith("." + b))) return false;
  const flat = (title || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  const words = (title || "").toLowerCase();
  // short tokens must start a word ("Hookr" must not match "hook research"); long ones may sit inside run-together text
  const named = toks.some((t) => new RegExp(`(^|[^a-z0-9])${t}`).test(words) || (t.length >= 8 && flat.includes(t)));
  const defi = DOMAIN_WORDS.some((w) => new RegExp(`(^|[^a-z0-9])${w}([^a-z0-9]|$)`).test(words));
  return named && defi;
}
