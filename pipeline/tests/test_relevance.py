import json
from pathlib import Path

from bait.rules import receipt_relevant

CASES = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "relevance_cases.json").read_text())


def test_receipt_relevance_matches_shared_cases():
    for c in CASES:
        assert receipt_relevant(c["project"], c["title"], c["source"]) is c["keep"], c
