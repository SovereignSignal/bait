import { test } from "node:test";
import assert from "node:assert/strict";
import { isSlugShaped, isUnlisted, isUnnamed, unlistedLabel } from "./unlisted";
import { applyTab, movers } from "./feed";
import type { RowView } from "./types";

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

test("only slug-shaped paths reach the alias lookup", () => {
  assert.equal(isSlugShaped("unlisted-hook-0x7bc2-2aec"), true);
  assert.equal(isSlugShaped("orderhook-base"), true);
  assert.equal(isSlugShaped("Unlisted%20hook"), false);
  assert.equal(isSlugShaped("a--b"), false);
  assert.equal(isSlugShaped("x".repeat(121)), false);
});

test("isUnnamed: placeholder and pair labels are unnamed; chosen names are not", () => {
  assert.equal(isUnnamed("Unlisted hook 0x58a6…94c0"), true);
  assert.equal(isUnnamed("Unlisted hook family 0x58a6…"), true);
  assert.equal(isUnnamed("GULD/ELEVATE hook · 0xaa67…0fc0"), true);
  assert.equal(isUnnamed("Aegis"), false);
  assert.equal(isUnnamed("PadHook (Ethereum)"), false);
  assert.equal(isUnnamed("Zora Post Hook v2.3.0 (Base)"), false);
});

test("unnamed rows stay out of Trending, New and the movers, but are listed in All", () => {
  const row = (slug: string, name: string, over: Partial<RowView> = {}): RowView => ({
    slug, name, whatLine: "", color: "#000", category: "other", secondaryCategories: [], chains: [1], firstSeenAt: "2026-10-12T00:00:00Z",
    isNew: false, isWash: false, scoreTrending: 50, vol7d: 500_000, vol7dPrev: 1_000, vol24h: 70_000, tvlUsd: 1_000, routable: "unknown", audited: false, ...over,
  });
  const rows = [row("a", "Aegis"), row("u", "Unlisted hook 0x58a6…94c0", { scoreTrending: 99, isNew: true }), row("p", "POP/USDT hook · 0xca9d…5800", { isNew: true })];
  assert.deepEqual(applyTab(rows, { tab: "trending" }).map((r) => r.slug), ["a"]);
  assert.deepEqual(applyTab(rows, { tab: "new" }).map((r) => r.slug), []);
  assert.deepEqual(movers(rows).map((r) => r.slug), ["a"]);
  assert.equal(applyTab(rows, { tab: "all" }).length, 3);
});
