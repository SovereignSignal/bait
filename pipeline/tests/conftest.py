import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from hookfeed import db
from hookfeed.steps import RunContext

FIXTURE = str(Path(__file__).resolve().parents[1] / "fixtures" / "hooklist.sample.json")
DAY = date(2026, 10, 6)                      # a Tuesday
NOW = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine():
    e = db.make_engine("sqlite://")
    db.create_all(e)
    return e


@pytest.fixture
def conn(engine):
    with engine.begin() as c:
        yield c


@pytest.fixture
def ctx():
    # clients pre-populated with None = "no key"; tests override what they need
    return RunContext(day=DAY, now=NOW, hooklist_file=FIXTURE, max_code_fetch=0,
                      clients={k: None for k in ("graph", "blockscout", "claude", "exa", "parallel", "nansen", "github")})


@pytest.fixture
def fixture_entries():
    return json.loads(Path(FIXTURE).read_text())


def insert_project(conn, name, first_seen, slug=None):
    r = conn.execute(sa.insert(db.projects).values(slug=slug or name.lower(), name=name, norm_name=name.lower(), first_seen_at=first_seen))
    return r.inserted_primary_key[0]


def insert_hook(conn, address, chain_id, project_id=None, name=None, code_hash=None, first_seen=NOW):
    conn.execute(sa.insert(db.hooks).values(address=address, chain_id=chain_id, project_id=project_id, name=name,
                                            code_hash=code_hash, first_seen_at=first_seen, flags={}, properties={}))


def insert_pool(conn, pool_id, chain_id, hook, created):
    conn.execute(sa.insert(db.pools).values(pool_id=pool_id, chain_id=chain_id, hook_address=hook, created_at=created))


def insert_snap(conn, pool_id, chain_id, day, tvl, vol, tx):
    conn.execute(sa.insert(db.pool_snapshots).values(pool_id=pool_id, chain_id=chain_id, day=day, tvl_usd=tvl, volume_usd_cum=vol, tx_count_cum=tx))


@pytest.fixture(autouse=True)
def _no_keyless_network(monkeypatch):
    """The snapshot step falls back to GeckoTerminal + public RPC; tests must never hit the network."""
    monkeypatch.setenv("KEYLESS", "0")
