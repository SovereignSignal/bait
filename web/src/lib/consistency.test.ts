// One project, one set of figures across pages: names, counts, TVL and audit badges (demo smoke, 2026-10-14).
import { test } from "node:test";
import assert from "node:assert/strict";
import { auditDocument, auditLinks, auditMention, topWalletText, deployerShareText, deployersOf, deployerSourceLabel, digestIntro, listedDigestItems, poolTvlNote, tradePool, withCurrentNames } from "./present";
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
    digestIntro([item("a", "A", "new")], [], { projects: 2254, traded: 820, new: 1, tvl_usd: 1_000_000, vol_7d: 5_000_000 }),
    "1 new project cleared the bar. 820 projects traded this week, $1M TVL, $5M 7d volume.",
  );
  assert.equal(
    digestIntro([], [], { projects: 2254, traded: 820, new: 0, tvl_usd: 1_000_000, vol_7d: 5_000_000 }).split(". ")[0],
    "No new projects cleared the bar",
  );
  // digests before 2026-10-26 kept 20 New by TVL and no count of how many cleared the bar: they say what they list
  assert.equal(
    digestIntro([], [], { projects: 2254, tvl_usd: 1_000_000, vol_7d: 5_000_000 }),
    "No new projects listed. 2,254 projects tracked (live pools, not wash-like), $1M TVL, $5M 7d volume.",
  );
});

test("a digest lists, counts and names what the New and Trending tabs would (2026-10-12: '20 new' above a list of 4)", () => {
  const stored: DigestItem[] = [
    { ...item("aegis", "Aegis", "new"), vol7d: 5_000 },
    { ...item("quiet", "Quiet", "new"), vol7d: null },                 // unknown week: listed, as on the New tab
    { ...item("seeded", "Seeded", "new"), vol7d: 0 },                  // liquidity, nothing traded: not listed
    item("u1", "Unlisted hook 0x58a6…94c0", "new"),
    item("u2", "POP/USDT hook · 0xca9d…5800", "new"),
    item("fam", "LUCKYKEY/WETH hook family · 0x0f48…", "trending"),
    item("hot", "Hot", "trending"),
  ];
  const listed = listedDigestItems(stored);
  assert.deepEqual(listed.map((i) => i.slug), ["aegis", "quiet", "hot"]);
  const fresh = listed.filter((i) => i.kind === "new"), hot = listed.filter((i) => i.kind === "trending");
  assert.equal(
    digestIntro(fresh, hot, { projects: 10, traded: 5, tvl_usd: 1e6, vol_7d: 5e6 }),
    "2 new projects listed. Hot led trending. 5 projects traded this week, $1M TVL, $5M 7d volume.",
  );
  // newer digests store how many cleared the bar; the list keeps 20 of them
  assert.equal(digestIntro(fresh, hot, { projects: 10, traded: 5, new: 34, tvl_usd: 1e6, vol_7d: 5e6 }).split(". ")[0], "34 new projects cleared the bar; 2 are listed");
  assert.equal(digestIntro(fresh, hot, { projects: 10, traded: 5, new: 2, tvl_usd: 1e6, vol_7d: 5e6 }).split(". ")[0], "2 new projects cleared the bar");
});

test("an audit link is a report, not code, and not one that is gone (Upeg and Angstrom, 2026-10-24)", () => {
  // Upeg's registry entries link its repository and a code folder: they counted as two audits
  assert.deepEqual(auditLinks(["https://github.com/ChainCreators/Upeg", "https://github.com/ChainCreators/Upeg/tree/main/upegs_hook"], null, false), []);
  for (const code of [
    "https://github.com/0xPilou/supertoken-wrapper-hook/blob/main/src/SuperTokenWrapperHook.sol",
    "https://github.com/audits-r-us/hook/blob/main/audit/Hook.sol",
    "https://github.com/Uniswap/contracts/pull/93",
    "https://github.com/ETFStrategy/etf-strategy-uniswap-v4",
    "not a url",
  ])
    assert.equal(auditDocument(code), false, code);
  for (const doc of [
    "https://github.com/glue-finance/glueHook/tree/main/audit",
    "https://github.com/Thanattt/unicat/blob/main/AUDIT.md",
    "https://github.com/Uniswap/v4-hooks-public/blob/main/docs/audits/Uniswap%20V4%20WETH%20and%20WstETH%20Hooks%20Audit.pdf",
    "https://github.com/SorellaLabs/angstrom/blob/main/contracts/security-reviews/spearbit-1.pdf",
    "https://raw.githubusercontent.com/quantidexyz/clanker-v4/main/audits/cantina_v4_audit_1.pdf",
    "https://github.com/RingProtocol/ring-v4-aggregator-hook-audit",
    "https://cantina.xyz/portfolio/c2fe4e46-66a3-416e-ab26-40dd4b437ff6",
    "https://docs.aegis.markets/audits",
  ])
    assert.equal(auditDocument(doc), true, doc);
  // Angstrom's registry link is a 404 (spearbit/portfolio dropped its pdfs folder); the pipeline marks it dead
  const dead = "https://github.com/spearbit/portfolio/blob/master/pdfs/Sorella-Spearbit-Security-Review-October-2024.pdf";
  const team = { team: "Sorella Labs", audit_url: "https://cantina.xyz/portfolio/c2fe4e46-66a3-416e-ab26-40dd4b437ff6" };
  assert.deepEqual(auditLinks([dead], team, false, [dead]), [team.audit_url]);
  assert.deepEqual(auditLinks([dead], null, false, [dead]), []);
  assert.deepEqual(auditLinks([dead], null, false), [dead]);                // not checked yet: shown as published
  assert.deepEqual(auditLinks([dead], null, false, "garbage"), [dead]);     // a malformed signal value is ignored
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

test("deployers of a project: a CREATE2 deployment proxy is nobody's wallet, even when the registry names it", () => {
  const out = deployersOf([
    { chainId: 4663, deployer: "0x4e59b44847b379578588920cA78FbF26c0B4956C", deployerSource: "hooklist" },
    { chainId: 1, deployer: "0xba5ed099633d3b313e4d5f7bdc1305d3c28ba5ed", deployerSource: "explorer" },
    { chainId: 1, deployer: "0xe49acc3b16c097ec88dc9352ce4cd57ab7e35b95", deployerSource: "sourcify" },
  ]);
  assert.deepEqual(out, [{ chainId: 1, address: "0xe49acc3b16c097ec88dc9352ce4cd57ab7e35b95", source: "sourcify" }]);
  assert.deepEqual(deployersOf([{ chainId: 1, deployer: "0x4e59b44847b379578588920ca78fbf26c0b4956c", deployerSource: "hooklist" }]), []);
  assert.deepEqual(["hooklist", "sourcify", "explorer", null].map((s) => deployerSourceLabel(s as never)), ["registry", "Sourcify", "explorer", "source unknown"]);
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
