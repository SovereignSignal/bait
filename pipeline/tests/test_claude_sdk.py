"""The same calls through the real anthropic SDK over a mock transport (no network, fake key). The fake-client tests in
test_claude_client.py cannot see an SDK release that rejects or reshapes bait's request; these can, so a Dependabot SDK
bump that breaks the client fails CI instead of the nightly run."""
import json

import anthropic
import httpx2

from bait.clients.claude import Claude
from bait.config import CLAUDE_BLURB_MODEL, CLAUDE_CLASSIFY_MODEL


def sdk_client(payload):
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append({"path": req.url.path, "beta": req.headers.get("anthropic-beta"), "body": body})
        return httpx2.Response(200, json={"id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
                                          "content": [{"type": "text", "text": json.dumps(payload)}], "stop_reason": "end_turn",
                                          "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}})

    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    return Claude(client=anthropic.Anthropic(api_key="sk-test", http_client=http, max_retries=0)), seen


def test_classify_through_the_sdk():
    claude, seen = sdk_client({"project": "Bunni", "category": "lp-management", "reason": "r"})
    assert claude.classify_hook("BunniHook", "desc", "0xabc", ["Bunni"])["project"] == "Bunni"
    (req,) = seen
    assert req["path"] == "/v1/messages" and req["beta"] is None
    assert req["body"]["model"] == CLAUDE_CLASSIFY_MODEL
    assert req["body"]["output_config"]["format"]["type"] == "json_schema" and "fallbacks" not in req["body"]


def test_blurb_through_the_beta_sdk_keeps_fallbacks():
    claude, seen = sdk_client({"why_blurb": "One. Two."})
    assert claude.why_blurb({"name": "x"}) == "One. Two."
    (req,) = seen
    assert req["path"] == "/v1/messages" and "server-side-fallback-2026-07-01" in req["beta"]
    assert req["body"]["model"] == CLAUDE_BLURB_MODEL
    assert req["body"]["fallbacks"] == "default" and req["body"]["output_config"]["effort"] == "low"
