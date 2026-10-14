import { test } from "node:test";
import assert from "node:assert/strict";
import { applyTab, movers, weekDelta } from "./feed";
import { usd } from "./format";
import { digestItem, pace, rowMetric, walletsText, washLine } from "./present";
import type { RowView } from "./types";

const row = (over: Partial<RowView>): RowView => ({
  slug: "x", name: "X", whatLine: "", color: "#000", category: "other", secondaryCategories: [], chains: [8453],
  firstSeenAt: "2026-10-01T00:00:00Z", isNew: false, isWash: false, scoreTrending: 0, vol7d: 0, vol7dPrev: 0, vol24h: null,
  tvlUsd: 0, routable: "unknown", audited: false, ...over,
});

test("wash rows: in All after the rest, never in Trending, New or movers", () => {
  const rows = [
    row({ slug: "real", scoreTrending: 5, vol7d: 50_000, vol7dPrev: 20_000 }),
    row({ slug: "wash", isWash: true, scoreTrending: 9, vol7d: 9e8, isNew: true }),
    row({ slug: "fresh", isNew: true, vol7d: 2_000 }),
  ];
  assert.deepEqual(applyTab(rows, { tab: "trending" }).map((r) => r.slug), ["real"]);
  assert.deepEqual(applyTab(rows, { tab: "new" }).map((r) => r.slug), ["fresh"]);
  assert.deepEqual(applyTab(rows, { tab: "all" }).map((r) => r.slug), ["real", "fresh", "wash"]);
  assert.ok(!movers(rows).some((r) => r.slug === "wash"));
});
test("wash rows are listed in All only, not in the In Uniswap app tab", () => {
  const rows = [row({ slug: "real", routable: "eligible", vol7d: 5 }), row({ slug: "wash", routable: "eligible", isWash: true, vol7d: 9 })];
  assert.deepEqual(applyTab(rows, { tab: "verified" }).map((r) => r.slug), ["real"]);
});
test("weekDelta compares two closed weeks: flat reads 0, a tiny prior week is no basis", () => {
  assert.equal(weekDelta({ vol7d: 70_000, vol7dPrev: 70_000 }), 0);
  assert.equal(weekDelta({ vol7d: 70_000, vol7dPrev: 999 }), null);
});
test("pace: rolling 24h against the closed week's daily average", () => {
  assert.deepEqual([pace(10_000, 70_000), pace(5_000, 70_000), pace(null, 70_000), pace(30_000, 70_000)], [100, 50, 0, 100]);
});
test("walletsText marks an upper bound", () => {
  assert.deepEqual([walletsText(1142, false), walletsText(30, true), walletsText(30, null), walletsText(null, false)], ["≤ 1,142", "30", "30", "–"]);
});
test("washLine names every reason", () => {
  assert.equal(washLine("24h,vol_tvl"), "Wash-like volume: $250K+ in a day through 25 or fewer wallets; 7-day volume over 300x its liquidity from fewer than 50 wallets.");
  assert.equal(washLine("7d,no_liquidity"), "Wash-like volume: $1M+ in 7 days through 10 or fewer wallets; $1M+ in 7 days through a pool with $1K or less of liquidity.");
  assert.equal(washLine("unverified"), "Wash-like volume: $1M+ moved through a pool that looks idle, with no wallet data to check who moved it.");
  assert.equal(washLine(null), "Wash-like volume.");
});
test("an unknown 7-day volume renders as unknown, never trends and sorts last", () => {
  const rows = [row({ slug: "unknown", vol7d: null, vol7dPrev: null, scoreTrending: 9 }), row({ slug: "real", vol7d: 50_000, vol7dPrev: 20_000, scoreTrending: 5 })];
  assert.deepEqual(applyTab(rows, { tab: "trending" }).map((r) => r.slug), ["real"]);
  assert.deepEqual(applyTab(rows, { tab: "all" }).map((r) => r.slug), ["real", "unknown"]);
  assert.equal(weekDelta({ vol7d: null, vol7dPrev: 70_000 }), null);
  assert.deepEqual(rowMetric({ vol24h: null, vol7d: null }, false), { v: null, win: "7d" });
  assert.equal(usd(rowMetric({ vol24h: null, vol7d: null }, false).v), "–");
  assert.equal(pace(5_000, null), 0);
});
test("a digest card keeps an unknown 7-day volume unknown", () => {
  const c = { slug: "x", name: "X", what_line: null, vol_7d: null };
  assert.deepEqual(digestItem(c, "trending"), { slug: "x", name: "X", kind: "trending", line: "", vol7d: null });
  assert.equal(usd(digestItem(c, "new").vol7d), "–");
  assert.equal(digestItem({ ...c, vol_7d: 1_500 }, "new").vol7d, 1_500);
});
