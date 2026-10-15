import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadE2eFixtures } from "../fixtures/e2e";

test("candidate fixture files are ignored unless fixture mode is explicit and no database is configured", () => {
  const path = "/missing/candidate-fixture.json";
  assert.equal(loadE2eFixtures({ BAIT_E2E_FIXTURE_PATH: path }), null);
  assert.equal(loadE2eFixtures({ BAIT_FIXTURES: "1", DATABASE_URL: "postgres://unused", BAIT_E2E_FIXTURE_PATH: path }), null);
  assert.throws(() => loadE2eFixtures({ BAIT_FIXTURES: "1", BAIT_E2E_FIXTURE_PATH: path }));
});

test("explicit candidate fixture input keeps provenance and fails visibly on malformed data", () => {
  const dir = mkdtempSync(join(tmpdir(), "bait-fixtures-"));
  const path = join(dir, "candidate.json");
  try {
    const data = { observedAt: "2026-10-15T12:00:00Z", source: "Synthetic regression cases", projects: [], aliases: { old: "new" } };
    writeFileSync(path, JSON.stringify(data));
    assert.deepEqual(loadE2eFixtures({ BAIT_FIXTURES: "1", BAIT_E2E_FIXTURE_PATH: path }), data);
    writeFileSync(path, JSON.stringify({ ...data, projects: [{}] }));
    assert.throws(() => loadE2eFixtures({ BAIT_FIXTURES: "1", BAIT_E2E_FIXTURE_PATH: path }), /Invalid BAIT_E2E_FIXTURE_PATH/);
  } finally { rmSync(dir, { recursive: true }); }
});
