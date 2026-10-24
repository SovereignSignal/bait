// One project, one set of figures across pages: names, counts, TVL and audit badges (demo smoke, 2026-10-14).
import { test } from "node:test";
import assert from "node:assert/strict";
import { auditLinks, auditMention, topWalletText, deployerShareText, deployersOf, digestIntro, poolTvlNote, tradePool, withCurrentNames } from "./present";
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

test("pool TVL note: unknown pool TVL is not zero or evidence that the total belongs to omitted pools", () => {
  assert.equal(poolTvlNote(9_800, [{ tvlUsd: 9_800 }]), null);
  assert.equal(poolTvlNote(null, [{ tvlUsd: null }]), null);                          // project TVL unknown: no contradictory note
  assert.equal(poolTvlNote(9_800, [{ tvlUsd: 9_790 }]), null);                       // rounding between the reads
  assert.equal(poolTvlNote(0, []), null);
  assert.equal(poolTvlNote(9_800, [{ tvlUsd: null }]), "Reported project TVL is $9.8K; 1 listed pool has unknown TVL, so the listed pool total is unknown.");
  assert.equal(poolTvlNote(10_000, [{ tvlUsd: 4_000 }, { tvlUsd: null }]), "Reported project TVL is $10K; 1 listed pool has unknown TVL, so the listed pool total is unknown.");
  assert.equal(poolTvlNote(0, [{ tvlUsd: null }, { tvlUsd: null }]), "Reported project TVL is $0; 2 listed pools have unknown TVL, so the listed pool total is unknown.");
  assert.equal(poolTvlNote(9_800, [{ tvlUsd: 0 }]), "TVL $9.8K is the sum over all pools; none of it is in the pools listed here.");
  assert.equal(poolTvlNote(10_000, [{ tvlUsd: 4_000 }, { tvlUsd: 0 }]), "TVL $10K is the sum over all pools; $6K of it is in pools not listed here.");
});

test("audit badge only for a real link that belongs to the project", () => {
  const team = { team: "Acme Labs", audit_url: "https://acme.xyz/audit.pdf" };
  assert.deepEqual(auditLinks(["https://h.org/a.pdf", "https://h.org/a.pdf", null, ""], null, false), ["https://h.org/a.pdf"]);
  assert.deepEqual(auditLinks([], team, false), ["https://acme.xyz/audit.pdf"]);
  // an unnamed hook is an address with no team: whatever Parallel returned is someone else's audit
  assert.deepEqual(auditLinks([], team, true), []);
  assert.deepEqual(auditLinks([], { team: "", audit_url: "https://x.org/audit" }, false), []);
  // a stored Parallel tombstone (a paid run that never delivered; none are written since 2026-10-24) is no audit
  assert.deepEqual(auditLinks(["https://h.org/a.pdf"], JSON.parse('{"_failed":"trun_1","reason":"404"}'), false), ["https://h.org/a.pdf"]);
  // placeholders and non-web values are not links
  assert.deepEqual(auditLinks(["N/A", "none", "javascript:alert(1)"], { team: "Acme", audit_url: "Not found" }, false), []);
  // Uniswap's own v4 audits cover the PoolManager, not this hook
  assert.deepEqual(auditLinks([], { team: "Acme", audit_url: "https://github.com/Uniswap/v4-core/tree/main/docs/security/audits" }, false), []);
});

test("the Trade panel offers the pool the title is named after, not the first pool by TVL", () => {
  const pools = [
    { poolId: "0xeqvta", chainId: 8453 },   // loadPools order: most TVL first
    { poolId: "0xLUCKY", chainId: 8453 },   // topPool: most 7-day volume
    { poolId: "0xlucky", chainId: 999999 }, // same id on an unknown chain never matches
  ];
  assert.equal(tradePool(pools, { poolId: "0xlucky", chainId: 8453 })?.poolId, "0xLUCKY");
  assert.equal(tradePool(pools, { poolId: "0xgone", chainId: 8453 })?.poolId, "0xeqvta");  // topPool not in the list: first known-chain pool
  assert.equal(tradePool(pools, null)?.poolId, "0xeqvta");
  assert.equal(tradePool([{ poolId: "0xa", chainId: 999999 }], { poolId: "0xa", chainId: 999999 }), null);
});

test("audit badge vs text: generated prose that names an audit without a link is flagged, not read as 'No audit'", () => {
  assert.equal(auditMention("Audited by Spearbit and Trail of Bits.", null), true);
  assert.equal(auditMention(null, "Two audits cover the vault and the hook."), true);
  assert.equal(auditMention("A per-block auction sells the right to arbitrage.", "LPs keep value."), false);
  assert.equal(auditMention("An auditorium of traders"), false);     // whole word only
  assert.equal(auditMention(null, undefined), false);
  // negated mentions are not mentions (live text from 10/16)
  assert.equal(auditMention(null, "Reported TVL is zero and the GitHub repo has no recent commits or stars, so there is no sign of outside attention and no audit is listed."), false);
  assert.equal(auditMention(null, "No team, audit, website or press coverage is listed, and the fee-setting logic is unknown."), false);
  assert.equal(auditMention(null, "It has not been audited."), false);
  assert.equal(auditMention(null, "Development looks quieter, with no GitHub commits in the last 30 days, though the team links audit reports from Spearbit via Cantina and Trail of Bits."), true);
});

test("top wallet share reads as a share of swaps at any size, and unknown as a dash", () => {
  assert.equal(topWalletText(null), "–");
  assert.equal(topWalletText(0.3333), "33% of swaps");
  assert.equal(topWalletText(0.8889), "89% of swaps");
  assert.equal(topWalletText(1), "100% of swaps");
  assert.equal(topWalletText(0.004), "<1% of swaps");
});

test("deployer share: unknown is a dash, a known deployer that never traded is none, otherwise a share of swaps", () => {
  assert.equal(deployerShareText(null), "–");
  assert.equal(deployerShareText(0), "none");
  assert.equal(deployerShareText(0.8889), "89% of swaps");
});

test("deployers of a project: one line per distinct wallet and chain, registry-supplied first", () => {
  const out = deployersOf([
    { chainId: 10, deployer: "0xAAAA", deployerSource: "explorer" },
    { chainId: 10, deployer: "0xaaaa", deployerSource: "explorer" },
    { chainId: 8453, deployer: "0xbbbb", deployerSource: "hooklist" },
    { chainId: 1, deployer: null, deployerSource: null },
  ]);
  assert.deepEqual(out, [
    { chainId: 8453, address: "0xbbbb", source: "hooklist" },
    { chainId: 10, address: "0xaaaa", source: "explorer" },
  ]);
});
