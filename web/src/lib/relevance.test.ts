import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import { receiptRelevant } from "./relevance";

const cases = JSON.parse(readFileSync(resolve(__dirname, "../../../fixtures/relevance_cases.json"), "utf8"));

test("receipt relevance matches the shared cases", () => {
  for (const c of cases) assert.equal(receiptRelevant(c.project, c.title, c.source), c.keep, `${c.project}: ${c.title}`);
});
