from bait.keyless import categorize, decode_pool_key


def _word(hex_no_prefix: str) -> str:
    return hex_no_prefix.rjust(64, "0")


def test_decode_pool_key_hooked():
    res = "0x" + _word("aa" * 20) + _word("bb" * 20) + _word("800000") + _word("3c") + _word("7ee62731ef799f653e520a26f9428b419ecbd000")
    pk = decode_pool_key(res)
    assert pk["currency0"] == "0x" + "aa" * 20
    assert pk["fee"] & 0x800000
    assert pk["tickSpacing"] == 60
    assert pk["hooks"] == "0x7ee62731ef799f653e520a26f9428b419ecbd000"


def test_decode_pool_key_never_touched_by_position_manager():
    assert decode_pool_key("0x" + "0" * 320) is None
    assert decode_pool_key("0x") is None
    assert decode_pool_key(None) is None


def test_categorize_keywords_then_dynamic_fee_then_other():
    assert categorize("A memecoin launch hook", {}, {}) == "launchpad"
    assert categorize("Executes limit orders on-chain", {}, {}) == "orders"
    assert categorize("plain hook", {}, {"dynamicFee": True}) == "dynamic-fee"
    assert categorize("plain hook", {}, {}) == "other"


def test_keyless_progress_goes_through_the_bait_logger_not_stderr(caplog, capsys):
    import logging
    from bait import keyless as K
    with caplog.at_level(logging.INFO, logger="bait"):
        K.log("base: 12 v4 pools from GeckoTerminal (uniswap_v4_base)")
        K.warn("GT 500 /networks/base/dexes?page=1")
    assert capsys.readouterr().err == ""
    recs = [(r.name, r.levelname, r.getMessage()) for r in caplog.records]
    assert ("bait", "INFO", "[keyless] base: 12 v4 pools from GeckoTerminal (uniswap_v4_base)") in recs
    assert ("bait", "WARNING", "[keyless] GT 500 /networks/base/dexes?page=1") in recs


def test_keyless_and_blockscout_write_the_same_code_hash():
    import httpx
    from bait import keyless as K
    from bait.clients.blockscout import Blockscout
    from bait.rules import code_hash
    code, a = "0x6080604052", "0x" + "1" * 40

    class Rpc:
        def rpc_batch(self, url, calls):
            return [{"id": c["id"], "result": code} for c in calls]

    bs = Blockscout(http_client=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"deployed_bytecode": code, "is_verified": False}, request=req))))
    assert K.code_hashes(Rpc(), {"base": {a}})[("base", a)] == bs.fetch_code(8453, a).code_hash == code_hash(code)


SLOT = "360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"


def test_code_facts_hash_and_proxy_flag_from_the_bytecode():
    from bait import keyless as K
    from bait.rules import code_hash
    proxy, plain = "0x6080" + SLOT + "54", "0x6080604052"
    a, b, c, d = ("0x" + ch * 40 for ch in "abcd")
    codes = {a: proxy, b: plain, c: "0x"}                    # d: never answered

    class Rpc:
        def rpc_batch(self, url, calls):
            return [{"id": x["id"], "result": codes[x["params"][0]]} for x in calls if x["params"][0] in codes]

    got = K.code_facts(Rpc(), {"base": {a, b, c, d}})
    assert got[("base", a)] == (code_hash(proxy), "eip1967") and got[("base", b)] == (code_hash(plain), None)
    assert got[("base", c)] is None and ("base", d) not in got      # no code: definitive; unanswered: absent


def test_blockscout_flags_proxies_from_bytecode_and_proxy_type():
    import httpx
    from bait.clients.blockscout import Blockscout

    def bs(body):
        return Blockscout(http_client=httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=body, request=req))))
    a = "0x" + "1" * 40
    assert bs({"deployed_bytecode": "0x6080" + SLOT}).fetch_code(8453, a).is_proxy is True
    assert bs({"deployed_bytecode": "0x6080", "proxy_type": "eip1967"}).fetch_code(8453, a).is_proxy is True
    assert bs({"deployed_bytecode": "0x6080"}).fetch_code(8453, a).is_proxy is False


def test_code_facts_stops_a_silent_chain_after_three_batches_and_at_the_deadline():
    import time
    from bait import keyless as K

    class Silent:
        def __init__(self):
            self.calls = 0

        def rpc_batch(self, url, calls):
            self.calls += 1
            return []
    addrs = {f"0x{i:040x}" for i in range(50)}                                        # 5 batches of 10
    rpc = Silent()
    assert K.code_facts(rpc, {"base": addrs, "ink": set()}) == {} and rpc.calls == 3
    late = Silent()
    assert K.code_facts(late, {"base": addrs}, deadline=time.monotonic() - 1) == {} and late.calls == 0


def test_an_explorer_can_upgrade_a_proxy_verdict_but_never_downgrade_eip1967():
    import httpx
    from types import SimpleNamespace
    from bait.clients.blockscout import Blockscout
    from bait.rules import code_evidence

    def bs(body):
        return Blockscout(http_client=httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=body, request=req))))
    a = "0x" + "1" * 40
    unresolved = bs({"deployed_bytecode": "0x6080" + SLOT, "proxy_type": None, "implementations": []}).fetch_code(8453, a)
    assert unresolved.is_proxy is True and unresolved.proxy_kind == "eip1967"     # Blockscout just could not resolve it
    assert code_evidence(SimpleNamespace(code_hash=unresolved.code_hash, is_proxy=True, proxy_kind="eip1967")) is None
    upgraded = bs({"deployed_bytecode": "0x6080", "proxy_type": "eip1967"}).fetch_code(8453, a)
    assert upgraded.is_proxy is True and upgraded.proxy_kind == "explorer"


def test_fetch_daily_parses_candles_oldest_first_and_none_on_failure():
    from bait import keyless as K

    class Http:
        def __init__(self, res):
            self.res, self.paths = res, []

        def gt(self, path):
            self.paths.append(path)
            return self.res

    h = Http({"data": {"attributes": {"ohlcv_list": [[1759968000, 1, 2, 0.5, 1.5, "1200.5"], [1759881600, 1, 1, 1, 1, None]]}}})
    assert K.fetch_daily(h, "base", "0xpool", limit=30) == [(1759881600, 0.0), (1759968000, 1200.5)]
    assert h.paths == ["/networks/base/pools/0xpool/ohlcv/day?aggregate=1&limit=30&currency=usd"]
    assert K.fetch_daily(Http({"data": {"attributes": {"ohlcv_list": []}}}), "base", "0xpool") == []
    assert K.fetch_daily(Http(None), "base", "0xpool") is None
