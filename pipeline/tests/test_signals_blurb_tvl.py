"""A blurb that called an unknown TVL "zero" is rewritten once the project's TVL reads unknown."""
from datetime import timedelta
from types import SimpleNamespace

import sqlalchemy as sa

from bait import db
from bait.steps import signals
from tests.conftest import NOW, insert_project


def test_zero_tvl_blurb_is_rewritten_only_when_tvl_is_unknown(conn):
    pid = insert_project(conn, "Flock", first_seen=NOW - timedelta(days=9))
    conn.execute(sa.update(db.projects).where(db.projects.c.id == pid).values(
        why_blurb="Volume is growing. Reported TVL is zero and no audit is listed.", blurb_at=NOW))
    p = conn.execute(sa.select(db.projects).where(db.projects.c.id == pid)).one()
    assert signals._needs_blurb(conn, p, SimpleNamespace(tvl_usd=None)) is True
    assert signals._needs_blurb(conn, p, SimpleNamespace(tvl_usd=0.0)) is False       # a measured zero stays
    assert signals._needs_blurb(conn, p) is False
    conn.execute(sa.update(db.projects).where(db.projects.c.id == pid).values(why_blurb="Usage is broad and growing."))
    p = conn.execute(sa.select(db.projects).where(db.projects.c.id == pid)).one()
    assert signals._needs_blurb(conn, p, SimpleNamespace(tvl_usd=None)) is False
