import { test } from "node:test";
import assert from "node:assert/strict";
import {
  apiRedirect, apiSite, findByHook, isHexAddress, lastMentionAt, listProjects, parseListQuery, projectSummaryV1, projectV1,
  REFERENCE_SITE,
} from "./api-v1";
import { SAMPLE_PROJECTS } from "../fixtures/sample";

const SITE = "https://example.test";

test("list query: defaults, clamps and ignores bad values instead of failing", () => {
  assert.deepEqual(parseListQuery(new URLSearchParams("")), { sort: "vol7d", limit: 100, offset: 0 });
  assert.deepEqual(parseListQuery(new URLSearchParams("sort=trending&limit=5000&offset=-3&chain=base&category=launchpad")), { sort: "trending", limit: 500, offset: 0, chain: 8453, category: "launchpad" });
  assert.deepEqual(parseListQuery(new URLSearchParams("sort=nope&limit=abc&chain=mars&category=nope")), { sort: "vol7d", limit: 100, offset: 0 });
});

test("list: the All order puts wash-like rows last, filters by chain and category, pages with offset", () => {
  const all = listProjects(SAMPLE_PROJECTS, { sort: "vol7d", limit: 500, offset: 0 });
  assert.equal(all.total, SAMPLE_PROJECTS.filter((p) => !p.isSpam).length);
  const washIdx = all.rows.findIndex((p) => p.isWash);
  if (washIdx >= 0) assert.ok(all.rows.slice(washIdx).every((p) => p.isWash), "wash rows trail");
  const page = listProjects(SAMPLE_PROJECTS, { sort: "vol7d", limit: 2, offset: 1 });
  assert.deepEqual(page.rows.map((p) => p.slug), all.rows.slice(1, 3).map((p) => p.slug));
  const chain = SAMPLE_PROJECTS[0].chains[0];
  assert.ok(listProjects(SAMPLE_PROJECTS, { sort: "vol7d", limit: 500, offset: 0, chain }).rows.every((p) => p.chains.includes(chain)));
  const trending = listProjects(SAMPLE_PROJECTS, { sort: "trending", limit: 500, offset: 0 });
  assert.ok(trending.rows.every((p, i) => p.rank === i + 1), "trending rows carry their site rank");
});

test("summary and detail serializers: snake_case, nulls for unknown, never NaN", () => {
  const p = SAMPLE_PROJECTS[0];
  const s = projectSummaryV1(p, SITE);
  assert.equal(s.url, `${SITE}/p/${p.slug}`);
  assert.ok(Array.isArray(s.status.wash_reasons));
  assert.equal(typeof s.metrics.pools, "number");
  for (const v of Object.values(s.metrics)) assert.ok(v === null || typeof v === "number" ? !Number.isNaN(v as number) : true);
  const d = projectV1(p, SITE);
  assert.equal(d.hooks.length, p.hooks.length);
  assert.equal(d.daily_volume_usd.length, p.series.length);
  assert.ok(d.hooks.every((h) => h.address === h.address.toLowerCase()));
  assert.equal(JSON.stringify(d).includes("NaN"), false);
});

test("daily_volume_usd: a day without a complete reading is vol_usd null on its own day, never 0", () => {
  const p = SAMPLE_PROJECTS[0];
  const series = [55_236, 47_722, 21_496, ...Array(10).fill(null), 0];
  const d = projectV1({ ...p, series, through: "2026-10-23" }, SITE);
  assert.equal(d.daily_volume_usd.length, 14);
  assert.deepEqual(d.daily_volume_usd[0], { day: "2026-10-10", vol_usd: 55_236 });
  assert.deepEqual(d.daily_volume_usd[3], { day: "2026-10-13", vol_usd: null });
  assert.deepEqual(d.daily_volume_usd[12], { day: "2026-10-22", vol_usd: null });
  assert.deepEqual(d.daily_volume_usd[13], { day: "2026-10-23", vol_usd: 0 });   // a measured $0 stays 0
  assert.deepEqual(d.daily_volume_usd.filter((x) => x.vol_usd === null).map((x) => x.day),
    ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]);
  assert.equal(JSON.stringify(d.daily_volume_usd).includes("NaN"), false);
});

test("last_mention_at is the newest receipt the response lists, null with none (never a stored date without a mention)", () => {
  const r = (publishedAt: string) => ({ url: "https://e.org/x", title: "X hook ships", source: "e.org", publishedAt });
  assert.equal(lastMentionAt([]), null);
  assert.equal(lastMentionAt([r(""), r("not a date")]), null);
  assert.equal(lastMentionAt([r("2026-10-03T00:00:00.000Z"), r(""), r("2026-10-17T00:00:00.000Z"), r("2026-09-14T00:00:00.000Z")]), "2026-10-17T00:00:00.000Z");
  const p = SAMPLE_PROJECTS[0];
  assert.equal(projectV1({ ...p, receipts: [] }, SITE).signals.last_mention_at, null);
  assert.equal(projectV1({ ...p, receipts: [r("2026-10-22T00:00:00.000Z")] }, SITE).signals.last_mention_at, "2026-10-22T00:00:00.000Z");
});

test("retired Nansen figures are gone from the summary", () => {
  const m = projectSummaryV1(SAMPLE_PROJECTS[0], SITE).metrics as Record<string, unknown>;
  assert.equal("smart_wallets_24h" in m, false);
});

test("top_wallets[] lists only the documented fields: the frozen Nansen labels and smart_money are gone", () => {
  const p = SAMPLE_PROJECTS[0];
  const w = { address: "0x" + "ab".repeat(20), chainId: p.chains[0], volumeUsd: 1200, trades: 3, buys: 2, sells: 1, lastTradeAt: null };
  const stale = { ...w, labels: ["Smart Trader"], smartMoney: true };   // the shape a wallet carried before 2026-10-24
  const d = projectV1({ ...p, topWallets: [stale] }, SITE);
  assert.deepEqual(Object.keys(d.top_wallets[0]).sort(), ["address", "buys", "chain_id", "last_trade_at", "sells", "trades", "volume_usd"]);
  assert.equal(JSON.stringify(d).includes("smart_money"), false);
});

test("hook lookup: by address regardless of case, optionally one chain; unknown addresses are empty", () => {
  const p = SAMPLE_PROJECTS.find((x) => x.hooks.length > 0)!;
  const h = p.hooks[0];
  const hits = findByHook(SAMPLE_PROJECTS, h.address.toUpperCase().replace("0X", "0x"));
  assert.ok(hits.some((m) => m.project.slug === p.slug && m.hooks.some((x) => x.address === h.address)));
  assert.equal(findByHook(SAMPLE_PROJECTS, h.address, (h.chainId + 1) as never).some((m) => m.project.slug === p.slug), false);
  assert.deepEqual(findByHook(SAMPLE_PROJECTS, "0x0000000000000000000000000000000000000000"), []);
  assert.equal(isHexAddress("0xabc"), false);
  assert.equal(isHexAddress(h.address), true);
});

test("a retired slug redirects with a relative Location, never the container's own host", () => {
  const r = apiRedirect("/api/v1/projects/launchhook-robinhood");
  assert.equal(r.status, 308);
  assert.equal(r.headers.get("Location"), "/api/v1/projects/launchhook-robinhood");
  assert.equal(r.headers.get("Cache-Control"), "no-store");
  assert.equal(r.headers.get("Access-Control-Allow-Origin"), "*");
});

test("the read API's origin: NEXT_PUBLIC_SITE_URL unset or empty (a copied .env.example) means the reference deployment", () => {
  assert.equal(apiSite(undefined), REFERENCE_SITE);
  assert.equal(apiSite(""), REFERENCE_SITE);
  assert.equal(apiSite("http://localhost:3000/"), "http://localhost:3000");
  const p = SAMPLE_PROJECTS[0];
  assert.equal(projectSummaryV1(p, apiSite("")).url, `${REFERENCE_SITE}/p/${p.slug}`, "url fields stay absolute");
});
