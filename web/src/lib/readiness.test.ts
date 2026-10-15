import { test } from "node:test";
import assert from "node:assert/strict";

test("readiness stays unavailable without a configured database, including fixture mode", async () => {
  const previousDatabase = process.env.DATABASE_URL;
  const previousFixtures = process.env.BAIT_FIXTURES;
  delete process.env.DATABASE_URL;
  process.env.BAIT_FIXTURES = "1";
  try {
    const { GET } = await import("../app/api/ready/route");
    const response = await GET();
    assert.equal(response.status, 503);
    assert.equal(response.headers.get("Cache-Control"), "no-store");
    assert.deepEqual(await response.json(), { ok: false, reason: "no DATABASE_URL" });
  } finally {
    if (previousDatabase === undefined) delete process.env.DATABASE_URL; else process.env.DATABASE_URL = previousDatabase;
    if (previousFixtures === undefined) delete process.env.BAIT_FIXTURES; else process.env.BAIT_FIXTURES = previousFixtures;
  }
});
