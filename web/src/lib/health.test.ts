import { test } from "node:test";
import assert from "node:assert/strict";
import { evaluateHealth, freshness } from "./health";

const chain = (over = {}) => ({ chainId: 8453, backfilled: true, headBlock: 1000, swapBlock: 990, updatedAt: new Date("2026-10-09T08:00:00Z"), ...over });

test("after the deadline a missing or failed receipt for today is a failure", () => {
  const now = new Date("2026-10-09T08:30:00Z");
  assert.equal(evaluateHealth({ receipt: null, chains: [chain()] }, now).ok, false);
  assert.equal(evaluateHealth({ receipt: { day: "2026-10-08", exitCode: 0, softErrors: 0, codeSha: "x" }, chains: [chain()] }, now).ok, false);
  assert.equal(evaluateHealth({ receipt: { day: "2026-10-09", exitCode: 1, softErrors: 0, codeSha: "x" }, chains: [chain()] }, now).ok, false);
  assert.equal(evaluateHealth({ receipt: { day: "2026-10-09", exitCode: 0, softErrors: 2, codeSha: "x" }, chains: [chain()] }, now).ok, true);
});

test("before the deadline yesterday's good receipt is fine", () => {
  const now = new Date("2026-10-09T07:00:00Z");
  assert.equal(evaluateHealth({ receipt: { day: "2026-10-08", exitCode: 0, softErrors: 0, codeSha: "x" }, chains: [chain()] }, now).ok, true);
});

test("one stale chain fails health even when others are fresh; big swap lag is a warning", () => {
  const now = new Date("2026-10-09T08:30:00Z");
  const ok = { day: "2026-10-09", exitCode: 0, softErrors: 0, codeSha: "x" };
  const r = evaluateHealth({ receipt: ok, chains: [chain(), chain({ chainId: 1, updatedAt: new Date("2026-10-09T05:00:00Z") })] }, now);
  assert.equal(r.ok, false);
  assert.match(r.reasons.join(" "), /chain 1/);
  const w = evaluateHealth({ receipt: ok, chains: [chain({ swapBlock: 100, headBlock: 100_000 })] }, now);
  assert.equal(w.ok, true);
  assert.match(w.warnings.join(" "), /8453/);
});

test("freshness: the last closed day the figures cover, and the reason class when health fails", () => {
  const now = new Date("2026-10-09T08:30:00Z");
  const ok = { day: "2026-10-09", exitCode: 0, softErrors: 0, codeSha: "x" };
  assert.deepEqual(freshness({ receipt: ok, chains: [chain()] }, now), { through: "2026-10-08", note: null });
  const stale = chain({ chainId: 1, updatedAt: new Date("2026-10-08T19:00:00Z") });
  assert.deepEqual(freshness({ receipt: ok, chains: [chain(), stale] }, now), { through: "2026-10-08", note: "catching_up" });
  const late = { ...ok, day: "2026-10-08" };
  assert.deepEqual(freshness({ receipt: late, chains: [chain()] }, now), { through: "2026-10-07", note: "run_late" });
  assert.equal(freshness({ receipt: late, chains: [chain(), stale] }, now).note, "run_late");     // the late run dates the figures
  assert.equal(freshness({ receipt: { ...ok, exitCode: 1 }, chains: [chain(), stale] }, now).note, "run_failed");
  assert.deepEqual(freshness({ receipt: null, chains: [] }, now), { through: null, note: "run_late" });
  assert.equal(freshness({ receipt: { ...ok, day: "1970-01-01" }, chains: [chain()] }, now).through, null);  // repair-only receipt
  // a swap-lag warning alone is not a failure: no chip
  assert.equal(freshness({ receipt: ok, chains: [chain({ headBlock: 200_000, swapBlock: 1 })] }, now).note, null);
});
