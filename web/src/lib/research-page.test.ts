import { test } from "node:test";
import assert from "node:assert/strict";
import { SAMPLE_PROJECTS } from "../fixtures/sample";
import { applyTab, listFor } from "./feed";

test("pipeline listing decisions retain dormant and unknown history on direct pages, rows, address search and watchlists", async () => {
  delete process.env.DATABASE_URL;
  process.env.BAIT_FIXTURES = "1";
  const originalLength = SAMPLE_PROJECTS.length;
  const dormant = structuredClone(SAMPLE_PROJECTS[0]);
  dormant.slug = "regression-dormant"; dormant.name = "Regression dormant"; dormant.isSpam = false; dormant.isWash = false;
  dormant.hasHistoricalUsage = true; dormant.activityStatus = "dormant";
  dormant.metrics.vol7d = 0; dormant.metrics.swaps7d = 0; dormant.isNew = false; dormant.scoreTrending = 0;
  const unknown = structuredClone(dormant);
  unknown.slug = "regression-unknown"; unknown.name = "Regression unknown"; unknown.activityStatus = "unknown";
  unknown.metrics.vol7d = null; unknown.metrics.swaps7d = null;
  const dust = structuredClone(dormant);
  dust.slug = "regression-dust"; dust.name = "Regression dust"; dust.hasHistoricalUsage = false; dust.activityStatus = "unknown"; dust.isSpam = true;
  dust.hooks.forEach(h => { h.hooklisted = true; h.verifiedSource = true; });
  SAMPLE_PROJECTS.push(dormant, unknown, dust);
  try {
    const { getProjectPage, getHookIndex } = await import("../db/queries");
    const { project, rows } = await getProjectPage(dormant.slug);
    assert.equal(project?.isDormant, true);
    const uncertain = await getProjectPage(unknown.slug);
    assert.equal(uncertain.project?.isDormant, false);
    assert.equal(uncertain.project?.metrics.vol7d, null);
    assert.equal((await getProjectPage(dust.slug)).project, null);
    assert.equal(rows.some(r => r.slug === dust.slug), false);
    const hooks = await getHookIndex();
    assert.equal(dust.slug in hooks, false);
    for (const p of [dormant, unknown]) {
      assert.ok(applyTab(rows, { tab: "all" }).some(r => r.slug === p.slug));
      assert.ok(listFor(rows, { tab: "trending" }, { search: p.name }).some(r => r.slug === p.slug));
      assert.ok(listFor(rows, { tab: "trending" }, { search: p.hooks[0].address, hooks }).some(r => r.slug === p.slug));
      assert.ok(listFor(rows, { tab: "new" }, { search: "", watchlist: [p.slug] }).some(r => r.slug === p.slug));
      assert.equal(applyTab(rows, { tab: "trending" }).some(r => r.slug === p.slug), false);
      assert.equal(applyTab(rows, { tab: "new" }).some(r => r.slug === p.slug), false);
    }
  } finally { SAMPLE_PROJECTS.splice(originalLength); }
});
