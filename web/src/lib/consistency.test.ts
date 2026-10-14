// One project, one set of figures across pages: names, counts, TVL and audit badges (demo smoke, 2026-10-14).
import { test } from "node:test";
import assert from "node:assert/strict";
import { auditLinks, digestIntro, poolTvlNote, toRow, withCurrentNames } from "./present";
import type { DigestItem } from "./types";

const item = (slug: string, name: string, kind: DigestItem["kind"] = "trending"): DigestItem => ({ slug, name, kind, line: "", vol7d: 1 });

test("week page names come from today's list, the same label /leaders and the project page show", () => {
  const names = new Map([["unlisted-hook-family-0x0f48", "LUCKYKEY/WETH hook family · 0x0f48…"]]);
  const out = withCurrentNames([item("unlisted-hook-family-0x0f48", "Unlisted hook family 0x0f48…"), item("gone", "Gone Hook")], names);
  assert.deepEqual(out.map((i) => i.name), ["LUCKYKEY/WETH hook family · 0x0f48…", "Gone Hook"]);   // a delisted slug keeps its stored name
  assert.equal(digestIntro([], out, { projects: 2254, traded: 820, tvl_usd: 1e6, vol_7d: 2e6 }).includes("LUCKYKEY/WETH hook family · 0x0f48… led trending."), true);
});

test("digest intro counts what /leaders counts; an older digest says what its number is", () => {
  assert.equal(
    digestIntro([item("a", "A", "new")], [], { projects: 2254, traded: 820, tvl_usd: 1_000_000, vol_7d: 5_000_000 }),
    "1 new project cleared the bar. 820 projects traded this week, $1M TVL, $5M 7d volume.",
  );
  assert.equal(
    digestIntro([], [], { projects: 2254, tvl_usd: 1_000_000, vol_7d: 5_000_000 }),
    "No new projects cleared the bar. 2,254 projects tracked (live pools, not wash-like), $1M TVL, $5M 7d volume.",
  );
});

test("pool TVL note: says where a header TVL the rows do not add up to comes from", () => {
  assert.equal(poolTvlNote(9_800, [{ tvlUsd: 9_800 }]), null);
  assert.equal(poolTvlNote(9_800, [{ tvlUsd: 9_790 }]), null);                       // rounding between the reads
  assert.equal(poolTvlNote(0, []), null);
  assert.equal(poolTvlNote(9_800, [{ tvlUsd: null }]), "TVL $9.8K is the sum over all pools; none of it is in the pools listed here.");
  assert.equal(poolTvlNote(10_000, [{ tvlUsd: 4_000 }, { tvlUsd: null }]), "TVL $10K is the sum over all pools; $6K of it is in pools not listed here.");
});

test("audit badge only for a real link that belongs to the project", () => {
  const team = { team: "Acme Labs", audit_url: "https://acme.xyz/audit.pdf" };
  assert.deepEqual(auditLinks(["https://h.org/a.pdf", "https://h.org/a.pdf", null, ""], null, false), ["https://h.org/a.pdf"]);
  assert.deepEqual(auditLinks([], team, false), ["https://acme.xyz/audit.pdf"]);
  // an unnamed hook is an address with no team: whatever Parallel returned is someone else's audit
  assert.deepEqual(auditLinks([], team, true), []);
  assert.deepEqual(auditLinks([], { team: "", audit_url: "https://x.org/audit" }, false), []);
  // placeholders and non-web values are not links
  assert.deepEqual(auditLinks(["N/A", "none", "javascript:alert(1)"], { team: "Acme", audit_url: "Not found" }, false), []);
  // Uniswap's own v4 audits cover the PoolManager, not this hook
  assert.deepEqual(auditLinks([], { team: "Acme", audit_url: "https://github.com/Uniswap/v4-core/tree/main/docs/security/audits" }, false), []);
});

test("the list row's audited flag follows the link, not the count", () => {
  const p = {
    slug: "x", name: "X", whatLine: "", color: "#000", category: "other", secondaryCategories: [], chains: [8453], firstSeenAt: "",
    isNew: false, isWash: false, scoreTrending: 0, isFactory: false,
    metrics: { tvlUsd: 0, vol7d: 0, vol7dPrev: 0, swaps7d: null, poolsN: 1, hooksN: 1 },
    usage: { vol24h: null },
    signals: { routable: "unknown", auditCount: 1, auditUrl: null },
  } as unknown as Parameters<typeof toRow>[0];
  assert.equal(toRow(p).audited, false);
  assert.equal(toRow({ ...p, signals: { ...p.signals, auditUrl: "https://a.org/x" } }).audited, true);
});
