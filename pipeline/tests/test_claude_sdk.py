"""The same calls through the real anthropic SDK against a loopback server (fake key, nothing leaves the machine). The
fake-client tests in test_claude_client.py cannot see an SDK release that rejects or reshapes bait's request; these can,
so a Dependabot SDK bump that breaks the client fails CI instead of the nightly run. A socket rather than a mock
transport keeps the test independent of whichever HTTP library the SDK uses."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import anthropic
import pytest

from bait.clients.claude import Claude
from bait.config import CLAUDE_BLURB_MODEL, CLAUDE_CLASSIFY_MODEL


@pytest.fixture
def sdk_client(monkeypatch):
    seen, reply = [], {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append({"path": urlsplit(self.path).path, "beta": self.headers.get("anthropic-beta"), "body": body})
            content = [{"type": "text", "text": json.dumps(reply["payload"])}]
            usage = {"input_tokens": 10, "output_tokens": 5}
            out = json.dumps({"id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
                              "content": content, "stop_reason": "end_turn", "stop_sequence": None, "usage": usage}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")

    def make(payload):
        reply["payload"] = payload
        api = anthropic.Anthropic(api_key="sk-test", base_url=f"http://127.0.0.1:{server.server_port}", max_retries=0)
        return Claude(client=api), seen

    yield make
    server.shutdown()
    server.server_close()


def test_classify_through_the_sdk(sdk_client):
    claude, seen = sdk_client({"project": "Bunni", "category": "lp-management", "reason": "r"})
    assert claude.classify_hook("BunniHook", "desc", "0xabc", ["Bunni"])["project"] == "Bunni"
    (req,) = seen
    assert req["path"] == "/v1/messages" and req["beta"] is None
    assert req["body"]["model"] == CLAUDE_CLASSIFY_MODEL
    assert req["body"]["output_config"]["format"]["type"] == "json_schema" and "fallbacks" not in req["body"]


def test_blurb_through_the_beta_sdk_keeps_fallbacks(sdk_client):
    claude, seen = sdk_client({"why_blurb": "One. Two."})
    assert claude.why_blurb({"name": "x"}) == "One. Two."
    (req,) = seen
    assert req["path"] == "/v1/messages" and "server-side-fallback-2026-07-01" in req["beta"]
    assert req["body"]["model"] == CLAUDE_BLURB_MODEL
    assert req["body"]["fallbacks"] == "default" and req["body"]["output_config"]["effort"] == "low"
