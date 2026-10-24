from datetime import timedelta

import sqlalchemy as sa

from bait import db
from bait.steps import enrich
from tests.conftest import DAY, NOW, insert_hook, insert_pool, insert_project


class FakeClaude:
    def __init__(self):
        self.calls = []

    def what_line(self, ctxd):
        self.calls.append(ctxd["name"])
        return {"what_line": f"{ctxd['name']} does a thing.", "category": "dynamic-fee"}


def test_enrich_only_projects_with_pools_busiest_first_and_capped(conn, ctx, monkeypatch):
    monkeypatch.setenv("ENRICH_MAX", "2")
    for name, vol in (("Quiet", 10.0), ("Busy", 5_000_000.0), ("Mid", 50_000.0)):
        pid = insert_project(conn, name, first_seen=NOW - timedelta(days=30))
        addr = "0x" + name.lower().ljust(40, "0")[:40]
        insert_hook(conn, addr, 8453, project_id=pid, name=name)
        insert_pool(conn, "0xpool" + name, 8453, addr, NOW - timedelta(days=30))
        conn.execute(sa.insert(db.pool_stats).values(pool_id="0xpool" + name, chain_id=8453, day=DAY, vol_24h=vol, traders_24h=50))
    ghost = insert_project(conn, "Ghost", first_seen=NOW, slug="ghost")       # registry hook, no pools: never paid for
    insert_hook(conn, "0x" + "9" * 40, 8453, project_id=ghost, name="Ghost")
    claude = FakeClaude()
    ctx.clients.update(claude=claude, blockscout=None)
    out = enrich.run(conn, ctx)
    assert claude.calls == ["Busy", "Mid"] and out["candidates"] == 2


def test_budget_stop_mid_loop_keeps_what_was_written(conn, ctx, monkeypatch):
    from bait.costs import BudgetExceeded
    monkeypatch.setenv("ENRICH_MAX", "3")
    for i, n in enumerate(("A", "B", "C")):
        pid = insert_project(conn, n, first_seen=NOW - timedelta(days=30), slug=n.lower())
        addr = f"0x{i + 7:040x}"
        insert_hook(conn, addr, 8453, project_id=pid, name=n)
        insert_pool(conn, f"0xq{i}", 8453, addr, NOW - timedelta(days=30))
        conn.execute(sa.insert(db.pool_stats).values(pool_id=f"0xq{i}", chain_id=8453, day=DAY, vol_24h=1000.0 * (3 - i), traders_24h=50))

    class StingyClaude:
        """First what_line succeeds, the second raises BudgetExceeded (daily budget hit mid-loop)."""

        def __init__(self):
            self.calls = []

        def what_line(self, ctxd):
            self.calls.append(ctxd["name"])
            if len(self.calls) == 2:
                raise BudgetExceeded("claude: daily budget")
            return {"what_line": "x", "category": "dynamic-fee"}

    claude = StingyClaude()
    ctx.clients.update(claude=claude, blockscout=None)
    out = enrich.run(conn, ctx)
    assert out["budget_stop"] == 1 and out["enriched"] == 1 and claude.calls == ["A", "B"]   # C is never tried
    rows = {r.slug: r for r in conn.execute(sa.select(db.projects))}
    assert rows["a"].enriched_at is not None and rows["b"].enriched_at is None and rows["c"].enriched_at is None


def _seed_one(conn, name="Gone", slug="gone", enrichment=None):
    pid = insert_project(conn, name, first_seen=NOW - timedelta(days=30), slug=slug)
    addr = "0x" + "6" * 40
    insert_hook(conn, addr, 8453, project_id=pid, name=name)
    insert_pool(conn, "0xp" + slug, 8453, addr, NOW - timedelta(days=30))
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xp" + slug, chain_id=8453, day=DAY, vol_24h=5000.0, traders_24h=50))
    if enrichment is not None:
        conn.execute(sa.update(db.projects).values(enrichment=enrichment).where(db.projects.c.id == pid))
    return pid



def test_cut_line_word_boundary_and_ellipsis():
    assert enrich.cut_line("  Dynamic fee hook.  ") == "Dynamic fee hook."
    long = ("word " * 80).strip()                        # 399 chars
    out = enrich.cut_line(long)
    assert out.endswith("…") and len(out) <= 200 and not out[:-1].endswith(" ")
    assert long.startswith(out[:-1]) and long[len(out) - 1] == " "   # the cut sits on a word boundary
    assert enrich.cut_line("x" * 300) == "x" * 199 + "…"               # no spaces: hard cut, still marked


def test_what_context_is_what_enrich_sends_claude(conn, ctx):
    pid = insert_project(conn, "Ctx", first_seen=NOW - timedelta(days=30), slug="ctx")
    insert_hook(conn, "0x" + "c" * 40, 8453, project_id=pid, name="Ctx")
    conn.execute(sa.update(db.hooks).values(description="limit orders", flags={"beforeSwap": True})
                 .where(db.hooks.c.address == "0x" + "c" * 40))
    insert_pool(conn, "0xpoolctx", 8453, "0x" + "c" * 40, NOW - timedelta(days=30))
    conn.execute(sa.insert(db.pool_stats).values(pool_id="0xpoolctx", chain_id=8453, day=DAY, vol_24h=1.0, traders_24h=1))
    conn.execute(sa.update(db.projects).values(enrichment={"source": {"name": "CtxHook"}}).where(db.projects.c.id == pid))
    seen = []

    class Spy(FakeClaude):
        def what_line(self, ctxd):
            seen.append(ctxd)
            return super().what_line(ctxd)

    ctx.clients.update(claude=Spy(), blockscout=None)
    enrich.run(conn, ctx)
    p = conn.execute(sa.select(db.projects).where(db.projects.c.id == pid)).one()
    assert seen == [enrich.what_context(p, enrich.hooks_of(conn, pid), {"source": {"name": "CtxHook"}})]
    assert seen[0]["hooklist_descriptions"] == ["limit orders"] and seen[0]["flags_union"] == ["beforeSwap"]
    assert seen[0]["enrichment"] == {"source": {"name": "CtxHook"}}
    vals = enrich.what_values(p, {"what_line": "x " * 150, "category": "orders"}, {})
    assert vals["what_line"].endswith("…") and len(vals["what_line"]) <= 200


def test_stored_parallel_data_is_kept_but_never_prompt_or_links(conn, ctx):
    """Parallel was removed on 2026-10-24. Rows from before keep their stored facts, tombstones and in-flight run ids
    untouched; none of it reaches Claude, none of it becomes a link, and a run id no longer holds a project back."""
    legacy = {"parallel": {"team": "T", "website": "https://c.xyz", "github": "https://github.com/c/c", "x_handle": "c"},
              "parallel_run_id": "trun_1", "parallel_run_at": NOW.isoformat()}
    pid = _seed_one(conn, name="Legacy", slug="legacy", enrichment=legacy)
    seen = []

    class Spy(FakeClaude):
        def what_line(self, ctxd):
            seen.append(ctxd)
            return super().what_line(ctxd)

    ctx.clients.update(claude=Spy(), blockscout=None)
    out = enrich.run(conn, ctx)
    row = conn.execute(sa.select(db.projects).where(db.projects.c.id == pid)).one()
    assert out["enriched"] == 1 and row.enriched_at is not None and row.what_line == "Legacy does a thing."
    assert seen[0]["enrichment"] == {} and "parallel" not in str(seen[0])
    assert row.enrichment == legacy                                             # stored as it was, nothing dropped
    assert (row.website, row.github, row.x_handle) == (None, None, None)       # no Parallel links fallback


def test_what_values_revalidates_stored_links_and_never_reads_parallel():
    from types import SimpleNamespace
    p = SimpleNamespace(website="https://ok.org", github="no repository", x_handle="@okhandle")
    out = enrich.what_values(p, {"what_line": "Example", "category": "other"},
                             {"parallel": {"github": "https://github.com/Uniswap/v4-core"}})
    assert (out["website"], out["github"], out["x_handle"]) == ("https://ok.org", None, "okhandle")


def test_enriched_but_empty_projects_are_retried_weekly_behind_new_ones(conn, ctx, monkeypatch):
    """A busy project whose enrichment produced no what_line gets another call once a week, after never-enriched
    projects in the same cap; a fresh empty result and a project with a line are left alone."""
    monkeypatch.setenv("ENRICH_MAX", "3")
    rows = {}
    for name, vol in (("Blank", 9_000_000.0), ("Fresh", 8_000_000.0), ("Described", 7_000_000.0), ("Never", 10.0)):
        pid = insert_project(conn, name, first_seen=NOW - timedelta(days=30))
        addr = "0x" + name.lower().ljust(40, "0")[:40]
        insert_hook(conn, addr, 8453, project_id=pid, name=name)
        insert_pool(conn, "0xpool" + name, 8453, addr, NOW - timedelta(days=30))
        conn.execute(sa.insert(db.pool_stats).values(pool_id="0xpool" + name, chain_id=8453, day=DAY, vol_24h=vol, traders_24h=50))
        rows[name] = pid
    conn.execute(sa.update(db.projects).where(db.projects.c.id == rows["Blank"])
                 .values(what_line="", enriched_at=NOW - timedelta(days=8)))            # old and empty: retry
    conn.execute(sa.update(db.projects).where(db.projects.c.id == rows["Fresh"])
                 .values(what_line=None, enriched_at=NOW - timedelta(days=2)))          # empty but recent: wait
    conn.execute(sa.update(db.projects).where(db.projects.c.id == rows["Described"])
                 .values(what_line="Described does a thing.", enriched_at=NOW - timedelta(days=60)))   # has a line: never
    assert [p.name for p in enrich.select_candidates(conn, now=NOW)] == ["Never", "Blank"]
    claude = FakeClaude()
    ctx.clients.update(claude=claude, blockscout=None)
    out = enrich.run(conn, ctx)
    assert claude.calls == ["Never", "Blank"] and out["enriched"] == 2
    blank = conn.execute(sa.select(db.projects).where(db.projects.c.id == rows["Blank"])).one()
    assert blank.what_line == "Blank does a thing." and db.as_utc(blank.enriched_at) > NOW - timedelta(days=1)
    assert enrich.select_candidates(conn, now=NOW) == []     # nothing left to retry tonight
