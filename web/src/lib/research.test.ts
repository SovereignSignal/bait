import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { SAMPLE_PROJECTS } from '../fixtures/sample';
import { activityNote, activityPace, githubUrl, isUniswapBrand, researchProject, retainedDormant, routingNote, webUrl, xHandle } from './research';
import { applyTab, listFor, movers, searchRows } from './feed';
import { hookKeys, toRow } from './present';
import { evaluateCoverage, evaluateHealth, freshness, type HealthChain } from './health';

const cases = JSON.parse(readFileSync(new URL('../../fixtures/link-fields.json', import.meta.url), 'utf8')) as Array<{ value: unknown; web: boolean; github: boolean; x: string | null }>;
for (const [i, c] of cases.entries()) test(`link publication parity case ${i}`, () => {
  assert.equal(!!webUrl(c.value), c.web);
  assert.equal(!!githubUrl(c.value), c.github);
  assert.equal(xHandle(c.value), c.x);
});
const p = () => structuredClone(SAMPLE_PROJECTS[0]);
test('unidentified address families cannot publish guessed identity links or narrative', () => {
  const project = p(); project.slug = 'unlisted-hook-family-0x0f48'; project.hooklisted = false;
  project.website = 'https://unrelated.example/'; project.github = 'https://github.com/unrelated'; project.xHandle = '@unrelated';
  project.whatLine = 'Invented project purpose'; project.whyBlurb = 'Invented evidence';
  const out = researchProject(project);
  assert.equal(out.website, null); assert.equal(out.github, null); assert.equal(out.xHandle, null);
  assert.equal(out.whyBlurb, ''); assert.match(out.whatLine, /not been independently confirmed/);
  assert.deepEqual(out.metrics, project.metrics); assert.deepEqual(out.hooks, project.hooks);
});
test('malformed stored provider fields do not become public hrefs', () => {
  const project = p(); project.website = 'javascript:alert(1)'; project.github = 'https://evil.example/github.com/owner';
  project.xHandle = 'No X/Twitter handle could be confidently tied to the project.';
  project.receipts = [{ url: 'javascript:alert(1)', title: 'unsafe', source: 'provider', publishedAt: '' }];
  const out = researchProject(project);
  assert.equal(out.website, null); assert.equal(out.github, null); assert.equal(out.xHandle, null); assert.deepEqual(out.receipts, []);
});
test('pipeline-confirmed dormant projects remain in All, search and watchlists without becoming ranked', () => {
  const project = p(); project.isSpam = false; project.isWash = false; project.metrics.vol7d = 0;
  project.hasHistoricalUsage = true; project.activityStatus = 'dormant'; project.metrics.swaps7d = 0;
  // Historical usage can qualify without registry membership or source verification.
  project.hooks.forEach(h => { h.hooklisted = false; h.verifiedSource = false; });
  project.isNew = true; project.scoreTrending = 100;
  const out = researchProject(project);
  assert.equal(out.isDormant, true); assert.equal(out.isSpam, false); assert.equal(out.isNew, false); assert.equal(out.scoreTrending, 0);
  const row = toRow(out);
  assert.equal(applyTab([row], { tab: 'all' }).length, 1);
  for (const tab of ['new', 'trending', 'verified'] as const) assert.deepEqual(applyTab([row], { tab }), []);
  assert.deepEqual(movers([row]), []);
  assert.equal(searchRows([row], row.name).length, 1);
  assert.equal(listFor([row], {tab:'trending'}, {search:'',watchlist:[row.slug]}).length, 1);
});
test('legacy metadata and an old dormant flag cannot resurrect spam or establish historical usage', () => {
  const project = p(); project.isSpam = true; project.isWash = false; project.metrics.vol7d = 0;
  project.metrics.swaps7d = 0; project.metrics.poolsN = 10; project.isDormant = true;
  project.hooks.forEach(h => { h.hooklisted = true; h.verifiedSource = true; });
  const out = researchProject(project);
  assert.equal(out.isSpam, true); assert.equal(out.isDormant, false);
});
test('dormancy requires history, pipeline coverage and zero swaps; positive unpriced swaps are active', () => {
  const base = p(); base.isSpam = false; base.isWash = false; base.metrics.vol7d = 0;
  base.hasHistoricalUsage = true; base.activityStatus = 'dormant'; base.metrics.swaps7d = 0;
  for (const alter of [
    (x: typeof base) => { x.isWash = true; x.isDormant = true; },
    (x: typeof base) => { x.hasHistoricalUsage = false; },
    (x: typeof base) => { x.activityStatus = null; },
    (x: typeof base) => { x.activityStatus = 'unknown'; },
    (x: typeof base) => { x.activityStatus = 'active'; x.metrics.vol7d = null; x.metrics.swaps7d = 2; },
    (x: typeof base) => { x.metrics.swaps7d = null; },
    (x: typeof base) => { x.metrics.swaps7d = 1; },
    (x: typeof base) => { x.metrics.vol7d = 1; },
  ]) { const x = structuredClone(base); alter(x); assert.equal(retainedDormant(x), false); }
  const unknownUsd = structuredClone(base); unknownUsd.metrics.vol7d = null;
  assert.equal(retainedDormant(unknownUsd), true); // exact zero swaps, not USD pricing, establishes inactivity
});
test('historical legitimacy does not replace missing recent data or suppress revival and wash controls', () => {
  const project = p(); project.isSpam = false; project.hasHistoricalUsage = true;
  project.activityStatus = 'unknown'; project.metrics.vol7d = null; project.metrics.swaps7d = null;
  project.isNew = false; project.scoreTrending = 0;
  const out = researchProject(project);
  assert.equal(out.isSpam, false); assert.equal(out.isDormant, false); assert.equal(out.metrics.vol7d, null);
  assert.match(activityNote(out)?.detail ?? '', /inactivity has not been established/);
  project.activityStatus = 'active'; project.metrics.vol7d = 20_000; project.metrics.swaps7d = 40; project.scoreTrending = 12;
  assert.equal(applyTab([toRow(researchProject(project))], {tab:'trending'}).length, 1);
  project.isWash = true;
  const wash = toRow(researchProject(project));
  assert.equal(applyTab([wash], {tab:'all'}).length, 1);
  assert.deepEqual(applyTab([wash], {tab:'trending'}), []);
});
test('dormancy evidence names the scored window without claiming shutdown or identity verification', () => {
  const project = p(); project.hasHistoricalUsage = true; project.activityStatus = 'dormant';
  project.metrics.swaps7d = 0; project.metrics.vol7d = 0; project.isWash = false; project.through = '2026-10-14';
  assert.match(activityNote(project)?.detail ?? '', /seven closed UTC days ending 2026-10-14/);
  assert.match(activityNote(project)?.detail ?? '', /does not establish shutdown/);
  assert.doesNotMatch(activityNote(project)?.detail ?? '', /source-verified|Registered/);
});
test('all distinct addresses are searchable, including beyond the former eight-address cap', () => {
  const addresses = Array.from({length: 20}, (_, i) => `0x${i.toString(16).padStart(40,'0')}`);
  const index = { project: hookKeys([...addresses, addresses[0].toUpperCase()]) };
  assert.equal(index.project.length, 20);
  assert.equal(searchRows([{slug:'project',name:'Project'}], addresses[19], index).length, 1);
});
test('pace does not cap numeric growth, and absent denominators stay unknown', () => {
  assert.equal(activityPace(200,700),200); assert.equal(activityPace(0,700),0);
  assert.equal(activityPace(100,0),null); assert.equal(activityPace(null,700),null); assert.equal(activityPace(Infinity,700),null);
});
test('routing labels describe automatic criteria without claiming an individual approval or executable quote', () => {
  assert.match(routingNote('eligible').detail, /have not been verified/);
  assert.match(routingNote('ineligible').detail, /approve individual hooks/);
  assert.doesNotMatch(routingNote('ineligible').label, /Not in app/);
});
const now = new Date('2026-10-15T01:00:00Z');
const receipt = {day:'2026-10-14',exitCode:0,softErrors:0,codeSha:'test'};
const chain: HealthChain = {chainId:8453,backfilled:true,headBlock:100,swapBlock:100,updatedAt:now,swapBlockTime:now,pendingPools:0,pendingBeforeThrough:0};
test('head current with outstanding historical pool IDs is live but incomplete', () => {
  const input = {receipt,chains:[{...chain,pendingPools:87000,pendingBeforeThrough:84000}]};
  assert.equal(evaluateHealth(input, now).ok, true);
  assert.equal(evaluateCoverage(input, now).status, 'partial');
  assert.deepEqual(freshness(input,now).partialChains,[8453]);
  assert.match(evaluateHealth(input,now).warnings.join(' '),/partial/);
});
test('new live-edge IDs do not prevent a closed day from being complete', () => {
  assert.equal(evaluateCoverage({receipt,chains:[{...chain,pendingPools:100,pendingBeforeThrough:0}]},now).status,'ready');
});
test('cursor time, absent evidence, no chains and stale evidence fail coverage closed', () => {
  for (const c of [{...chain,backfilled:false},{...chain,swapBlockTime:new Date('2026-10-13T23:59:59Z')},{...chain,updatedAt:new Date('2026-10-14T01:00:00Z')}])
    assert.equal(evaluateCoverage({receipt,chains:[c]},now).status,'partial');
  assert.equal(evaluateCoverage({receipt,chains:[{...chain,pendingBeforeThrough:null}]},now).status,'unknown');
  assert.equal(evaluateCoverage({receipt,chains:[]},now).status,'unknown');
});
test('actual scored day outranks run receipt date in freshness labels', () => {
  assert.equal(freshness({receipt,chains:[chain],reportedThrough:'2026-10-12'},now).through,'2026-10-12');
  assert.equal(evaluateCoverage({receipt,chains:[chain],reportedThrough:null},now).status,'unknown');
});

test('Uniswap and Uniswap Foundation links show only on a project in the hooklist registry', () => {
  // FoundationHookV2 (2026-10-16 audit): a launch template linked to the Foundation by a name-only search
  const fake = p();
  fake.hooklisted = false;
  fake.hooks = fake.hooks.map((h) => ({...h, hooklisted: false, deployer: null}));
  Object.assign(fake, {website: 'https://uniswapfoundation.org/', github: 'https://github.com/uniswapfoundation/foundational-hooks', xHandle: 'UniswapFND'});
  fake.signals = {...fake.signals, auditUrl: 'https://developers.uniswap.org/docs/audit.pdf'};
  const r = researchProject(fake);
  assert.deepEqual([r.website, r.github, r.xHandle, r.signals.auditUrl], [null, null, null, null]);
  // StablePairHook, DualPool, the Liquidity Launcher strategies: in Uniswap's hooklist
  const real = structuredClone(fake);
  real.hooklisted = true;
  real.hooks = real.hooks.map((h, i) => ({...h, hooklisted: i === 0}));
  Object.assign(real, {website: 'https://developers.uniswap.org/docs/protocols/uniswap-labs-hooks/stable-pair/overview', github: 'https://github.com/Uniswap/v4-hooks-public', xHandle: 'Uniswap'});
  const k = researchProject(real);
  assert.deepEqual([k.website, k.github, k.xHandle], ['https://developers.uniswap.org/docs/protocols/uniswap-labs-hooks/stable-pair/overview', 'https://github.com/Uniswap/v4-hooks-public', 'Uniswap']);
  // other teams' links are untouched
  const other = structuredClone(fake);
  Object.assign(other, {website: 'https://kyber.network/', github: 'https://github.com/KyberNetwork/kyber-exclusive-amm-sc', xHandle: 'KyberNetwork'});
  assert.equal(researchProject(other).github, 'https://github.com/KyberNetwork/kyber-exclusive-amm-sc');
  assert.equal(isUniswapBrand('https://blog.uniswap.org/x'), true);
  assert.equal(isUniswapBrand('https://notuniswap.org/'), false);
  assert.equal(isUniswapBrand('https://x.com/UniswapFND'), true);
});
