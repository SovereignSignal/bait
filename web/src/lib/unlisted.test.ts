import { test } from "node:test";
import assert from "node:assert/strict";
import { isUnlisted, oldUnlistedSlugPattern, unlistedLabel } from "./unlisted";

test("unlisted names get the busiest pair", () => {
  assert.equal(unlistedLabel("Unlisted hook 0x7bc2…2aec", "WETH", "USDC"), "WETH/USDC hook · 0x7bc2…2aec");
  assert.equal(unlistedLabel("Unlisted hook family 0x4103…", "ETH", "OBH"), "ETH/OBH hook · 0x4103…");
});

test("named projects and missing symbols are left alone", () => {
  assert.equal(unlistedLabel("OuroborosHook", "WETH", "USDC"), "OuroborosHook");
  assert.equal(unlistedLabel("Unlisted hook 0x7bc2…2aec", null, "USDC"), "Unlisted hook 0x7bc2…2aec");
  assert.equal(isUnlisted("Unlisted hook 0x7bc2…2aec"), true);
  assert.equal(isUnlisted("Unlisted hooks are fun"), false);
});

test("old unlisted slugs map to an address pattern", () => {
  assert.equal(oldUnlistedSlugPattern("unlisted-hook-0x7bc2-2aec"), "0x7bc2%2aec");
  assert.equal(oldUnlistedSlugPattern("unlisted-hook-0x7bc2-2aec-2"), "0x7bc2%2aec");
  assert.equal(oldUnlistedSlugPattern("unlisted-hook-family-0x4103"), "0x4103%");
  assert.equal(oldUnlistedSlugPattern("ouroboroshook"), null);
});
