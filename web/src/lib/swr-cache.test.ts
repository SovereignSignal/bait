import { test } from "node:test";
import assert from "node:assert/strict";
import { createSwrCache } from "./swr-cache";

const tick = () => new Promise((r) => setImmediate(r));

test("fresh hits are shared, stale hits are served while one refresh runs", async () => {
  let t = 1000;
  let calls = 0;
  const c = createSwrCache<number>({ ttlMs: 100, max: 10, now: () => t });
  const load = async () => ++calls;
  assert.equal(await c.get("k", load), 1);
  assert.equal(await c.get("k", load), 1);
  assert.equal(calls, 1);
  t += 150;                                          // expired
  const a = c.get("k", load);
  const b = c.get("k", load);
  assert.equal(await a, 1);                          // stale value, immediately
  assert.equal(await b, 1);
  await tick();
  assert.equal(calls, 2);                            // exactly one refresh
  assert.equal(await c.get("k", load), 2);           // refreshed value now current
});

test("a failed refresh keeps the stale value; a failed first load is not cached", async () => {
  let t = 0;
  const c = createSwrCache<string>({ ttlMs: 10, max: 10, now: () => t });
  assert.equal(await c.get("a", async () => "v1"), "v1");
  t = 50;
  assert.equal(await c.get("a", async () => { throw new Error("db down"); }), "v1");
  await tick();
  assert.equal(await c.get("a", async () => "v2"), "v1");   // still stale-served, refresh in flight or done
  await assert.rejects(c.get("b", async () => { throw new Error("nope"); }));
  assert.equal(c.size(), 1);
});

test("hard cap evicts the oldest entry; invalidate clears by prefix", async () => {
  const c = createSwrCache<number>({ ttlMs: 1000, max: 2 });
  await c.get("x1", async () => 1);
  await c.get("x2", async () => 2);
  await c.get("y1", async () => 3);
  assert.equal(c.size(), 2);
  c.invalidate("x");
  assert.equal(c.size(), 1);
  c.invalidate();
  assert.equal(c.size(), 0);
});
