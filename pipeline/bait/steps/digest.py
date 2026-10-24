"""Step 8 (digest): Monday (or --force-digest) -> digests(week_of=day): the week's New + Trending over the closed days
ending day - 1; wash-like projects are left out. Both lists follow the feed's tabs (web/src/lib/feed.ts applyTab): a
project nobody named is never listed, and New skips a launch whose 7-day volume is a measured $0 (unknown stays)."""
from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa

from ..db import digests, project_daily, projects, upsert
from . import RunContext, log


def named(name: str | None) -> bool:
    """False for a name nobody chose: the group step's "Unlisted hook 0x…" and "Unlisted hook family 0x…" placeholders.
    The web labels those by a pool pair and keeps them out of Trending, New and the movers (web/src/lib/unlisted.ts)."""
    return bool(name) and not name.startswith("Unlisted hook")


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    if ctx.day.weekday() != 0 and not ctx.force_digest:
        log.info("digest skipped: %s is not a Monday", ctx.day)
        return {"written": 0}
    rows = conn.execute(
        sa.select(projects.c.slug, projects.c.name, projects.c.category, projects.c.what_line, project_daily)
        .join(project_daily, project_daily.c.project_id == projects.c.id)
        .where(project_daily.c.day == ctx.day, project_daily.c.is_spam.is_(False))).all()
    live = [r for r in rows if not r.is_wash]   # listed in All, never counted in the week
    # the /leaders count (queries.ts getLeaders): non-spam, non-wash, 7-day volume > 0. One definition on both pages.
    traded = [r for r in live if (r.vol_7d or 0) > 0]

    def card(r):
        return {"slug": r.slug, "name": r.name, "category": r.category, "what_line": r.what_line,
                "tvl_usd": r.tvl_usd, "vol_7d": r.vol_7d, "vol_7d_prev": r.vol_7d_prev,
                "swaps_7d": r.swaps_7d, "pools_n": r.pools_n, "score_trending": r.score_trending}

    trending = sorted([r for r in live if (r.score_trending or 0) > 0 and named(r.name)],
                      key=lambda r: (-(r.score_trending or 0), -(r.tvl_usd or 0)))[:20]
    # The 2026-10-12 and 10-19 digests kept 20 New each, mostly unnamed hooks and $0 launches, so the week page (which
    # skips those) said "20 new projects" above a list of 4 and of 6. Filter here, and count every one that qualified.
    fresh = [r for r in live if r.is_new and named(r.name) and r.vol_7d != 0]
    new = sorted(fresh, key=lambda r: -(r.tvl_usd or 0))[:20]
    body = {
        "week_of": ctx.day.isoformat(), "window_end": (ctx.day - timedelta(days=1)).isoformat(), "generated_at": ctx.now.isoformat(),
        # new: every project that cleared the New bar this week; the "new" list keeps the 20 with the most TVL
        "totals": {"projects": len(live), "traded": len(traded), "new": len(fresh),
                   "tvl_usd": sum(r.tvl_usd or 0 for r in traded), "vol_7d": sum(r.vol_7d or 0 for r in traded)},
        "new": [card(r) for r in new], "trending": [card(r) for r in trending],
    }
    upsert(conn, digests, [{"week_of": ctx.day, "body": body}], keys=["week_of"])
    return {"written": 1, "new": len(new), "trending": len(trending)}
