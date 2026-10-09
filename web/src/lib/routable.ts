import type { Eligibility, HookFlags, HookProperties } from "./types";

export type { Eligibility } from "./types";

const PERMISSION_BITS: Array<[keyof HookFlags, number]> = [
  ["beforeInitialize", 13], ["afterInitialize", 12],
  ["beforeAddLiquidity", 11], ["afterAddLiquidity", 10],
  ["beforeRemoveLiquidity", 9], ["afterRemoveLiquidity", 8],
  ["beforeSwap", 7], ["afterSwap", 6],
  ["beforeDonate", 5], ["afterDonate", 4],
  ["beforeSwapReturnsDelta", 3], ["afterSwapReturnsDelta", 2],
  ["afterAddLiquidityReturnsDelta", 1], ["afterRemoveLiquidityReturnsDelta", 0],
];

export const DYNAMIC_FEE_FLAG = 0x800000;

/** Hook permissions are the low 14 bits of the address (v4 Hooks.sol); the chain enforces them. */
export function permissionsFromAddress(address: string): HookFlags {
  const bits = Number(BigInt(address) & 0x3fffn);
  return Object.fromEntries(PERMISSION_BITS.map(([k, b]) => [k, ((bits >> b) & 1) === 1])) as unknown as HookFlags;
}

/**
 * Routing eligibility is computed, never stored (DESIGN.md). Three states, evaluated per hook and pool:
 *
 *   ineligible: address starts 0x91, OR any returnsDelta permission bit is set in the address
 *               (beforeSwap / afterSwap / afterAddLiquidity / afterRemoveLiquidity), OR the fee is dynamic
 *   eligible:   none of the above, and the fee is known to be static
 *   unknown:    clean bits but no pool fee and no dynamicFee property to decide the fee
 *
 * Dynamic fee: poolFee & 0x800000 when a pool fee is given, else properties.dynamicFee when that key is present.
 * Permissions come from the address bits, not the declared flags (the `flags` argument is kept for call-site
 * compatibility and ignored). The published rule (support.uniswap.org "Routing for hooked pools",
 * developers.uniswap.org/hook-allowlist) lists only the 0x91 prefix, the two swap delta flags and dynamic fees.
 * DESIGN.md adds the two liquidity delta flags as a stricter house rule. Hooks that Labs allowlisted manually
 * will show as ineligible; the badge is a rule, not a guarantee.
 */
export function eligibility(
  address: string,
  _flags: Partial<HookFlags> | null | undefined,
  properties: Partial<HookProperties> | null | undefined,
  poolFee?: number | null,
): Eligibility {
  if (address.toLowerCase().startsWith("0x91")) return "ineligible";
  const f = permissionsFromAddress(address);
  if (f.beforeSwapReturnsDelta || f.afterSwapReturnsDelta || f.afterAddLiquidityReturnsDelta || f.afterRemoveLiquidityReturnsDelta) return "ineligible";
  if (poolFee != null) return (poolFee & DYNAMIC_FEE_FLAG) !== 0 ? "ineligible" : "eligible";
  if (properties && "dynamicFee" in properties) return properties.dynamicFee ? "ineligible" : "eligible";
  return "unknown";
}

export function isRoutable(
  address: string,
  flags: Partial<HookFlags> | null | undefined,
  properties: Partial<HookProperties> | null | undefined,
): boolean {
  return eligibility(address, flags, properties) === "eligible";
}

/** A project routes if at least one of its hooks does. (Task 3 replaces this with the per-pool version.) */
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
export function flagLabels(address: string, flags: Partial<HookFlags>, properties: Partial<HookProperties>): string[] {
  const out: string[] = [];
  flags = { ...flags, ...permissionsFromAddress(address) };
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
export function activeFlags(address: string, flags: Partial<HookFlags>): string[] {
  flags = { ...flags, ...permissionsFromAddress(address) };
  return (Object.keys(flags) as Array<keyof HookFlags>).filter((k) => flags[k]);
}
