import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { SAMPLE_PROJECTS } from '../fixtures/sample';
import { activityPace, githubUrl, researchProject, retainedDormant, routingNote, webUrl, xHandle } from './research';
import { applyTab, movers, searchRows } from './feed';
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
test('registered verified quiet hooks retain research pages without becoming ranked', () => {
  const project = p(); project.isSpam = true; project.isWash = false; project.metrics.vol7d = 0;
  project.metrics.poolsN = 1; project.hooks[0].hooklisted = true; project.hooks[0].verifiedSource = true;
  project.isNew = true; project.scoreTrending = 100;
  const out = researchProject(project);
  assert.equal(out.isDormant, true); assert.equal(out.isSpam, false); assert.equal(out.isNew, false); assert.equal(out.scoreTrending, 0);
  const row = toRow(out);
  assert.equal(applyTab([row], { tab: 'all' }).length, 1);
  for (const tab of ['new', 'trending', 'verified'] as const) assert.deepEqual(applyTab([row], { tab }), []);
  assert.deepEqual(movers([row]), []);
  assert.equal(searchRows([row], row.name).length, 1);
});
test('dormant retention never promotes wash, unregistered, unverified, unknown-volume or pool-less spam', () => {
  const base = p(); base.isSpam = true; base.isWash = false; base.metrics.vol7d = 0; base.metrics.poolsN = 1;
  base.hooks = [{ ...base.hooks[0], hooklisted: true, verifiedSource: true }];
  for (const alter of [
    (x: typeof base) => { x.isWash = true; x.isDormant = true; },
    (x: typeof base) => { x.hooks[0].hooklisted = false; },
    (x: typeof base) => { x.hooks[0].verifiedSource = false; },
    (x: typeof base) => { x.metrics.vol7d = null; },
    (x: typeof base) => { x.metrics.poolsN = 0; },
    (x: typeof base) => { x.metrics.vol7d = 1; },
  ]) { const x = structuredClone(base); alter(x); assert.equal(retainedDormant(x), false); }
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
