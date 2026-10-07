"""github.com/Uniswap/hooklist hooklist.json."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from ..config import HOOKLIST_URL
from . import http


class HookMeta(BaseModel):
    address: str
    chain: str
    chainId: int
    name: str = ""
    description: str = ""
    deployer: str = ""
    verifiedSource: bool = False
    auditUrl: str = ""


class HookEntry(BaseModel):
    hook: HookMeta
    flags: dict[str, bool] = Field(default_factory=dict)
    properties: dict = Field(default_factory=dict)


def load(path: str | None = None, url: str = HOOKLIST_URL, http_client=None) -> list[HookEntry]:
    if path:
        raw = json.loads(Path(path).read_text())
    else:
        c = http_client or http.client()
        raw = http.get_json(c, url)
    return [HookEntry.model_validate(e) for e in raw]
