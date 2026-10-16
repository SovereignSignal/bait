// Shared routable fixture: ../../../fixtures/routable_cases.json is also asserted by
// pipeline/tests/test_routable_shared.py, so TypeScript and Python cannot drift apart silently.
// Run: npm test   (tsx --test; Node's built-in runner, no extra framework)
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import assert from "node:assert/strict";
import { eligibility, isRoutable, permissionsFromAddress, poolRouting, projectRoutable, projectRouting } from "./routable";
import type { RoutingLists } from "./uniswap-routing";
import type { Eligibility, HookFlags, HookProperties } from "./types";

interface Case {
  name: string;
  address: string;
  flags: Partial<HookFlags>;
  properties: Partial<HookProperties>;
  eligibility: Eligibility;
  poolFee?: number;
}

const file = resolve(__dirname, "../../../fixtures/routable_cases.json");
const cases: Case[] = JSON.parse(readFileSync(file, "utf8")).cases;

test("fixture has ten cases with all three outcomes", () => {
  assert.equal(cases.length, 10);
  assert.deepEqual(new Set(cases.map((c) => c.eligibility)), new Set(["eligible", "ineligible", "unknown"]));
});

for (const c of cases) {
  test(`eligibility: ${c.name}`, () => {
    assert.equal(eligibility(c.address, c.flags, c.properties, c.poolFee ?? null), c.eligibility);
    assert.equal(isRoutable(c.address, c.flags, c.properties), c.eligibility === "eligible" && c.poolFee == null);
  });
}

test("permissionsFromAddress decodes the low 14 bits", () => {
  const all = permissionsFromAddress("0x69af000000000000000000000000000000003fff");
  assert.equal(Object.values(all).every(Boolean), true);
  const two = permissionsFromAddress("0x720e000000000000000000000000000000002080");
  assert.deepEqual(Object.entries(two).filter(([, v]) => v).map(([k]) => k).sort(), ["beforeInitialize", "beforeSwap"]);
});

test("projectRoutable evaluates hook and pool together", () => {
  const clean = "0x5555000000000000000000000000000000000080";
  const delta = "0x4444000000000000000000000000000000000008";
  assert.equal(projectRoutable([{ address: clean, properties: {} }], [{ hookAddress: clean, fee: 3000 }]), "eligible");
  assert.equal(projectRoutable([{ address: clean, properties: {} }], [{ hookAddress: clean, fee: 8388608 }]), "ineligible");
  assert.equal(projectRoutable([{ address: clean, properties: {} }], []), "unknown");
  assert.equal(projectRoutable([{ address: delta, properties: {} }], [{ hookAddress: delta, fee: 3000 }]), "ineligible");
  assert.equal(projectRoutable([{ address: delta, properties: {} }, { address: clean, properties: {} }], [{ hookAddress: clean, fee: 500 }]), "eligible");
});

test("a hook's dynamicFee property does not hide a static-fee pool through it", () => {
  // arrakisprivatehook, spot (2026-10-16 audit): registry says dynamicFee, but a pool through the hook has a static fee
  const clean = "0x5555000000000000000000000000000000000080";
  assert.equal(projectRoutable([{ address: clean, properties: { dynamicFee: true } }], [{ hookAddress: clean, fee: 3000 }]), "eligible");
  assert.equal(projectRoutable([{ address: clean, properties: { dynamicFee: true } }], []), "ineligible");
});

test("Uniswap's denylist beats the rule and its allowlist beats the bits and the fee", () => {
  const clean = "0xa735d365630987d86789d5aefbd91b244a7feac0";     // CASHCAT/USDG, quoter-exempt, denylisted on Robinhood
  const dyn = "0x0000113dcf4add69999fad8f20f2b63f979bfcc0";       // StablePairHook, dynamic fee, allowlisted on Ethereum
  const lists: RoutingLists = {
    allow: new Map([[1, new Set([dyn])]]),
    deny: new Map([[4663, new Set([clean])]]),
    checkedAt: "2026-10-16T19:00:00Z",
  };
  assert.deepEqual(projectRouting([{ address: clean, chainId: 4663, properties: {} }], [{ hookAddress: clean, chainId: 4663, fee: 3000 }], lists),
    { state: "ineligible", source: "denylist" });
  assert.deepEqual(projectRouting([{ address: dyn, chainId: 1, properties: { dynamicFee: true } }], [{ hookAddress: dyn, chainId: 1, fee: 8388608 }], lists),
    { state: "eligible", source: "allowlist" });
  // the same address on another chain is not on that chain's list
  assert.deepEqual(projectRouting([{ address: dyn, chainId: 42161, properties: {} }], [{ hookAddress: dyn, chainId: 42161, fee: 8388608 }], lists),
    { state: "ineligible", source: "rule" });
  assert.deepEqual(poolRouting(clean, 4663, {}, 3000, lists), { state: "ineligible", source: "denylist" });
  assert.deepEqual(poolRouting(clean, 4663, {}, 3000, undefined), { state: "eligible", source: "rule" });
});
