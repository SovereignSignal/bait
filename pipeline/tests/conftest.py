import json
import os
import socket
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from bait import db
from bait.steps import RunContext

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
                      clients={k: None for k in ("graph", "blockscout", "claude", "exa", "github", "names", "sourcify")})


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


def insert_snap(conn, pool_id, chain_id, day, tvl, vol, tx, vol_day=None, tx_day=None):
    conn.execute(sa.insert(db.pool_snapshots).values(pool_id=pool_id, chain_id=chain_id, day=day, tvl_usd=tvl, volume_usd_cum=vol,
                                                     tx_count_cum=tx, volume_usd_day=vol_day, tx_count_day=tx_day))


@pytest.fixture(autouse=True)
def _no_keyless_network(monkeypatch):
    """The snapshot step falls back to GeckoTerminal + public RPC, and signals asks audit links; tests must never hit the
    network (a test that checks audit links passes its own probe)."""
    monkeypatch.setenv("KEYLESS", "0")
    monkeypatch.setenv("AUDIT_LINKS", "0")


NETWORK_ATTEMPTS: list[str] = []      # hosts a test tried to reach; _no_network fails the test that added one


def _loopback(host) -> bool:
    host = host.decode() if isinstance(host, bytes) else host
    return host is None or (isinstance(host, str) and (host in ("localhost", "::1") or host.startswith("127.")))


@pytest.fixture(autouse=True, scope="session")
def _block_network():
    """Tests never reach the network: name lookups and TCP connects to anything but loopback are refused and recorded
    (the Claude SDK test's local server and CI's Postgres are loopback). Code under test usually reads the refusal as a
    transient failure and carries on, which is how the CLI test reached the explorers and Sourcify unnoticed until
    2026-10-24 (KEYLESS=0 above covers the snapshot step only), so _no_network fails the test that tried."""
    real_getaddrinfo, real_connect, real_connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex

    def refuse(host):
        NETWORK_ATTEMPTS.append(str(host))
        return f"tests must not reach the network: {host}"

    def getaddrinfo(host, *a, **k):
        if not _loopback(host):
            raise socket.gaierror(refuse(host))
        return real_getaddrinfo(host, *a, **k)

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(address[0]):
            raise ConnectionRefusedError(refuse(address[0]))
        return real_connect(self, address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(address[0]):
            refuse(address[0])
            return 111                                   # ECONNREFUSED
        return real_connect_ex(self, address)

    mp = pytest.MonkeyPatch()
    mp.setattr(socket, "getaddrinfo", getaddrinfo)
    mp.setattr(socket.socket, "connect", connect)
    mp.setattr(socket.socket, "connect_ex", connect_ex)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _no_network(_block_network):
    before = len(NETWORK_ATTEMPTS)
    yield
    if tried := NETWORK_ATTEMPTS[before:]:
        pytest.fail(f"tried to reach the network {len(tried)} times: {', '.join(sorted(set(tried))[:5])}", pytrace=False)


@pytest.fixture
def pg_engine():
    """Real Postgres for behaviour SQLite cannot show (aborted transactions, savepoints, advisory locks).
    Skips unless TEST_DATABASE_URL is set; see scripts/pg-test-cluster.sh."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    e = db.make_engine(url)
    db.metadata.drop_all(e)
    db.create_all(e)
    db.add_missing_columns(e)
    yield e
    db.metadata.drop_all(e)
    e.dispose()
