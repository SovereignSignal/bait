import { test } from "node:test";
import assert from "node:assert/strict";
import { processSingleton } from "./singleton";

test("one value per name for the whole process, built once", () => {
  let built = 0;
  const a = processSingleton("test:a", () => ({ n: ++built }));
  const b = processSingleton("test:a", () => ({ n: ++built }));
  assert.equal(a, b);
  assert.equal(built, 1);
  assert.notEqual(processSingleton("test:b", () => ({ n: ++built })), a);
  assert.equal(built, 2);
});
