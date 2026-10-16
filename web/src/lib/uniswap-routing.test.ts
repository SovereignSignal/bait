import { test } from "node:test";
import assert from "node:assert/strict";
import { listedOn, listSize, parseRoutingList } from "./uniswap-routing";

// The shapes found in Uniswap/uniroute-public on 2026-10-16: constants, multi-line constants, arrays, spreads, local
// chain ids, comments, the zero address, and calls that cannot be resolved here.
const SAMPLE = `
import {ChainId} from '@uniswap/sdk-core';
const CHAIN_ID_INK = 57073 as ChainId;
export const STABLE_PAIR_HOOK_ON_MAINNET = '0x0000113DCF4ADD69999FAD8F20F2B63F979BFCC0';
export const CLANKER_HOOK_ON_BASE =
  '0xb429d62f8f3bffb98cdb9569533ea23bf0ba28cc';
export const QUOTER_EXEMPT_HOOKS_ON_ROBINHOOD = [
  '0xa735d365630987d86789d5aefbd91b244a7feac0',
  '0x8e21aff7115d3a91259897db144db7fe841d6ac0',
];
// export const COMMENTED = '0x1111111111111111111111111111111111111111';
export const HOOKS_ADDRESSES_ALLOWLIST: Partial<
  Record<ChainId, Array<string>>
> &
  Record<number, Array<string>> = {
  [ChainId.MAINNET]: [ADDRESS_ZERO, STABLE_PAIR_HOOK_ON_MAINNET, '0x2222222222222222222222222222222222222222'],
  [ChainId.BASE]: [CLANKER_HOOK_ON_BASE, ...getDynamicZlcaHooks(ChainId.BASE)],
  [ChainId.ROBINHOOD]: [...QUOTER_EXEMPT_HOOKS_ON_ROBINHOOD],
  [CHAIN_ID_INK]: [CLANKER_HOOK_ON_BASE],
  [ChainId.SEPOLIA]: [ADDRESS_ZERO],
};
`;

test("parses constants, spreads and local chain ids, and skips what it cannot resolve", () => {
  const m = parseRoutingList(SAMPLE, "HOOKS_ADDRESSES_ALLOWLIST");
  assert.deepEqual([...m.get(1)!].sort(), ["0x0000113dcf4add69999fad8f20f2b63f979bfcc0", "0x2222222222222222222222222222222222222222"]);
  assert.deepEqual([...m.get(8453)!], ["0xb429d62f8f3bffb98cdb9569533ea23bf0ba28cc"]);
  assert.equal(m.get(4663)!.size, 2);
  assert.equal(m.get(57073)!.size, 1);
  assert.equal(m.has(11155111), false);                     // Sepolia is not a known id, and held only the zero address
  assert.equal(listSize(m), 6);
  assert.equal(parseRoutingList(SAMPLE, "NO_SUCH_MAP").size, 0);
});

test("listedOn is per chain, case-insensitive, and the denylist wins", () => {
  const a = "0xa735d365630987d86789d5aefbd91b244a7feac0";
  const lists = { allow: new Map([[4663, new Set([a])]]), deny: new Map([[4663, new Set([a])]]), checkedAt: null };
  assert.equal(listedOn(lists, 4663, a.toUpperCase().replace("0X", "0x")), "denylist");
  assert.equal(listedOn(lists, 1, a), null);
  assert.equal(listedOn(undefined, 4663, a), null);
});
