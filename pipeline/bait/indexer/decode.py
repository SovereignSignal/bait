"""Uniswap v4 PoolManager event decoding (IPoolManager, v4-core src/interfaces/IPoolManager.sol).

Topic hashes are keccak256 of the canonical signatures, checked against 4byte.directory event signatures
(https://www.4byte.directory/api/v1/event-signatures/?hex_signature=<hash>, 2026-10-08):
  Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)
  Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)
tests/test_indexer.py recomputes both with a pure-Python keccak.
"""
from __future__ import annotations

from datetime import datetime, timezone

INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
ZERO = "0x" + "0" * 40


def num(v) -> int | None:
    """HyperSync JSON quantities may be ints or hex strings; RPC uses hex strings."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.startswith(("0x", "0X")) else int(s)


def _words(data: str) -> list[str]:
    d = (data or "0x")[2:]
    return [d[i:i + 64] for i in range(0, len(d), 64)]


def _signed(word: str, bits: int = 256) -> int:
    v = int(word, 16)
    return v - (1 << bits) if v >= 1 << (bits - 1) else v


def _addr(topic_or_word: str) -> str:
    return "0x" + topic_or_word[-40:].lower()


def topics_of(log: dict) -> list[str | None]:
    """HyperSync logs carry topic0..topic3; RPC logs carry a topics list."""
    if "topics" in log and isinstance(log["topics"], list):
        t = [x.lower() if x else None for x in log["topics"]]
        return (t + [None] * 4)[:4]
    return [(log.get(f"topic{i}") or "").lower() or None for i in range(4)]


def decode_initialize(log: dict) -> dict | None:
    """-> pool fields (all hex lowercase) or None when the log is not an Initialize."""
    t = topics_of(log)
    if t[0] != INITIALIZE_TOPIC or not all(t[1:4]):
        return None
    w = _words(log.get("data") or "0x")
    if len(w) < 5:
        return None
    return {
        "pool_id": t[1],
        "currency0": _addr(t[2]),
        "currency1": _addr(t[3]),
        "fee": int(w[0], 16),
        "tick_spacing": _signed(w[1]),
        "hook_address": _addr(w[2]),
        "sqrt_price_x96": str(int(w[3], 16)),
        "tick": _signed(w[4]),
    }


def decode_swap(log: dict) -> dict | None:
    t = topics_of(log)
    if t[0] != SWAP_TOPIC or not t[1]:
        return None
    w = _words(log.get("data") or "0x")
    if len(w) < 6:
        return None
    return {
        "pool_id": t[1],
        "sender": _addr(t[2]) if t[2] else None,
        "amount0": _signed(w[0]),
        "amount1": _signed(w[1]),
        "sqrt_price_x96": int(w[2], 16),
        "liquidity": int(w[3], 16),
        "tick": _signed(w[4]),
        "fee": int(w[5], 16),
    }


def is_hooked(pool: dict) -> bool:
    return int(pool["hook_address"], 16) != 0


def utc(ts: int | None) -> datetime | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None else None
