// A project's deployments, for the chips under its badges (DESIGN.md "Same-team merge"). One team's per-chain and
// per-version hooks are one project, so the registry names they came under ("Clanker Static Fee Hook (Base)", "Aegis v2
// Hook (Unichain)") show here instead of as cards of their own.
import type { ChainId, HookView } from "./types";

// Same chain words as pipeline rules.STEM_CHAINS; a trailing "(Base)" or "(Robinhood Chain)" is the chain, said once.
const CHAIN_WORDS =
  "ethereum|base|arbitrum|optimism|unichain|polygon|avalanche|avax|bnb|bsc|ink|robinhood|monad|world ?chain|soneium|mega ?eth|celo|blast|linea|x ?layer|arc|tempo|zora";
const CHAIN_SUFFIX = new RegExp(`\\s*\\((?:${CHAIN_WORDS})(?: chain)?\\)\\s*$`, "i");

export interface Variant {
  label: string;
  chains: ChainId[];
  hooks: number;
}

/** The registry name without a trailing chain in parentheses. */
export function variantLabel(name: string): string {
  return name.replace(CHAIN_SUFFIX, "").trim() || name;
}

function key(label: string): string {
  const flat = label.toLowerCase().replace(/[^a-z0-9]/g, "");
  return flat.replace(/hooks?$/, "") || flat; // "Aegis" and "AegisHook" are one name, as in rules.normalize_name
}

/** Named hooks grouped by variant label, busiest first; empty unless there are two or more to tell apart. */
export function variants(hooks: Pick<HookView, "name" | "chainId">[]): Variant[] {
  const by = new Map<string, Variant>();
  for (const h of hooks) {
    const name = (h.name ?? "").trim();
    if (!name || name === "Unnamed hook" || /^unlisted hook/i.test(name)) continue;
    const label = variantLabel(name);
    const k = key(label);
    const v = by.get(k) ?? { label, chains: [], hooks: 0 };
    if (!v.chains.includes(h.chainId)) v.chains.push(h.chainId);
    v.hooks += 1;
    by.set(k, v);
  }
  const out = [...by.values()].sort((a, b) => b.hooks - a.hooks || a.label.localeCompare(b.label));
  return out.length >= 2 ? out : [];
}
