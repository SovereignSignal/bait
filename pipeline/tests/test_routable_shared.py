"""Shared routable fixture: fixtures/routable_cases.json at the repo root is also asserted by
web/src/lib/routable.test.ts, so Python and TypeScript cannot drift apart silently."""
import json
from pathlib import Path

import pytest

from bait.rules import eligibility, routable

CASES_FILE = Path(__file__).resolve().parents[2] / "fixtures" / "routable_cases.json"
CASES = json.loads(CASES_FILE.read_text())["cases"]


def test_fixture_has_ten_cases_with_all_three_outcomes():
    assert len(CASES) == 10
    assert {c["eligibility"] for c in CASES} == {"eligible", "ineligible", "unknown"}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_eligibility_matches_shared_fixture(case):
    hook = {"hook": {"address": case["address"]}, "flags": case["flags"], "properties": case["properties"]}
    assert eligibility(hook, case.get("poolFee")) == case["eligibility"]
    assert routable(hook) is (case["eligibility"] == "eligible" and case.get("poolFee") is None)
