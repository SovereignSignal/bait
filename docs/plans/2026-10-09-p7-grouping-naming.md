# Package 7: Grouping and naming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One hook, one right project: bytecode matches across sources, hooks move when the registry names them, template and proxy names never merge strangers, verified-name merges need evidence, and every moved or renamed project leaves a redirect.

**Architecture:** One canonical `code_hash` (`rules.code_hash`) for every writer, plus a bounded re-hash of legacy rows. A `hooks.regroup` flag set by ingest (hook got a hooklist name) and by code-hash writes; the group step's new `_regroup` pass re-runs rule 1 and rule 2 for flagged hooks. Name policy lives in `rules.py` (`useful_name`, `is_proxy_name`, `SHARED_DEPLOYERS`, `merge_evidence`); a name is a candidate and a merge needs shared code, a shared (non-factory) deployer, or the same hooklist name. Every rename, merge and regroup writes `project_aliases`, and `/p/<slug>` redirects through it. Contract-name lookups resolve proxies to their implementation name and stop parking transient failures for 14 days. Two small client fixes ride along: GitHub `activity()` and paid POST retries.

**Tech Stack:** Python 3.12, SQLAlchemy 2 Core, httpx (`MockTransport` in tests), tenacity, pytest; Next.js 16, Drizzle (`db.execute(sql…)`), `tsx --test`.

**Spec:** The 2026-10-09 audit, package 7 findings 1 to 6. **Out of scope:** a teams alias map (FablesRamp, FablesRampETH and FablesRWA stay separate cards); whether `LimitOrderHook` is a template name (owner call, see Task 3 note); the static `keyless.build` snapshot JSON slugs; Claude rule 3's exact-name match.

## Global Constraints

- Python 3.12. Run tests with `cd pipeline && .venv/bin/python -m pytest -q` (baseline: 132 passed, 3 skipped). Web: `cd web && npm test && npx tsc --noEmit && npm run build`.
- Never call a paid or live API in tests. Fakes and `httpx.MockTransport` only.
- Schema changes go in `db/migrations/0011_grouping.sql` and `pipeline/bait/db.py` (table metadata plus `ADDITIVE_COLUMNS`). Do not use the names package 5 adds in 0010 (`onchain_watermarks`, `pool_stats.vol_source`, `indexer_state.swap_block_time`).
- Do not touch `pipeline/bait/indexer/`, `pipeline/bait/steps/score.py`, or `pipeline/bait/steps/onchain.py` (it already delegates hashing to `keyless.code_hashes`, so it needs no diff). Web changes are limited to `web/src/lib/unlisted.ts`, `web/src/lib/unlisted.test.ts`, `web/src/db/queries.ts` (the redirect query) and `web/src/app/p/[slug]/page.tsx`.
- Commit after every task. Imperative subject, short body when the why is not obvious. End every commit message with these two trailer lines, exactly:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01A2FHLwk9NeThmyZ1JbHe3o`
- No hostnames, absolute paths or secrets in code, docs or commits. Do not push; the orchestrator reviews and pushes.

## Review Focus

1. A hook discovered on-chain, named "OrderHook (Base)" from verified source, then hooklisted as "OrderHook": its project is renamed in place, no `orderhook-2` appears, and the old slug redirects. (Task 6)
2. Two independent `OrderHook` contracts on one chain with no shared code or deployer stay two projects with distinct names; with a shared code hash they merge and the merge is recorded. (Task 4)
3. The existing `/p/erc1967proxy` project is split; proxy clones never re-merge through their shared proxy bytecode, and template names (`FeeHook`) never group by name. (Task 7)
4. Sourcify answering 503 (or a network error) leaves `name_checked_at` empty so the hook is asked again next run, not 14 days later. (Task 8)
5. A paid POST that hits a read timeout or a 5xx is sent exactly once. (Task 10)

---

### Task 1: One canonical code_hash for every writer

**Files:**
- Modify: `pipeline/bait/rules.py` (new `code_hash`, after `slugify`, line 80)
- Modify: `pipeline/bait/keyless.py:26` (import) and `:352-365` (`code_hashes`)
- Modify: `pipeline/bait/clients/blockscout.py:4,54,71,74-75`
- Test: `pipeline/tests/test_rules.py`, `pipeline/tests/test_keyless.py`

- [ ] **Step 1: Write the failing tests**

Append to `pipeline/tests/test_rules.py`:

```python
def test_code_hash_is_0x_sha256_of_the_bytes():
    import hashlib
    from bait.rules import code_hash
    want = "0x" + hashlib.sha256(bytes.fromhex("6080604052")).hexdigest()
    assert code_hash("0x6080604052") == code_hash("6080604052") == code_hash("0X6080604052") == want
    assert len(want) == 66
    for empty in (None, "", "0x", "0xzz"):
        assert code_hash(empty) is None
```

Append to `pipeline/tests/test_keyless.py`:

```python
def test_keyless_and_blockscout_write_the_same_code_hash():
    import httpx
    from bait import keyless as K
    from bait.clients.blockscout import Blockscout
    from bait.rules import code_hash
    code, a = "0x6080604052", "0x" + "1" * 40

    class Rpc:
        def rpc_batch(self, url, calls):
            return [{"id": c["id"], "result": code} for c in calls]

    bs = Blockscout(http_client=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"deployed_bytecode": code, "is_verified": False}, request=req))))
    assert K.code_hashes(Rpc(), {"base": {a}})[("base", a)] == bs.fetch_code(8453, a).code_hash == code_hash(code)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_rules.py tests/test_keyless.py -k code_hash`
Expected: FAIL, `ImportError: cannot import name 'code_hash'`.

- [ ] **Step 3: Implement**

`pipeline/bait/rules.py`: add `import hashlib` to the imports and, after `slugify`:

```python
def code_hash(code_hex: str | None) -> str | None:
    """Canonical bytecode fingerprint: "0x" + sha256 of the deployed bytes, lowercase hex. None for no code or non-hex.
    Every writer (keyless RPC, Blockscout) goes through here so grouping rule 2 compares like with like."""
    s = (code_hex or "").strip().lower().removeprefix("0x")
    if not s:
        return None
    try:
        return "0x" + hashlib.sha256(bytes.fromhex(s)).hexdigest()
    except ValueError:
        return None
```

`pipeline/bait/keyless.py:26`: `from .rules import code_hash as canonical_code_hash, flags_from_address, normalize_name, routable, score_trending, slugify`. In `code_hashes`, docstring becomes `"""Canonical code_hash (rules.code_hash) per (net, address); clones of one implementation share it."""` and the inner write becomes:

```python
            h = canonical_code_hash(code) if code and code != "0x" else None
            if h:
                out[(net, a)] = h
```

Keep `import hashlib` in keyless (line 99 still uses sha1 for cache file names, which is not a code hash).

`pipeline/bait/clients/blockscout.py`: delete `import hashlib` and `_hash`; add `from ..rules import code_hash as canonical_code_hash`; replace both `_hash(code)` calls with `canonical_code_hash(code)`.

- [ ] **Step 4: Run the full suite**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: 134 passed, 3 skipped.

- [ ] **Step 5: Commit**

```bash
git add pipeline/bait/rules.py pipeline/bait/keyless.py pipeline/bait/clients/blockscout.py pipeline/tests/test_rules.py pipeline/tests/test_keyless.py
git commit -m "Grouping: one canonical code_hash (0x + sha256 of the bytes) for keyless and Blockscout

keyless stored sha1 of the hex string, Blockscout 0x+sha256 of the bytes, so rule 2
never matched an unlisted clone to its registered hook."
```

---

### Task 2: Schema (regroup flag, project_aliases) and alias helpers

**Files:**
- Create: `db/migrations/0011_grouping.sql`
- Modify: `pipeline/bait/db.py:52-69` (hooks), after `digests` (new table), `:308-316` (`ADDITIVE_COLUMNS`)
- Modify: `pipeline/bait/steps/names.py` (imports, `_rename:79-119`, `_unique_slug:132-138`, `_delete_project:141-144`)
- Test: `pipeline/tests/test_group.py`, `pipeline/tests/test_names.py`

**Interfaces:**
- Produces: `hooks.regroup` (bool, default false). `project_aliases(old_slug PK, project_id FK, reason, created_at)`.
- Produces in `bait.steps.names`: `add_alias(conn, old_slug, pid, reason, now)`, `retire_project(conn, pid, into, reason, now)`, `unique_slug(conn, name, pid)`, `hook_rows(conn, pid)`, `backfill_unlisted_aliases(conn, now) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `pipeline/tests/test_group.py`:

```python
def test_regroup_column_is_added_to_an_existing_hooks_table(tmp_path):
    e = db.make_engine(f"sqlite:///{tmp_path}/old.sqlite")
    with e.begin() as c:
        c.execute(sa.text("CREATE TABLE hooks (address text NOT NULL, chain_id integer NOT NULL, name text,"
                          " first_seen_at timestamp NOT NULL, PRIMARY KEY (address, chain_id))"))
        c.execute(sa.text("INSERT INTO hooks VALUES ('0xa', 1, NULL, '2026-10-06 06:00:00')"))
    db.create_all(e)
    assert "hooks.regroup" in db.add_missing_columns(e)
    with e.begin() as c:
        assert c.execute(sa.text("SELECT regroup FROM hooks")).scalar_one() == 0
        assert sa.inspect(c).has_table("project_aliases")
```

Append to `pipeline/tests/test_names.py`:

```python
def _aliases(conn):
    return {r.old_slug: (r.project_id, r.reason) for r in conn.execute(sa.select(db.project_aliases))}


def test_retire_moves_hooks_and_repoints_aliases(conn, ctx):
    a, b, c = (insert_project(conn, n, NOW, slug=n.lower()) for n in ("A", "B", "C"))
    insert_hook(conn, A(20), 1, project_id=a)
    names.add_alias(conn, "old-a", a, "renamed", NOW)
    names.retire_project(conn, a, into=b, reason="test", now=NOW)
    assert _aliases(conn) == {"old-a": (b, "renamed"), "a": (b, "test")}
    assert conn.execute(sa.select(db.hooks.c.project_id).where(db.hooks.c.address == A(20))).scalar_one() == b
    names.retire_project(conn, c, into=None, reason="split", now=NOW)
    assert {r.id for r in conn.execute(sa.select(db.projects.c.id))} == {b}


def test_backfill_unlisted_aliases_recreates_old_unlisted_slugs(conn, ctx):
    named = insert_project(conn, "OuroborosHook", NOW, slug="ouroboroshook")
    insert_hook(conn, "0x7bc2af6fb9a989505e9629470869a19143532aec", 4663, project_id=named)
    x, y = insert_project(conn, "X", NOW, slug="x"), insert_project(conn, "Y", NOW, slug="y")
    insert_hook(conn, "0x1111" + "0" * 32 + "beef", 1, project_id=x)        # these two would share one old slug:
    insert_hook(conn, "0x1111" + "1" * 32 + "beef", 8453, project_id=y)     # ambiguous, so neither gets it
    conn.execute(sa.update(db.hooks).values(hooklisted=False))
    assert names.backfill_unlisted_aliases(conn, NOW) == 1
    assert names.backfill_unlisted_aliases(conn, NOW) == 0                  # idempotent
    assert _aliases(conn) == {"unlisted-hook-0x7bc2-2aec": (named, "backfill")}
```

In `test_no_client_still_renames_from_stored_names`, append:

```python
    assert _aliases(conn) == {"unlisted-000009-130": (pid, "renamed")}
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_group.py tests/test_names.py`
Expected: 4 FAIL (`AttributeError: ... has no attribute 'project_aliases'` / `'add_alias'`, and `"hooks.regroup" not in` the added list).

- [ ] **Step 3: Migration**

Create `db/migrations/0011_grouping.sql`:

```sql
-- Package 7: regrouping and project aliases (pipeline group step). The pipeline also creates these itself
-- (db.create_all + db.add_missing_columns), so applying this file is optional on a database the pipeline has run on.
ALTER TABLE hooks ADD COLUMN IF NOT EXISTS regroup boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS project_aliases (
  old_slug    TEXT PRIMARY KEY,
  project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  reason      TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS project_aliases_project_idx ON project_aliases (project_id);
```

- [ ] **Step 4: db.py**

In `hooks`, after `name_checked_at`:

```python
    sa.Column("regroup", sa.Boolean, nullable=False, server_default=sa.false()),  # ingest named it / code_hash landed; group clears
```

After `digests`:

```python
# Old project slugs -> current project (db/migrations/0011_grouping.sql). Written by the group step on every rename,
# merge and regroup; the web redirects /p/<old_slug> through it.
project_aliases = sa.Table(
    "project_aliases", metadata,
    sa.Column("old_slug", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("created_at", TS, nullable=False),
    sa.Index("project_aliases_project_idx", "project_id"),
)
```

Append to `ADDITIVE_COLUMNS`: `("hooks", "regroup", "boolean NOT NULL DEFAULT false"),`

- [ ] **Step 5: names.py helpers**

Imports: `from ..db import hooks, project_aliases, project_daily, projects, receipts, signals, upsert`. Rename `_unique_slug` to `unique_slug` (one call site in `_rename`). Replace `_delete_project` with:

```python
def add_alias(conn, old_slug: str, pid: int, reason: str, now) -> None:
    """/p/<old_slug> redirects to project pid. The latest write wins."""
    upsert(conn, project_aliases, [{"old_slug": old_slug, "project_id": pid, "reason": reason, "created_at": now}],
           keys=["old_slug"])


def retire_project(conn, pid: int, into: int | None, reason: str, now) -> None:
    """Delete a project. With `into`, its hooks move there and its slug, plus every alias that pointed at it, redirects
    there. Without (a split; hooks already detached), aliases to it are dropped: no single project is the right target."""
    if into is not None:
        conn.execute(sa.update(hooks).values(project_id=into).where(hooks.c.project_id == pid))
        conn.execute(sa.update(project_aliases).values(project_id=into).where(project_aliases.c.project_id == pid))
        add_alias(conn, conn.execute(sa.select(projects.c.slug).where(projects.c.id == pid)).scalar_one(), into, reason, now)
    else:
        conn.execute(sa.delete(project_aliases).where(project_aliases.c.project_id == pid))
    for t in (project_daily, signals, receipts):
        conn.execute(sa.delete(t).where(t.c.project_id == pid))
    conn.execute(sa.delete(projects).where(projects.c.id == pid))


def hook_rows(conn, pid: int):
    return conn.execute(sa.select(hooks.c.code_hash, hooks.c.deployer, hooks.c.name, hooks.c.contract_name)
                        .where(hooks.c.project_id == pid)).all()


def backfill_unlisted_aliases(conn, now) -> int:
    """Bounded migration (delete 30 days after deploy): links shared before project_aliases existed point at
    "unlisted-hook-0x7bc2-2aec". Recreate that slug for each on-chain hook whose project slug differs now, unless two hooks
    would share it (ambiguous) or a live project or alias already holds it. Family slugs are not recreated: their 16-bit
    prefix is too ambiguous to redirect safely. Idempotent."""
    rows = conn.execute(sa.select(hooks.c.address, hooks.c.project_id)
                        .where(hooks.c.project_id.is_not(None), hooks.c.hooklisted.is_(False))).all()
    want: dict[str, set[int]] = {}
    for r in rows:
        want.setdefault(slugify(f"Unlisted hook {r.address[:6]}…{r.address[-4:]}"), set()).add(r.project_id)
    taken = set(conn.execute(sa.select(projects.c.slug)).scalars()) | set(conn.execute(sa.select(project_aliases.c.old_slug)).scalars())
    n = 0
    for old, pids in sorted(want.items()):
        if len(pids) == 1 and old not in taken:
            add_alias(conn, old, pids.pop(), "backfill", now)
            n += 1
    return n
```

In `run`, call `_rename(conn, out, ctx.now)`. In `_rename(conn, out: dict, now)`, replace the merge branch body with `retire_project(conn, pid, into=target, reason="names:merge", now=now)` (the hook update moves inside it), and replace the rename write with:

```python
        old = conn.execute(sa.select(projects.c.slug).where(projects.c.id == pid)).scalar_one()
        slug = unique_slug(conn, display, pid)
        conn.execute(sa.update(projects).values(name=display, norm_name=key, slug=slug).where(projects.c.id == pid))
        if slug != old:
            add_alias(conn, old, pid, "renamed", now)
```

- [ ] **Step 6: Run the full suite**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: 137 passed, 3 skipped.

- [ ] **Step 7: Commit**

```bash
git add db/migrations/0011_grouping.sql pipeline/bait/db.py pipeline/bait/steps/names.py pipeline/tests/test_group.py pipeline/tests/test_names.py
git commit -m "Schema: hooks.regroup and project_aliases; every rename and merge records the old slug"
```

---

### Task 3: Name policy in rules.py

**Files:**
- Modify: `pipeline/bait/rules.py` (after `normalize_name`, line 75)
- Modify: `pipeline/bait/clients/contract_names.py:17-28` (GENERIC and `useful_name` move out, re-exported)
- Test: `pipeline/tests/test_rules.py`

**Interfaces:**
- Produces: `GENERIC`, `useful_name(name, max_len=60)` (pass `max_len=None` for hooklist names), `is_proxy_name(name)`, `SHARED_DEPLOYERS`, `merge_evidence(a_rows, b_rows) -> "code_hash" | "deployer" | "hooklist_name" | None`. Rows need `code_hash`, `deployer`, `name`, `contract_name`.

- [ ] **Step 1: Write the failing tests**

Append to `pipeline/tests/test_rules.py`:

```python
def test_useful_name_and_proxy_names():
    from bait.rules import is_proxy_name, useful_name
    assert useful_name("FoundationHookV2") == "FoundationHookV2"
    assert useful_name("x" * 61) is None and useful_name("Long " + "x" * 70, max_len=None) == "Long " + "x" * 70
    for g in ("ERC1967Proxy", "FeeHook", "Counter", " BaseHook ", "", None):
        assert useful_name(g, max_len=None) is None
    assert is_proxy_name("TransparentUpgradeableProxy") and not is_proxy_name("OrderHook") and not is_proxy_name(None)


def test_merge_evidence_needs_code_deployer_or_hooklist_name():
    from types import SimpleNamespace
    from bait.rules import merge_evidence

    def h(code=None, dep=None, name=None, cname=None):
        return SimpleNamespace(code_hash=code, deployer=dep, name=name, contract_name=cname)
    assert merge_evidence([h(cname="OrderHook")], [h(cname="OrderHook")]) is None        # a name alone is not evidence
    assert merge_evidence([h(code="0xa")], [h(code="0xa")]) == "code_hash"
    assert merge_evidence([h(code="0xp", cname="ERC1967Proxy")], [h(code="0xp")]) is None  # proxy bytecode is shared
    assert merge_evidence([h(dep="0xd")], [h(dep="0xd")]) == "deployer"
    create2 = "0x4e59b44847b379578588920ca78fbf26c0b4956c"
    assert merge_evidence([h(dep=create2)], [h(dep=create2)]) is None                     # CREATE2 factory deploys for everyone
    assert merge_evidence([h(name="Bunni Hooks")], [h(name="BunniHook")]) == "hooklist_name"
    assert merge_evidence([h(name="FeeHook")], [h(name="FeeHook")]) is None                # template name
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_rules.py -k "proxy or evidence"`
Expected: 2 FAIL, `ImportError`.

- [ ] **Step 3: Implement**

In `pipeline/bait/rules.py`, after `normalize_name`:

```python
# Names that say nothing about the project: proxies and template defaults. Applies to hooklist and verified-source names.
GENERIC = re.compile(
    r"(?i)^(hook|basehook|v4hook|uniswapv4hook|uniswaphook|anyhook|customhook|myhook|testhook|counter|counterhook|"
    r"swapfeehook|feehook|platformhook|.*proxy.*|implementation|main|contract)$")

# Deterministic CREATE2 deployers (Arachnid, ImmutableCreate2Factory, CreateX) deploy for everyone: not team evidence.
SHARED_DEPLOYERS = frozenset({
    "0x4e59b44847b379578588920ca78fbf26c0b4956c",
    "0x0000000000ffe8b47b3e2130213b802212439497",
    "0xba5ed099633d3b313e4d5f7bdc1305d3c28ba5ed",
})


def useful_name(name: str | None, max_len: int | None = 60) -> str | None:
    """The name stripped, or None when it is empty, generic, or (verified-source names) longer than max_len."""
    n = (name or "").strip()
    if not n or (max_len and len(n) > max_len) or GENERIC.match(n):
        return None
    return n


def is_proxy_name(name: str | None) -> bool:
    return "proxy" in (name or "").lower()


def merge_evidence(a, b) -> str | None:
    """Why two sets of hooks are one project, or None. A shared name is only a candidate; evidence is shared non-proxy
    bytecode, a shared deployer that is not a CREATE2 factory, or the same non-generic hooklist name."""
    def hashes(rows):
        return {r.code_hash for r in rows if r.code_hash and not (is_proxy_name(r.name) or is_proxy_name(r.contract_name))}

    def deployers(rows):
        return {r.deployer for r in rows if r.deployer and r.deployer not in SHARED_DEPLOYERS}

    def listed(rows):
        return {normalize_name(useful_name(r.name, max_len=None)) for r in rows} - {""}

    if hashes(a) & hashes(b):
        return "code_hash"
    if deployers(a) & deployers(b):
        return "deployer"
    if listed(a) & listed(b):
        return "hooklist_name"
    return None
```

In `pipeline/bait/clients/contract_names.py`, delete `GENERIC` and `useful_name`, drop `import re`, and add `from ..rules import GENERIC, useful_name  # noqa: F401  re-exported; the list lives in rules`.

- [ ] **Step 4: Run the full suite**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: 139 passed, 3 skipped.

- [ ] **Step 5: Commit**

```bash
git add pipeline/bait/rules.py pipeline/bait/clients/contract_names.py pipeline/tests/test_rules.py
git commit -m "rules: generic names, CREATE2 deployers and merge evidence in one place"
```

Note for the owner, not a step: `LimitOrderHook` is not added to `GENERIC`. Finding 4 counts the same hooklist name as a family, so its 8-chain project stays one card; adding it to `GENERIC` is a one-line follow-up if you decide it is a template name.

---

### Task 4: Verified-name merges need evidence

**Files:**
- Modify: `pipeline/bait/steps/names.py` (`run` counters, `_rename` rows select and merge branch)
- Test: `pipeline/tests/test_names.py`

- [ ] **Step 1: Write the failing tests**

In `test_rename_merge_clash_generic_and_miss`, after `p6 = ...` add:

```python
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address.in_([A(1), A(2)])).values(code_hash="0x" + "c" * 64))
```

and append:

```python
    assert _aliases(conn)["unlisted-000002-8453"] == (p1, "names:code_hash")
```

Append a new test:

```python
def test_same_name_same_chain_without_evidence_stays_two_projects(conn, ctx):
    p1 = _unlisted(conn, A(10), 8453, vol=9)
    p2 = _unlisted(conn, A(11), 8453, vol=8)
    ctx.clients["names"] = FakeNames({A(10): "OrderHook", A(11): "OrderHook"})
    out = {}
    names.run(conn, ctx, out)
    proj = {r.id: r for r in conn.execute(sa.select(db.projects))}
    assert out["projects_merged"] == 0 and out["names_split"] == 1
    assert proj[p1].name == "OrderHook" and proj[p2].name == "OrderHook 0x0000…000b"
    assert proj[p2].slug == "orderhook-0x0000-000b" and _aliases(conn)["unlisted-00000b-8453"] == (p2, "renamed")
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_names.py`
Expected: 2 FAIL (reason is `names:merge`; `KeyError: 'names_split'`).

- [ ] **Step 3: Implement**

In `run`, add `out.setdefault("names_split", 0)`. Import `merge_evidence` from `..rules`. In `_rename`, select `hooks.c.address` too, and replace the block from `target = by_key.get(key)` to the `display` clash rule with:

```python
        target = by_key.get(key)
        display = cname
        if target is not None and target != pid:
            why = merge_evidence(hook_rows(conn, pid), hook_rows(conn, target))
            if why:
                retire_project(conn, pid, into=target, reason=f"names:{why}", now=now)
                out["projects_merged"] += 1
                continue
            # Same contract name on one chain, no shared code or deployer: likely two teams. Keep both, tell them apart.
            first = min(h.address for h in hs)
            key, display = f"{key}:{first}", f"{cname} {first[:6]}…{first[-4:]}"
            out["names_split"] += 1
        elif display.lower() in taken_names or norm in curated_norms:
            display = f"{cname} ({chain_label(chain_id)})"
```

Update the module docstring point 2: "Same contract name on one chain merges only with evidence (shared code_hash or deployer, `rules.merge_evidence`); without it the later project gets a short-address suffix."

- [ ] **Step 4: Run the full suite**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: 140 passed, 3 skipped.

- [ ] **Step 5: Commit**

```bash
git add pipeline/bait/steps/names.py pipeline/tests/test_names.py
git commit -m "names: merge same-named contracts only on shared code or deployer; otherwise keep both, suffixed"
```

---

### Task 5: Ingest marks hooks the registry just named

**Files:**
- Modify: `pipeline/bait/steps/ingest.py:14-46`
- Test: `pipeline/tests/test_ingest.py`

- [ ] **Step 1: Write the failing test**

Update both `assert out == {...}` dicts in `test_ingest_fixture_then_idempotent` to include `"regroup": 0`. Append:

```python
def test_ingest_marks_grouped_hooks_the_registry_just_named(conn, ctx, fixture_entries):
    from tests.conftest import NOW, insert_project
    e = fixture_entries[0]["hook"]
    addr, cid = e["address"].lower(), e["chainId"]
    pid = insert_project(conn, "Unlisted hook x", NOW, slug="unlisted-x")
    insert_hook(conn, addr, cid, project_id=pid)                         # found on-chain before its PR merged
    insert_hook(conn, "0x" + "c" * 40, 8453, project_id=pid)             # never listed: not marked
    conn.execute(sa.update(db.hooks).values(hooklisted=False))
    out = ingest.run(conn, ctx)
    marked = {r.address for r in conn.execute(sa.select(db.hooks.c.address).where(db.hooks.c.regroup.is_(True)))}
    assert out["regroup"] == 1 and marked == {addr}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_ingest.py`
Expected: 2 FAIL (`KeyError: 'regroup'` and the dict mismatch).

- [ ] **Step 3: Implement**

In `run`, `out` gains `"regroup": 0`. Replace the `existing = ...` line with:

```python
    prev = {(r.address, r.chain_id): r for r in conn.execute(
        sa.select(hooks.c.address, hooks.c.chain_id, hooks.c.name, hooks.c.hooklisted, hooks.c.project_id))}
    existing = set(prev)
```

After the `stale` loop:

```python
    # A hook grouped before the registry knew it ("Unlisted hook ..." or its verified-source name) is regrouped by the
    # group step once the registry names it or lists it.
    named = {(r["address"], r["chain_id"]): r["name"] for r in rows}
    regroup = [k for k, nm in named.items() if (p := prev.get(k)) is not None and p.project_id is not None
               and ((nm and not (p.name or "").strip()) or p.hooklisted is False)]
    for i in range(0, len(regroup), 500):
        conn.execute(sa.update(hooks).values(regroup=True)
                     .where(sa.tuple_(hooks.c.address, hooks.c.chain_id).in_(regroup[i:i + 500])))
    out["regroup"] = len(regroup)
```

- [ ] **Step 4: Run the full suite**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: 141 passed, 3 skipped.

- [ ] **Step 5: Commit**

```bash
git add pipeline/bait/steps/ingest.py pipeline/tests/test_ingest.py
git commit -m "ingest: mark grouped hooks for regroup when the hooklist names or lists them"
```

---

### Task 6: Group step regroups (name, contract adoption, bytecode) and re-hashes legacy rows

**Files:**
- Modify: `pipeline/bait/steps/group.py:1-31` (imports, `run` head, rule 1), `:153-171` (`_fetch_code_hashes`), new helpers after `_create_project`
- Test: `pipeline/tests/test_group.py`

**Interfaces:**
- Consumes: Task 2 helpers, Task 3 rules, Task 5 flags.
- Produces: group out keys `hashes_rehashed, aliases_backfilled, split, promoted, moved, retired, adopted, by_deployer` (`split` and `by_deployer` are filled in Task 7).

- [ ] **Step 1: Write the failing tests**

Append to `pipeline/tests/test_group.py` (add `import copy, json` and `from tests.conftest import NOW, insert_project` at the top):

```python
def A(n):
    return "0x" + f"{n:040x}"


def _unlisted_project(conn, addr, chain_id, **hook):
    pid = insert_project(conn, f"Unlisted hook {addr[:6]}…{addr[-4:]}", NOW, slug=f"unlisted-hook-{addr[:6]}-{addr[-4:]}")
    conn.execute(sa.update(db.projects).where(db.projects.c.id == pid).values(norm_name=None))
    insert_hook(conn, addr, chain_id, project_id=pid, **hook)
    return pid


def _hooklist(tmp_path, fixture_entries, *hooks):
    out = []
    for addr, chain_id, name in hooks:
        e = copy.deepcopy(fixture_entries[0])
        e["hook"].update(address=addr, chainId=chain_id, name=name)
        out.append(e)
    path = tmp_path / "hooklist.json"
    path.write_text(json.dumps(out))
    return str(path)


def _alias(conn, slug):
    return conn.execute(sa.select(db.project_aliases.c.project_id, db.project_aliases.c.reason)
                        .where(db.project_aliases.c.old_slug == slug)).first()


def test_hooklisted_later_renames_its_contract_project_in_place(conn, ctx, tmp_path, fixture_entries):
    pid = insert_project(conn, "OrderHook (Base)", NOW, slug="orderhook-base")
    conn.execute(sa.update(db.projects).where(db.projects.c.id == pid).values(norm_name="contract:8453:order"))
    insert_hook(conn, A(1), 8453, project_id=pid)
    conn.execute(sa.update(db.hooks).values(hooklisted=False, contract_name="OrderHook"))
    ctx.hooklist_file = _hooklist(tmp_path, fixture_entries, (A(1), 8453, "OrderHook"))
    ingest.run(conn, ctx)
    out = group.run(conn, ctx)
    assert out["promoted"] == 1 and out["created"] == 0
    assert [(p.id, p.name, p.norm_name, p.slug) for p in conn.execute(sa.select(db.projects))] == [(pid, "OrderHook", "order", "orderhook")]
    assert tuple(_alias(conn, "orderhook-base")) == (pid, "renamed")
    assert conn.execute(sa.select(db.hooks.c.regroup)).scalar_one() is False


def test_hooklisted_later_joins_the_curated_project_and_old_link_redirects(conn, ctx, tmp_path, fixture_entries):
    bunni = insert_project(conn, "Bunni", NOW, slug="bunni")
    lone = _unlisted_project(conn, A(2), 8453)
    conn.execute(sa.update(db.hooks).values(hooklisted=False))
    ctx.hooklist_file = _hooklist(tmp_path, fixture_entries, (A(2), 8453, "BunniHook"))
    ingest.run(conn, ctx)
    out = group.run(conn, ctx)
    assert out["moved"] == 1 and out["retired"] == 1
    assert conn.execute(sa.select(db.hooks.c.project_id)).scalar_one() == bunni
    assert lone not in {r.id for r in conn.execute(sa.select(db.projects.c.id))}
    assert tuple(_alias(conn, "unlisted-hook-0x0000-0002")) == (bunni, "regroup:name")


def test_legacy_sha1_hash_is_rehashed_and_the_clone_joins_the_registered_project(conn, ctx, monkeypatch):
    canon = "0x" + "c" * 64
    saturn = insert_project(conn, "Saturn", NOW, slug="saturn")
    insert_hook(conn, A(3), 8453, project_id=saturn, name="Saturn", code_hash=canon)   # Blockscout wrote this one
    _unlisted_project(conn, A(4), 8453, code_hash="f" * 40)                            # keyless sha1: never matched
    other = _unlisted_project(conn, A(5), 1, code_hash="e" * 40)                      # RPC did not answer: kept
    asked = []
    monkeypatch.setattr(group, "_rpc_code_hashes", lambda ctx, rows, out: asked.extend(rows) or {(8453, A(4)): canon})
    out = group.run(conn, ctx)
    pid = {r.address: (r.project_id, r.code_hash) for r in conn.execute(sa.select(db.hooks))}
    assert out["hashes_rehashed"] == 1 and len(asked) == 2
    assert pid[A(4)] == (saturn, canon) and pid[A(5)] == (other, "e" * 40)
    assert tuple(_alias(conn, "unlisted-hook-0x0000-0004")) == (saturn, "regroup:code_hash")


def test_rule1_adopts_a_same_chain_contract_project_only_with_evidence(conn, ctx):
    order = insert_project(conn, "OrderHook (Base)", NOW, slug="orderhook-base")
    pool = insert_project(conn, "PoolHook", NOW, slug="poolhook")
    conn.execute(sa.update(db.projects).where(db.projects.c.id == order).values(norm_name="contract:8453:order"))
    conn.execute(sa.update(db.projects).where(db.projects.c.id == pool).values(norm_name="contract:8453:pool"))
    insert_hook(conn, A(6), 8453, project_id=order)
    insert_hook(conn, A(7), 8453, project_id=pool)
    insert_hook(conn, A(8), 8453, name="OrderHook")
    insert_hook(conn, A(9), 8453, name="PoolHook")
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address.in_([A(6), A(8)])).values(deployer="0x" + "d" * 40))
    out = group.run(conn, ctx)
    assert out["adopted"] == 1 and out["created"] == 1 and out["by_name"] == 2
    p = {r.id: r for r in conn.execute(sa.select(db.projects))}
    assert (p[order].name, p[order].norm_name, p[order].slug) == ("OrderHook", "order", "orderhook")
    pid = {r.address: r.project_id for r in conn.execute(sa.select(db.hooks))}
    assert pid[A(8)] == order and pid[A(9)] not in (pool, None)                       # a name alone does not merge
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_group.py`
Expected: 4 FAIL (`KeyError: 'promoted'` / `'moved'`, `AttributeError: ... '_rpc_code_hashes'`, `KeyError: 'adopted'`).

- [ ] **Step 3: Implement**

Imports in `group.py`:

```python
from types import SimpleNamespace

from ..db import backfill_address_flags, hooks, projects, utcnow
from ..rules import SHARED_DEPLOYERS, is_proxy_name, merge_evidence, normalize_name, slugify, useful_name
from . import RunContext, log
from .names import add_alias, backfill_unlisted_aliases, hook_rows, retire_project, unique_slug
from .onchain import _code_hashes as _rpc_code_hashes     # free public-RPC bytecode, keyless chains, KEYLESS != 0
```

Head of `run`, replacing lines 15-31:

```python
    out = {"by_name": 0, "by_code": 0, "by_deployer": 0, "by_claude": 0, "created": 0, "unassigned": 0,
           "code_fetched": 0, "factories": 0, "split": 0, "promoted": 0, "moved": 0, "retired": 0, "adopted": 0}
    out["flags_backfilled"] = backfill_address_flags(conn)
    out["hashes_rehashed"] = _backfill_code_hashes(conn, ctx)
    out["aliases_backfilled"] = backfill_unlisted_aliases(conn, ctx.now)
    _regroup(conn, ctx, out)
    unassigned = _unassigned(conn)

    # Rule 1: hooklist name -> curated project, else a same-chain verified-name project with evidence, else a new one.
    by_norm = _projects_by_norm(conn)
    for h in unassigned:
        name = _gname(h)
        norm = normalize_name(name)
        if not norm:
            continue
        pid = by_norm.get(norm)
        if pid is None and (pid := _adopt_contract(conn, ctx, h, name, norm, by_norm)) is not None:
            out["adopted"] += 1
        if pid is None:
            pid = _create_project(conn, ctx, name, norm, h.description)
            by_norm[norm] = pid
            out["created"] += 1
        _assign(conn, h, pid)
        out["by_name"] += 1
```

In `_fetch_code_hashes`, the write becomes `vals = {"code_hash": info.code_hash, "regroup": h.project_id is not None}` so a newly hashed grouped hook is checked against same-bytecode projects next run.

New helpers after `_create_project`:

```python
LEGACY_HASH_BATCH = 2000


def _gname(h) -> str | None:
    """The hooklist name if it identifies a project; template and proxy names count as no name."""
    return useful_name(h.name, max_len=None)


def _proxy(h) -> bool:
    return is_proxy_name(h.name) or is_proxy_name(h.contract_name)


def _provisional(name: str, norm: str | None) -> bool:
    """Auto-named projects a hook may leave: unlisted, bytecode families, verified-source names. Curated ones never."""
    n = norm or ""
    return not n or n.startswith(("contract:", "code:")) or name.startswith("Unlisted hook")


def _backfill_code_hashes(conn, ctx) -> int:
    """Bounded migration (a no-op once no 40-char code_hash is left; delete then): keyless stored sha1 of the hex string.
    Re-hash legacy rows from public-RPC bytecode and mark them for regroup. Rows the RPC did not answer keep their value
    and are retried next run; a 40-char value can never equal a canonical 66-char one, so it cannot false-match."""
    legacy = conn.execute(sa.select(hooks.c.address, hooks.c.chain_id).where(sa.func.length(hooks.c.code_hash) == 40)
                          .order_by(hooks.c.chain_id, hooks.c.address).limit(LEGACY_HASH_BATCH)).all()
    if not legacy:
        return 0
    got = _rpc_code_hashes(ctx, [SimpleNamespace(chain_id=r.chain_id, hook_address=r.address) for r in legacy],
                           {"errors": 0, "code_hashes": 0})
    for (cid, addr), h in got.items():
        conn.execute(sa.update(hooks).values(code_hash=h, regroup=True).where(hooks.c.address == addr, hooks.c.chain_id == cid))
    log.info("group: re-hashed %d of %d legacy code_hash rows", len(got), len(legacy))
    return len(got)


def _move(conn, ctx, h, src: int, target: int, reason: str, out: dict) -> None:
    _assign(conn, h, target)
    out["moved"] += 1
    if not conn.execute(sa.select(hooks.c.address).where(hooks.c.project_id == src).limit(1)).first():
        retire_project(conn, src, into=target, reason=reason, now=ctx.now)
        out["retired"] += 1


def _promote(conn, ctx, p, name: str, norm: str) -> None:
    """An auto-named project becomes the curated project for `name`; its old slug keeps working."""
    slug = unique_slug(conn, name, p.id)
    conn.execute(sa.update(projects).values(name=name, norm_name=norm, slug=slug).where(projects.c.id == p.id))
    if slug != p.slug:
        add_alias(conn, p.slug, p.id, "renamed", ctx.now)


def _adopt_contract(conn, ctx, h, name: str, norm: str, by_norm: dict) -> int | None:
    pid = by_norm.get(f"contract:{h.chain_id}:{norm}")
    if pid is None or not merge_evidence([h], hook_rows(conn, pid)):
        return None                                      # two teams can both write "OrderHook"
    _promote(conn, ctx, conn.execute(sa.select(projects).where(projects.c.id == pid)).one(), name, norm)
    by_norm[norm] = pid
    return pid


def _regroup_by_code(conn, ctx, code_hash: str, out: dict) -> None:
    """Rule 2 across time: same non-proxy bytecode, one project. Target: a curated holder, else a verified-name one,
    else the oldest. Only auto-named projects give hooks up."""
    rows = [r for r in conn.execute(
        sa.select(hooks.c.address, hooks.c.chain_id, hooks.c.name, hooks.c.contract_name, hooks.c.project_id,
                  projects.c.name.label("pname"), projects.c.norm_name.label("pnorm"))
        .join(projects, projects.c.id == hooks.c.project_id).where(hooks.c.code_hash == code_hash)) if not _proxy(r)]
    if len({r.project_id for r in rows}) < 2:
        return
    target = min(rows, key=lambda r: (_provisional(r.pname, r.pnorm), r.pname.startswith("Unlisted hook"), r.project_id)).project_id
    for r in rows:
        if r.project_id != target and _provisional(r.pname, r.pnorm):
            _move(conn, ctx, r, r.project_id, target, "regroup:code_hash", out)


def _regroup(conn, ctx, out: dict) -> None:
    """Hooks flagged by ingest (the registry named them) or by a code_hash write. A named hook in an auto-named project
    joins the curated project with that name, or its project is promoted to that name. A hashed one pulls same-bytecode
    hooks together. Unassigned flagged hooks are left to rules 1-3."""
    marked = conn.execute(sa.select(hooks).where(hooks.c.regroup.is_(True)).order_by(hooks.c.chain_id, hooks.c.address)).all()
    by_norm = _projects_by_norm(conn)
    for h in marked:
        cur = conn.execute(sa.select(projects).join(hooks, hooks.c.project_id == projects.c.id)
                           .where(hooks.c.address == h.address, hooks.c.chain_id == h.chain_id)).first()
        if cur is None:
            continue
        name = _gname(h)
        norm = normalize_name(name)
        if norm and _provisional(cur.name, cur.norm_name):
            target = by_norm.get(norm)
            if target is not None and target != cur.id:
                _move(conn, ctx, h, cur.id, target, "regroup:name", out)
            else:
                _promote(conn, ctx, cur, name, norm)
                by_norm[norm] = cur.id
                out["promoted"] += 1
        elif h.code_hash and not _proxy(h):
            _regroup_by_code(conn, ctx, h.code_hash, out)
    if marked:
        conn.execute(sa.update(hooks).values(regroup=False).where(hooks.c.regroup.is_(True)))
```

Update the module docstring: "Rule 1 hooklist name (generic names excluded; a same-chain verified-name project is adopted only with evidence), rule 2 code_hash or deployer, rule 3 Claude. Flagged hooks are regrouped first; moved and renamed projects leave aliases."

- [ ] **Step 4: Run the full suite**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: 145 passed, 3 skipped.

- [ ] **Step 5: Commit**

```bash
git add pipeline/bait/steps/group.py pipeline/tests/test_group.py
git commit -m "group: regroup hooks the registry named, re-hash legacy code hashes, adopt verified-name projects on evidence

A hook found on-chain before its hooklist PR merged stayed 'Unlisted hook' forever, and a
later hooklist hook with its contract name made a duplicate <slug>-2 project."
```

---

### Task 7: Template and proxy names never group by name or by proxy bytecode

**Files:**
- Modify: `pipeline/bait/steps/group.py` (`run`: split call, rule 2 lines 33-64, rule 3 lines 70-87; new `_split_generic`, `_join_targets`)
- Test: `pipeline/tests/test_group.py`

- [ ] **Step 1: Write the failing tests**

In `test_group_by_name_and_factories`, change the first assert to:

```python
    # 9 template-named hooks (CustomHook x2, TestHook, FeeHook x3, Counter x2, SwapFeeHook) no longer merge by name
    assert out["by_name"] == 291 and out["created"] == 95 and out["unassigned"] == 0
```

Append:

```python
def test_proxy_named_project_is_split_and_proxies_never_join_by_bytecode(conn, ctx):
    proxy = insert_project(conn, "ERC1967Proxy", NOW, slug="erc1967proxy")
    insert_hook(conn, A(11), 8453, project_id=proxy, name="ERC1967Proxy", code_hash="0xp")
    insert_hook(conn, A(12), 56, project_id=proxy, name="ERC1967Proxy", code_hash="0xp")
    insert_hook(conn, A(13), 8453, code_hash="0xp")                                    # on-chain proxy clone
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == A(13)).values(contract_name="ERC1967Proxy"))
    out = group.run(conn, ctx)
    assert out["split"] == 1 and out["by_code"] == 0 and out["by_name"] == 0
    rows = conn.execute(sa.select(db.projects.c.name, db.projects.c.slug).join(db.hooks)).all()
    assert len({r.slug for r in rows}) == 3 and all(r.name.startswith("Unlisted hook 0x") for r in rows)
    assert _alias(conn, "erc1967proxy") is None                                       # no single right redirect


def test_template_named_hooks_group_by_deployer_never_by_name(conn, ctx):
    acme = insert_project(conn, "Acme", NOW, slug="acme")
    other = insert_project(conn, "Other", NOW, slug="other")
    insert_hook(conn, A(21), 8453, project_id=acme, name="Acme")
    insert_hook(conn, A(22), 8453, project_id=other, name="Other")
    for n in (23, 24, 25):
        insert_hook(conn, A(n), 8453, name="FeeHook")
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address.in_([A(21), A(23)])).values(deployer="0x" + "d" * 40))
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address.in_([A(22), A(25)]))
                 .values(deployer="0x4e59b44847b379578588920ca78fbf26c0b4956c"))
    out = group.run(conn, ctx)
    pid = {r.address: r.project_id for r in conn.execute(sa.select(db.hooks))}
    assert out["by_deployer"] == 1 and out["by_name"] == 0
    assert pid[A(23)] == acme and pid[A(25)] != other and pid[A(24)] != pid[A(25)]
    assert "FeeHook" not in {r.name for r in conn.execute(sa.select(db.projects.c.name))}
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_group.py`
Expected: 3 FAIL (`by_name == 300`, `split == 0`, `by_deployer == 0`).

- [ ] **Step 3: Implement**

In `run`, before `_regroup(conn, ctx, out)`: `out["split"] = _split_generic(conn, ctx)`.

Rule 2, replacing lines 38-54 (from `hash_to_pid = ...` through the `still = [...]` filter):

```python
        hash_to_pid, dep_to_pid = _join_targets(conn)
        still = []
        for h in unassigned:
            pid = hash_to_pid.get(h.code_hash) if h.code_hash and not _proxy(h) else None
            if pid:
                _assign(conn, h, pid)
                out["by_code"] += 1
            elif h.deployer and (pid := dep_to_pid.get((h.chain_id, h.deployer))):
                _assign(conn, h, pid)
                out["by_deployer"] += 1
            else:
                still.append(h)
        # Unassigned hooks that share non-proxy bytecode with each other (one launchpad, many clones) become one project.
        fam: dict[str, list] = {}
        for h in still:
            if h.code_hash and not _proxy(h):
                fam.setdefault(h.code_hash, []).append(h)
        still = [h for h in still if _proxy(h) or len(fam.get(h.code_hash, ())) < 2]
```

Rule 3: in the no-Claude branch use `_gname(h) or f"Unlisted hook {h.address[:6]}…{h.address[-4:]}"`; in the Claude branch replace the `bare = ...` line with:

```python
            # Only an address, or a template/proxy name (a free contract-name lookup comes first): never paid for.
            bare = [h for h in unassigned if (h.name or "").strip() or not (h.description or "").strip()]
```

(Every unassigned hook here has no useful name, so a non-empty name means a generic one.)

New helpers:

```python
def _split_generic(conn, ctx) -> int:
    """Projects keyed on a template or proxy name ("ERC1967Proxy") merged unrelated hooks by name alone. Detach their
    hooks so the rules regroup them on evidence; the project goes with no alias (its hooks now live in several)."""
    bad = [p.id for p in conn.execute(sa.select(projects.c.id, projects.c.name, projects.c.norm_name)
                                      .where(projects.c.norm_name.is_not(None)))
           if not p.norm_name.startswith(("contract:", "code:")) and not useful_name(p.name, max_len=None)]
    for pid in bad:
        conn.execute(sa.update(hooks).values(project_id=None).where(hooks.c.project_id == pid))
        retire_project(conn, pid, into=None, reason="split:generic", now=ctx.now)
    if bad:
        log.info("group: split %d projects keyed on a template or proxy name", len(bad))
    return len(bad)


def _join_targets(conn) -> tuple[dict, dict]:
    """code_hash -> project and (chain, deployer) -> project from assigned hooks. Proxy bytecode is shared by unrelated
    deployments and CREATE2 factories deploy for everyone, so neither is evidence."""
    by_hash: dict[str, int] = {}
    by_dep: dict[tuple[int, str], int] = {}
    for r in conn.execute(sa.select(hooks.c.code_hash, hooks.c.deployer, hooks.c.chain_id, hooks.c.name,
                                    hooks.c.contract_name, hooks.c.project_id)
                          .where(hooks.c.project_id.is_not(None)).order_by(hooks.c.project_id)):
        if r.code_hash and not _proxy(r):
            by_hash.setdefault(r.code_hash, r.project_id)
        if r.deployer and r.deployer not in SHARED_DEPLOYERS:
            by_dep.setdefault((r.chain_id, r.deployer), r.project_id)
    return by_hash, by_dep
```

- [ ] **Step 4: Run the full suite**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: 147 passed, 3 skipped.

- [ ] **Step 5: Commit**

```bash
git add pipeline/bait/steps/group.py pipeline/tests/test_group.py
git commit -m "group: template and proxy names never merge by name or proxy bytecode; split /p/erc1967proxy

The hooklist name ERC1967Proxy merged 8+ unrelated hooks on four chains. Generic names now
group by a non-factory deployer or shared non-proxy code only, and wait for a free
contract-name lookup instead of Claude."
```

---

### Task 8: Contract names: proxy implementations, transient failures, per-source outcomes

**Files:**
- Modify: `pipeline/bait/clients/contract_names.py:31-81`
- Modify: `pipeline/bait/steps/names.py:38-76` (`run`, `_lookup`)
- Test: `pipeline/tests/test_names.py`

- [ ] **Step 1: Write the failing tests**

In `test_names.py` add `import pytest`; give `_Http` a `self.params = []` and `self.params.append(params)` in `get`. Append:

```python
def test_proxy_resolves_to_its_implementation_name():
    src = {"compilation": {"name": "ERC1967Proxy"},
           "proxyResolution": {"isProxy": True, "implementations": [{"address": A(2), "name": "OrderBookHook"}]}}
    h = _Http({"sourcify.dev": _Resp(200, src)})
    assert ContractNames(None, http_client=h).lookup(4663, A(1)) == ("OrderBookHook", "sourcify")
    assert h.params[0] == {"fields": "compilation.name,proxyResolution"}
    bs = {"name": "ERC1967Proxy", "is_verified": True, "implementations": [{"address_hash": A(2), "name": "OrderBookHook"}]}
    h = _Http({"robinhoodchain.blockscout.com": _Resp(200, bs)})
    assert ContractNames(None, http_client=h).lookup(4663, A(1)) == ("OrderBookHook", "blockscout")


def test_transient_failure_raises_and_every_source_outcome_is_counted():
    from bait.clients.contract_names import LookupTransient
    cn = ContractNames(None, http_client=_Http({"sourcify.dev": _Resp(503, {}),
                                                 "robinhoodchain.blockscout.com": _Resp(403, "challenge")}))
    with pytest.raises(LookupTransient):
        cn.lookup(4663, A(1))
    assert cn.outcomes == {"sourcify:error": 1, "blockscout_pro:skipped": 1, "blockscout:miss": 1, "routescan:miss": 1}
    only_cloudflare = _Http({"robinhoodchain.blockscout.com": _Resp(403, "challenge")})
    assert ContractNames(None, http_client=only_cloudflare).lookup(4663, A(1)) is None    # a real miss


def test_transient_lookups_are_asked_again_and_generic_hooklist_names_are_looked_up(conn, ctx):
    from bait.clients.contract_names import LookupTransient
    _unlisted(conn, A(30), 4663, vol=9)
    p2 = _unlisted(conn, A(31), 8453, vol=8)
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == A(31)).values(name="ERC1967Proxy", hooklisted=True))

    class Flaky(FakeNames):
        def lookup(self, chain_id, address):
            if address == A(30):
                self.calls.append((chain_id, address))
                raise LookupTransient(address)
            return super().lookup(chain_id, address)

    ctx.clients["names"] = flaky = Flaky({A(31): "OrderBookHook"})
    out = {}
    names.run(conn, ctx, out)
    assert out["names_transient"] == 1
    assert conn.execute(sa.select(db.hooks.c.name_checked_at).where(db.hooks.c.address == A(30))).scalar_one() is None
    assert conn.execute(sa.select(db.projects.c.name).where(db.projects.c.id == p2)).scalar_one() == "OrderBookHook"
    names.run(conn, ctx, {})
    assert flaky.calls.count((4663, A(30))) == 2                                       # not parked for 14 days
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_names.py`
Expected: 3 FAIL (`ERC1967Proxy` returned, `ImportError: LookupTransient`).

- [ ] **Step 3: Implement the client**

`contract_names.py`: add `import threading` and `from collections import Counter`. Replace the class with:

```python
class LookupTransient(Exception):
    """No source had a name and at least one failed in a way that may clear (network, 429, 5xx): ask again next run."""


class _Transient(Exception):
    pass


class _Skip(Exception):
    pass


def _impl_name(impls) -> str | None:
    """First useful implementation name of a proxy (Sourcify proxyResolution, Blockscout implementations)."""
    for i in impls or []:
        if n := useful_name((i or {}).get("name")):
            return n
    return None


def _blockscout_name(d: dict | None) -> str | None:
    d = d or {}
    return _impl_name(d.get("implementations")) or (d.get("name") if d.get("is_verified", True) else None)


class ContractNames:
    def __init__(self, blockscout_key: str | None = None, http_client=None):
        self.c = http_client or http.client(timeout=20.0)
        self.key = blockscout_key
        self.outcomes: Counter = Counter()        # "source:hit|miss|error|skipped|bad_payload", logged per run
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, http_client=None):
        return cls(env("BLOCKSCOUT_API_KEY"), http_client)   # always usable; the key only widens coverage

    def lookup(self, chain_id: int, address: str) -> tuple[str, str] | None:
        """(name, source) for verified code (a proxy's implementation name when known), else None. Raises
        LookupTransient when nothing was found and a source failed transiently."""
        transient = False
        for source, fn in (("sourcify", self._sourcify), ("blockscout_pro", self._blockscout_pro),
                           ("blockscout", self._blockscout), ("routescan", self._routescan)):
            outcome = None
            try:
                name = fn(chain_id, address)
            except _Skip:
                name, outcome = None, "skipped"
            except (httpx.HTTPError, _Transient):
                name, outcome, transient = None, "error", True
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                name, outcome = None, "bad_payload"
            with self._lock:
                self.outcomes[f"{source}:{outcome or ('hit' if name else 'miss')}"] += 1
            if name:
                return name.strip(), source
        if transient:
            raise LookupTransient(f"{chain_id}:{address}")
        return None

    def _get(self, url: str, params: dict | None = None) -> dict | None:
        r = self.c.get(url, params=params or {})
        if r.status_code == 429 or r.status_code >= 500:
            raise _Transient(r.status_code)
        if r.status_code != 200:
            return None       # 404 unverified; 403 Cloudflare challenge is persistent, so it is a miss, not a retry
        return r.json()

    def _sourcify(self, chain_id: int, address: str) -> str | None:
        d = self._get(f"https://sourcify.dev/server/v2/contract/{chain_id}/{address}",
                      {"fields": "compilation.name,proxyResolution"}) or {}
        return _impl_name((d.get("proxyResolution") or {}).get("implementations")) or (d.get("compilation") or {}).get("name")

    def _blockscout_pro(self, chain_id: int, address: str) -> str | None:
        if not (CHAIN_BY_ID.get(chain_id) and CHAIN_BY_ID[chain_id].blockscout):
            return None
        if not self.key:
            raise _Skip
        return _blockscout_name(self._get(f"https://api.blockscout.com/{chain_id}/api/v2/smart-contracts/{address}",
                                          {"apikey": self.key}))

    def _blockscout(self, chain_id: int, address: str) -> str | None:
        ch = CHAIN_BY_ID.get(chain_id)
        if not ch or not ch.blockscout:
            return None
        return _blockscout_name(self._get(f"{ch.blockscout}/api/v2/smart-contracts/{address}"))
```

Keep `_routescan` as is. The Sourcify v2 shape was checked against the live API on 2026-10-09: `?fields=compilation.name,proxyResolution` returns `{"compilation": {"name": ...}, "proxyResolution": {"isProxy", "proxyType", "implementations": [{"address", "name"}]}, "match", ...}` and 404 when unverified.

- [ ] **Step 4: Implement the step**

`names.py`: `from ..clients.contract_names import LookupTransient, useful_name`; in `run` add `out.setdefault("names_transient", 0)`. In `_lookup`, drop the `sa.or_(hooks.c.name.is_(None), hooks.c.name == "")` condition (a hook in an "Unlisted hook" project has no useful name: none, or a template or proxy name, Task 7). Replace the executor and write loop with:

```python
    def one(h):
        try:
            return client.lookup(h.chain_id, h.address)
        except LookupTransient:
            return _TRANSIENT
    with ThreadPoolExecutor(max_workers=6) as ex:     # network only; all writes stay on this thread
        found = list(ex.map(one, todo))
    for h, res in zip(todo, found):
        if res is _TRANSIENT:
            out["names_transient"] += 1               # not a miss: no name_checked_at, asked again next run
            continue
        vals = {"name_checked_at": ctx.now}
        if res:
            vals.update(contract_name=res[0], verified_source=True)
            out["names_found"] += 1
        conn.execute(sa.update(hooks).values(**vals).where(hooks.c.address == h.address, hooks.c.chain_id == h.chain_id))
    out["names_checked"] += len(todo)
    log.info("names: checked %d unlisted hooks, %d named, %d transient; per source %s", len(todo), out["names_found"],
             out["names_transient"], dict(sorted(getattr(client, "outcomes", {}).items())))
```

with `_TRANSIENT = object()` at module level. Update the docstring: "stores the implementation name for a proxy; transient failures are not stamped".

- [ ] **Step 5: Run the full suite**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: 150 passed, 3 skipped.

- [ ] **Step 6: Commit**

```bash
git add pipeline/bait/clients/contract_names.py pipeline/bait/steps/names.py pipeline/tests/test_names.py
git commit -m "names: resolve proxies to implementation names, retry transient lookups next run, log per-source outcomes

Sourcify already returns OuroborosHook for Robinhood 0x7bc2...2aec; a Sourcify 429/5xx
was stamped as a miss and parked the hook for 14 days."
```

---

### Task 9: GitHub activity: private or deleted repos are not errors

**Files:**
- Modify: `pipeline/bait/clients/github.py:1-10` (imports), `:62-84` (`activity`)
- Test: Create `pipeline/tests/test_github.py`

- [ ] **Step 1: Write the failing tests**

```python
import httpx
import pytest

from bait.clients.github import GitHub


def _gh(handler):
    return GitHub("t", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("status", [403, 404, 451])
def test_private_deleted_or_blocked_repo_is_none_not_an_error(status):
    assert _gh(lambda req: httpx.Response(status, json={"message": "x"}, request=req)).activity("https://github.com/acme/gone") is None


def test_rate_limit_403_still_raises():
    gh = _gh(lambda req: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={}, request=req))
    with pytest.raises(httpx.HTTPStatusError):
        gh.activity("https://github.com/acme/repo")


def test_non_repo_urls_are_rejected_without_a_call():
    calls = []
    gh = _gh(lambda req: calls.append(req) or httpx.Response(200, json={}, request=req))
    for url in ("https://gist.github.com/x/abc", "https://github.com/orgs/acme", "https://evil.example/github.com/a/b",
                "https://github.com.evil.example/a/b", "https://github.com/", "not a url", None):
        assert gh.activity(url) is None
    assert calls == []


def test_repo_url_with_a_deep_path_reads_the_repo():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if req.url.path.endswith("/commits"):
            return httpx.Response(200, json=[], request=req)
        return httpx.Response(200, json={"stargazers_count": 3, "pushed_at": "2026-10-01T00:00:00Z"}, request=req)
    assert _gh(handler).activity("https://github.com/acme/repo/tree/main")["repo"] == "acme/repo"
    assert seen == ["/repos/acme/repo", "/repos/acme/repo/commits"]
```

The first test is parametrized, so the file adds 6 tests (3 + 1 + 1 + 1).

- [ ] **Step 2: Run to verify they fail**

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_github.py`
Expected: FAIL (`HTTPStatusError` raised; non-repo URLs reach the transport).

- [ ] **Step 3: Implement**

`github.py`: add `import httpx`. After `parse_repo` (radar keeps using it unchanged):

```python
_REPO_URL = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})(?:/([A-Za-z0-9_.-]{1,100}))?(?:[/?#].*)?$")
_NOT_OWNERS = {"orgs", "settings", "features", "topics", "sponsors", "marketplace", "apps", "search", "about",
               "collections", "login", "explore"}


def repo_from_url(url: str | None) -> tuple[str, str | None] | None:
    """Strict parse for API calls: github.com/<owner>[/<repo>...] with a real owner segment, else None."""
    m = _REPO_URL.match((url or "").strip())
    if not m or m.group(1).lower() in _NOT_OWNERS:
        return None
    repo = (m.group(2) or "").removesuffix(".git") or None
    return None if repo in (".", "..") else (m.group(1), repo)
```

Rename the current `activity` body (from `owner, repo = parsed` on) into `def _activity(self, owner: str, repo: str | None, days: int, now: datetime | None) -> dict | None:` and make `activity`:

```python
    def activity(self, url: str, days: int = 30, now: datetime | None = None) -> dict | None:
        """None for anything that is not a readable public repo: a malformed URL (never called), or a private, deleted
        or blocked repo (403/404/451). A rate-limit 403 still raises: that is our problem, not the repo's."""
        parsed = repo_from_url(url)
        if not parsed:
            return None
        try:
            return self._activity(*parsed, days=days, now=now)
        except httpx.HTTPStatusError as e:
            r = e.response
            limited = r.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in r.headers
            if r.status_code in (404, 451) or (r.status_code == 403 and not limited):
                return None
            raise
```

Org-only URLs (`https://github.com/acme`) keep their deliberate behaviour: the most recently pushed public repo.

- [ ] **Step 4: Run the full suite**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: 156 passed, 3 skipped.

- [ ] **Step 5: Commit**

```bash
git add pipeline/bait/clients/github.py pipeline/tests/test_github.py
git commit -m "github: private, deleted or blocked repos return None; validate the URL before calling"
```

---

### Task 10: Paid POSTs are never retried after the request was sent

**Files:**
- Modify: `pipeline/bait/clients/http.py` (new predicate and `post_json_paid`)
- Modify: `pipeline/bait/clients/parallel.py:53`, `pipeline/bait/clients/exa.py:33`
- Test: Create `pipeline/tests/test_http.py`

- [ ] **Step 1: Write the failing tests**

```python
import httpx
import pytest

from bait.clients import http


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    for fn in ("get_json", "post_json", "post_json_paid"):
        if hasattr(http, fn):
            monkeypatch.setattr(getattr(http, fn).retry, "sleep", lambda s: None)


def _seq(*steps):
    calls = []

    def handler(req):
        step = steps[min(len(calls), len(steps) - 1)]
        calls.append(req)
        if isinstance(step, type):
            raise step("x", request=req)
        return httpx.Response(step, json={"ok": True}, request=req)
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


@pytest.mark.parametrize("first", [httpx.ReadTimeout, 500, 502, 503])
def test_paid_post_is_sent_once_when_the_provider_may_have_started(first):
    c, calls = _seq(first, 200)
    with pytest.raises((httpx.ReadTimeout, httpx.HTTPStatusError)):
        http.post_json_paid(c, "https://paid.test/run", json={})
    assert len(calls) == 1


@pytest.mark.parametrize("first", [httpx.ConnectError, httpx.ConnectTimeout, 429])
def test_paid_post_retries_when_the_request_never_landed(first):
    c, calls = _seq(first, 200)
    assert http.post_json_paid(c, "https://paid.test/run", json={}) == {"ok": True} and len(calls) == 2


def test_gets_keep_retrying_5xx_and_read_timeouts():
    c, calls = _seq(503, httpx.ReadTimeout, 200)
    assert http.get_json(c, "https://free.test/x") == {"ok": True} and len(calls) == 3


def test_parallel_and_exa_use_the_paid_post(monkeypatch):
    from bait.clients.exa import Exa
    from bait.clients.parallel import Parallel
    sent = []
    monkeypatch.setattr(http, "post_json", lambda *a, **k: pytest.fail("paid call through the retrying POST"))
    monkeypatch.setattr(http, "post_json_paid", lambda c, url, **k: sent.append(url) or {"run_id": "r1", "results": []})
    assert Parallel("k", http_client=object()).submit("P", "hints") == "r1"
    assert Exa("k", http_client=object()).search_recent("q") == []
    assert sent == ["https://api.parallel.ai/v1/tasks/runs", "https://api.exa.ai/search"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd pipeline && .venv/bin/python -m pytest -q tests/test_http.py`
Expected: FAIL, `AttributeError: module 'bait.clients.http' has no attribute 'post_json_paid'`.

- [ ] **Step 3: Implement**

Append to `pipeline/bait/clients/http.py`:

```python
def _retryable_paid(exc: BaseException) -> bool:
    """A paid POST is retried only when the provider cannot have received it (connect failed, pool exhausted) or
    refused it (429). A read timeout or 5xx can arrive after the provider started, and billed, the work."""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8),
       retry=retry_if_exception(_retryable_paid))
def post_json_paid(c: httpx.Client, url: str, **kw):
    r = c.post(url, **kw)
    r.raise_for_status()
    return r.json()
```

`parallel.py:53` and `exa.py:33`: `http.post_json(` becomes `http.post_json_paid(`. `graph.py` (free subgraph POST) keeps `post_json`.

- [ ] **Step 4: Run the full suite**

Run: `cd pipeline && .venv/bin/python -m pytest -q`
Expected: 165 passed, 3 skipped.

- [ ] **Step 5: Commit**

```bash
git add pipeline/bait/clients/http.py pipeline/bait/clients/parallel.py pipeline/bait/clients/exa.py pipeline/tests/test_http.py
git commit -m "http: paid POSTs (Parallel submit, Exa search) retry only on connect errors and 429

A read timeout or 5xx after the request was sent can mean the provider already billed it;
retrying could pay twice."
```

---

### Task 11: Web redirect through project_aliases

**Files:**
- Modify: `web/src/lib/unlisted.ts:16-25`, `web/src/lib/unlisted.test.ts:17-22`
- Modify: `web/src/db/queries.ts:12` (import) and `:135-148` (`slugForOldUnlisted`)
- Modify: `web/src/app/p/[slug]/page.tsx:4,33`

- [ ] **Step 1: Write the failing test**

In `unlisted.test.ts`, import `isSlugShaped` instead of `oldUnlistedSlugPattern` and replace the third test:

```ts
test("only slug-shaped paths reach the alias lookup", () => {
  assert.equal(isSlugShaped("unlisted-hook-0x7bc2-2aec"), true);
  assert.equal(isSlugShaped("orderhook-base"), true);
  assert.equal(isSlugShaped("Unlisted%20hook"), false);
  assert.equal(isSlugShaped("a--b"), false);
  assert.equal(isSlugShaped("x".repeat(121)), false);
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && npm test`
Expected: FAIL, `isSlugShaped` is not exported.

- [ ] **Step 3: Implement**

`unlisted.ts`: delete `oldUnlistedSlugPattern` and its comment; add:

```ts
/** Slugs are lowercase words joined by single dashes; anything else cannot be an old slug, so skip the alias query. */
export function isSlugShaped(slug: string): boolean {
  return slug.length <= 120 && /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug);
}
```

`queries.ts`: import `{ isSlugShaped, isUnlisted, unlistedLabel }` from `@/lib/unlisted`; replace `slugForOldUnlisted` with:

```ts
/**
 * Where a moved slug lives now: renamed, merged, regrouped, or named since the link was shared. Only aliases the
 * pipeline recorded (project_aliases), never an address-prefix guess.
 */
export async function slugForAlias(slug: string): Promise<string | null> {
  const db = getDb();
  if (!db || !isSlugShaped(slug)) return null;
  const rows = await safe(
    "slugForAlias",
    async () =>
      rowsOf<{ slug: string }>(
        await db.execute(sql`select p.slug from project_aliases a join ${t.projects} p on p.id = a.project_id
                             where a.old_slug = ${slug} limit 1`),
      ),
    [],
  );
  const to = rows[0]?.slug;
  return to && to !== slug ? to : null;
}
```

`page.tsx`: import `slugForAlias` instead of `slugForOldUnlisted`; line 33 becomes `const moved = await slugForAlias(slug);   // renamed or merged since this link was shared`.

- [ ] **Step 4: Verify**

Run: `grep -rn "oldUnlistedSlugPattern\|slugForOldUnlisted" web/src` (expect no output), then `cd web && npm test && npx tsc --noEmit && npm run build`.
Expected: every test passes (unlisted suite: 3 tests), `tsc` prints nothing, `next build` completes without errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/unlisted.ts web/src/lib/unlisted.test.ts web/src/db/queries.ts "web/src/app/p/[slug]/page.tsx"
git commit -m "web: redirect moved slugs through project_aliases, not a 16-bit address-prefix guess"
```

---

### Task 12: DESIGN.md and the final run

**Files:**
- Modify: `DESIGN.md` (append a section after "Update 2026-10-09: permissions from the address…")

- [ ] **Step 1: Edit**

Append:

```markdown
## Grouping and naming, package 7 (2026-10-09)

- **One code hash.** `code_hash` is `0x` + sha256 of the deployed bytes everywhere (`rules.code_hash`). Keyless rows that
  stored sha1 of the hex string are re-hashed from public RPC, 2,000 per run, until none are left.
- **Regroup.** Ingest flags a grouped hook (`hooks.regroup`) when the hooklist names or lists it; a code-hash write flags
  it too. The group step moves it into the curated project with that name, or renames its auto-named project in place,
  and pulls same-bytecode hooks out of auto-named projects. Curated projects never give hooks up.
- **Names are candidates, merges need evidence.** Shared non-proxy bytecode, a shared deployer that is not a CREATE2
  factory, or the same hooklist name (`rules.merge_evidence`). Two same-named contracts on one chain without evidence
  stay apart; the later one gets a short-address suffix.
- **Template and proxy names** (`rules.GENERIC`) never group by name, hooklist or verified source. Proxies resolve to
  their implementation name (Sourcify `proxyResolution`, Blockscout `implementations`); proxy bytecode is never
  evidence. Projects keyed on such a name were split.
- **Aliases.** Every rename, merge and regroup writes `project_aliases`; `/p/<old>` redirects through it.
- **Lookups.** A transient contract-name failure (network, 429, 5xx) is asked again next run; Cloudflare 403 is a miss.
  The log line carries per-source outcomes.
- **Out of scope:** a teams alias map (FablesRamp, FablesRampETH, FablesRWA stay separate cards). `LimitOrderHook` is
  treated as a real name (one hooklist family across chains) until the owner says it is a template.
```

- [ ] **Step 2: Final full run**

```bash
cd pipeline && .venv/bin/python -m pytest -q
cd ../web && npm test && npx tsc --noEmit && npm run build
```
Expected: 165 passed, 3 skipped; web tests pass, `tsc` silent, build succeeds. Record the numbers in the final report.

- [ ] **Step 3: Commit**

```bash
git add DESIGN.md
git commit -m "DESIGN: grouping and naming rules after package 7"
```

---

## Final report (the implementer returns this)

For each task: commit SHA, test counts before and after, anything that deviated from the plan and why. Then `git log --oneline main..HEAD` and `git diff --stat main..HEAD`. Do not push.
