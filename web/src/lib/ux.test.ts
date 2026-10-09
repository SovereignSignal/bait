import { test } from "node:test";
import assert from "node:assert/strict";
import { clip, fmtDelta, NONE, usd } from "./format";
import { rowMetric } from "./present";
import { moversFor } from "./feed";

test("usd: trimmed .0, B to one decimal, one missing glyph", () => {
  assert.deepEqual([usd(1000), usd(1040), usd(22_800), usd(10_000_000), usd(10_600_000), usd(1_210_000_000), usd(812), usd(-2500)],
    ["$1K", "$1K", "$22.8K", "$10M", "$10.6M", "$1.2B", "$812", "-$2.5K"]);
  assert.equal(usd(null), NONE);
});
test("fmtDelta: one signed format", () => {
  assert.deepEqual([fmtDelta(24.4), fmtDelta(-8.6), fmtDelta(0)], ["+24%", "-9%", "+0%"]);
});
test("clip: word boundary, ellipsis only when cut", () => {
  assert.equal(clip("short line", 90), "short line");
  const long = "Singleton dynamic-fee hook for native ETH pools on Robinhood Chain with a bounded override";
  const c = clip(long, 40);
  assert.ok(c.endsWith("…") && c.length <= 40 && long[c.length - 1] === " ");
  assert.equal(clip("x".repeat(100), 10), "x".repeat(9) + "…");
});
test("rowMetric: 24h only when asked for and known", () => {
  assert.deepEqual(rowMetric({ vol24h: 5, vol7d: 70 }, true), { v: 5, win: "24h" });
  assert.deepEqual(rowMetric({ vol24h: null, vol7d: 70 }, true), { v: 70, win: "7d" });
  assert.deepEqual(rowMetric({ vol24h: 5, vol7d: 70 }, false), { v: 70, win: "7d" });
});
test("moversFor: chain filter first, then the cap, order kept", () => {
  const rows = [{ slug: "a", chains: [1] }, { slug: "b", chains: [8453] }, { slug: "c", chains: [1, 8453] }] as never[];
  assert.deepEqual(moversFor(rows, 8453, 1).map((x: { slug: string }) => x.slug), ["b"]);
  assert.deepEqual(moversFor(rows, 10), []);
});
