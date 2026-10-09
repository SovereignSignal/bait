// Shared routable fixture: ../../../fixtures/routable_cases.json is also asserted by
// pipeline/tests/test_routable_shared.py, so TypeScript and Python cannot drift apart silently.
// Run: npm test   (tsx --test; Node's built-in runner, no extra framework)
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import assert from "node:assert/strict";
import { eligibility, isRoutable, permissionsFromAddress, projectRoutable } from "./routable";
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

test("projectRoutable is true when any hook routes", () => {
  const hooks = cases.map((c) => ({ address: c.address, flags: c.flags, properties: c.properties }));
  assert.equal(projectRoutable(hooks), true);
  assert.equal(projectRoutable(hooks.filter((_, i) => !isRoutable(cases[i].address, cases[i].flags, cases[i].properties))), false);
});
