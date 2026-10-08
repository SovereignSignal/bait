from datetime import timedelta

import sqlalchemy as sa

from bait import db
from bait.steps import digest, signals
from tests.conftest import DAY, NOW, insert_hook, insert_project


def seed_daily(conn, pid, score=0.0, new=False, spam=False, tvl=0.0):
    conn.execute(sa.insert(db.project_daily).values(project_id=pid, day=DAY, tvl_usd=tvl, vol_7d=0, vol_7d_prev=0,
                                                    swaps_7d=0, pools_n=1, score_trending=score, is_new=new, is_spam=spam))


class FakeExa:
    def __init__(self, hits):
        self.hits, self.calls = hits, 0

    def search_recent(self, query, days=30, n=10, now=None):
        self.calls += 1
        return self.hits


class FakeGitHub:
    def activity(self, url, days=30, now=None):
        return {"repo": "x/y", "stars": 12, "last_commit_at": (NOW - timedelta(days=2)).isoformat(), "commits_30d": True}


class FakeClaude:
    def __init__(self):
        self.calls = 0

    def why_blurb(self, ctx):
        self.calls += 1
        assert "metrics" in ctx and "signals" in ctx
        return "People use it. Numbers went up."


def test_signals_targets_receipts_alive_and_blurb_only_on_change(conn, ctx):
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

    hit = {"url": "https://blog.example.com/post", "title": "Hot hook ships", "source": "blog.example.com",
           "published_at": NOW - timedelta(days=3), "highlights": []}
    fresh_hit = {"url": "https://blog.example.com/fresh", "title": "Fresh v4 hook launches", "source": "blog.example.com",
                 "published_at": NOW - timedelta(days=2), "highlights": []}
    exa, claude = FakeExa([hit, fresh_hit]), FakeClaude()   # each project keeps only the title that names it
    ctx.clients.update(exa=exa, github=FakeGitHub(), claude=claude)
    out = signals.run(conn, ctx)
    assert out["projects"] == 2 and out["exa"] == 2 and out["receipts"] == 2 and out["github"] == 1
    assert out["blurbs"] == 2 and out["errors"] == 0 and out["nansen"] == 0
    rec = conn.execute(sa.select(db.receipts).where(db.receipts.c.project_id == hot)).one()
    assert rec.source == "blog.example.com" and rec.title == "Hot hook ships"
    sig = {r.kind: r.value for r in conn.execute(sa.select(db.signals).where(db.signals.c.project_id == hot))}
    assert sig["alive"] == {"alive": True} and sig["github"]["stars"] == 12 and sig["exa"]["hits_30d"] == 1
    p = conn.execute(sa.select(db.projects).where(db.projects.c.id == hot)).one()
    assert p.why_blurb == "People use it. Numbers went up." and p.blurb_at is not None

    # same signals next run -> as_of unchanged -> no new blurb
    ctx.now = NOW + timedelta(days=1)
    out2 = signals.run(conn, ctx)
    assert out2["blurbs"] == 0 and claude.calls == 2
    # signal changes -> blurb regenerated
    exa.hits = []
    ctx.now = NOW + timedelta(days=2)
    out3 = signals.run(conn, ctx)
    assert out3["blurbs"] == 2 and claude.calls == 4


def test_signals_without_keys_still_writes_alive(conn, ctx):
    p = insert_project(conn, "Solo", NOW)
    insert_hook(conn, "0x1", 1, project_id=p)
    seed_daily(conn, p, new=True)
    out = signals.run(conn, ctx)
    assert out["projects"] == 1 and out["exa"] == 0 and out["blurbs"] == 0
    assert conn.execute(sa.select(db.signals.c.value).where(db.signals.c.project_id == p)).scalar_one() == {"alive": False}


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
