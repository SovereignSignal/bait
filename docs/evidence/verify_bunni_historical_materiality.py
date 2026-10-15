#!/usr/bin/env python3
"""Verify the adjacent proposed public evidence bundle offline, with the Python standard library only.

No network, database, source edits, imports, or metric updates. Source response hashes identify
upstream evidence; this verifier does not re-fetch the sources or certify current coverage.
"""
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path

# Existing repository test helper, copied without runtime/test dependencies from
# pipeline/tests/test_indexer.py. Validate it against the standard Transfer topic below.
def _keccak256(data: bytes) -> str:
    rc = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
          0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
          0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
          0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
          0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
    rot = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
    m = (1 << 64) - 1
    rl = lambda v, n: ((v << n) | (v >> (64 - n))) & m if n else v
    rate = 136
    msg = bytearray(data) + b"\x01"
    while len(msg) % rate:
        msg.append(0)
    msg[-1] |= 0x80
    s = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        for i in range(rate // 8):
            s[i % 5][i // 5] ^= int.from_bytes(msg[off + 8 * i: off + 8 * i + 8], "little")
        for r in range(24):
            c = [s[x][0] ^ s[x][1] ^ s[x][2] ^ s[x][3] ^ s[x][4] for x in range(5)]
            d = [c[(x - 1) % 5] ^ rl(c[(x + 1) % 5], 1) for x in range(5)]
            s = [[s[x][y] ^ d[x] for y in range(5)] for x in range(5)]
            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    b[y][(2 * x + 3 * y) % 5] = rl(s[x][y], rot[x][y])
            s = [[b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
            s[0][0] ^= rc[r]
    return "0x" + b"".join(s[i % 5][i // 5].to_bytes(8, "little") for i in range(4)).hex()

def main():
    path = Path(__file__).with_name("bunni-historical-materiality-2025.proposed.json")
    bundle = json.loads(path.read_text(), parse_float=Decimal)
    grain = bundle["grain"]
    pool = bundle["identity"]["pool_row"]
    assert grain["chain_id"] == pool["chain_id"] == 1
    assert grain["pool_id"] == pool["pool_id"]
    assert grain["hook_address"] == pool["hook_address"]
    assert _keccak256(b"Transfer(address,address,uint256)") == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    custom = _keccak256(b"Swap(bytes32,address,bool,bool,uint256,uint256,uint160,int24,uint24,uint256)")
    standard = _keccak256(b"Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
    assert custom == bundle["identity"]["custom_event_signature_topic_recomputed"]
    c0, c1 = pool["token0"]["id"], pool["token1"]["id"]
    encoded = b"".join(int(x, 16).to_bytes(32, "big") for x in [c0, c1])
    encoded += int(pool["fee"]).to_bytes(32, "big")
    encoded += int(pool["tick_spacing"]).to_bytes(32, "big", signed=True)
    encoded += int(pool["hook_address"], 16).to_bytes(32, "big")
    assert _keccak256(encoded) == pool["pool_id"]
    events = bundle["events"]
    event_ids = set()
    for event in events:
        raw = event["raw_log"]
        identity = f"{event['chain_id']}:{event['transaction_hash']}:{event['log_index']}"
        assert identity == event["event_id"] and identity not in event_ids
        event_ids.add(identity)
        assert event["chain_id"] == grain["chain_id"]
        assert event["pool_id"] == grain["pool_id"] == raw["topics"][1]
        assert event["hook_address"] == grain["hook_address"] == raw["address"]
        assert event["event_topic"] == custom == raw["topics"][0]
        assert event["transaction_hash"] == raw["transaction_hash"]
        assert event["block_number"] == raw["block_number"] < 23273098
        assert event["block_hash"] == raw["block_hash"]
        assert event["log_index"] == raw["log_index"]
        words = [int(raw["data"][i:i+64], 16) for i in range(2, len(raw["data"]), 64)]
        assert len(words) == 8
        assert bool(words[1]) == event["zero_for_one"]
        assert words[2] == int(event["input_amount_raw"]) > 0
        assert words[3] == int(event["output_amount_raw"]) > 0
        assert event["volume_usd"] is None
    assert len(events) == len(event_ids) == 19
    assert len({e["transaction_hash"] for e in events}) == 19
    exclusions = bundle["excluded_events"]
    assert exclusions["count"] == len(set(exclusions["event_ids"])) == 8
    assert not event_ids.intersection(exclusions["event_ids"])
    assert sum(exclusions["known_exploit_tx"] in x for x in exclusions["event_ids"]) == 5
    priced = [e for e in events if e["historical_usd_lower_bound"] is not None]
    assert len(priced) == 1
    event = priced[0]
    assert event["zero_for_one"] is True
    quote = bundle["price_evidence"]
    raw_quote = quote["raw_response_utf8"]
    assert hashlib.sha256(raw_quote.encode()).hexdigest() == quote["raw_response_sha256"]
    coins = json.loads(raw_quote, parse_float=Decimal)["coins"]
    p0, p1 = coins["ethereum:"+c0], coins["ethereum:"+c1]
    assert p0["decimals"] == p1["decimals"] == 6
    assert p0["confidence"] == p1["confidence"] == Decimal("0.99")
    timestamp = int(datetime.fromisoformat(event["block_timestamp"].replace("Z", "+00:00")).timestamp())
    assert timestamp == quote["requested_event_timestamp"] == 1756759751
    assert timestamp-p0["timestamp"] == quote["input_quote_lag_seconds"] == 11
    assert timestamp-p1["timestamp"] == quote["output_quote_lag_seconds"] == 12
    lower = min(Decimal(event["input_amount_raw"])/10**p0["decimals"]*p0["price"],
                Decimal(event["output_amount_raw"])/10**p1["decimals"]*p1["price"])
    assert lower == Decimal(event["historical_usd_lower_bound"]) == Decimal("262.4545875") > 100
    assert len(events) >= 10
    assert bundle["qualifying_evidence"]["historical_total_usd"] is None
    pm = bundle["paired_standard_event"]["raw_log"]
    assert pm["address"] == "0x000000000004444c5dc75cb358380d2e3de08a90"
    assert pm["transaction_hash"] == event["transaction_hash"]
    assert pm["log_index"] == event["log_index"]+1 == 167
    assert pm["topics"][0] == standard and pm["topics"][1] == pool["pool_id"]
    words = [int(pm["data"][i:i+64], 16) for i in range(2, len(pm["data"]), 64)]
    assert words[0] == words[1] == words[3] == 0
    print(json.dumps({"offline_checks": "passed", "accepted_positive_events": len(events),
                      "excluded_events": 8, "single_event_usd_lower_bound": str(lower),
                      "historical_total_usd": None, "production_changed": False,
                      "bundle_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}, indent=2))

if __name__ == "__main__":
    main()
