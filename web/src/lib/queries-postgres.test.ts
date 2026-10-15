// Opt-in integration test. BAIT_TEST_DATABASE_URL must point to a disposable local PostgreSQL instance.
// Each run creates and drops its own schema, applies the real migrations and exercises the public query surface.
import { test } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import postgres from "postgres";

const testUrl = process.env.BAIT_TEST_DATABASE_URL;

test("Postgres project pages preserve unknown/zero windows and the pipeline listing contract", { skip: !testUrl }, async () => {
  const target = new URL(testUrl!);
  const socket = !target.hostname && process.env.PGHOST?.startsWith("/tmp/");
  assert.ok(socket || ["localhost", "127.0.0.1", "[::1]"].includes(target.hostname), "Test database must be local");
  const schema = `bait_web_test_${randomUUID().replaceAll("-", "")}`;
  const admin = postgres(testUrl!, { max: 1, prepare: false, onnotice: () => {} });
  await admin`create schema ${admin(schema)}`;
  target.searchParams.set("search_path", schema);
  const isolatedUrl = target.href;
  const sql = postgres(isolatedUrl, { max: 1, prepare: false, onnotice: () => {} });
  const oldDatabase = process.env.DATABASE_URL;
  const oldFixtures = process.env.BAIT_FIXTURES;
  try {
    const dir = new URL("../../../db/migrations/", import.meta.url);
    for (const file of readdirSync(dir).filter(f => f.endsWith(".sql")).sort()) {
      await sql.unsafe(readFileSync(new URL(file, dir), "utf8"));
    }
    const states = ["dormant", "missing", "partial", "raw-ahead", "legacy-volume", "before-created", "dust", "prior-wash",
      "raw-earlier", "raw-measured", "raw-earlier-measured", "raw-positive-week-only"];
    for (const [index, name] of states.entries()) {
      const id = index + 1;
      const hook = `0x${String(id).padStart(40, "0")}`;
      const pool = `0x${String(id).padStart(64, "0")}`;
      await sql`insert into projects (id, slug, name, first_seen_at) values (${id}, ${name}, ${name}, '2026-01-01')`;
      await sql`insert into hooks (address, chain_id, project_id, first_seen_at, hooklisted, verified_source)
                values (${hook}, 1, ${id}, '2026-01-01', true, true)`;
      await sql`insert into pools (pool_id, chain_id, hook_address, token0, token1, fee, created_at)
                values (${pool}, 1, ${hook}, ${sql.json({ id: "0x0", symbol: "ETH" })}, ${sql.json({ id: "0x1", symbol: "USD" })}, 3000,
                  ${name === "before-created" ? "2026-10-15T12:00:00Z" : "2026-01-01T00:00:00Z"})`;
      const known = name === "dormant" || name === "before-created" || name === "partial";
      const zeroSwaps = name === "dormant" || name === "before-created" || name === "legacy-volume";
      const dayVolume = name === "raw-measured" || name === "raw-earlier-measured" ? 125 : known || name === "raw-earlier" ? 0 : null;
      const weekVolume = name === "raw-positive-week-only" || dayVolume === 125 ? 125 : known ? 0 : null;
      await sql`insert into project_daily
        (project_id, day, window_end, tvl_usd, vol_24h, vol_7d, vol_7d_prev, swaps_24h, swaps_7d, pools_n, is_spam,
         is_wash, wash_historical, wash_reason, has_historical_usage, activity_status, score_trending)
        values (${id}, '2026-10-15', '2026-10-14', 0, ${dayVolume}, ${weekVolume}, null,
          ${zeroSwaps ? 0 : null}, ${zeroSwaps ? 0 : null}, 1, ${name === "dust"},
          ${name === "prior-wash"}, ${name === "prior-wash"}, ${name === "prior-wash" ? "7d" : null},
          ${name !== "dust" && name !== "before-created"},
          ${name === "dormant" ? "dormant" : name.startsWith("raw-") ? "active" : "unknown"}, 0)`;
      if (["dormant", "partial", "legacy-volume"].includes(name) || name.startsWith("raw-")) {
        for (let day = name === "partial" ? 14 : 8; day <= 14; day++) {
          const measured = ((name === "raw-measured" || name === "raw-earlier-measured") && day === 14)
            || (name === "raw-positive-week-only" && day === 10);
          await sql`insert into pool_snapshots (pool_id, chain_id, day, tvl_usd, volume_usd_cum, volume_usd_day, tx_count_day)
            values (${pool}, 1, ${`2026-10-${String(day).padStart(2, "0")}`}, 0, 0, ${name === "legacy-volume" ? null : measured ? 125 : 0}, 0)`;
        }
      }
      if (name.startsWith("raw-")) {
        await sql`insert into pool_daily_onchain (pool_id, chain_id, day, swaps, unpriced_swaps)
          values (${pool}, 1, ${name.startsWith("raw-earlier") ? "2026-10-10" : "2026-10-14"}, 2, 2)`;
      }
    }
    process.env.DATABASE_URL = isolatedUrl;
    delete process.env.BAIT_FIXTURES;
    const { getProjectPage, getHookIndex } = await import("../db/queries");
    const dormant = await getProjectPage("dormant");
    assert.equal(dormant.project?.isDormant, true);
    assert.equal(dormant.project?.pools[0]?.vol7d, 0);
    assert.equal(dormant.project?.pools[0]?.vol24h, 0);
    assert.equal(dormant.project?.pools[0]?.tx7d, 0);
    for (const name of ["missing", "legacy-volume"]) {
      const { project } = await getProjectPage(name);
      assert.ok(project);
      assert.equal(project.metrics.vol7d, null);
      assert.equal(project.pools[0]?.vol7d, null);
      assert.equal(project.pools[0]?.vol24h, null);
    }
    const partial = (await getProjectPage("partial")).project!;
    assert.equal(partial.pools[0]?.vol7d, 0); // observed partial volume, no inference about swap completeness
    assert.equal(partial.pools[0]?.tx7d, null);
    const raw = (await getProjectPage("raw-ahead")).project!;
    assert.equal(raw.activityStatus, "active");
    assert.equal(raw.pools[0]?.tx7d, null); // raw swaps invalidate copied zeros until the snapshot is reconciled
    assert.equal(raw.pools[0]?.vol7d, null);
    assert.equal(raw.pools[0]?.vol24h, null); // the unknown USD value must not look like zero volume
    const earlier = (await getProjectPage("raw-earlier")).project!;
    assert.equal(earlier.pools[0]?.vol7d, null);
    assert.equal(earlier.pools[0]?.vol24h, 0); // the mismatch is before the closed day
    for (const name of ["raw-measured", "raw-earlier-measured"]) {
      const measured = (await getProjectPage(name)).project!;
      assert.equal(measured.pools[0]?.tx7d, null);
      assert.equal(measured.metrics.vol7d, 125);
      assert.equal(measured.usage.vol24h, 125);
      assert.equal(measured.pools[0]?.vol7d, 125);
      assert.equal(measured.pools[0]?.vol24h, 125); // retain measured positive totals, including from another day
    }
    const weekOnly = (await getProjectPage("raw-positive-week-only")).project!;
    assert.equal(weekOnly.pools[0]?.vol7d, 125);
    assert.equal(weekOnly.pools[0]?.vol24h, null);
    const future = (await getProjectPage("before-created")).project!;
    assert.equal(future.pools[0]?.tx7d, 0);
    assert.equal(future.pools[0]?.vol7d, 0);
    assert.equal(future.pools[0]?.vol24h, 0);
    const priorWash = (await getProjectPage("prior-wash")).project!;
    assert.equal(priorWash.isWash, true);
    assert.equal(priorWash.washHistorical, true);
    assert.equal((await getProjectPage("dust")).project, null);
    assert.equal(dormant.rows.some(r => r.slug === "dust"), false);
    assert.equal("dust" in await getHookIndex(), false);
  } finally {
    if (oldDatabase === undefined) delete process.env.DATABASE_URL; else process.env.DATABASE_URL = oldDatabase;
    if (oldFixtures === undefined) delete process.env.BAIT_FIXTURES; else process.env.BAIT_FIXTURES = oldFixtures;
    await sql.end();
    await admin`drop schema ${admin(schema)} cascade`;
    await admin.end();
  }
});
