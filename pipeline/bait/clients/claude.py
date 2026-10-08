"""Claude via the Anthropic SDK (1.x). Structured JSON through output_config.format.

Model gotchas (claude-api skill, 2026-09): Sonnet 5.5 runs adaptive thinking by default and rejects
thinking disabled / forced tool_choice, so depth is set with output_config.effort and JSON comes from
structured outputs; server-side fallbacks ("default") handle stop_reason=refusal. Haiku 4.5 is
pre-4.6: no effort param, structured outputs work, thinking left off.
"""
from __future__ import annotations

import json

import anthropic

from ..config import CATEGORIES, CLAUDE_BLURB_MODEL, CLAUDE_CLASSIFY_MODEL, env

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "project": {"type": "string", "description": "Project name. Reuse an existing name exactly when the hook belongs to it."},
        "category": {"type": "string", "enum": CATEGORIES},
        "reason": {"type": "string"},
    },
    "required": ["project", "category", "reason"],
    "additionalProperties": False,
}

WHAT_SCHEMA = {
    "type": "object",
    "properties": {
        "what_line": {"type": "string", "description": "One plain sentence, max 120 chars, what the hook does for a user"},
        "category": {"type": "string", "enum": CATEGORIES},
    },
    "required": ["what_line", "category"],
    "additionalProperties": False,
}

WHY_SCHEMA = {
    "type": "object",
    "properties": {"why_blurb": {"type": "string", "description": "Exactly two sentences, max 280 chars total"}},
    "required": ["why_blurb"],
    "additionalProperties": False,
}

SYSTEM = (
    "You write for Bait, a feed of Uniswap v4 hooks. Be concrete and plain. No hype, no emojis, "
    "no marketing adjectives. Only state facts present in the input. Never claim an audit or security "
    "property that is not in the input."
)


class Claude:
    def __init__(self, api_key: str | None = None, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic(api_key=api_key, max_retries=3)

    @classmethod
    def from_env(cls):
        key = env("ANTHROPIC_API_KEY")
        return cls(key) if key else None

    # --- transport -------------------------------------------------------------------------
    def _json(self, model: str, user: str, schema: dict, max_tokens: int) -> dict:
        fmt = {"type": "json_schema", "schema": schema}
        if model == CLAUDE_CLASSIFY_MODEL:
            resp = self.client.messages.create(
                model=model, max_tokens=max_tokens, system=SYSTEM,
                messages=[{"role": "user", "content": user}],
                output_config={"format": fmt},
            )
        else:
            resp = self.client.beta.messages.create(
                model=model, max_tokens=max_tokens, system=SYSTEM,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": "low", "format": fmt},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
        if resp.stop_reason == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            raise RuntimeError(f"claude refusal ({cat})")
        text = next(b.text for b in resp.content if b.type == "text")
        return json.loads(text)

    # --- tasks ------------------------------------------------------------------------------
    def classify_hook(self, name: str, description: str, address: str, known_projects: list[str]) -> dict:
        known = "\n".join(f"- {p}" for p in known_projects[:400]) or "(none)"
        user = (
            f"Classify this Uniswap v4 hook into a project and a category.\n"
            f"Hook name: {name or '(unnamed)'}\nAddress: {address}\nDescription: {description or '(none)'}\n\n"
            f"Existing projects (reuse the exact name if this hook is one of theirs; a fork or copy of a "
            f"template is NOT the same project):\n{known}\n\n"
            f"If it matches none, give a short new project name (the hook name is usually right)."
        )
        return self._json(CLAUDE_CLASSIFY_MODEL, user, CLASSIFY_SCHEMA, 400)

    def what_line(self, ctx: dict) -> dict:
        user = "Write the 'what it does' line and pick a category for this hook project.\n\n" + json.dumps(ctx, indent=1, default=str)[:12000]
        return self._json(CLAUDE_BLURB_MODEL, user, WHAT_SCHEMA, 600)

    def why_blurb(self, ctx: dict) -> str:
        user = (
            "Write 'why people care' for this hook project: two sentences grounded in the metrics, "
            "signals and receipts below. Mention a number if one is notable.\n\n"
            + json.dumps(ctx, indent=1, default=str)[:12000]
        )
        return self._json(CLAUDE_BLURB_MODEL, user, WHY_SCHEMA, 600)["why_blurb"]
