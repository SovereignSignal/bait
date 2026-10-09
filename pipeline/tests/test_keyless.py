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
