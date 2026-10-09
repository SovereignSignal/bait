import { test } from "node:test";
import assert from "node:assert/strict";
import { createTokenBucket } from "./token-bucket";

test("token bucket allows capacity then refills over time", () => {
  let t = 0;
  const b = createTokenBucket({ capacity: 3, refillPerSec: 1, now: () => t });
  assert.deepEqual([b.take(), b.take(), b.take(), b.take()], [true, true, true, false]);
  t = 2000;
  assert.deepEqual([b.take(), b.take(), b.take()], [true, true, false]);
});
