"""Transport-level checks on the Claude client with a fake Anthropic SDK object: right model, right params."""
import json
from types import SimpleNamespace

import pytest

from hookfeed.clients.claude import Claude
from hookfeed.config import CLAUDE_BLURB_MODEL, CLAUDE_CLASSIFY_MODEL


class FakeMessages:
    def __init__(self, payload, stop="end_turn"):
        self.payload, self.stop, self.kwargs = payload, stop, None

    def create(self, **kw):
        self.kwargs = kw
        return SimpleNamespace(stop_reason=self.stop, stop_details=SimpleNamespace(category="cyber") if self.stop == "refusal" else None,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def fake_client(payload, stop="end_turn"):
    m = FakeMessages(payload, stop)
    return SimpleNamespace(messages=m, beta=SimpleNamespace(messages=m)), m


def test_classify_uses_haiku_without_effort():
    client, m = fake_client({"project": "Bunni", "category": "lp-management", "reason": "r"})
    res = Claude(client=client).classify_hook("BunniHook", "desc", "0xabc", ["Bunni", "Saturn"])
    assert res["project"] == "Bunni"
    assert m.kwargs["model"] == CLAUDE_CLASSIFY_MODEL == "claude-haiku-4-5"
    assert m.kwargs["output_config"]["format"]["type"] == "json_schema"
    assert "effort" not in m.kwargs["output_config"] and "fallbacks" not in m.kwargs and "thinking" not in m.kwargs
    assert "Saturn" in m.kwargs["messages"][0]["content"]


def test_blurb_uses_sonnet_low_effort_with_default_fallbacks():
    client, m = fake_client({"why_blurb": "One. Two."})
    assert Claude(client=client).why_blurb({"name": "x"}) == "One. Two."
    assert m.kwargs["model"] == CLAUDE_BLURB_MODEL == "claude-sonnet-5-5"
    assert m.kwargs["output_config"]["effort"] == "low"
    assert m.kwargs["fallbacks"] == "default" and m.kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in m.kwargs and "tool_choice" not in m.kwargs


def test_what_line_schema_and_refusal():
    client, m = fake_client({"what_line": "Does a thing.", "category": "oracle"})
    assert Claude(client=client).what_line({"name": "x"})["category"] == "oracle"
    assert m.kwargs["output_config"]["format"]["schema"]["properties"]["category"]["enum"][0] == "dynamic-fee"
    client, _ = fake_client({}, stop="refusal")
    with pytest.raises(RuntimeError, match="refusal"):
        Claude(client=client).what_line({"name": "x"})
