import { test } from "node:test";
import assert from "node:assert/strict";
import { isSlugShaped, isUnlisted, unlistedLabel } from "./unlisted";

test("unlisted names get the busiest pair", () => {
  assert.equal(unlistedLabel("Unlisted hook 0x7bc2…2aec", "WETH", "USDC"), "WETH/USDC hook · 0x7bc2…2aec");
  assert.equal(unlistedLabel("Unlisted hook family 0x4103…", "ETH", "OBH"), "ETH/OBH hook family · 0x4103…");
});

test("named projects and missing symbols are left alone", () => {
  assert.equal(unlistedLabel("OuroborosHook", "WETH", "USDC"), "OuroborosHook");
  assert.equal(unlistedLabel("Unlisted hook 0x7bc2…2aec", null, "USDC"), "Unlisted hook 0x7bc2…2aec");
  assert.equal(isUnlisted("Unlisted hook 0x7bc2…2aec"), true);
  assert.equal(isUnlisted("Unlisted hooks are fun"), false);
});

test("only slug-shaped paths reach the alias lookup", () => {
  assert.equal(isSlugShaped("unlisted-hook-0x7bc2-2aec"), true);
  assert.equal(isSlugShaped("orderhook-base"), true);
  assert.equal(isSlugShaped("Unlisted%20hook"), false);
  assert.equal(isSlugShaped("a--b"), false);
  assert.equal(isSlugShaped("x".repeat(121)), false);
});
