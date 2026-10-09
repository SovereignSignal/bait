"""github.com/Uniswap/hooklist hooklist.json."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from ..config import HOOKLIST_URL
from . import http

log = logging.getLogger("bait")


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


def load_entries(path: str | None = None, url: str = HOOKLIST_URL, http_client=None) -> tuple[list[HookEntry], int]:
    """Valid entries plus the number skipped. One malformed entry must not take the whole registry down."""
    if path:
        raw = json.loads(Path(path).read_text())
    else:
        c = http_client or http.client()
        raw = http.get_json(c, url)
    entries, skipped = [], 0
    for i, e in enumerate(raw):
        try:
            entries.append(HookEntry.model_validate(e))
        except ValidationError as err:
            skipped += 1
            log.warning("hooklist entry %d skipped: %s", i, str(err).splitlines()[0])
    return entries, skipped


def load(path: str | None = None, url: str = HOOKLIST_URL, http_client=None) -> list[HookEntry]:
    return load_entries(path, url, http_client)[0]
