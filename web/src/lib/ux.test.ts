import { test } from "node:test";
import assert from "node:assert/strict";
import { clip, dayLabel, deltaWords, fmtDelta, nextDigestDay, NONE, usd, usdWhole } from "./format";
import { activePool, dayWindow, firstWebUrl, hookKeys, poolsNote, poolVolume, rowLabel, rowMetric, seriesLabels } from "./present";
import { emptyCopy, moversFor, searchRows } from "./feed";
import { indexerLine, liveDotOn, quietNote, tradesNote } from "./trades";
import { respond, respondJson } from "./http";
import { gunzipSync } from "node:zlib";
import { loadHookIndex, resetHookIndex } from "./hook-index";

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
test("rowMetric: the closed day only when asked for and known", () => {
  assert.deepEqual(rowMetric({ vol24h: 5, vol7d: 70 }, true), { v: 5, win: "1d" });
  assert.deepEqual(rowMetric({ vol24h: null, vol7d: 70 }, true), { v: 70, win: "7d" });
  assert.deepEqual(rowMetric({ vol24h: 5, vol7d: 70 }, false), { v: 70, win: "7d" });
});
test("unpriced swaps ahead of a copied snapshot do not render zero USD; positive observed weekly volume remains visible", () => {
  assert.deepEqual(poolVolume({ vol24h: null, tx7d: null }), { text: NONE });
  assert.equal(usd(rowMetric({ vol24h: null, vol7d: null }, true).v), NONE);
  assert.deepEqual(rowMetric({ vol24h: null, vol7d: 125 }, true), { v: 125, win: "7d" });
  assert.deepEqual(poolVolume({ vol24h: 0, tx7d: 0 }), { text: "$0" });
});
test("moversFor: chain filter first, then the cap, order kept", () => {
  const rows = [{ slug: "a", chains: [1] }, { slug: "b", chains: [8453] }, { slug: "c", chains: [1, 8453] }] as never[];
  assert.deepEqual(moversFor(rows, 8453, 1).map((x: { slug: string }) => x.slug), ["b"]);
  assert.deepEqual(moversFor(rows, 10), []);
});
test("rowLabel reads rank, name, category, chain, window, direction", () => {
  const r = { name: "StablePairHook", category: "dynamic-fee" as const, chains: [1 as const], isNew: false, vol7d: 10_600_000, vol7dPrev: 1_000_000 };
  assert.equal(rowLabel(r, 1, { v: r.vol7d, win: "7d" }), "#1 StablePairHook, Dynamic fee on Ethereum, $10.6M 7-day volume, up 960 percent");
  assert.equal(rowLabel({ ...r, isNew: true, category: "other" }, undefined, { v: 5000, win: "1d" }), "StablePairHook, Ethereum, $5K 1-day volume, new");
  assert.match(rowLabel({ ...r, isWash: true, washHistorical: true }, undefined, { v: 0, win: "7d" }), /historical wash-like volume$/);
  assert.match(rowLabel({ ...r, isDormant: true }, undefined, { v: 0, win: "7d" }), /dormant in reported window$/);
  assert.match(rowLabel({ ...r, activityStatus: "unknown" }, undefined, { v: null, win: "7d" }), /recent activity unknown$/);
});
test("emptyCopy names the filter that matched nothing", () => {
  assert.equal(emptyCopy({ tab: "new", category: "launchpad", chain: 8453 }), "No new Launchpad hooks on Base.");
  assert.equal(emptyCopy({ tab: "all", chain: 8453 }), "No hooks on Base.");
  assert.equal(emptyCopy({ tab: "verified", category: "oracle" }), "No routable Oracle hooks.");
});
test("tradesNote separates nothing-traded from feed-unavailable", () => {
  assert.equal(tradesNote("empty", 0), "No trades over $10 right now.");
  assert.equal(tradesNote("rate_limited", 0), "Trade feed unavailable, retrying in 60s.");
  assert.equal(tradesNote("unknown_pool", 0), "No live trade data for this pool.");
  assert.equal(tradesNote("upstream_error", 3), null);
  assert.equal(tradesNote("timeout", 0), "Trade feed did not answer, retrying in 60s.");   // never "Loading…" forever
});

test("quietNote names the newest trade's age once the feed has gone an hour without one", () => {
  const now = Date.parse("2026-10-14T02:00:00Z");
  // Hookr MOW/WETH on Robinhood 10/14: GeckoTerminal's newest trade over $10 was 15:52 the day before
  const quiet = [{ ts: "2026-10-13T15:44:33Z" }, { ts: "2026-10-13T15:52:49Z" }];
  assert.equal(quietNote(quiet, now), "No trades over $10 in the last hour; the newest is 10h old.");
  assert.equal(quietNote([{ ts: "2026-10-14T01:30:00Z" }, ...quiet], now), null);
  assert.equal(quietNote([], now), null);
  assert.equal(liveDotOn("ok", quiet, now), false);
});

test("indexerLine shows the pool's closed-day figure, or nothing without one", () => {
  assert.equal(indexerLine({ vol24h: 17_100_000, wallets24h: 809, walletsExact: true }), "Indexer, last closed day: $17.1M from 809 wallets in this pool.");
  assert.equal(indexerLine({ vol24h: 292_000, wallets24h: 41, walletsExact: false }), "Indexer, last closed day: $292K from up to 41 wallets in this pool.");
  assert.equal(indexerLine({ vol24h: 5_000, wallets24h: null, walletsExact: true }), "Indexer, last closed day: $5K in this pool.");
  assert.equal(indexerLine({ vol24h: null, wallets24h: 3, walletsExact: true }), null);
});

test("a week-over-week change past +999% reads >999%, in tiles and in prose", () => {
  assert.deepEqual([fmtDelta(297_195), fmtDelta(999.4), fmtDelta(999.6), fmtDelta(-99.9)], [">999%", "+999%", ">999%", "-100%"]);
  assert.deepEqual([deltaWords(297_195), deltaWords(-24.4), deltaWords(13)], ["more than 999%", "24%", "13%"]);
  assert.equal(deltaWords(-24.5), fmtDelta(-24.5).slice(1));   // prose and tile round the same way
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

test("0x search matches hook address prefixes once the index has loaded; names match without it", () => {
  const rows = [{ slug: "a", name: "Angstrom" }, { slug: "b", name: "0xBet" }];
  const index = { a: ["0x0d62529346ac2c61f5c0582210d01214687bc0cc"], b: [] };
  assert.deepEqual(searchRows(rows, "0x0d62").map((r) => r.slug), []);             // index not loaded yet
  assert.deepEqual(searchRows(rows, "0x0D62 ", index).map((r) => r.slug), ["a"]);
  assert.deepEqual(searchRows(rows, "0xbe", index).map((r) => r.slug), ["b"]);      // a name match still counts
  assert.deepEqual(searchRows(rows, "ang").map((r) => r.slug), ["a"]);
});

test("hookKeys: lowercase, deduplicated, complete", () => {
  const many = Array.from({ length: 12 }, (_, i) => `0xAB${i.toString(16).padStart(38, "0")}`);
  assert.deepEqual(hookKeys(["0xAA", "0xaa", "0xBB"]), ["0xaa", "0xbb"]);
  assert.equal(hookKeys(many).length, 12);
});

test("the hook index loads once, and a failed load is retried on the next call", async () => {
  resetHookIndex();
  let calls = 0;
  const flaky = (async () => {
    calls++;
    return calls === 1 ? new Response("down", { status: 500 }) : Response.json({ a: ["0xaa"] });
  }) as unknown as typeof fetch;
  await assert.rejects(loadHookIndex(flaky));
  assert.deepEqual(await loadHookIndex(flaky), { a: ["0xaa"] });
  assert.deepEqual(await loadHookIndex(flaky), { a: ["0xaa"] });
  assert.equal(calls, 2);
  resetHookIndex();
});

test("wallet volumes show whole dollars under $1M, so near-equal buys read as distinct", () => {
  assert.deepEqual([usdWhole(22_126.39), usdWhole(22_009.46), usdWhole(812.6), usdWhole(2_400_000), usdWhole(null)], ["$22,126", "$22,009", "$813", "$2.4M", NONE]);
  assert.deepEqual([usdWhole(999_999.6), usdWhole(-0.4)], [usd(999_999.6), "$0"]);   // the rounded value picks the format
});

test("pools: active first, and the note says the rest are idle only when they are", () => {
  assert.equal(activePool({ tvlUsd: 0, vol24h: null }), false);
  assert.equal(activePool({ tvlUsd: 0, vol24h: 12 }), true);
  assert.equal(activePool({ tvlUsd: 0, vol24h: null, tx7d: 40 }), true);       // unknown USD volume, but it trades
  assert.equal(activePool({ tvlUsd: 0, vol24h: 0, vol7d: 5_000 }), true);      // traded earlier in the week
  assert.equal(poolsNote(1, 1, 47, 47), "46 more pools with no TVL or volume on record.");
  assert.equal(poolsNote(8, 12, 47, 47), "Top 8 of 47 pools.");
  assert.equal(poolsNote(1, 1, 50, 2400), "Top 1 of 2,400 pools.");             // pools past the 50 loaded were never looked at
  assert.equal(poolsNote(1, 1, 3, 9), "Top 1 of 9 pools.");                    // the total counts chains Bait does not track
  assert.equal(poolsNote(8, 0, 30, 30), "No pool has TVL or volume on record; showing 8 of 30.");
  assert.equal(poolsNote(2, 2, 3, 3), "1 more pool with no TVL or volume on record.");
  assert.equal(poolsNote(5, 5, 5, 5), null);
});

test("a pool with unknown USD volume shows the swaps that make it active", () => {
  assert.deepEqual(poolVolume({ vol24h: 13_543_022, tx7d: 3742 }), { text: "$13.5M" });
  assert.equal(poolVolume({ vol24h: null, tx7d: 66 }).text, "66 swaps/7d");
  assert.equal(poolVolume({ vol24h: null, tx7d: 1 }).text, "1 swap/7d");
  assert.match(poolVolume({ vol24h: null, tx7d: 66 }).note ?? "", /custom-accounting hook or unpriced swaps/);
  assert.deepEqual(poolVolume({ vol24h: null, tx7d: null }), { text: NONE });
  assert.deepEqual(poolVolume({ vol24h: 0, tx7d: 5 }), { text: "$0" });           // known zero stays $0
});

test("an audit link is only ever an http(s) URL", () => {
  assert.equal(firstWebUrl(["javascript:alert(1)", "data:text/html,x", "https://x.org/audit.pdf"]), "https://x.org/audit.pdf");
  assert.equal(firstWebUrl(["HTTP://x.org/a"]), "HTTP://x.org/a");
  assert.equal(firstWebUrl([null, 3, "ftp://x"]), null);
});

test("nextDigestDay names the coming Monday in UTC", () => {
  assert.equal(nextDigestDay(Date.parse("2026-10-10T20:00:00Z")), "Monday, Oct 12");   // Saturday
  assert.equal(nextDigestDay(Date.parse("2026-10-11T23:59:00Z")), "Monday, Oct 12");   // Sunday
  assert.equal(nextDigestDay(Date.parse("2026-10-12T03:00:00Z")), "today");
  assert.equal(nextDigestDay(Date.parse("2026-10-12T12:00:00Z")), "Monday, Oct 19");   // past the deadline: that run did not write one
  assert.equal(nextDigestDay(Date.parse("2026-10-13T03:00:00Z")), "Monday, Oct 19");
});

test("route responses are gzipped when accepted and worth it, plain otherwise", async () => {
  const big = "x".repeat(5000), req = (enc?: string) => new Request("http://t/", { headers: enc ? { "accept-encoding": enc } : {} });
  const z = respond(req("gzip, br"), big, { headers: { "Content-Type": "text/plain" } });
  assert.equal(z.headers.get("content-encoding"), "gzip");
  assert.equal(gunzipSync(Buffer.from(await z.arrayBuffer())).toString(), big);
  assert.equal(z.headers.get("vary"), "Accept-Encoding");
  assert.equal(respond(req(), big, { headers: {} }).headers.get("content-encoding"), null);
  assert.equal(respond(req("gzip"), "tiny", { headers: {} }).headers.get("content-encoding"), null);
  assert.equal(respond(req("gzip"), big, { status: 404, headers: {} }).status, 404);
  assert.equal(respondJson(req(), { a: 1 }).headers.get("content-type"), "application/json");
});

// Aegis USDC/USD₮0 on Ink, 2026-10-13 evening PT: Bait $292.3K (UTC day Oct 12; on-chain $292.8K) vs the Uniswap app's
// "$35.8K Past day" (rolling, mostly Oct 13). Same pool, different windows: the label must name the day.
test("dayWindow names the closed UTC day and says how the Uniswap app's Past day differs", () => {
  const w = dayWindow("2026-10-12");
  assert.equal(w.short, "Oct 12");
  assert.match(w.note, /^Oct 12, 00:00 to 24:00 UTC\. The Uniswap app's "Past day" is the 24 hours before you look/);
  assert.equal(dayWindow(null).short, "1d");
  assert.ok(!/24h/.test(dayWindow(null).short + dayWindow("2026-10-12").short));
});
test("seriesLabels date every bar back from the closed day, across a month boundary; relative when the day is unknown", () => {
  const l = seriesLabels(14, "2026-10-12");
  assert.equal(l.length, 14);
  assert.deepEqual([l[0], l[2], l[3], l[13]], ["Sep 29", "Oct 1", "Oct 2", "Oct 12"]);
  assert.deepEqual(seriesLabels(3, null), ["3d ago", "2d ago", "1d ago"]);
  assert.ok(!seriesLabels(14, "2026-10-12").some((x) => /yesterday/i.test(x)));
});
