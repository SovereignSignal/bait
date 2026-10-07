"""Shared routable fixture: fixtures/routable_cases.json at the repo root is also asserted by
web/src/lib/routable.test.ts, so Python and TypeScript cannot drift apart silently."""
import json
from pathlib import Path

import pytest

from hookfeed.rules import routable

CASES_FILE = Path(__file__).resolve().parents[2] / "fixtures" / "routable_cases.json"
CASES = json.loads(CASES_FILE.read_text())["cases"]


def test_fixture_has_six_cases_with_both_outcomes():
    assert len(CASES) == 6
    assert {c["routable"] for c in CASES} == {True, False}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_routable_matches_shared_fixture(case):
    hook = {"hook": {"address": case["address"]}, "flags": case["flags"], "properties": case["properties"]}
    assert routable(hook) is case["routable"]
