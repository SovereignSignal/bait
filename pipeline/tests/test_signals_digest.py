from datetime import timedelta

import sqlalchemy as sa

from bait import db
from bait.steps import digest, signals
from tests.conftest import DAY, NOW, insert_hook, insert_project


def seed_daily(conn, pid, score=0.0, new=False, spam=False, tvl=0.0):
    conn.execute(sa.insert(db.project_daily).values(project_id=pid, day=DAY, tvl_usd=tvl, vol_7d=0, vol_7d_prev=0,
                                                    swaps_7d=0, pools_n=1, score_trending=score, is_new=new, is_spam=spam))


class NoExa:
    """Exa serves the radar step only since 2026-10-24: signals must never search it."""

    def search_recent(self, *a, **k):
        raise AssertionError("signals searched Exa")


class FakeGitHub:
    def __init__(self, stars=12):
        self.stars = stars

    def activity(self, url, days=30, now=None):
        return {"repo": "x/y", "stars": self.stars, "last_commit_at": (NOW - timedelta(days=2)).isoformat(), "commits_30d": True}


class FakeClaude:
    def __init__(self):
        self.calls = 0

    def why_blurb(self, ctx):
        self.calls += 1
        assert "metrics" in ctx and "signals" in ctx
        return "People use it. Numbers went up."


def test_signals_targets_github_alive_and_blurb_only_on_change(conn, ctx):
    hot = insert_project(conn, "Hot", NOW - timedelta(days=40))
    conn.execute(sa.update(db.projects).values(github="https://github.com/x/y").where(db.projects.c.id == hot))
    fresh = insert_project(conn, "Fresh", NOW - timedelta(days=1), slug="fresh")
    dull = insert_project(conn, "Dull", NOW - timedelta(days=40), slug="dull")
    junk = insert_project(conn, "Junk", NOW - timedelta(days=1), slug="junk")
    for pid in (hot, fresh, dull, junk):
        insert_hook(conn, f"0x{pid}", 1, project_id=pid)
    seed_daily(conn, hot, score=30.0, tvl=1e6)
    seed_daily(conn, fresh, new=True)
    seed_daily(conn, dull)
    seed_daily(conn, junk, new=True, spam=True)

    targets = [p.slug for p, _ in signals.select_targets(conn, ctx)]
    assert targets == ["hot", "fresh"]          # trending first, then new; spam excluded

    gh, claude = FakeGitHub(), FakeClaude()
    ctx.clients.update(exa=NoExa(), github=gh, claude=claude)
    out = signals.run(conn, ctx)
    assert out == {"projects": 2, "github": 1, "blurbs": 2, "errors": 0, "audit_links": 0, "audit_links_dead": 0}
    assert conn.execute(sa.select(sa.func.count()).select_from(db.receipts)).scalar_one() == 0
    sig = {r.kind: r.value for r in conn.execute(sa.select(db.signals).where(db.signals.c.project_id == hot))}
    assert sig["alive"] == {"alive": True} and sig["github"]["stars"] == 12 and "exa" not in sig
    p = conn.execute(sa.select(db.projects).where(db.projects.c.id == hot)).one()
    assert p.why_blurb == "People use it. Numbers went up." and p.blurb_at is not None

    # same signals next run -> as_of unchanged -> no new blurb
    ctx.now = NOW + timedelta(days=1)
    out2 = signals.run(conn, ctx)
    assert out2["blurbs"] == 0 and claude.calls == 2
    # a signal changes -> that project's blurb is regenerated
    gh.stars = 13
    ctx.now = NOW + timedelta(days=2)
    out3 = signals.run(conn, ctx)
    assert out3["blurbs"] == 1 and claude.calls == 3


def test_signals_without_keys_still_writes_alive(conn, ctx):
    p = insert_project(conn, "Solo", NOW)
    insert_hook(conn, "0x1", 1, project_id=p)
    seed_daily(conn, p, new=True)
    out = signals.run(conn, ctx)
    assert out["projects"] == 1 and out["github"] == 0 and out["blurbs"] == 0
    # no GitHub reading: alive is unknown (null), never false
    assert conn.execute(sa.select(db.signals.c.value).where(db.signals.c.project_id == p)).scalar_one() == {"alive": None}


def test_digest_only_on_monday_or_forced(conn, ctx):
    p = insert_project(conn, "Hot", NOW - timedelta(days=40))
    seed_daily(conn, p, score=30.0, tvl=5.0)
    assert digest.run(conn, ctx) == {"written": 0}            # DAY is a Tuesday
    ctx.force_digest = True
    assert digest.run(conn, ctx) == {"written": 1, "new": 0, "trending": 1}
    body = conn.execute(sa.select(db.digests.c.body).where(db.digests.c.week_of == DAY)).scalar_one()
    assert body["trending"][0]["slug"] == "hot" and body["totals"]["projects"] == 1
    ctx.force_digest = False
    ctx.day = DAY - timedelta(days=1)                          # Monday
    seed_daily_monday = conn.execute(sa.insert(db.project_daily).values(project_id=p, day=ctx.day, score_trending=0, is_new=False, is_spam=False))
    assert digest.run(conn, ctx)["written"] == 1
    assert conn.execute(sa.select(sa.func.count()).select_from(db.digests)).scalar_one() == 2


def test_the_blurb_prompt_gets_only_the_receipts_the_page_lists(conn, ctx):
    # The web lists a stored receipt only when its title names the project's current name and reads as DeFi
    # (relevance.ts), none for an "Unlisted hook", and merges and splits move receipts between projects. The prompt gets
    # the same filter (rules.receipt_relevant) before the five-newest cut, so a blurb cannot cite a post its page hides.
    p = insert_project(conn, "Angstrom", NOW - timedelta(days=40), slug="angstrom")
    unnamed = insert_project(conn, "Unlisted hook 0x16f2…3a80", NOW - timedelta(days=40), slug="unlisted-hook-0x16f2-3a80")
    for pid, score in ((p, 30.0), (unnamed, 20.0)):
        insert_hook(conn, f"0x{pid}", 1, project_id=pid)
        seed_daily(conn, pid, score=score, tvl=1e6)
    hidden = [("Invincible Vs developers provide a detailed overview of Angstrom Levy's fighting style", "eventhubs.com"),
              ("Kyle Hall", "linkedin.com"),
              ("Fables: the deepest ETH/USDG pool on Robinhood Chain", "x.com")]       # another project's, moved by a merge
    shown = [(f"Angstrom brings MEV protection to Uniswap v4 pools, part {i}", "theblock.co") for i in range(6)]
    rows = [(p, t, s, age) for age, (t, s) in enumerate(hidden + shown)]                # the hidden three are the newest
    rows.append((unnamed, "New Uniswap v4 hook launched", "x.com", 0))
    conn.execute(sa.insert(db.receipts), [
        {"project_id": pid, "url": f"https://news.example.com/{i}", "title": t, "source": s,
         "published_at": NOW - timedelta(days=1 + age)} for i, (pid, t, s, age) in enumerate(rows)])
    seen = {}

    class Capture(FakeClaude):
        def why_blurb(self, c):
            seen[c["name"]] = c
            return super().why_blurb(c)
    ctx.clients.update(exa=NoExa(), github=None, claude=Capture())
    assert signals.run(conn, ctx)["blurbs"] == 2
    assert [r["title"] for r in seen["Angstrom"]["receipts"]] == [t for t, _ in shown[:5]]
    assert seen["Unlisted hook 0x16f2…3a80"]["receipts"] == []


def test_frozen_exa_and_nansen_signals_stay_out_of_alive_and_the_blurb(conn, ctx):
    # Signal rows written before 2026-10-24 stay stored, but no step refreshes them: a recent Exa hit no longer makes a
    # project alive, and the blurb prompt sees only live signals. Stored receipts are still dated facts it may use.
    p = insert_project(conn, "Hot", NOW - timedelta(days=40), slug="hot")
    insert_hook(conn, f"0x{p}", 1, project_id=p)
    seed_daily(conn, p, score=30.0, tvl=1e6)
    conn.execute(sa.insert(db.signals), [
        {"project_id": p, "kind": "exa", "value": {"hits_30d": 3, "latest": (NOW - timedelta(days=2)).isoformat(), "top": []},
         "as_of": NOW - timedelta(days=5)},
        {"project_id": p, "kind": "nansen", "value": {"labeled": [], "smart_money": False}, "as_of": NOW - timedelta(days=20)}])
    conn.execute(sa.insert(db.receipts).values(project_id=p, url="https://blog.example.com/hot", title="Hot hook ships",
                                               source="blog.example.com", published_at=NOW - timedelta(days=2)))
    conn.execute(sa.update(db.projects).values(enrichment={"parallel": {"team": "T"}}).where(db.projects.c.id == p))
    seen = []

    class Capture(FakeClaude):
        def why_blurb(self, c):
            seen.append(c)
            return super().why_blurb(c)
    ctx.clients.update(exa=NoExa(), github=None, claude=Capture())
    out = signals.run(conn, ctx)
    assert out == {"projects": 1, "github": 0, "blurbs": 1, "errors": 0, "audit_links": 0, "audit_links_dead": 0}
    sig = {r.kind: r.value for r in conn.execute(sa.select(db.signals).where(db.signals.c.project_id == p))}
    assert sig["alive"] == {"alive": None} and sig["exa"]["hits_30d"] == 3           # stored row left as it was
    assert seen[0]["signals"] == {} and [r["title"] for r in seen[0]["receipts"]] == ["Hot hook ships"]
    assert "enrichment" not in seen[0]                                                # stored Parallel facts: never prompt


def test_a_changed_audit_link_verdict_alone_never_marks_the_blurb_stale_or_reaches_the_prompt(conn, ctx):
    # audit_links (audit_links.py) changes whenever a published audit link dies or comes back. It is for the badge: on
    # its own it must not make the why_blurb stale (a paid Claude call), and its dead links never reach the prompt.
    url = "https://github.com/spearbit/portfolio/blob/master/pdfs/Sorella-Spearbit-Security-Review-October-2024.pdf"
    p = insert_project(conn, "Angstrom", NOW - timedelta(days=40), slug="angstrom")
    insert_hook(conn, f"0x{p}", 1, project_id=p)
    conn.execute(sa.update(db.hooks).values(audit_url=url).where(db.hooks.c.project_id == p))
    seed_daily(conn, p, score=30.0, tvl=1e6)
    claude = FakeClaude()
    ctx.clients.update(exa=NoExa(), github=None, claude=claude)
    ctx.extras["audit_probe"] = {url: 200}.get
    assert signals.run(conn, ctx)["blurbs"] == 1                                # the first blurb; the link answers

    ctx.extras["audit_probe"] = {url: 404}.get                                  # the next night the link is gone
    ctx.now = NOW + timedelta(days=1)
    out = signals.run(conn, ctx)
    assert out["audit_links_dead"] == 1 and out["blurbs"] == 0 and claude.calls == 1
    row = conn.execute(sa.select(db.signals)
                       .where(db.signals.c.project_id == p, db.signals.c.kind == "audit_links")).one()
    blurb_at = conn.execute(sa.select(db.projects.c.blurb_at).where(db.projects.c.id == p)).scalar_one()
    assert row.value == {"dead": [url]} and db.as_utc(row.as_of) > db.as_utc(blurb_at)   # newer than the blurb, yet
    target, daily = signals.select_targets(conn, ctx)[0]
    assert signals._needs_blurb(conn, target, daily) is False
    assert "audit_links" not in signals._blurb_ctx(conn, target, daily)["signals"]
    signals._set_signal(conn, p, "github", {"repo": "x/y", "stars": 1}, NOW + timedelta(days=2))
    assert signals._needs_blurb(conn, target, daily) is True                    # a signal the blurb reads still counts


def test_alive_is_unknown_without_a_github_reading_and_never_reaches_the_blurb_as_false(conn, ctx):
    # GitHub is alive's only input since 2026-10-24, and new projects no longer get a GitHub link (that came from
    # Parallel). With no GitHub reading there is no evidence either way: the row says null and the blurb prompt leaves it
    # out. Sent as false it read as a fact: a live blurb said "the alive signal reads false, so treat the activity as
    # unverified" of a project with no GitHub link. A repo whose last commit is older than 30 days is a real false.
    ids = {}
    for i, (name, gh) in enumerate((("Linkless", None), ("Stale", "https://github.com/x/stale"), ("Busy", "https://github.com/x/busy"))):
        pid = insert_project(conn, name, NOW - timedelta(days=40), slug=name.lower())
        insert_hook(conn, f"0x{pid}", 1, project_id=pid)
        seed_daily(conn, pid, score=30.0 - i, tvl=1e6)
        conn.execute(sa.update(db.projects).values(github=gh).where(db.projects.c.id == pid))
        ids[name] = pid
    # written before 2026-10-24, when no Exa hit and no GitHub link made it false
    conn.execute(sa.insert(db.signals).values(project_id=ids["Linkless"], kind="alive", value={"alive": False},
                                              as_of=NOW - timedelta(days=3)))

    class GitHub:
        def activity(self, url, days=30, now=None):
            ago = 45 if url.endswith("/stale") else 2
            return {"repo": url.removeprefix("https://github.com/"), "stars": 1,
                    "last_commit_at": (NOW - timedelta(days=ago)).isoformat(), "commits_30d": ago <= 30}
    seen = {}

    class Capture(FakeClaude):
        def why_blurb(self, c):
            seen[c["name"]] = c
            return super().why_blurb(c)
    ctx.clients.update(exa=NoExa(), github=GitHub(), claude=Capture())
    out = signals.run(conn, ctx)
    assert out == {"projects": 3, "github": 2, "blurbs": 3, "errors": 0, "audit_links": 0, "audit_links_dead": 0}
    alive = {n: conn.execute(sa.select(db.signals.c.value).where(db.signals.c.project_id == pid, db.signals.c.kind == "alive"))
             .scalar_one() for n, pid in ids.items()}
    assert alive == {"Linkless": {"alive": None}, "Stale": {"alive": False}, "Busy": {"alive": True}}
    assert "alive" not in seen["Linkless"]["signals"]
    assert seen["Stale"]["signals"]["alive"] == {"alive": False} and seen["Busy"]["signals"]["alive"] == {"alive": True}


def test_blurb_context_names_its_windows_and_never_a_made_up_swap_count(conn, ctx):
    hot = insert_project(conn, "Hot", NOW - timedelta(days=40))
    insert_hook(conn, f"0x{hot}", 1, project_id=hot)
    conn.execute(sa.insert(db.project_daily).values(
        project_id=hot, day=DAY, window_end=DAY - timedelta(days=1), tvl_usd=1e6, vol_24h=10_000.0, vol_7d=70_000.0,
        vol_7d_prev=35_000.0, swaps_24h=500, swaps_7d=None, wallets_24h=1_142, wallets_exact=False, pools_n=6,
        score_trending=30.0, is_new=False, is_spam=False))
    washy = insert_project(conn, "Washy", NOW - timedelta(days=40), slug="washy")
    insert_hook(conn, f"0x{washy}", 1, project_id=washy)
    conn.execute(sa.insert(db.project_daily).values(project_id=washy, day=DAY, tvl_usd=1.0, vol_7d=9e8, vol_7d_prev=1e6, pools_n=1,
                                                    score_trending=40.0, is_wash=True, wash_reason="24h"))
    seen = []

    class Capture(FakeClaude):
        def why_blurb(self, c):
            seen.append(c)
            return super().why_blurb(c)
    ctx.clients.update(claude=Capture())
    assert [p.slug for p, _ in signals.select_targets(conn, ctx)] == ["hot"]       # wash-like volume gets no blurb
    signals.run(conn, ctx)
    m = seen[0]["metrics"]
    assert m["windows"].startswith("closed UTC days ending 2026-10-05")
    assert (m["vol_24h"], m["swaps_24h"], m["swaps_7d"]) == (10_000.0, 500, None)
    assert (m["wallets_24h"], m["wallets_24h_kind"]) == (1_142, "upper bound (buyers + sellers per pool)")


def test_digest_leaves_wash_out_of_the_week_and_names_the_window(conn, ctx):
    a = insert_project(conn, "Real", NOW - timedelta(days=40), slug="real")
    b = insert_project(conn, "Washy", NOW - timedelta(days=40), slug="washy")
    conn.execute(sa.insert(db.project_daily).values(project_id=a, day=DAY, tvl_usd=100.0, vol_7d=50_000.0, vol_7d_prev=20_000.0,
                                                    pools_n=1, score_trending=12.0))
    conn.execute(sa.insert(db.project_daily).values(project_id=b, day=DAY, tvl_usd=1.0, vol_7d=9e8, vol_7d_prev=1e6, pools_n=1,
                                                    score_trending=0.0, is_wash=True, wash_reason="7d"))
    ctx.force_digest = True
    digest.run(conn, ctx)
    body = conn.execute(sa.select(db.digests.c.body)).scalar_one()
    assert body["window_end"] == "2026-10-05"
    assert body["totals"] == {"projects": 1, "traded": 1, "new": 0, "tvl_usd": 100.0, "vol_7d": 50_000.0}
    assert [c["slug"] for c in body["trending"]] == ["real"]


def test_digest_counts_projects_traded_this_week_as_leaders_does(conn, ctx):
    # /leaders "Projects traded, last 7 days" = non-spam, non-wash, vol_7d > 0; the digest totals use the same set
    a = insert_project(conn, "Busy", NOW - timedelta(days=40), slug="busy")
    b = insert_project(conn, "Idle", NOW - timedelta(days=40), slug="idle")
    c = insert_project(conn, "Unknown", NOW - timedelta(days=40), slug="unknown")
    conn.execute(sa.insert(db.project_daily).values(project_id=a, day=DAY, tvl_usd=100.0, vol_7d=5_000.0, pools_n=1, score_trending=0.0))
    conn.execute(sa.insert(db.project_daily).values(project_id=b, day=DAY, tvl_usd=40.0, vol_7d=0.0, pools_n=1, score_trending=0.0))
    conn.execute(sa.insert(db.project_daily).values(project_id=c, day=DAY, tvl_usd=7.0, vol_7d=None, pools_n=1, score_trending=0.0))
    ctx.force_digest = True
    digest.run(conn, ctx)
    totals = conn.execute(sa.select(db.digests.c.body)).scalar_one()["totals"]
    assert totals == {"projects": 3, "traded": 1, "new": 0, "tvl_usd": 100.0, "vol_7d": 5_000.0}


def test_digest_lists_new_and_trending_as_the_feed_tabs_do(conn, ctx):
    # 2026-10-12: the digest stored 20 New, 16 of them unnamed hooks or $0 launches; the week page skipped those and
    # still said "20 new projects cleared the bar" above four. The stored lists now follow the feed's New and Trending.
    def row(name, slug, **kw):
        pid = insert_project(conn, name, NOW - timedelta(days=2), slug=slug)
        conn.execute(sa.insert(db.project_daily).values(project_id=pid, day=DAY, pools_n=1, **kw))
    row("Fresh", "fresh", tvl_usd=5_000.0, vol_7d=12_000.0, is_new=True)
    row("Quiet", "quiet", tvl_usd=9_000.0, vol_7d=None, is_new=True)            # unknown volume is not $0: listed
    row("Seeded", "seeded", tvl_usd=50_000.0, vol_7d=0.0, is_new=True)          # liquidity, no trade yet: not listed
    row("Unlisted hook 0xabcd…1234", "unlisted-hook-0xabcd-1234", tvl_usd=80_000.0, vol_7d=40_000.0, is_new=True)
    row("Unlisted hook family 0x0f48…", "unlisted-hook-family-0x0f48", tvl_usd=1e6, vol_7d=2e6, score_trending=40.0)
    row("Hot", "hot", tvl_usd=1e6, vol_7d=5e5, score_trending=30.0)
    ctx.force_digest = True
    assert digest.run(conn, ctx) == {"written": 1, "new": 2, "trending": 1}
    body = conn.execute(sa.select(db.digests.c.body)).scalar_one()
    assert [c["slug"] for c in body["new"]] == ["quiet", "fresh"]               # most TVL first
    assert [c["slug"] for c in body["trending"]] == ["hot"]
    assert body["totals"]["new"] == 2


def test_digest_counts_every_new_project_but_lists_twenty(conn, ctx):
    for i in range(23):
        pid = insert_project(conn, f"Launch{i}", NOW - timedelta(days=1), slug=f"launch{i}")
        conn.execute(sa.insert(db.project_daily).values(project_id=pid, day=DAY, pools_n=1, tvl_usd=1_000.0 + i,
                                                        vol_7d=100.0, is_new=True))
    ctx.force_digest = True
    digest.run(conn, ctx)
    body = conn.execute(sa.select(db.digests.c.body)).scalar_one()
    assert len(body["new"]) == 20 and body["totals"]["new"] == 23
    assert body["new"][0]["slug"] == "launch22"


def test_named_matches_the_group_steps_placeholders():
    assert not digest.named("Unlisted hook 0x7bc2…2aec") and not digest.named("Unlisted hook family 0x0f48…")
    assert not digest.named(None) and not digest.named("")
    assert digest.named("BeigeBoxHook 0x9c40…6a80") and digest.named("Aegis")   # a names.py two-team split is named
