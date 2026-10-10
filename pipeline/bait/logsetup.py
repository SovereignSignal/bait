"""Logging for the Railway services: JSON lines on stdout with a level field (Railway maps severity from it).
BAIT_LOG_FORMAT=text gives the old human format for local runs."""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
             "level": record.levelname.lower(), "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False)


def configure(name: str = "bait") -> None:
    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get("BAIT_LOG_FORMAT", "json") == "text":
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    else:
        handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
