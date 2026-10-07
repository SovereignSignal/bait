"""Env + chain registry. Secrets come from env only (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DYNAMIC_FEE_FLAG = 0x800000

CATEGORIES = [
    "dynamic-fee", "launchpad", "orders", "lp-management", "compliance",
    "oracle", "mev-lvr", "lending", "gaming-nft", "other",
]

CLAUDE_BLURB_MODEL = "claude-sonnet-5-5"
CLAUDE_CLASSIFY_MODEL = "claude-haiku-4-5"

HOOKLIST_URL = "https://raw.githubusercontent.com/Uniswap/hooklist/main/hooklist.json"
GRAPH_GATEWAY = "https://gateway.thegraph.com/api/subgraphs/id/{subgraph_id}"


@dataclass(frozen=True)
class Chain:
    slug: str
    chain_id: int
    subgraph_id: str | None     # None = no public v4 subgraph (RPC log-scan fallback not built yet)
    nansen: bool                # Nansen covers this chain
    blockscout: str | None      # base URL, None = no instance


# From the research results; see README "Chains".
CHAINS: list[Chain] = [
    Chain("ethereum", 1, "DiYPVdygkfjDWhbxGSqAQxwBKmfKnkWQojqeM2rkLb3G", True, "https://eth.blockscout.com"),
    Chain("optimism", 10, "6RBtsmGUYfeLeZsYyxyKSUiaA6WpuC69shMEQ1Cfuj9u", True, "https://optimism.blockscout.com"),
    Chain("bnb", 56, "EAq1nJKgjnuKH6Gj4RFjCW7LcL7E2uipbncdwV7TTWkX", True, None),
    Chain("unichain", 130, "aa3YpPCxatg4LaBbLFuv2iBC8Jvs9u3hwt5GTpS4Kit", False, "https://unichain.blockscout.com"),
    Chain("polygon", 137, "2CB2uQxcDKWDenagn2z17KQVCtfwSx5eXYuvqTciRTJu", True, "https://polygon.blockscout.com"),
    Chain("robinhood", 4663, None, True, "https://robinhoodchain.blockscout.com"),
    Chain("base", 8453, "Gqm2b5J85n1bhCyDMpGbtbVn4935EvvdyHdHrx3dibyj", True, "https://base.blockscout.com"),
    Chain("arbitrum", 42161, "G5TsTKNi8yhPSV7kycaE23oWbqv9zzNqR49FoEQjzq1r", True, "https://arbitrum.blockscout.com"),
    Chain("avalanche", 43114, "49JxRo9FGxWpSf5Y5GKQPj5NUpX2HhpoZHpGzNEWQZjq", True, None),
    Chain("ink", 57073, None, False, "https://explorer.inkonchain.com"),
]
CHAIN_BY_ID = {c.chain_id: c for c in CHAINS}
CHAIN_BY_SLUG = {c.slug: c for c in CHAINS}


def env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def database_url() -> str:
    url = env("DATABASE_URL") or "sqlite:///./hookfeed.sqlite"
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url
