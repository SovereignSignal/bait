import { test } from "node:test";
import assert from "node:assert/strict";
import { clip, dayLabel, deltaWords, fmtDelta, NONE, usd } from "./format";
import { rowLabel, rowMetric } from "./present";
import { emptyCopy, moversFor } from "./feed";
import { liveDotOn, tradesNote } from "./trades";

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
test("rowLabel reads rank, name, category, chain, window, direction", () => {
  const r = { name: "StablePairHook", category: "dynamic-fee" as const, chains: [1 as const], isNew: false, vol7d: 10_600_000, vol7dPrev: 1_000_000 };
  assert.equal(rowLabel(r, 1, { v: r.vol7d, win: "7d" }), "#1 StablePairHook, Dynamic fee on Ethereum, $10.6M 7-day volume, up 960 percent");
  assert.equal(rowLabel({ ...r, isNew: true, category: "other" }, undefined, { v: 5000, win: "24h" }), "StablePairHook, Ethereum, $5K 24-hour volume, new");
});
test("emptyCopy names the filter that matched nothing", () => {
  assert.equal(emptyCopy({ tab: "new", category: "launchpad", chain: 8453 }), "No new Launchpad hooks on Base.");
  assert.equal(emptyCopy({ tab: "all", chain: 8453 }), "No hooks on Base.");
  assert.equal(emptyCopy({ tab: "verified", category: "oracle" }), "No in-app Oracle hooks.");
});
test("tradesNote separates nothing-traded from feed-unavailable", () => {
  assert.equal(tradesNote("empty", 0), "No trades over $10 right now.");
  assert.equal(tradesNote("rate_limited", 0), "Trade feed unavailable, retrying in 60s.");
  assert.equal(tradesNote("unknown_pool", 0), "No live trade data for this pool.");
  assert.equal(tradesNote("upstream_error", 3), null);
});

test("a week-over-week change past +999% reads >999%, in tiles and in prose", () => {
  assert.deepEqual([fmtDelta(297_195), fmtDelta(999.4), fmtDelta(999.6), fmtDelta(-99.9)], [">999%", "+999%", ">999%", "-100%"]);
  assert.deepEqual([deltaWords(297_195), deltaWords(-24.4), deltaWords(13)], ["more than 999%", "24%", "13%"]);
});

test("dayLabel names the UTC day", () => {
  assert.equal(dayLabel("2026-10-10"), "Oct 10");
  assert.equal(dayLabel("2026-01-01"), "Jan 1");
});

test("the live dot is green only for an answering feed whose newest trade is under an hour old", () => {
  const now = Date.parse("2026-10-10T12:00:00Z");
  const t = (iso: string) => ({ ts: iso });
  assert.equal(liveDotOn("ok", [t("2026-10-10T11:10:00Z"), t("2026-10-09T15:00:00Z")], now), true);
  assert.equal(liveDotOn("ok", [t("2026-10-09T15:00:00Z")], now), false);          // 21h old: not live
  assert.equal(liveDotOn("upstream_error", [t("2026-10-10T11:59:00Z")], now), false);
  assert.equal(liveDotOn("ok", [], now), false);
  assert.equal(liveDotOn(undefined, [t("2026-10-10T11:59:00Z")], now), false);
});
