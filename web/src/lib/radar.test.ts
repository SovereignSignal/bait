// Run: npm test
import { test } from "node:test";
import assert from "node:assert/strict";
import { anyChainColor, anyChainName, anyExplorerTxUrl, NEUTRAL_DOT, nativeSymbol } from "./chains";
import { groupRadar, isDynamicFee, launchFee, launchLabel, launchPair, ZERO } from "./radar";
import type { RadarItemView } from "./types";

test("dynamic fee is the 0x800000 bit", () => {
  assert.equal(isDynamicFee(0x800000), true);
  assert.equal(isDynamicFee(0x800000 | 3000), true);
  assert.equal(isDynamicFee(3000), false);
  assert.equal(launchFee(3000), "0.3%");
  assert.equal(launchFee(0x800000), "dynamic");
  assert.equal(launchFee(null), "–");
});

test("pair: symbols, native 0x0 per chain, short addresses", () => {
  const usdc = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913";
  assert.equal(launchPair({ chainId: 8453, currency0: ZERO, currency1: usdc, symbol0: null, symbol1: "USDC" }), "ETH / USDC");
  assert.equal(launchPair({ chainId: 56, currency0: ZERO, currency1: usdc, symbol0: null, symbol1: null }), "BNB / 0x8335…2913");
  assert.equal(launchPair({ chainId: 42220, currency0: ZERO, currency1: usdc, symbol0: null, symbol1: null }).split(" / ")[0], "CELO");
  assert.equal(launchPair({ chainId: 999999, currency0: ZERO, currency1: usdc, symbol0: null, symbol1: null }).split(" / ")[0], "native");
  assert.equal(launchLabel({ hook: usdc, projectName: null }), "0x8335…2913");
  assert.equal(launchLabel({ hook: usdc, projectName: "Bunni" }), "Bunni");
});

test("chains outside the ten are guarded", () => {
  assert.equal(anyChainName(7777777), "Zora");
  assert.equal(anyChainName(424242), "Chain 424242");
  assert.equal(anyChainName(8453), "Base");
  assert.equal(anyChainColor(480), NEUTRAL_DOT);
  assert.equal(nativeSymbol(143), "MON");
  assert.equal(anyExplorerTxUrl(4326, "0xabc"), null);
  assert.equal(anyExplorerTxUrl(1, "0xabc"), "https://etherscan.io/tx/0xabc");
});

test("radar groups follow display order, newest first, empty kinds dropped", () => {
  const item = (url: string, kind: RadarItemView["kind"], publishedAt: string | null): RadarItemView => ({
    url, kind, title: url, summary: null, source: null, repo: null, stars: null, chainId: null, hookAddress: null,
    projectSlug: null, publishedAt, lastSeen: "2026-10-08T00:00:00Z",
  });
  const g = groupRadar([
    item("a", "repo", "2026-10-01T00:00:00Z"),
    item("b", "repo", "2026-10-05T00:00:00Z"),
    item("c", "audit", null),
  ]);
  assert.deepEqual(g.map(([k, xs]) => [k, xs.map((x) => x.url)]), [["audit", ["c"]], ["repo", ["b", "a"]]]);
  assert.deepEqual(groupRadar([item("a", "repo", null)], "audit"), []);
});
