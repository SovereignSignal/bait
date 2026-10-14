// The project page renders its detail and its sidebar from one list load (fixtures here; the same code path with a DB).
import { test } from "node:test";
import assert from "node:assert/strict";

test("getProjectPage: the detail's name and rank come from the sidebar rows it ships with", async () => {
  delete process.env.DATABASE_URL;
  process.env.BAIT_FIXTURES = "1";
  const { getProjectPage } = await import("../db/queries");
  const { SAMPLE_PROJECTS } = await import("../fixtures/sample");
  const { applyTab } = await import("./feed");
  const slug = SAMPLE_PROJECTS.find((p) => !p.isSpam)!.slug;
  const { project, rows } = await getProjectPage(slug);
  assert.ok(project);
  const row = rows.find((r) => r.slug === slug);
  assert.equal(project.name, row?.name);
  assert.equal(project.rank, applyTab(rows, { tab: "trending" }).find((r) => r.slug === slug)?.rank);   // the sidebar's own rank
  assert.equal((await getProjectPage("no-such-project")).project, null);
});
