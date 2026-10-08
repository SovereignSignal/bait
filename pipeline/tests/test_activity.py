from datetime import timedelta

import sqlalchemy as sa

from bait import db
from bait.steps import activity
from tests.conftest import DAY, NOW, insert_pool

BASE, UNI = 8453, 130
POOL_A = "0x" + "a" * 64        # base, busy: 25 wallets -> capped at 20
POOL_B = "0x" + "b" * 64        # unichain: Nansen does not cover it
POOL_W = "0x" + "c" * 64        # wash: $5M from 5 wallets, never fetched


def _w(i: int) -> str:
    return "0x" + f"{i:040x}"


def _trade(addr, usd, kind, ts):
    return {"attributes": {"tx_from_address": addr, "kind": kind, "volume_in_usd": str(usd),
                           "block_timestamp": ts, "tx_hash": "0x" + "f" * 64}}


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.paths = []

    def gt(self, path):
        self.paths.append(path)
        return self.responses.get(path)


class FakeNansen:
    source = "nansen-labels"

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    @staticmethod
    def supports(chain_id):
        return chain_id not in (130, 57073)

    def labels(self, address, chain_id):
        self.calls.append((address, chain_id))
        return self.answers.get(address, [])


def _seed(conn):
    for pid, cid, vol, traders in ((POOL_A, BASE, 900_000, 400), (POOL_B, UNI, 50_000, 30), (POOL_W, BASE, 5_000_000, 5)):
        insert_pool(conn, pid, cid, "0x" + "e" * 40, NOW - timedelta(days=30))
        conn.execute(sa.insert(db.pool_stats).values(pool_id=pid, chain_id=cid, day=DAY, vol_24h=vol, traders_24h=traders))
    # wallet i trades (i+1)*100 USD on pool A; wallet 0 also sells once later, mixed-case address
    a_trades = [_trade(_w(i), (i + 1) * 100, "buy", "2026-10-06T01:00:00Z") for i in range(25)]
    a_trades.append(_trade(_w(24).upper().replace("0X", "0x"), 50, "sell", "2026-10-06T05:30:00Z"))
    b_trades = [_trade(_w(100 + i), 5_000, "sell", "2026-10-06T02:00:00Z") for i in range(3)]
    return FakeHttp({
        f"/networks/base/pools/{POOL_A}/trades": {"data": a_trades},
        f"/networks/unichain/pools/{POOL_B}/trades": {"data": b_trades},
    })


def test_activity_aggregates_top_wallets_and_skips_wash(conn, ctx):
    http = _seed(conn)
    ctx.extras["keyless_http"] = http
    # stale rows beyond retention are pruned
    conn.execute(sa.insert(db.pool_top_wallets).values(pool_id=POOL_A, chain_id=BASE, day=DAY - timedelta(days=15),
                                                       address=_w(1), volume_usd=1.0, trades=1, buys=1, sells=0))
    out = activity.run(conn, ctx)
    assert out["errors"] == 0 and out["pools"] == 2 and out["pools_fetched"] == 2 and out["pruned"] == 1
    assert not any(POOL_W in p for p in http.paths)
    assert http.paths[0].endswith(f"{POOL_A}/trades")       # highest volume first

    rows = conn.execute(sa.select(db.pool_top_wallets).where(db.pool_top_wallets.c.pool_id == POOL_A)
                        .order_by(db.pool_top_wallets.c.volume_usd.desc())).all()
    assert len(rows) == 20                                   # 25 wallets, capped
    top = rows[0]
    assert top.address == _w(24) and top.trades == 2 and top.buys == 1 and top.sells == 1
    assert top.volume_usd == 2_550 and db.as_utc(top.last_trade_at).hour == 5
    assert {r.address for r in rows} == {_w(i) for i in range(5, 25)}
    assert all(r.address == r.address.lower() for r in rows)
    b = conn.execute(sa.select(db.pool_top_wallets).where(db.pool_top_wallets.c.pool_id == POOL_B)).all()
    assert len(b) == 3 and all(r.sells == 1 and r.buys == 0 for r in b)

    # idempotent re-run for the same day
    activity.run(conn, ctx)
    assert conn.execute(sa.select(sa.func.count()).select_from(db.pool_top_wallets)).scalar_one() == 23


def test_activity_pool_limit(conn, ctx, monkeypatch):
    ctx.extras["keyless_http"] = _seed(conn)
    monkeypatch.setenv("ACTIVITY_POOLS", "1")
    out = activity.run(conn, ctx)
    assert out["pools"] == 1 and len(ctx.extras["keyless_http"].paths) == 1


def test_activity_without_keyless_or_keys_is_a_noop(conn, ctx):
    _seed(conn)
    out = activity.run(conn, ctx)                            # KEYLESS=0 from conftest, nansen None
    assert out["pools"] == 2 and out["pools_fetched"] == 0 and out["errors"] == 0


def test_nansen_labels_cap_window_chains_and_flags(conn, ctx, monkeypatch):
    ctx.extras["keyless_http"] = _seed(conn)
    monkeypatch.setenv("NANSEN_MAX_ADDRESSES", "4")
    # unichain wallets rank first by volume but are not covered. _w(24) was labeled 2 days ago -> fresh, skipped. _w(23) is stale (30d) -> re-queried.
    conn.execute(sa.insert(db.wallet_labels).values(address=_w(24), chain_id=BASE, labels=["Old"], smart_money=False,
                                                    source="nansen-labels", as_of=NOW - timedelta(days=2)))
    conn.execute(sa.insert(db.wallet_labels).values(address=_w(23), chain_id=BASE, labels=[], smart_money=False,
                                                    source="nansen-labels", as_of=NOW - timedelta(days=30)))
    nansen = FakeNansen({_w(23): ["Smart Trader"], _w(22): ["Binance: Hot Wallet"]})
    ctx.clients["nansen"] = nansen
    out = activity.run(conn, ctx)

    assert nansen.calls == [(_w(23), BASE), (_w(22), BASE), (_w(21), BASE), (_w(20), BASE)]   # cap 4, by volume
    assert all(cid != UNI for _, cid in nansen.calls)
    assert out["nansen_queried"] == 4 and out["nansen_labeled"] == 2 and out["nansen_smart_money"] == 1
    assert out["nansen_fresh"] == 1 and out["nansen_skipped_chain"] == 3 and out["errors"] == 0

    lab = {r.address: r for r in conn.execute(sa.select(db.wallet_labels))}
    assert lab[_w(23)].smart_money and lab[_w(23)].labels == ["Smart Trader"] and db.as_utc(lab[_w(23)].as_of) == NOW
    assert not lab[_w(22)].smart_money
    assert lab[_w(21)].labels == [] and lab[_w(21)].source == "nansen-labels"     # empty answer stored
    assert lab[_w(24)].labels == ["Old"]                                          # untouched
    assert _w(100) not in lab

    # second run the same day: everything already queried is fresh; the cap moves on to the next wallets
    nansen.calls.clear()
    activity.run(conn, ctx)
    assert nansen.calls == [(_w(19), BASE), (_w(18), BASE), (_w(17), BASE), (_w(16), BASE)]


def test_nansen_errors_are_counted_not_raised(conn, ctx):
    ctx.extras["keyless_http"] = _seed(conn)

    class Boom(FakeNansen):
        def labels(self, address, chain_id):
            self.calls.append(address)
            if address == _w(24):
                raise RuntimeError("timeout")
            return []

    nansen = Boom({})
    ctx.clients["nansen"] = nansen
    out = activity.run(conn, ctx)
    assert out["errors"] == 1 and out["nansen_queried"] == 20
    assert conn.execute(sa.select(sa.func.count()).select_from(db.wallet_labels)).scalar_one() == 19


def test_additive_migration_on_sqlite_is_idempotent(tmp_path):
    e = db.make_engine(f"sqlite:///{tmp_path}/old.sqlite")
    with e.begin() as c:   # production shape before 0003
        c.execute(sa.text("CREATE TABLE pool_stats (pool_id text NOT NULL, chain_id integer NOT NULL, day date NOT NULL,"
                          " vol_24h double precision, traders_24h integer, PRIMARY KEY (pool_id, chain_id, day))"))
        c.execute(sa.text("INSERT INTO pool_stats VALUES ('0xp', 8453, '2026-10-06', 1.5, 7)"))
    db.create_all(e)                                     # leaves the existing table alone
    added = db.add_missing_columns(e)
    assert added == ["pool_stats.buys_24h", "pool_stats.sells_24h", "pool_stats.buyers_24h", "pool_stats.sellers_24h"]
    assert db.add_missing_columns(e) == []               # second run is a no-op
    cols = {c["name"] for c in sa.inspect(e).get_columns("pool_stats")}
    assert {"buys_24h", "sells_24h", "buyers_24h", "sellers_24h", "vol_24h"} <= cols
    with e.begin() as c:
        db.upsert(c, db.pool_stats, [{"pool_id": "0xp", "chain_id": 8453, "day": DAY, "vol_24h": 2.0, "traders_24h": 8,
                                      "buys_24h": 1, "sells_24h": 2, "buyers_24h": 3, "sellers_24h": 4}],
                  keys=["pool_id", "chain_id", "day"])
        r = c.execute(sa.select(db.pool_stats)).one()
    assert (r.vol_24h, r.buys_24h, r.sellers_24h) == (2.0, 1, 4)
    # tables that do not exist are skipped, not created
    assert db.add_missing_columns(e, [("nope", "x", "integer")]) == []


def test_parse_nansen_labels_defensively():
    from bait.clients.nansen import parse_labels
    assert parse_labels({"data": [{"label": "Smart LP"}, {"name": "Fund"}, "Whale", {"x": 1}, None]}) == ["Smart LP", "Fund", "Whale"]
    assert parse_labels({"labels": ["A", "A", " "]}) == ["A"]
    assert parse_labels({"data": {"labels": [{"label": "B"}]}}) == ["B"]
    assert parse_labels({"data": []}) == [] and parse_labels(None) == [] and parse_labels([{"label": "C"}]) == ["C"]


def test_signals_reuses_fresh_wallet_labels(conn, ctx):
    from bait.steps import signals
    conn.execute(sa.insert(db.wallet_labels).values(address=_w(1), chain_id=BASE, labels=["Smart Money"], smart_money=True,
                                                    source="nansen-labels", as_of=NOW))
    nansen = FakeNansen({_w(2): ["Fund"]})
    out = {}
    assert signals._labels_cached(conn, ctx, nansen, _w(1).upper().replace("0X", "0x"), BASE, out) == ["Smart Money"]
    assert signals._labels_cached(conn, ctx, nansen, _w(2), BASE, out) == ["Fund"]
    assert nansen.calls == [(_w(2), BASE)] and out["nansen_cached"] == 1
    assert conn.execute(sa.select(db.wallet_labels.c.smart_money).where(db.wallet_labels.c.address == _w(2))).scalar_one()
