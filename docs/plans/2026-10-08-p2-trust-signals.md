# Package 2: Trust signals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every trust claim on a Bait card is backed by data: hook permissions come from the address bits, routing eligibility is a three-state result evaluated per hook and pool, the Hooklist badge reflects real registry membership, and the trade panel never shows a primary Swap button for a pool it cannot vouch for.

**Architecture:** One shared rule, two implementations (Python `rules.py`, TypeScript `routable.ts`), one shared fixture that both test suites assert. The pipeline derives flags from the address for every hook it discovers and backfills existing rows; it records `hooklisted` at ingest. The web reads those columns, computes eligibility per (hook, pool), and renders badges and the CTA from that. The decorative swap controls are replaced by a pool action card.

**Tech Stack:** Python 3.12 + SQLAlchemy Core (pipeline), Next.js 16 + Drizzle + TypeScript (web), `node:test` via `tsx` for web unit tests, `tsc --noEmit` and `npm run build` as the component check.

**Spec:** Audit findings: Sov Critical (routable badge + CTA), Sov High "Hooklist badge", Astra H1, M3, M10. Uniswap v4 `Hooks.sol` defines permissions in the low 14 bits of the hook address; the data review found 0 disagreements between those bits and the hooklist's declared flags across 4,955 entries, so the bits are authoritative.

## Global Constraints

- Pipeline tests: `cd pipeline && .venv/bin/python -m pytest -q`. Web: `cd web && npm test && npx tsc --noEmit && npm run build` (build without `DATABASE_URL` is the documented path).
- Never call a paid API. No secrets, hostnames, or absolute paths in files or commits.
- Commit after every task. End every commit message with these two trailer lines, verbatim:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01A2FHLwk9NeThmyZ1JbHe3o`
- Schema changes go in BOTH `db/migrations/0007_trust.sql` and `pipeline/bait/db.py` `ADDITIVE_COLUMNS` (the pipeline adds columns itself on start; the migration documents it). And in `web/src/db/schema.ts`.
- Do not touch `pipeline/bait/indexer/`, `web/src/db/queries.ts` cache logic, or any CSS beyond what Task 5 names. Package 6 owns UX polish.
- Do not push.

## Review Focus

1. A hook with empty metadata whose address carries a returnsDelta bit must be ineligible everywhere (row, detail, OG image, CTA). (Task 1, 3)
2. A hook with clean bits and no known pool fee must be "unknown", never "eligible". (Task 1)
3. A project found on-chain (never in the hooklist) must show "Not in hooklist" and never "✓ Hooklist". (Task 2, 4)
4. A pool with `$0` TVL or an unknown token symbol must not get the primary Swap CTA even when eligible. (Task 5)
5. Existing hooklist hooks must keep exactly their current flags after the address-bit derivation (no regressions on listed hooks). (Task 1, 2)

---

### Task 1: One eligibility rule, two implementations, one fixture

**Files:**
- Modify: `fixtures/routable_cases.json`
- Modify: `pipeline/bait/rules.py` (add `flags_from_address`, `eligibility`; keep `routable`)
- Modify: `pipeline/tests/test_routable_shared.py`, `pipeline/tests/test_rules.py`
- Modify: `web/src/lib/routable.ts` (add `permissionsFromAddress`, `eligibility`, `Eligibility` type; keep `isRoutable`)
- Modify: `web/src/lib/routable.test.ts`

**Interfaces:**
- Produces (Python): `flags_from_address(address: str) -> dict[str, bool]` with all 14 permission keys. `eligibility(hook: dict, pool_fee: int | None = None) -> str` returning `"eligible" | "ineligible" | "unknown"`. `routable(hook) -> bool` becomes `eligibility(hook) == "eligible"`.
- Produces (TS): `type Eligibility = "eligible" | "ineligible" | "unknown"`; `permissionsFromAddress(address: string): HookFlags`; `eligibility(address, flags, properties, poolFee?: number | null): Eligibility`; `isRoutable(...)` becomes `eligibility(...) === "eligible"`.
- Rule, identical in both: permissions are the address bits (ignore declared permission flags; keep other keys). `0x91` prefix → ineligible. Any of the four returnsDelta bits → ineligible. Dynamic fee: `poolFee & 0x800000` when a pool fee is given, else `properties.dynamicFee` when that key is present → true means ineligible. If neither a pool fee nor a `dynamicFee` key is known → unknown. Otherwise eligible.

Bit map (Hooks.sol): bit 13 beforeInitialize, 12 afterInitialize, 11 beforeAddLiquidity, 10 afterAddLiquidity, 9 beforeRemoveLiquidity, 8 afterRemoveLiquidity, 7 beforeSwap, 6 afterSwap, 5 beforeDonate, 4 afterDonate, 3 beforeSwapReturnsDelta, 2 afterSwapReturnsDelta, 1 afterAddLiquidityReturnsDelta, 0 afterRemoveLiquidityReturnsDelta.

- [ ] **Step 1: Extend the fixture**

Replace the `routable` boolean on every existing case with `"eligibility": "eligible"` or `"ineligible"` (same truth), add an optional `poolFee`, and append four cases:

```json
    {
      "name": "empty metadata, address bit 3 set (beforeSwapReturnsDelta)",
      "address": "0x4444000000000000000000000000000000000008",
      "flags": {},
      "properties": {},
      "eligibility": "ineligible"
    },
    {
      "name": "empty metadata, clean bits (beforeSwap only), static pool fee",
      "address": "0x5555000000000000000000000000000000000080",
      "flags": {},
      "properties": {},
      "poolFee": 3000,
      "eligibility": "eligible"
    },
    {
      "name": "clean bits, dynamic pool fee bit 0x800000",
      "address": "0x5555000000000000000000000000000000000080",
      "flags": {},
      "properties": {},
      "poolFee": 8388608,
      "eligibility": "ineligible"
    },
    {
      "name": "clean bits, no pool fee, no dynamicFee key: unknown",
      "address": "0x5555000000000000000000000000000000000080",
      "flags": {},
      "properties": {},
      "eligibility": "unknown"
    }
```

Update `_comment` to describe the three-state rule and the address-bit source. Case 1's address `0x19aa…0000` has clean low bits (0x0000) and `properties.dynamicFee: false`, so it stays eligible. Check each existing case's address low 14 bits against its intended outcome; the `0x0000fe59…1888` case has bit 3 set (0x1888 & 0x8), consistent with ineligible.

- [ ] **Step 2: Python tests**

`pipeline/tests/test_routable_shared.py`:

```python
from bait.rules import eligibility, routable


def test_fixture_has_ten_cases_with_all_three_outcomes():
    assert len(CASES) == 10
    assert {c["eligibility"] for c in CASES} == {"eligible", "ineligible", "unknown"}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_eligibility_matches_shared_fixture(case):
    hook = {"hook": {"address": case["address"]}, "flags": case["flags"], "properties": case["properties"]}
    assert eligibility(hook, case.get("poolFee")) == case["eligibility"]
    assert routable(hook) is (case["eligibility"] == "eligible" and case.get("poolFee") is None)
```

(The last line: `routable(hook)` has no pool fee, so the static-pool-fee case is "unknown" to it and therefore not routable. That is intended.)

Append to `pipeline/tests/test_rules.py`:

```python
def test_flags_from_address_decodes_all_fourteen_bits():
    from bait.rules import flags_from_address
    f = flags_from_address("0x69af000000000000000000000000000000003fff")
    assert all(f.values()) and len(f) == 14
    g = flags_from_address("0x720e000000000000000000000000000000002080")      # beforeInitialize + beforeSwap
    assert {k for k, v in g.items() if v} == {"beforeInitialize", "beforeSwap"}
    assert flags_from_address("0X0000000000000000000000000000000000000000") == {k: False for k in g}
```

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_routable_shared.py tests/test_rules.py` → FAIL (ImportError).

- [ ] **Step 3: Python implementation**

In `pipeline/bait/rules.py`:

```python
PERMISSION_BITS: tuple[tuple[str, int], ...] = (
    ("beforeInitialize", 13), ("afterInitialize", 12),
    ("beforeAddLiquidity", 11), ("afterAddLiquidity", 10),
    ("beforeRemoveLiquidity", 9), ("afterRemoveLiquidity", 8),
    ("beforeSwap", 7), ("afterSwap", 6),
    ("beforeDonate", 5), ("afterDonate", 4),
    ("beforeSwapReturnsDelta", 3), ("afterSwapReturnsDelta", 2),
    ("afterAddLiquidityReturnsDelta", 1), ("afterRemoveLiquidityReturnsDelta", 0),
)


def flags_from_address(address: str) -> dict[str, bool]:
    """Hook permissions live in the low 14 bits of the address (v4 Hooks.sol); the chain enforces them."""
    bits = int(address, 16) & 0x3FFF
    return {name: bool(bits >> bit & 1) for name, bit in PERMISSION_BITS}


def eligibility(hook: dict, pool_fee: int | None = None) -> str:
    """Routing eligibility by Bait's rule: 'eligible' | 'ineligible' | 'unknown'. Permissions come from the address;
    dynamic fee from the pool fee when given, else properties.dynamicFee when present, else unknown."""
    address = (hook.get("hook") or {}).get("address") or hook.get("address") or ""
    props = hook.get("properties") or {}
    if address.lower().startswith("0x91"):
        return "ineligible"
    flags = flags_from_address(address)
    if any(flags[f] for f in NON_ROUTABLE_FLAGS):
        return "ineligible"
    if pool_fee is not None:
        return "ineligible" if is_dynamic_fee(pool_fee) else "eligible"
    if "dynamicFee" in props:
        return "ineligible" if bool(props.get("dynamicFee")) else "eligible"
    return "unknown"


def routable(hook: dict) -> bool:
    return eligibility(hook) == "eligible"
```

Run the two test files → PASS. Then the full suite → all pass (if `test_rules.py` had assertions on the old `routable()` for a case with empty properties, update them to the new truth and say so in the report).

- [ ] **Step 4: TypeScript tests**

`web/src/lib/routable.test.ts`: change the `Case` interface to `eligibility: Eligibility; poolFee?: number`, assert `cases.length === 10` and all three outcomes present, and per case:

```ts
  test(`eligibility: ${c.name}`, () => {
    assert.equal(eligibility(c.address, c.flags, c.properties, c.poolFee ?? null), c.eligibility);
    assert.equal(isRoutable(c.address, c.flags, c.properties), c.eligibility === "eligible" && c.poolFee == null);
  });

test("permissionsFromAddress decodes the low 14 bits", () => {
  const all = permissionsFromAddress("0x69af000000000000000000000000000000003fff");
  assert.equal(Object.values(all).every(Boolean), true);
  const two = permissionsFromAddress("0x720e000000000000000000000000000000002080");
  assert.deepEqual(Object.entries(two).filter(([, v]) => v).map(([k]) => k).sort(), ["beforeInitialize", "beforeSwap"]);
});
```

Keep the `projectRoutable` test but adapt it in Task 3 (its signature changes there); for now assert it still returns `true`/`false` on the same inputs using `isRoutable` semantics.

Run: `cd web && npm test` → FAIL.

- [ ] **Step 5: TypeScript implementation**

`web/src/lib/routable.ts`:

```ts
export type Eligibility = "eligible" | "ineligible" | "unknown";

const PERMISSION_BITS: Array<[keyof HookFlags, number]> = [
  ["beforeInitialize", 13], ["afterInitialize", 12],
  ["beforeAddLiquidity", 11], ["afterAddLiquidity", 10],
  ["beforeRemoveLiquidity", 9], ["afterRemoveLiquidity", 8],
  ["beforeSwap", 7], ["afterSwap", 6],
  ["beforeDonate", 5], ["afterDonate", 4],
  ["beforeSwapReturnsDelta", 3], ["afterSwapReturnsDelta", 2],
  ["afterAddLiquidityReturnsDelta", 1], ["afterRemoveLiquidityReturnsDelta", 0],
];

export const DYNAMIC_FEE_FLAG = 0x800000;

/** Hook permissions are the low 14 bits of the address (v4 Hooks.sol); the chain enforces them. */
export function permissionsFromAddress(address: string): HookFlags {
  const bits = Number(BigInt(address) & 0x3fffn);
  return Object.fromEntries(PERMISSION_BITS.map(([k, b]) => [k, ((bits >> b) & 1) === 1])) as unknown as HookFlags;
}

export function eligibility(
  address: string,
  _flags: Partial<HookFlags> | null | undefined,
  properties: Partial<HookProperties> | null | undefined,
  poolFee?: number | null,
): Eligibility {
  if (address.toLowerCase().startsWith("0x91")) return "ineligible";
  const f = permissionsFromAddress(address);
  if (f.beforeSwapReturnsDelta || f.afterSwapReturnsDelta || f.afterAddLiquidityReturnsDelta || f.afterRemoveLiquidityReturnsDelta) return "ineligible";
  if (poolFee != null) return (poolFee & DYNAMIC_FEE_FLAG) !== 0 ? "ineligible" : "eligible";
  if (properties && "dynamicFee" in properties) return properties.dynamicFee ? "ineligible" : "eligible";
  return "unknown";
}

export function isRoutable(address: string, flags: Partial<HookFlags> | null | undefined, properties: Partial<HookProperties> | null | undefined): boolean {
  return eligibility(address, flags, properties) === "eligible";
}
```

Update the doc comment at the top of the file to the three-state rule. `flagLabels` and `activeFlags` should read permissions from the address too: change their first parameter use to `const f = { ...flags, ...permissionsFromAddress(address) }`; this needs an `address` parameter. Add it as the first parameter of both (`flagLabels(address, flags, properties)`, `activeFlags(address, flags)`) and update the two call sites (`web/src/db/queries.ts:51` and the hook list in `Detail.tsx`; grep `activeFlags(`).

Run `cd web && npm test && npx tsc --noEmit` → PASS.

- [ ] **Step 6: Commit**

```bash
git add fixtures/routable_cases.json pipeline/bait/rules.py pipeline/tests/test_routable_shared.py pipeline/tests/test_rules.py web/src/lib/routable.ts web/src/lib/routable.test.ts web/src/db/queries.ts web/src/components/Detail.tsx
git commit -m "Routing eligibility: permissions from address bits, three-state result, pool fee decides dynamic fee

Unlisted hooks had empty flags and passed the old boolean check. The low 14 address
bits are enforced on-chain and agree with the hooklist on all 4,955 entries."
```

---

### Task 2: Pipeline: address-derived flags for every hook, `hooklisted` at ingest

**Files:**
- Create: `db/migrations/0007_trust.sql`
- Modify: `pipeline/bait/db.py` (`ADDITIVE_COLUMNS`, `hooks` table column, new `backfill_address_flags`)
- Modify: `pipeline/bait/steps/ingest.py` (set `hooklisted`, demote hooks no longer listed)
- Modify: `pipeline/bait/steps/onchain.py:74`, `pipeline/bait/steps/snapshot.py:65`, `pipeline/bait/keyless.py:422` (new hooks get derived flags and `hooklisted: False`)
- Modify: `pipeline/bait/steps/group.py` (call `backfill_address_flags` at the start of `run`)
- Test: `pipeline/tests/test_ingest.py`, `pipeline/tests/test_group.py`, `pipeline/tests/test_onchain.py`

**Interfaces:**
- Produces: `hooks.hooklisted BOOLEAN` (NULL = not yet known; ingest sets true/false every night).
- Produces: `db.backfill_address_flags(conn) -> int`: for every hook whose `flags` is NULL or `{}`, write `flags_from_address(address)`; returns rows updated.

- [ ] **Step 1: Migration and schema**

`db/migrations/0007_trust.sql`:

```sql
-- Registry membership is a fact, not a default. NULL = not yet evaluated by ingest.
ALTER TABLE hooks ADD COLUMN IF NOT EXISTS hooklisted BOOLEAN;
-- flags for unlisted hooks are derived from the address bits by the pipeline (db.backfill_address_flags).
```

`pipeline/bait/db.py`: add `sa.Column("hooklisted", sa.Boolean),` to the `hooks` table after `audit_url`, and `("hooks", "hooklisted", "boolean")` to `ADDITIVE_COLUMNS`. Add:

```python
def backfill_address_flags(conn: sa.Connection) -> int:
    """Hooks discovered on-chain were stored with flags={}; derive them from the address bits. Idempotent."""
    from .rules import flags_from_address
    rows = conn.execute(sa.select(hooks.c.address, hooks.c.chain_id, hooks.c.flags)).all()
    todo = [r for r in rows if not r.flags]
    for r in todo:
        conn.execute(sa.update(hooks).values(flags=flags_from_address(r.address))
                     .where(hooks.c.address == r.address, hooks.c.chain_id == r.chain_id))
    return len(todo)
```

- [ ] **Step 2: Failing tests**

Append to `pipeline/tests/test_ingest.py`:

```python
def test_ingest_marks_listed_hooks_and_demotes_the_rest(conn, ctx):
    insert_hook(conn, "0x" + "c" * 40, 8453, name=None)            # found on-chain, never listed
    out = ingest.run(conn, ctx)
    rows = {(r.address, r.chain_id): r.hooklisted for r in conn.execute(sa.select(db.hooks.c.address, db.hooks.c.chain_id, db.hooks.c.hooklisted))}
    assert rows[("0x" + "c" * 40, 8453)] is False
    listed = [v for k, v in rows.items() if k != ("0x" + "c" * 40, 8453)]
    assert listed and all(v is True for v in listed)
```

(`insert_hook` is in `tests/conftest.py`; import it.)

Append to `pipeline/tests/test_group.py`:

```python
def test_group_backfills_address_flags_for_empty_metadata_hooks(conn, ctx):
    from bait.steps import group
    insert_hook(conn, "0x4444000000000000000000000000000000000008", 8453)   # flags={} from conftest
    group.run(conn, ctx)
    f = conn.execute(sa.select(db.hooks.c.flags).where(db.hooks.c.address == "0x4444000000000000000000000000000000000008")).scalar_one()
    assert f["beforeSwapReturnsDelta"] is True and f["beforeSwap"] is False
```

In `pipeline/tests/test_onchain.py`, find the test that asserts new hooks are inserted from `v4_pools` and add an assertion that the inserted hook's `flags` equals `flags_from_address(address)` and `hooklisted is False`. If no such test exists, add one modelled on the file's existing fixture helpers.

Run → FAIL.

- [ ] **Step 3: Implement**

`ingest.py`: add `"hooklisted"` to `UPDATE_COLS`, set `"hooklisted": True` in each row dict, and after the upsert loop:

```python
    listed = [(r["address"], r["chain_id"]) for r in rows]
    demoted = conn.execute(sa.update(hooks).values(hooklisted=False).where(
        sa.tuple_(hooks.c.address, hooks.c.chain_id).not_in(listed) if listed else sa.true())).rowcount
    out["delisted"] = demoted or 0
```

On SQLite `sa.tuple_(...).not_in` works with SQLAlchemy 2 (row values). If it does not on your version, fall back to a Python set difference and a chunked update by primary key, and note it.

`onchain.py:74`, `snapshot.py:65`, `keyless.py:422`: replace `"flags": {}` with `"flags": flags_from_address(<address var>)` and add `"hooklisted": False`. Import `from ..rules import flags_from_address` (in `keyless.py`: `from .rules import flags_from_address`).

`group.py`: at the top of `run()`: `out["flags_backfilled"] = backfill_address_flags(conn)` (import from `..db`).

Run the full suite → PASS.

- [ ] **Step 4: Commit**

```bash
git add db/migrations/0007_trust.sql pipeline
git commit -m "hooks: hooklisted set nightly by ingest; flags derived from address bits for every discovered hook, backfilled in group"
```

---

### Task 3: Web reads `hooklisted` and computes three-state eligibility per pool

**Files:**
- Modify: `web/src/db/schema.ts` (`hooklisted: boolean("hooklisted")`)
- Modify: `web/src/lib/types.ts` (`HookView.hooklisted: boolean | null`; `ProjectSummary.hooklisted: boolean`; `SignalsView.routable: Eligibility`; `RowView.routable: Eligibility`; `PoolView.eligibility: Eligibility`)
- Modify: `web/src/lib/routable.ts` (`projectRoutable(hooks, pools): Eligibility`)
- Modify: `web/src/db/queries.ts` (`derive`, `toHook`, the summary builder near line 465-492, `loadPools` mapping)
- Modify: `web/src/lib/feed.ts` (`isVerified` → `inApp`)
- Modify: `web/src/app/p/[slug]/opengraph-image.tsx:130`
- Modify: `web/src/fixtures/sample.ts`, `web/src/fixtures/terminal-sample.json` (add `hooklisted`, change `routable` values to strings)
- Test: `web/src/lib/routable.test.ts`

**Interfaces:**
- Produces: `projectRoutable(hooks: Array<{address; properties}>, pools: Array<{hookAddress; fee: number | null}>): Eligibility`: `"eligible"` if any pool's hook is eligible with that pool's fee; else `"ineligible"` if every hook is ineligible on its own (0x91 or delta bits) or every known pool is dynamic; else `"unknown"`.
- Produces: `PoolView.eligibility` per pool via `eligibility(pool.hookAddress, {}, hookProps, pool.fee)`.
- Produces: `ProjectSummary.hooklisted = hooks.some(h => h.hooklisted === true)`.

- [ ] **Step 1: Failing test**

In `routable.test.ts` replace the `projectRoutable` test:

```ts
test("projectRoutable evaluates hook and pool together", () => {
  const clean = "0x5555000000000000000000000000000000000080";
  const delta = "0x4444000000000000000000000000000000000008";
  assert.equal(projectRoutable([{ address: clean, properties: {} }], [{ hookAddress: clean, fee: 3000 }]), "eligible");
  assert.equal(projectRoutable([{ address: clean, properties: {} }], [{ hookAddress: clean, fee: 8388608 }]), "ineligible");
  assert.equal(projectRoutable([{ address: clean, properties: {} }], []), "unknown");
  assert.equal(projectRoutable([{ address: delta, properties: {} }], [{ hookAddress: delta, fee: 3000 }]), "ineligible");
  assert.equal(projectRoutable([{ address: delta, properties: {} }, { address: clean, properties: {} }], [{ hookAddress: clean, fee: 500 }]), "eligible");
});
```

Run `npm test` → FAIL.

- [ ] **Step 2: Implement `projectRoutable`**

```ts
export function projectRoutable(
  hooks: Array<{ address: string; properties: Partial<HookProperties> | null | undefined }>,
  pools: Array<{ hookAddress: string; fee: number | null | undefined }>,
): Eligibility {
  const results: Eligibility[] = [];
  for (const h of hooks) {
    const own = eligibility(h.address, null, h.properties);
    if (own === "ineligible") { results.push(own); continue; }
    const hp = pools.filter((p) => p.hookAddress.toLowerCase() === h.address.toLowerCase() && p.fee != null);
    if (hp.length === 0) { results.push(own); continue; }            // "eligible" via properties or "unknown"
    results.push(...hp.map((p) => eligibility(h.address, null, h.properties, p.fee)));
  }
  if (results.includes("eligible")) return "eligible";
  if (results.length && results.every((r) => r === "ineligible")) return "ineligible";
  return "unknown";
}
```

- [ ] **Step 3: Wire the web**

- `schema.ts`: add `hooklisted: boolean("hooklisted"),` to `hooks`.
- `types.ts`: as in Interfaces. Export `Eligibility` from `routable.ts` and import it in `types.ts` (or move the type to `types.ts` and import it in `routable.ts`; pick one, avoid a cycle: put it in `types.ts`).
- `queries.ts` `toHook`: add `hooklisted: h.hooklisted ?? null`. In `derive()`: `routable: projectRoutable(p.hooks, p.pools)` and add `hooklisted: p.hooks.some((h) => h.hooklisted === true)`. In the summary builder (around line 465-492) set `hooklisted: hs.some((h) => h.hooklisted === true)`; its `routable: false` placeholder becomes `"unknown"` before `derive` runs (confirm `derive` runs for rows too; if rows get `routable` elsewhere, apply `projectRoutable` there with whatever pool list that path already loads, and `"unknown"` when none). In `loadPools` mapping add `eligibility: eligibility(pool.hookAddress, {}, hookPropsFor(pool.hookAddress) ?? {}, pool.fee)` where `hookPropsFor` looks up the project's hook by address (the pools and hooks are both in scope in `getProject`).
- `feed.ts`: rename `isVerified` to `inApp(r: { routable: Eligibility })` returning `r.routable === "eligible"`; in `applyTab`, `q.tab === "verified"` → use `inApp`. Keep the tab id `"verified"` in URLs for now (Task 4 renames the label; changing the id would break old links, so keep `verified` as the id and label it "In Uniswap app").
- `opengraph-image.tsx:130`: three labels: eligible → "● Routes in Uniswap app", ineligible → "○ Not routable in app", unknown → "○ Routing unknown".
- Fixtures: `sample.ts` / `terminal-sample.json`: set `routable` to one of the three strings and add `hooklisted: true` to listed sample hooks (and `false` on any "Unlisted" sample).

Run `cd web && npm test && npx tsc --noEmit` → PASS. Fix every type error `tsc` reports; `RowView.routable` is read in `ProjectList.tsx` and `Detail.tsx`, which Task 4 rewrites, but make them compile now with minimal edits (`=== "eligible"`).

- [ ] **Step 4: Commit**

```bash
git add web
git commit -m "web: hooklisted from the database, routing eligibility per hook and pool, three-state everywhere"
```

---

### Task 4: Badges and tab tell the truth

**Files:**
- Modify: `web/src/components/Detail.tsx:56-57` (badges), `:269` (Registry activity row), the hook list that prints `activeFlags`
- Modify: `web/src/lib/types.ts:20-25` (`TABS` label)
- Modify: `web/src/app/about/page.tsx` (one paragraph: what "In Uniswap app" means; what "Not in hooklist" means)

- [ ] **Step 1: Badges**

Replace lines 56-57 in `Detail.tsx`:

```tsx
              {p.hooklisted ? <span className="b b-v">✓ Hooklist</span> : <span className="b b-n">Not in hooklist</span>}
              {p.signals.routable === "eligible" && <span className="b b-g">Routes in Uniswap app</span>}
              {p.signals.routable === "ineligible" && <span className="b b-n">Not routable in app</span>}
              {p.signals.routable === "unknown" && <span className="b b-n">Routing unknown</span>}
```

Replace the Registry row at line 269:

```tsx
  out.push(
    p.hooklisted
      ? { cls: "b-v", label: "Registry", text: `Listed in the Uniswap hooklist${p.signals.auditCount > 0 ? " with an audit link" : ""}` }
      : { cls: "b-n", label: "Discovered on-chain", text: "Not in the Uniswap hooklist; found from its pools. Name and flags come from the chain." },
  );
```

- [ ] **Step 2: Tab label and about page**

`types.ts`: `["verified", "In Uniswap app"],`. `about/page.tsx`: find the paragraph that explains the routable badge and add two sentences: "The In Uniswap app tab lists projects with at least one pool our rule says the app will route to: no returnsDelta permission bits, not a 0x91 address, static fee. Hooks without a known pool show Routing unknown." and "Not in hooklist means we found the hook from its pools, not from Uniswap's registry; its name and permissions come from the chain."

Run `cd web && npx tsc --noEmit && npm run build` → PASS. Start `BAIT_FIXTURES=1 npm run dev` is not required; the build is the gate.

- [ ] **Step 3: Commit**

```bash
git add web
git commit -m "web: Hooklist badge only for listed hooks, three routing badges, Verified tab becomes In Uniswap app"
```

---

### Task 5: Trade panel: a pool action card, honest CTA

**Files:**
- Modify: `web/src/components/TradePanel.tsx` (the `<div className={s.swap}>` block)
- Modify: `web/src/components/Trade.module.css` (only: add `.reason` if missing; leave the rest)

**Interfaces:**
- Consumes: `top` (pool chosen as today), `pool.eligibility` (Task 3), `pool.tvlUsd` (check `PoolView` has it; if the field is named differently, use that name), `s0`/`s1` symbols.
- CTA rule: primary `Swap on Uniswap` only when `top.eligibility === "eligible" && (top.tvlUsd ?? 0) >= 1000 && s0 !== "?" && s1 !== "?"`. Otherwise a secondary `View pool on Uniswap` link plus one reason line.

- [ ] **Step 1: Replace the swap mock**

Remove the `Limit` span, the `⚙` span, the `<input>`, the `You receive … 0` well, the flip arrow, and the `Chevron` in `Token`. Replace the `s.swap` block with:

```tsx
      <div className={s.swap}>
        <div className={s.swapH}>
          <div className={s.swapT}>
            <span>Trade</span>
          </div>
        </div>
        <div className={s.well}>
          <div className={s.lbl}>Pool</div>
          <div className={s.ln}>
            <Token sym={s0} /> <span className="mut3">/</span> <Token sym={s1} />
          </div>
          <div className={s.wsub}>{top ? `${chainName}${top.fee ? ` · ${top.fee} fee` : ""}` : "No pool indexed yet"}</div>
        </div>
        <div className={s.route}>
          <span>Through</span>
          <b>{p.name}</b>
        </div>
        {link && top && (() => {
          const elig = top.eligibility;
          const tvl = top.tvlUsd ?? 0;
          const ok = elig === "eligible" && tvl >= 1000 && s0 !== "?" && s1 !== "?";
          const reason =
            elig === "ineligible" ? "This hook does not route in the Uniswap app; trading happens through the project's own interface."
            : elig === "unknown" ? "Routing in the Uniswap app is unknown for this pool."
            : tvl < 1000 ? `No liquidity in this pool (${tvl < 1 ? "$0" : "under $1K"} TVL). Volume may not be organic.`
            : "One of the tokens could not be identified.";
          return ok ? (
            <a className={s.cta} href={link} target="_blank" rel="noopener">Swap on Uniswap</a>
          ) : (
            <>
              <a className={`${s.cta} ${s.alt}`} href={link} target="_blank" rel="noopener">View pool on Uniswap</a>
              <div className={s.note}>{reason}</div>
            </>
          );
        })()}
      </div>
```

Carry `eligibility` and `tvlUsd` into the `top` object where it is built from `pool` (`p.pools.find(...)`); when `top` comes from `p.topPool`, set `eligibility: eligibility(p.topPool.hookAddress, {}, hookProps, null)` and `tvlUsd: p.topPool.tvlUsd ?? null` (check the field exists on `topPool`; if not, use `p.metrics.tvlUsd`). Remove unused imports (`ArrowDown`, `Chevron`).

- [ ] **Step 2: Build**

`cd web && npx tsc --noEmit && npm run build` → PASS. Then `BAIT_FIXTURES=1 npm run dev`, open `/p/<a fixture slug>` once, confirm the card renders and no console error; stop the server. Record which slug you looked at.

- [ ] **Step 3: Commit**

```bash
git add web/src/components/TradePanel.tsx web/src/components/Trade.module.css
git commit -m "trade panel: pool action card replaces the decorative swap form; primary CTA only for eligible pools with liquidity and known tokens"
```

---

### Task 6: DESIGN.md records the new rule

**Files:**
- Modify: `DESIGN.md` (append a dated update section; later sections override earlier ones)
- Modify: `README.md` and `pipeline/README.md` where they describe the routable badge or "Verified"

- [ ] **Step 1: Append to `DESIGN.md`**

```markdown
## Update 2026-10-09: permissions from the address, three-state routing, registry membership

- **Permissions.** A hook's 14 permission flags are the low 14 bits of its address (v4 `Hooks.sol`), enforced on-chain. The pipeline derives them for every hook it discovers (`rules.flags_from_address`) and backfills hooks stored with empty flags. Declared hooklist flags are informational; the bits win.
- **Routing eligibility** is `eligible | ineligible | unknown`, evaluated per hook and pool: `0x91` prefix or any returnsDelta bit → ineligible; a pool's `fee & 0x800000` decides dynamic fee when a pool is known, else `properties.dynamicFee` when present; neither known → unknown. A project is eligible if any of its pools is. The badge is still a rule, not a guarantee.
- **Registry membership** is stored (`hooks.hooklisted`), set every night by ingest from the full hooklist. The "✓ Hooklist" badge and the Registry activity entry render only when true; otherwise "Not in hooklist" / "Discovered on-chain".
- **Tab.** "Verified" is now "In Uniswap app" (eligible projects only). Audit status is a separate badge, not part of the tab.
- **Trade panel.** The decorative swap form is gone. The pool action card shows the pair, chain and fee, and a primary "Swap on Uniswap" only for an eligible pool with at least $1K TVL and two identified tokens; otherwise "View pool on Uniswap" plus the reason.
```

Update the README sentences that mention "Verified" or the boolean routable badge to match. Run both test suites one last time.

- [ ] **Step 2: Commit**

```bash
git add DESIGN.md README.md pipeline/README.md
git commit -m "DESIGN: address-bit permissions, three-state routing, stored registry membership"
```

---

## Final report (the implementer returns this)

Per task: commit SHA, test counts (pipeline, web) before and after, deviations. Then `git log --oneline main..HEAD`, `git diff --stat main..HEAD`, the `npm run build` route table, and the fixture slug you rendered. Do not push.
