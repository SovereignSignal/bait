// Opt-in integration test. BAIT_TEST_DATABASE_URL must point to a disposable local PostgreSQL instance.
// Each run creates and drops its own schema, applies the real migrations and exercises the public query surface.
import { test } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import postgres from "postgres";
import { applyTab, listFor } from "./feed";

const testUrl = process.env.BAIT_TEST_DATABASE_URL;

test("Postgres readiness requires migration 0016 and project pages preserve unknown/zero windows", { skip: !testUrl }, async () => {
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
    process.env.DATABASE_URL = isolatedUrl;
    delete process.env.BAIT_FIXTURES;
    const { GET: ready } = await import("../app/api/ready/route");
    const dir = new URL("../../../db/migrations/", import.meta.url);
    for (const file of readdirSync(dir).filter(f => f.endsWith(".sql")).sort()) {
      if (file === "0016_historical_usage.sql") {
        const beforeMigration = await ready();
        assert.equal(beforeMigration.status, 503);
        assert.equal(beforeMigration.headers.get("Cache-Control"), "no-store");
        assert.deepEqual(await beforeMigration.json(), { ok: false, reason: "database or required schema unavailable" });
      }
      await sql.unsafe(readFileSync(new URL(file, dir), "utf8"));
    }
    // An empty database is ready once its schema is compatible; no run receipt or fresh data is required.
    const migrated = await ready();
    assert.equal(migrated.status, 200);
    assert.equal(migrated.headers.get("Cache-Control"), "no-store");
    assert.deepEqual(await migrated.json(), { ok: true });
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
    // A Base pool (DESIGN.md "Daily volume: a day without a reading is blank, not $0"): the bars draw what was read, $0
    // only where score measured it (project_daily.vol_24h by window_end), and a gap for every other day.
    {
      const id = 100;
      const hook = `0x${String(id).padStart(40, "0")}`;
      const pool = `0x${String(id).padStart(64, "0")}`;
      await sql`insert into projects (id, slug, name, first_seen_at) values (${id}, 'base-gaps', 'base-gaps', '2026-01-01')`;
      await sql`insert into hooks (address, chain_id, project_id, first_seen_at, hooklisted, verified_source)
                values (${hook}, 8453, ${id}, '2026-01-01', true, true)`;
      await sql`insert into pools (pool_id, chain_id, hook_address, token0, token1, fee, created_at)
                values (${pool}, 8453, ${hook}, ${sql.json({ id: "0x0", symbol: "ETH" })}, ${sql.json({ id: "0x1", symbol: "USD" })}, 3000,
                  '2026-01-01T00:00:00Z')`;
      // day -> score's vol_24h for window_end = day - 1; 10-10 is a row scored before window_end was stored
      const scored: Array<[string, string | null, number | null]> = [
        ["2026-10-02", "2026-10-01", 0], ["2026-10-10", null, 0], ["2026-10-11", "2026-10-10", null],
        ["2026-10-12", "2026-10-11", null], ["2026-10-13", "2026-10-12", 0], ["2026-10-14", "2026-10-13", 21_000],
        ["2026-10-15", "2026-10-14", 0],
      ];
      for (const [day, windowEnd, vol] of scored) {
        await sql`insert into project_daily (project_id, day, window_end, tvl_usd, vol_24h, vol_7d, pools_n, is_spam, activity_status, score_trending)
                  values (${id}, ${day}, ${windowEnd}, 1000, ${vol}, null, 1, false, 'unknown', 0)`;
      }
      // closed day -> the pool's volume_usd_day (null: a row whose volume is unknown); 10-11 and 10-14 have no row
      const read: Array<[string, number | null]> = [["2026-10-08", 500], ["2026-10-09", 0], ["2026-10-10", 0], ["2026-10-12", null], ["2026-10-13", 21_000]];
      for (const [day, vol] of read) {
        await sql`insert into pool_snapshots (pool_id, chain_id, day, tvl_usd, volume_usd_cum, volume_usd_day, tx_count_day)
                  values (${pool}, 8453, ${day}, 1000, 21500, ${vol}, null)`;
      }
    }
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
    // Hypothetical scorer output after accepted historical evidence: the importer/scorer
    // is tested in the pipeline. Here, no recent snapshots must remain unknown and discoverable.
    const { project: historicalUnknown, rows } = await getProjectPage("missing");
    assert.ok(historicalUnknown);
    assert.equal(historicalUnknown.hasHistoricalUsage, true);
    assert.equal(historicalUnknown.activityStatus, "unknown");
    assert.equal(historicalUnknown.isSpam, false);
    assert.equal(historicalUnknown.isDormant, false);
    assert.equal(historicalUnknown.isNew, false);
    assert.equal(historicalUnknown.scoreTrending, 0);
    assert.equal(historicalUnknown.metrics.vol7d, null);
    assert.equal(historicalUnknown.metrics.vol7dPrev, null);
    assert.equal(historicalUnknown.metrics.swaps7d, null);
    assert.equal(historicalUnknown.usage.vol24h, null);
    assert.equal(historicalUnknown.usage.tx24h, null);
    assert.equal(historicalUnknown.pools[0]?.tx7d, null);
    const hooks = await getHookIndex();
    assert.deepEqual(hooks.missing, [historicalUnknown.hooks[0].address]);
    assert.ok(applyTab(rows, { tab: "all" }).some(r => r.slug === "missing"));
    for (const search of [historicalUnknown.name, historicalUnknown.hooks[0].address]) {
      assert.ok(listFor(rows, { tab: "trending" }, { search, hooks }).some(r => r.slug === "missing"));
    }
    assert.ok(listFor(rows, { tab: "new" }, { search: "", watchlist: ["missing"] }).some(r => r.slug === "missing"));
    for (const tab of ["new", "trending"] as const) {
      assert.equal(applyTab(rows, { tab }).some(r => r.slug === "missing"), false);
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
    const gaps = (await getProjectPage("base-gaps")).project!;
    assert.equal(gaps.through, "2026-10-14");
    assert.deepEqual(gaps.series, [
      0,                                       // 10-01: no row, score measured $0 (first day: the run on 10-02)
      null, null, null, null, null, null,      // 10-02..10-07: no row, never scored
      500,                                     // 10-08: read, score has no verdict: a lower bound, drawn
      0,                                       // 10-09: $0 read, $0 measured (a row without window_end: day - 1)
      null,                                    // 10-10: $0 read, score could not vouch for it
      null,                                    // 10-11: no row, no verdict: the Base gap that used to draw $0
      null,                                    // 10-12: the row's volume is unknown, whatever score said
      21_000,                                  // 10-13
      0,                                       // 10-14: no row, score measured $0 (last day: the run on `latest`)
    ]);
  } finally {
    if (oldDatabase === undefined) delete process.env.DATABASE_URL; else process.env.DATABASE_URL = oldDatabase;
    if (oldFixtures === undefined) delete process.env.BAIT_FIXTURES; else process.env.BAIT_FIXTURES = oldFixtures;
    await sql.end();
    await admin`drop schema ${admin(schema)} cascade`;
    await admin.end();
  }
});
