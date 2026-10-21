import { test } from "node:test";
import assert from "node:assert/strict";
import { apiRedirect, findByHook, isHexAddress, listProjects, parseListQuery, projectSummaryV1, projectV1 } from "./api-v1";
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
