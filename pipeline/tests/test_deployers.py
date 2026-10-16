"""Explorer creator lookup fills hooks.deployer for unlisted hooks; registry deployers are never overwritten."""
from datetime import timedelta

import sqlalchemy as sa

from bait import db
from bait.steps import deployers
from tests.conftest import NOW, insert_hook, insert_project


class FakeBlockscout:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def base_url(self, chain_id):
        return None if chain_id == 56 else "https://x.blockscout.com"

    def fetch_creator(self, chain_id, address):
        self.calls.append(address)
        if chain_id in self.blocked:
            return None, "blocked"
        return self.answers.get(address, (None, "miss"))

    blocked = ()


def test_creator_lookup_fills_misses_stamps_and_skips_transient(conn, ctx, monkeypatch):
    monkeypatch.setenv("DEPLOYER_LOOKUPS", "10")
    pid = insert_project(conn, "Unlisted hook 0x1", first_seen=NOW - timedelta(days=3))
    for addr in ("0xcreated", "0xeoa", "0xflaky", "0xlisted", "0xbnb"):
        insert_hook(conn, addr, 56 if addr == "0xbnb" else 10, project_id=pid, name=addr)
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == "0xlisted").values(deployer="0xregistry", deployer_source="hooklist"))
    bs = FakeBlockscout({"0xcreated": ("0xDEAD000000000000000000000000000000000001", "found"), "0xeoa": (None, "miss"), "0xflaky": (None, "transient")})
    ctx.clients.update(blockscout=bs)
    out = {}
    deployers.run(conn, ctx, out)
    rows = {r.address: r for r in conn.execute(sa.select(db.hooks))}
    assert rows["0xcreated"].deployer == "0xdead000000000000000000000000000000000001" and rows["0xcreated"].deployer_source == "explorer"
    assert rows["0xeoa"].deployer is None and rows["0xeoa"].deployer_checked_at is not None          # definitive miss: stamped
    assert rows["0xflaky"].deployer is None and rows["0xflaky"].deployer_checked_at is None           # transient: asked again
    assert rows["0xlisted"].deployer == "0xregistry" and "0xlisted" not in bs.calls                   # registry value kept
    assert "0xbnb" not in bs.calls                                                                   # no explorer instance
    assert (out["deployers_checked"], out["deployers_found"], out["deployers_transient"]) == (3, 1, 1)
    deployers.run(conn, ctx, out)
    assert bs.calls.count("0xeoa") == 1 and bs.calls.count("0xflaky") == 2                            # miss waits 14 days


def test_creator_lookup_is_off_without_a_client_or_with_a_zero_cap(conn, ctx, monkeypatch):
    pid = insert_project(conn, "Unlisted hook 0x2", first_seen=NOW)
    insert_hook(conn, "0xone", 10, project_id=pid, name="one")
    ctx.clients.update(blockscout=None)
    out = {}
    deployers.run(conn, ctx, out)
    assert out["deployers_checked"] == 0
    monkeypatch.setenv("DEPLOYER_LOOKUPS", "0")
    ctx.clients.update(blockscout=FakeBlockscout({}))
    deployers.run(conn, ctx, out)
    assert out["deployers_checked"] == 0


def test_a_blocked_instance_costs_one_probe_and_the_cap_is_filled_from_chains_that_answer(conn, ctx, monkeypatch):
    """The 10/16 nightly: Base and Robinhood challenge bots, sat at the top of the busiest-first queue, and took 199 of
    200 slots every night. One probe per chain now; blocked chains wait for the next run, unstamped."""
    monkeypatch.setenv("DEPLOYER_LOOKUPS", "3")
    pid = insert_project(conn, "Unlisted hook 0x3", first_seen=NOW)
    for i in range(6):
        insert_hook(conn, f"0xbase{i}", 8453, project_id=pid, name=f"b{i}", first_seen=NOW - timedelta(minutes=i))
    for i in range(4):
        insert_hook(conn, f"0xeth{i}", 1, project_id=pid, name=f"e{i}", first_seen=NOW - timedelta(hours=1, minutes=i))
    bs = FakeBlockscout({f"0xeth{i}": (f"0x{i:040x}", "found") for i in range(4)})
    bs.blocked = (8453,)
    ctx.clients.update(blockscout=bs)
    out = {}
    deployers.run(conn, ctx, out)
    assert sum(a.startswith("0xbase") for a in bs.calls) == 1               # one probe for the blocked chain
    assert [a for a in bs.calls if a.startswith("0xeth")] == ["0xeth0", "0xeth1", "0xeth2"]   # the cap, from Ethereum
    rows = {r.address: r for r in conn.execute(sa.select(db.hooks))}
    assert all(rows[f"0xbase{i}"].deployer_checked_at is None for i in range(6))           # never stamped
    assert out["deployers_found"] == 3 and out["deployers_blocked_chains"] == [8453]
