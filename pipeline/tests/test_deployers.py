"""Deployer lookup fills hooks.deployer for grouped hooks (Sourcify, then the explorer); registry deployers are never
overwritten and a shared CREATE2 deployer is never stored."""
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from bait import db
from bait.clients.blockscout import Blockscout
from bait.clients.sourcify import Sourcify
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


# --- Sourcify first, Blockscout for the rest, never a CREATE2 proxy (2026-10-24) ---
# pipeline/fixtures/deployers-2026-10-24.json: Sourcify's ?fields=deployment replies for five hooks on four chains, and
# Blockscout's address and transaction records for StablePairHook on Ethereum (trimmed to the fields read), captured
# from the public APIs on 2026-10-24.

RECORDED = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "deployers-2026-10-24.json").read_text())
ARACHNID = "0x4e59b44847b379578588920ca78fbf26c0b4956c"
STABLEPAIR_ETH = "0x0000113dcf4add69999fad8f20f2b63f979bfcc0"
STABLEPAIR_TEAM = "0xe49acc3b16c097ec88dc9352ce4cd57ab7e35b95"      # also the registry's deployer for this hook


def W(n):
    return "0x" + f"{n:040x}"


def _sourcify(handler, sleep=lambda s: None):
    return Sourcify(http_client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleep)


def _replay_sourcify(req):
    for r in RECORDED["sourcify"]:
        if req.url.path == f"/server/v2/contract/{r['chain_id']}/{r['address']}":
            assert req.url.params["fields"] == "deployment"
            return httpx.Response(r["status"], json=r["body"])
    raise AssertionError(f"unrecorded {req.url}")


def test_sourcify_names_the_wallet_that_sent_the_creation_transaction():
    """StablePairHook on Ethereum came out of Arachnid's CREATE2 proxy and Blockscout calls the proxy its creator;
    Sourcify's deployer is the wallet that sent the transaction (the registry names the same wallet)."""
    s = _sourcify(_replay_sourcify)
    got = {(r["chain_id"], r["address"][:6]): s.fetch_deployer(r["chain_id"], r["address"]) for r in RECORDED["sourcify"]}
    assert got == {
        (1, "0x0000"): (STABLEPAIR_TEAM, "found"),                                       # StablePairHook, Ethereum
        (42161, "0x630e"): (None, "miss"),                                               # StablePairHook, Arbitrum: 404
        (8453, "0xd60d"): ("0xd46618f35099074c5a456b21d2967a6ff6841bd3", "found"),       # Clanker, Base
        (4663, "0x4445"): ("0x697161a9ece8afd6ae30946d790077050f993cbc", "found"),       # Kyber KEM hook, Robinhood
        (8453, "0x8f29"): (None, "miss"),                    # Aegis, Base: verified from runtime alone, no deployment
    }


@pytest.mark.parametrize("status,body,expected", [
    (200, b"<html>maintenance</html>", "transient"),                                   # not JSON
    (200, b"[]", "transient"),                                                         # not an object
    (200, b'{"deployment": "0xe49acc3b16c097ec88dc9352ce4cd57ab7e35b95"}', "miss"),    # deployment not an object
    (200, b'{"deployment": {"deployer": "0x1234"}}', "miss"),                          # not an address
    (200, b'{"deployment": {"deployer": "0x4e59b44847b379578588920cA78FbF26c0B4956C"}}', "miss"),   # the proxy
    (200, b'{"match": "match"}', "miss"),                                              # no deployment field
    (400, b'{"customCode": "unsupported_chain"}', "miss"),
    (403, b"challenge", "blocked"),
    (502, b"", "transient"),
])
def test_sourcify_malformed_and_error_replies(status, body, expected):
    assert _sourcify(lambda req: httpx.Response(status, content=body)).fetch_deployer(1, STABLEPAIR_ETH) == (None, expected)


def test_sourcify_waits_out_a_429_and_a_lasting_one_or_a_dead_network_is_transient():
    replies, slept = iter([httpx.Response(429), httpx.Response(200, json={"deployment": {"deployer": W(5)}})]), []
    assert _sourcify(lambda req: next(replies), sleep=slept.append).fetch_deployer(1, STABLEPAIR_ETH) == (W(5), "found")
    assert 2.0 in slept
    assert _sourcify(lambda req: httpx.Response(429)).fetch_deployer(1, STABLEPAIR_ETH) == (None, "transient")

    def down(req):
        raise httpx.ConnectError("down", request=req)
    assert _sourcify(down).fetch_deployer(1, STABLEPAIR_ETH) == (None, "transient")


def _blockscout(routes):
    def handler(req):
        for frag, (status, body) in routes.items():
            if frag in req.url.path:
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={})
    return Blockscout(http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_blockscout_gives_the_creation_transaction_sender_never_the_create2_proxy():
    addr, tx = RECORDED["blockscout"]
    assert addr["body"]["creator_address_hash"].lower() == ARACHNID                   # what the explorer calls creator
    routes = {"/addresses/": (addr["status"], addr["body"]), "/transactions/": (tx["status"], tx["body"])}
    assert _blockscout(routes).fetch_creator(1, STABLEPAIR_ETH) == (STABLEPAIR_TEAM, "found")
    unreadable = {"/addresses/": routes["/addresses/"]}                                # the transaction 404s
    assert _blockscout(unreadable).fetch_creator(1, STABLEPAIR_ETH) == (None, "miss")  # unknown, never the proxy
    no_tx = {"/addresses/": (200, {"creator_address_hash": "0x" + "AB" * 20})}         # no hash: the creator stands
    assert _blockscout(no_tx).fetch_creator(1, STABLEPAIR_ETH) == ("0x" + "ab" * 20, "found")
    challenged = {**routes, "/transactions/": (403, {})}
    assert _blockscout(challenged).fetch_creator(1, STABLEPAIR_ETH) == (None, "blocked")
    assert _blockscout({}).fetch_creator(1, STABLEPAIR_ETH) == (None, "miss")          # no record
    assert _blockscout(routes).fetch_creator(56, STABLEPAIR_ETH) == (None, "miss")     # no instance for BNB


class FakeSourcify:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def fetch_deployer(self, chain_id, address):
        self.calls.append(address)
        return self.answers.get(address, (None, "miss"))


def test_sourcify_first_then_the_explorer_and_a_proxy_from_either_is_never_stored(conn, ctx, monkeypatch):
    monkeypatch.setenv("DEPLOYER_LOOKUPS", "20")
    pid = insert_project(conn, "Unlisted hook 0x4", first_seen=NOW)
    chains = {"0xsrc": 10, "0xexp": 10, "0xnone": 10, "0xflaky": 10, "0xproxy": 10, "0xbnb": 56, "0xbase1": 8453,
              "0xbase2": 8453, "0xlisted": 10}
    for i, (a, c) in enumerate(chains.items()):
        insert_hook(conn, a, c, project_id=pid, name=a, first_seen=NOW - timedelta(minutes=i))
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == "0xlisted").values(deployer=W(9), deployer_source="hooklist"))
    src = FakeSourcify({"0xsrc": ("0x" + "AB" * 20, "found"), "0xflaky": (None, "transient"),
                        "0xproxy": (ARACHNID, "found"), "0xbnb": (W(3), "found")})
    bs = FakeBlockscout({"0xexp": (W(2), "found"), "0xproxy": (ARACHNID, "found")})
    bs.blocked = (8453,)
    ctx.clients.update(sourcify=src, blockscout=bs)
    out = {}
    deployers.run(conn, ctx, out)
    rows = {r.address: r for r in conn.execute(sa.select(db.hooks))}

    def got(a):
        return rows[a].deployer, rows[a].deployer_source, rows[a].deployer_checked_at is not None
    assert got("0xsrc") == ("0x" + "ab" * 20, "sourcify", True) and "0xsrc" not in bs.calls   # Sourcify answered
    assert got("0xbnb") == (W(3), "sourcify", True)                          # a chain without an explorer instance
    assert got("0xexp") == (W(2), "explorer", True)                          # not verified: the explorer's sender
    assert got("0xnone") == (None, None, True)                               # both answered, neither knows: stamped
    assert got("0xflaky") == (None, None, False)                             # Sourcify failed: asked again next run
    assert got("0xproxy") == (None, None, True) and "0xproxy" in bs.calls    # the proxy from both: unknown
    assert got("0xbase1") == got("0xbase2") == (None, None, True)            # Sourcify answered; explorer challenges
    assert sum(a.startswith("0xbase") for a in bs.calls) == 1                # one probe for the blocked instance
    assert got("0xlisted") == (W(9), "hooklist", False) and "0xlisted" not in src.calls
    assert (out["deployers_checked"], out["deployers_found"], out["deployers_transient"], out["deployers_sourcify"]) == (8, 3, 1, 2)
    assert out["deployers_blocked_chains"] == [8453]


def test_a_stored_create2_proxy_is_forgotten_and_looked_up_again(conn, ctx):
    pid = insert_project(conn, "Unlisted hook 0x5", first_seen=NOW)
    insert_hook(conn, "0xold", 1, project_id=pid, name="old")
    conn.execute(sa.update(db.hooks).values(deployer=ARACHNID, deployer_source="explorer", deployer_checked_at=NOW))
    ctx.clients.update(sourcify=FakeSourcify({"0xold": (STABLEPAIR_TEAM, "found")}))
    out = {}
    deployers.run(conn, ctx, out)
    row = conn.execute(sa.select(db.hooks)).one()
    assert (row.deployer, row.deployer_source, out["deployers_cleared"]) == (STABLEPAIR_TEAM, "sourcify", 1)


def test_web_and_pipeline_share_the_create2_deployer_list():
    """web/src/lib/present.ts leaves the same addresses off the project page's "Deployer" line."""
    import re
    from bait.rules import SHARED_DEPLOYERS
    src = (Path(__file__).resolve().parents[2] / "web/src/lib/present.ts").read_text()
    block = src.split("export const SHARED_DEPLOYERS", 1)[1].split("]);", 1)[0]
    assert set(re.findall(r'"(0x[0-9a-f]{40})"', block)) == SHARED_DEPLOYERS
