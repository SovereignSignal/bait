"""Chains the indexer covers: HyperSync endpoint, v4 PoolManager, DefiLlama chain key, native coin, reorg lag.

PoolManager addresses: Uniswap v4 deployments, https://developers.uniswap.org/llms.mdx/docs/protocols/v4/deployments
(fetched 2026-10-08). HyperSync URLs: https://docs.envio.dev/docs/HyperSync/hypersync-supported-networks.
DefiLlama chain keys verified against https://coins.llama.fi/prices/current (2026-10-08).
"""
from __future__ import annotations

from dataclasses import dataclass

ETH = "coingecko:ethereum"


@dataclass(frozen=True)
class IndexedChain:
    chain_id: int
    slug: str
    hypersync: str          # https://<name>.hypersync.xyz
    pool_manager: str       # lowercase
    llama: str              # DefiLlama coins chain key
    native: str             # DefiLlama coin id for currency 0x0 (native, 18 decimals)
    lag: int                # blocks kept behind the archive head (reorg safety)
    rpc_net: str | None     # key into keyless.LOGS / keyless.CHAINS for the no-token RPC fallback


CHAINS: list[IndexedChain] = [
    IndexedChain(1, "ethereum", "https://eth.hypersync.xyz", "0x000000000004444c5dc75cb358380d2e3de08a90", "ethereum", ETH, 3, "eth"),
    IndexedChain(10, "optimism", "https://optimism.hypersync.xyz", "0x9a13f98cb987694c9f086b1f5eb990eea8264ec3", "optimism", ETH, 10, "optimism"),
    IndexedChain(56, "bnb", "https://bsc.hypersync.xyz", "0x28e2ea090877bf75740558f6bfb36a5ffee9e9df", "bsc", "coingecko:binancecoin", 15, "bsc"),
    IndexedChain(130, "unichain", "https://unichain.hypersync.xyz", "0x1f98400000000000000000000000000000000004", "unichain", ETH, 20, "unichain"),
    IndexedChain(137, "polygon", "https://polygon.hypersync.xyz", "0x67366782805870060151383f4bbff9dab53e5cd6", "polygon", "coingecko:polygon-ecosystem-token", 30, "polygon_pos"),
    IndexedChain(143, "monad", "https://monad.hypersync.xyz", "0x188d586ddcf52439676ca21a244753fa19f9ea8e", "monad", "coingecko:monad", 10, None),
    IndexedChain(480, "worldchain", "https://worldchain.hypersync.xyz", "0xb1860d529182ac3bc1f51fa2abd56662b7d13f33", "wc", ETH, 10, None),
    IndexedChain(1868, "soneium", "https://soneium.hypersync.xyz", "0x360e68faccca8ca495c1b759fd9eee466db9fb32", "soneium", ETH, 10, None),
    IndexedChain(4326, "megaeth", "https://megaeth.hypersync.xyz", "0xacb7e78fa05d562e0a5d3089ec896d57d057d38e", "megaeth", ETH, 20, None),
    IndexedChain(4663, "robinhood", "https://robinhood.hypersync.xyz", "0x8366a39cc670b4001a1121b8f6a443a643e40951", "robinhood", ETH, 40, "robinhood"),
    IndexedChain(8453, "base", "https://base.hypersync.xyz", "0x498581ff718922c3f8e6a244956af099b2652b2b", "base", ETH, 10, "base"),
    IndexedChain(42161, "arbitrum", "https://arbitrum.hypersync.xyz", "0x360e68faccca8ca495c1b759fd9eee466db9fb32", "arbitrum", ETH, 40, "arbitrum"),
    IndexedChain(42220, "celo", "https://celo.hypersync.xyz", "0x288dc841a52fca2707c6947b3a777c5e56cd87bc", "celo", "coingecko:celo", 10, None),
    IndexedChain(43114, "avalanche", "https://avalanche.hypersync.xyz", "0x06380c0e0912312b5150364b9dc4542ba0dbbc85", "avax", "coingecko:avalanche-2", 5, "avax"),
    IndexedChain(57073, "ink", "https://ink.hypersync.xyz", "0x360e68faccca8ca495c1b759fd9eee466db9fb32", "ink", ETH, 10, "ink"),
    IndexedChain(7777777, "zora", "https://zora.hypersync.xyz", "0x0575338e4c17006ae181b47900a84404247ca30f", "zora", ETH, 10, None),
]
CHAIN_BY_ID = {c.chain_id: c for c in CHAINS}
CHAIN_BY_SLUG = {c.slug: c for c in CHAINS}


def select_chains(spec: str | None) -> list[IndexedChain]:
    """INDEXER_CHAINS: comma list of slugs or chain ids; empty = all."""
    if not spec or not spec.strip():
        return list(CHAINS)
    out = []
    for part in spec.split(","):
        p = part.strip().lower()
        if not p:
            continue
        ch = CHAIN_BY_ID.get(int(p)) if p.isdigit() else CHAIN_BY_SLUG.get(p)
        if ch is None:
            raise ValueError(f"unknown chain {part!r}; known: {','.join(CHAIN_BY_SLUG)}")
        out.append(ch)
    return out
