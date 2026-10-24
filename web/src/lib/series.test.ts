import { test } from "node:test";
import assert from "node:assert/strict";
import { dailySeries, SERIES_DAYS, seriesGapNote, shiftDay } from "./present";

// The project page's daily volume bars (DESIGN.md "Daily volume: a day without a reading is blank, not $0"). On
// 2026-10-24, 140 of 377 live project pages ended on a $0 bar under a "–" 1d: a day with no snapshot row was drawn as 0.
const END = "2026-10-23";
const day = (back: number) => shiftDay(END, -back); // back = 0 is the last bar
const at = (s: (number | null)[], back: number) => s[SERIES_DAYS - 1 - back];
const map = (entries: Array<[number, number | null]>) => new Map(entries.map(([back, v]) => [day(back), v]));

test("shiftDay moves UTC days across month and year ends", () => {
  assert.equal(shiftDay("2026-10-01", -1), "2026-09-30");
  assert.equal(shiftDay("2026-12-31", 1), "2027-01-01");
  assert.equal(shiftDay(END, 0), END);
});

test("dailySeries: 14 closed days ending on `end`, oldest first", () => {
  const observed = map([[13, 5], [0, 7]]);
  const s = dailySeries(END, observed, new Map());
  assert.equal(s.length, SERIES_DAYS);
  assert.equal(s[0], 5);
  assert.equal(s[SERIES_DAYS - 1], 7);
});

test("dailySeries: no rows and no verdict is unknown, never $0 (pushbasedynamicfeehook's ten $0 bars on Base)", () => {
  const observed = map([[12, 55_236], [11, 47_722], [10, 21_496]]);
  const s = dailySeries(END, observed, map([[0, null], [1, null]]));
  assert.deepEqual(s.slice(0, 4), [null, 55_236, 47_722, 21_496]);
  assert.deepEqual(s.slice(4), Array(10).fill(null));
  assert.equal(s.includes(0), false);
});

test("dailySeries: a $0 sum is $0 only when score measured $0 that day", () => {
  const observed = map([[2, 0], [1, 0], [0, 0]]);
  const s = dailySeries(END, observed, map([[2, 0], [1, null]]));   // day 0: no project_daily row
  assert.deepEqual([at(s, 2), at(s, 1), at(s, 0)], [0, null, null]);
});

test("dailySeries: no rows but a measured $0 (a ready chain where nothing traded) is $0", () => {
  const s = dailySeries(END, map([[5, 10], [4, 20]]), map([[0, 0]]));
  assert.equal(at(s, 0), 0);
});

test("dailySeries: a positive observed sum is drawn even when score has no verdict (a lower bound)", () => {
  const s = dailySeries(END, map([[1, 873.11], [0, 4.72]]), map([[1, null]]));
  assert.deepEqual([at(s, 1), at(s, 0)], [873.11, 4.72]);
});

test("dailySeries: rows whose volume is all unknown stay unknown, even beside a measured $0", () => {
  const s = dailySeries(END, map([[1, null], [0, 12]]), map([[1, 0]]));
  assert.equal(at(s, 1), null);
});

test("dailySeries: a day score never scored (no project_daily row) is unknown", () => {
  const s = dailySeries(END, map([[3, 0], [2, 0], [1, 0], [0, 0]]), map([[3, 0], [1, 0], [0, 0]]));
  assert.deepEqual([at(s, 3), at(s, 2), at(s, 1), at(s, 0)], [0, null, 0, 0]);
});

test("dailySeries: fewer than two days with rows is no history yet", () => {
  assert.deepEqual(dailySeries(END, new Map(), map([[0, 0]])), []);
  assert.deepEqual(dailySeries(END, map([[0, 500]]), map([[0, 500], [1, 0]])), []);
  assert.equal(dailySeries(END, map([[1, null], [0, null]]), new Map()).length, SERIES_DAYS);
});

test("seriesGapNote: only when a day is unknown; names the chains still indexing", () => {
  assert.equal(seriesGapNote([1, 0, 2], []), null);
  assert.equal(seriesGapNote([1, 0, 2], [8453]), null);
  assert.equal(seriesGapNote([1, null], []), "Days without a complete reading are left blank; blank is unknown, not $0.");
  assert.equal(
    seriesGapNote([null, null], [8453]),
    "Days without a complete reading are left blank; blank is unknown, not $0. Base is still indexing, so Base pools may be missing from these totals.",
  );
  assert.match(seriesGapNote([null, 3], [8453, 4663]) ?? "", /Base and Robinhood are still indexing, so their pools may be missing from these totals\.$/);
});
