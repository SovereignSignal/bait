import type { HookFlags, HookProperties } from "./types";

/**
 * Routable is computed, never stored (DESIGN.md).
 *
 * routable = address does not start with 0x91
 *        AND none of beforeSwapReturnsDelta / afterSwapReturnsDelta /
 *            afterAddLiquidityReturnsDelta / afterRemoveLiquidityReturnsDelta
 *        AND not dynamicFee
 *
 * The published rule (support.uniswap.org "Routing for hooked pools", developers.uniswap.org/hook-allowlist)
 * lists only the 0x91 prefix, the two swap delta flags and dynamicFees. DESIGN.md adds the two liquidity
 * delta flags as a stricter house rule. Hooks that Labs allowlisted manually will show as not routable;
 * the badge is a rule, not a guarantee.
 */
export function isRoutable(
  address: string,
  flags: Partial<HookFlags> | null | undefined,
  properties: Partial<HookProperties> | null | undefined,
): boolean {
  if (address.toLowerCase().startsWith("0x91")) return false;
  const f = flags ?? {};
  if (f.beforeSwapReturnsDelta || f.afterSwapReturnsDelta) return false;
  if (f.afterAddLiquidityReturnsDelta || f.afterRemoveLiquidityReturnsDelta) return false;
  if (properties?.dynamicFee) return false;
  return true;
}

/** A project routes if at least one of its hooks does. */
export function projectRoutable(
  hooks: Array<{ address: string; flags: Partial<HookFlags>; properties: Partial<HookProperties> }>,
): boolean {
  return hooks.some((h) => isRoutable(h.address, h.flags, h.properties));
}

const FLAG_SHORT: Array<[keyof HookFlags, string]> = [
  ["beforeInitialize", "beforeInit"],
  ["afterInitialize", "afterInit"],
  ["beforeAddLiquidity", "beforeAddLiquidity"],
  ["afterAddLiquidity", "afterAddLiquidity"],
  ["beforeRemoveLiquidity", "beforeRemoveLiquidity"],
  ["afterRemoveLiquidity", "afterRemoveLiquidity"],
  ["beforeSwap", "beforeSwap"],
  ["afterSwap", "afterSwap"],
  ["beforeDonate", "beforeDonate"],
  ["afterDonate", "afterDonate"],
];

/** Short labels for the card's well foot: lifecycle flags first, then a single "returnsDelta", then dynamicFee. */
export function flagLabels(flags: Partial<HookFlags>, properties: Partial<HookProperties>): string[] {
  const out: string[] = [];
  if (properties.dynamicFee) out.push("dynamicFee");
  for (const [k, label] of FLAG_SHORT) if (flags[k]) out.push(label);
  if (
    flags.beforeSwapReturnsDelta ||
    flags.afterSwapReturnsDelta ||
    flags.afterAddLiquidityReturnsDelta ||
    flags.afterRemoveLiquidityReturnsDelta
  )
    out.push("returnsDelta");
  return out;
}

/** Full flag names that are set, for the project page hook list. */
export function activeFlags(flags: Partial<HookFlags>): string[] {
  return (Object.keys(flags) as Array<keyof HookFlags>).filter((k) => flags[k]);
}
