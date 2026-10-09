"""Step 8 (digest): Monday (or --force-digest) -> digests(week_of=day): the week's New + Trending over the closed days
ending day - 1; wash-like projects are left out."""
from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa

from ..db import digests, project_daily, projects, upsert
from . import RunContext, log


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    if ctx.day.weekday() != 0 and not ctx.force_digest:
        log.info("digest skipped: %s is not a Monday", ctx.day)
        return {"written": 0}
    rows = conn.execute(
        sa.select(projects.c.slug, projects.c.name, projects.c.category, projects.c.what_line, project_daily)
        .join(project_daily, project_daily.c.project_id == projects.c.id)
        .where(project_daily.c.day == ctx.day, project_daily.c.is_spam.is_(False))).all()
    live = [r for r in rows if not r.is_wash]   # listed in All, never counted in the week

    def card(r):
        return {"slug": r.slug, "name": r.name, "category": r.category, "what_line": r.what_line,
                "tvl_usd": r.tvl_usd, "vol_7d": r.vol_7d, "vol_7d_prev": r.vol_7d_prev,
                "swaps_7d": r.swaps_7d, "pools_n": r.pools_n, "score_trending": r.score_trending}

    trending = sorted([r for r in live if (r.score_trending or 0) > 0], key=lambda r: (-(r.score_trending or 0), -(r.tvl_usd or 0)))[:20]
    new = sorted([r for r in live if r.is_new], key=lambda r: -(r.tvl_usd or 0))[:20]   # 85 were New on 10/8
    body = {
        "week_of": ctx.day.isoformat(), "window_end": (ctx.day - timedelta(days=1)).isoformat(), "generated_at": ctx.now.isoformat(),
        "totals": {"projects": len(live), "tvl_usd": sum(r.tvl_usd or 0 for r in live), "vol_7d": sum(r.vol_7d or 0 for r in live)},
        "new": [card(r) for r in new], "trending": [card(r) for r in trending],
    }
    upsert(conn, digests, [{"week_of": ctx.day, "body": body}], keys=["week_of"])
    return {"written": 1, "new": len(new), "trending": len(trending)}
