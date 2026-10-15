import { test } from "node:test";
import assert from "node:assert/strict";
import { applyTab, feedHrefAt, feedSeed, LIST_PAGE, listFor, sameQuery } from "./feed";
import { loadRows, packRows, peekRows, resetRows, ROW_KEYS, rowsVersion, unpackRows } from "./rows";
import type { RowView } from "./types";

const row = (over: Partial<RowView>): RowView => ({
  slug: "x", name: "X", whatLine: "", color: "#000", category: "other", secondaryCategories: [], chains: [8453],
  firstSeenAt: "2026-10-01T00:00:00Z", isNew: false, isWash: false, isDormant: false, scoreTrending: 0, vol7d: 0, vol7dPrev: 0, vol24h: null,
  tvlUsd: 0, routable: "unknown", ...over,
});

const many = (n: number) =>
  Array.from({ length: n }, (_, i) => row({ slug: `p${i}`, name: `Hook ${i}`, vol7d: 1_000_000 - i, scoreTrending: i % 7, category: i % 2 ? "launchpad" : "dynamic-fee" }));

test("packed rows round-trip, and every packed row is much smaller than the object it replaces", () => {
  const rows = [row({ slug: "a", name: "Angstrom", whatLine: "Batch auction", chains: [1, 8453], vol7d: null, secondaryCategories: ["orders"] }), row({ slug: "b", isNew: true, vol24h: 12 })];
  const packed = JSON.parse(JSON.stringify(packRows(rows)));
  assert.deepEqual(unpackRows(packed), rows);
  const objectBytes = JSON.stringify(rows).length / rows.length;
  const packedBytes = (JSON.stringify(packed).length - JSON.stringify(ROW_KEYS).length) / rows.length;
  assert.ok(packedBytes < objectBytes * 0.6, `${packedBytes} vs ${objectBytes} bytes per row`);
});

test("unpack follows the server's key list, so a newer server cannot misalign an open tab", () => {
  const p = { keys: ["name", "slug", "extra"], rows: [["Angstrom", "angstrom", 1]] };
  assert.deepEqual(unpackRows(p), [{ name: "Angstrom", slug: "angstrom", extra: 1 }] as unknown as RowView[]);
  assert.throws(() => unpackRows({} as never));
});

test("the rows version changes with the content and the count, and only then", () => {
  const a = packRows(many(3));
  assert.equal(rowsVersion(a), rowsVersion(packRows(many(3))));
  assert.notEqual(rowsVersion(a), rowsVersion(packRows([...many(2), row({ slug: "p2", name: "Hook 2", vol7d: 5 })])));
  assert.match(rowsVersion(packRows(many(250))), /\.250$/);
});

test("the row set loads once per version, a failure is retried, and a new version fetches again", async () => {
  resetRows();
  const urls: string[] = [];
  let fail = true;
  const fetcher = (async (u: string) => {
    urls.push(u);
    if (fail) {
      fail = false;
      return new Response("down", { status: 503 });
    }
    return Response.json(packRows(many(2)));
  }) as unknown as typeof fetch;
  await assert.rejects(loadRows("v1", fetcher));
  assert.equal(peekRows(), undefined);
  assert.equal((await loadRows("v1", fetcher)).length, 2);
  await loadRows("v1", fetcher);
  assert.equal(peekRows()?.length, 2);
  await loadRows("v2", fetcher);
  assert.deepEqual(urls, ["/api/rows?v=v1", "/api/rows?v=v1", "/api/rows?v=v2"]);
  resetRows();
});

test("a page seeds one page of its own view, plus what the list needs before the full set lands", () => {
  const rows = many(250);
  const seed = feedSeed(rows, { tab: "all" }, "v");
  assert.equal(seed.rows.length, LIST_PAGE);
  assert.equal(seed.total, 250);
  assert.equal(seed.count, 250);
  assert.deepEqual(seed.cats, ["dynamic-fee", "launchpad"]);
  assert.deepEqual(seed.rows.map((r) => r.slug), applyTab(rows, { tab: "all" }).slice(0, LIST_PAGE).map((r) => r.slug));
  const launch = feedSeed(rows, { tab: "all", category: "launchpad" }, "v");
  assert.equal(launch.total, 125);
  assert.ok(launch.rows.every((r) => r.category === "launchpad"));
});

test("listFor: a tab, a search across every tab, or the watchlist by 7-day volume", () => {
  const rows = [
    row({ slug: "hot", name: "Hot", scoreTrending: 5, vol7d: 50_000, vol7dPrev: 10_000 }),
    row({ slug: "quiet", name: "Quiet hook", vol7d: 900 }),
    row({ slug: "big", name: "Big hook", vol7d: 2_000_000, chains: [1] }),
  ];
  assert.deepEqual(listFor(rows, { tab: "trending" }, { search: "" }).map((r) => r.slug), ["hot"]);
  assert.deepEqual(listFor(rows, { tab: "trending" }, { search: " hook" }).map((r) => r.slug), ["big", "quiet"]);   // search spans All
  assert.deepEqual(listFor(rows, { tab: "trending", chain: 1 }, { search: "hook" }).map((r) => r.slug), ["big"]);    // filters still apply
  assert.deepEqual(listFor(rows, { tab: "new" }, { search: "", watchlist: ["quiet", "big"] }).map((r) => r.slug), ["big", "quiet"]);
  assert.deepEqual(listFor(rows, { tab: "all" }, { search: "", watchlist: [] }), []);
});

test("tab and filter links stay on the open project, else go to the feed", () => {
  assert.equal(feedHrefAt("/", { tab: "all", category: "launchpad" }), "/?tab=all&category=launchpad");
  assert.equal(feedHrefAt("/p/angstrom", { tab: "new", chain: 8453 }), "/p/angstrom?tab=new&chain=base");
  assert.equal(feedHrefAt("/leaders", { tab: "trending" }), "/");
  assert.equal(feedHrefAt(null, { tab: "trending", chain: 1 }), "/?chain=ethereum");
  assert.ok(sameQuery({ tab: "trending" }, { tab: "trending", chain: undefined }));
  assert.ok(!sameQuery({ tab: "trending" }, { tab: "all" }));
});
