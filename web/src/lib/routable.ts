import type { Eligibility, HookFlags, HookProperties } from "./types";
import { listedOn, type RoutingLists, type RoutingSource } from "./uniswap-routing";

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

/** Routing for one pool: Uniswap's published lists first (the denylist wins), then the automatic rule. */
export function poolRouting(
  address: string,
  chainId: number,
  properties: Partial<HookProperties> | null | undefined,
  poolFee: number | null | undefined,
  lists?: RoutingLists,
): { state: Eligibility; source: RoutingSource } {
  const listed = listedOn(lists, chainId, address);
  if (listed === "denylist") return { state: "ineligible", source: "denylist" };
  if (listed === "allowlist") return { state: "eligible", source: "allowlist" };
  return { state: eligibility(address, null, properties, poolFee), source: "rule" };
}

/**
 * A project routes if at least one of its pools does. A hook on Uniswap's denylist never routes and one on its
 * allowlist always does (whatever its bits or fee); otherwise a hook with known pools is judged per pool (that pool's fee
 * decides dynamic fee) and a hook with none falls back to its own properties. The address bits (0x91, returns-delta) rule
 * a hook out before any pool is read; a hook's dynamicFee property never does when its pools are known, because a static
 * pool through it is still eligible. Ineligible only when every hook is; anything else short of eligible is unknown.
 */
export function projectRouting(
  hooks: Array<{ address: string; chainId?: number; properties: Partial<HookProperties> | null | undefined }>,
  pools: Array<{ hookAddress: string; chainId?: number; fee: number | null | undefined }>,
  lists?: RoutingLists,
): { state: Eligibility; source: RoutingSource } {
  const results: Array<{ state: Eligibility; source: RoutingSource }> = [];
  for (const h of hooks) {
    const listed = h.chainId != null ? listedOn(lists, h.chainId, h.address) : null;
    if (listed === "denylist") { results.push({ state: "ineligible", source: "denylist" }); continue; }
    if (listed === "allowlist") { results.push({ state: "eligible", source: "allowlist" }); continue; }
    if (eligibility(h.address, null, {}) === "ineligible") { results.push({ state: "ineligible", source: "rule" }); continue; }
    const addr = h.address.toLowerCase();
    const hp = pools.filter((p) => p.hookAddress.toLowerCase() === addr && p.fee != null
      && (p.chainId == null || h.chainId == null || p.chainId === h.chainId));
    if (hp.length === 0) { results.push({ state: eligibility(h.address, null, h.properties), source: "rule" }); continue; }
    results.push(...hp.map((p) => ({ state: eligibility(h.address, null, h.properties, p.fee), source: "rule" as const })));
  }
  const eligible = results.filter((r) => r.state === "eligible");
  if (eligible.length) return { state: "eligible", source: eligible.some((r) => r.source === "allowlist") ? "allowlist" : "rule" };
  if (results.length && results.every((r) => r.state === "ineligible")) {
    return { state: "ineligible", source: results.some((r) => r.source === "denylist") ? "denylist" : "rule" };
  }
  return { state: "unknown", source: "rule" };
}

export function projectRoutable(
  hooks: Array<{ address: string; chainId?: number; properties: Partial<HookProperties> | null | undefined }>,
  pools: Array<{ hookAddress: string; chainId?: number; fee: number | null | undefined }>,
  lists?: RoutingLists,
): Eligibility {
  return projectRouting(hooks, pools, lists).state;
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
