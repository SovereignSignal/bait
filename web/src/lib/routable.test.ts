// Shared routable fixture: ../../../fixtures/routable_cases.json is also asserted by
// pipeline/tests/test_routable_shared.py, so TypeScript and Python cannot drift apart silently.
// Run: npm test   (tsx --test; Node's built-in runner, no extra framework)
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import assert from "node:assert/strict";
import { isRoutable, projectRoutable } from "./routable";
import type { HookFlags, HookProperties } from "./types";

interface Case {
  name: string;
  address: string;
  flags: Partial<HookFlags>;
  properties: Partial<HookProperties>;
  routable: boolean;
}

const file = resolve(__dirname, "../../../fixtures/routable_cases.json");
const cases: Case[] = JSON.parse(readFileSync(file, "utf8")).cases;

test("fixture has six cases with both outcomes", () => {
  assert.equal(cases.length, 6);
  assert.deepEqual(new Set(cases.map((c) => c.routable)), new Set([true, false]));
});

for (const c of cases) {
  test(`isRoutable: ${c.name}`, () => {
    assert.equal(isRoutable(c.address, c.flags, c.properties), c.routable);
  });
}

test("projectRoutable is true when any hook routes", () => {
  const hooks = cases.map((c) => ({ address: c.address, flags: c.flags, properties: c.properties }));
  assert.equal(projectRoutable(hooks), true);
  assert.equal(projectRoutable(hooks.filter((_, i) => !cases[i].routable)), false);
});
