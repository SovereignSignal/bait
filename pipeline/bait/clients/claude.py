"""Claude via the Anthropic SDK (1.x). Structured JSON through output_config.format.

Model gotchas (claude-api skill, 2026-09): Sonnet 5.5 runs adaptive thinking by default and rejects
thinking disabled / forced tool_choice, so depth is set with output_config.effort and JSON comes from
structured outputs; server-side fallbacks ("default") handle stop_reason=refusal. Haiku 4.5 is
pre-4.6: no effort param, structured outputs work, thinking left off.
"""
from __future__ import annotations

import json
import logging

import anthropic

from ..config import CATEGORIES, CLAUDE_BLURB_MODEL, CLAUDE_CLASSIFY_MODEL, env
from ..prose_checks import clean_blurb, clean_what_line

log = logging.getLogger("bait")

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

RADAR_KINDS = ["hackathon", "incubator", "audit", "launch_post"]

RADAR_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": RADAR_KINDS,
                 "description": "hackathon = hackathon project or showcase; incubator = Uniswap Hook Incubator (UHI) or similar cohort/demo; "
                                "audit = security review or audit report; launch_post = launch or release announcement"},
        "summary": {"type": "string", "description": "One plain sentence, max 160 chars: what was built, audited or launched"},
        "score": {"type": "number", "description": "0-1: how clearly this is about a specific Uniswap v4 hook being built, audited or launched. "
                                                   "Generic v4 explainers, price news, listicles and unrelated pages score below 0.5"},
    },
    "required": ["kind", "summary", "score"],
    "additionalProperties": False,
}

SYSTEM = (
    "You write for Bait, a feed of Uniswap v4 hooks. Be concrete and plain. No hype, no emojis, "
    "no marketing adjectives. Only state facts present in the input. Never claim an audit or security "
    "property that is not in the input."
)


class Claude:
    def __init__(self, api_key: str | None = None, client: anthropic.Anthropic | None = None):
        from ..costs import NullMeter
        self.meter = NullMeter()
        self.client = client or anthropic.Anthropic(api_key=api_key, max_retries=3)

    @classmethod
    def from_env(cls):
        key = env("ANTHROPIC_API_KEY")
        return cls(key) if key else None

    # --- transport -------------------------------------------------------------------------
    def _json(self, model: str, user: str, schema: dict, max_tokens: int) -> dict:
        from ..costs import anthropic_estimate, anthropic_usd
        self.meter.check("anthropic", anthropic_estimate(model, len(user) + len(SYSTEM), max_tokens))
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
        u = getattr(resp, "usage", None)
        if u is not None:
            served = getattr(resp, "model", None) or model
            self.meter.record("anthropic", anthropic_usd(served, getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0,
                                                         getattr(u, "cache_creation_input_tokens", 0) or 0,
                                                         getattr(u, "cache_read_input_tokens", 0) or 0),
                              units=(getattr(u, "input_tokens", 0) or 0) + (getattr(u, "output_tokens", 0) or 0), detail=served)
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

    def classify_radar(self, item: dict) -> dict:
        """Dev radar (steps/radar.py): kind + one-sentence summary + relevance score for one web result."""
        user = (
            "Classify this web result for a radar of Uniswap v4 hooks being built, audited or launched.\n\n"
            + json.dumps(item, indent=1, default=str)[:4000]
        )
        out = self._json(CLAUDE_CLASSIFY_MODEL, user, RADAR_SCHEMA, 300)
        try:
            out["score"] = max(0.0, min(1.0, float(out.get("score", 0))))
        except (TypeError, ValueError):
            out["score"] = 0.0
        return out

    # Both prose calls pass the model's text through prose_checks before anyone stores it: a clause or sentence the
    # facts in ctx contradict is dropped (logged), and an empty result is returned as "" rather than the bad text.
    def what_line(self, ctx: dict) -> dict:
        res = self._json(CLAUDE_BLURB_MODEL, what_line_prompt(ctx), WHAT_SCHEMA, PROSE_MAX_TOKENS)
        line, found = clean_what_line(res.get("what_line") or "", ctx)
        if found:
            log.warning("what_line project=%s dropped: %s", ctx.get("name"), " | ".join(map(str, found)))
        return {**res, "what_line": line}

    def why_blurb(self, ctx: dict) -> str:
        text = self._json(CLAUDE_BLURB_MODEL, why_blurb_prompt(ctx), WHY_SCHEMA, PROSE_MAX_TOKENS)["why_blurb"]
        blurb, found = clean_blurb(text or "", ctx)
        if found:
            log.warning("why_blurb project=%s dropped: %s", ctx.get("name"), " | ".join(map(str, found)))
        return blurb


PROSE_MAX_TOKENS = 600     # what_line and why_blurb


# Rules from the 2026-10-24 fact-check of the 30 busiest described projects (DESIGN.md "Prose accuracy"): each one was
# broken on the live site. prose_checks.py enforces the ones a pattern can see; the rest rely on these words.
WHAT_RULES = (
    "Rules:\n"
    "- Use only what the input states: the hooklist descriptions, the verified source snippet, the flags and the facts "
    "under enrichment. Never add a mechanism, fee, share, duration or recipient that none of them states.\n"
    "- If the source snippet or hooklist descriptions disagree with researched facts (website, GitHub, team), follow the "
    "source and descriptions: research can describe a different project with a similar name.\n"
    "- When the hooks differ (a family, versions, chains), describe what they share; never give one hook's fee or "
    "parameter as the project's.\n"
    "- Name a chain only if it is in chains.\n"
    "- Do not say whether the source is verified: the input does not record it. Call the hook unlisted only when "
    "hooklist_descriptions is empty, and its purpose unknown only when there is no description and no source.\n"
    "- Mention an audit only when audit_urls is not empty.\n"
    "- Write for a reader of the page: never mention the input, a field name or these rules.\n\n"
)

WHY_RULES = (
    "Rules:\n"
    # No figures: a blurb is rewritten only when signals change, while the page shows current numbers beside it, so a
    # quoted figure goes stale (an Oct 8 blurb said $119.7M next to a $135.5M tile).
    "- Do not quote dollar amounts, counts or percentages: the page shows the current figures beside this text. Say what "
    "they mean instead: growing or fading, concentrated or broad, who uses it.\n"
    "- A null metric is unknown, never zero: do not call unknown TVL or volume zero, and say nothing about the weekly "
    "trend when vol_7d or vol_7d_prev is null.\n"
    "- Say growing only when vol_7d is above vol_7d_prev and fading only when it is below; say sharply, clearly or well "
    "above only for a change of more than 10%.\n"
    "- Size words must fit the metric: a single pool means pools_n is 1; dozens of wallets is 24 to 99, hundreds is 200 "
    "to 999, thousands is 2,000 or more.\n"
    "- Mention press or coverage only from the receipts below, and an audit only when an audit link is given.\n"
    "- Name a chain only if the input lists it. Do not say whether the source is verified.\n"
    "- Write for a reader of the page: never mention the input, a field name (alive, signals, receipts, metrics) or "
    "these rules.\n\n"
)


def what_line_prompt(ctx: dict) -> str:
    return ("Write the 'what it does' line and pick a category for this hook project.\n" + WHAT_RULES
            + json.dumps(ctx, indent=1, default=str)[:12000])


def why_blurb_prompt(ctx: dict) -> str:
    return ("Write 'why people care' for this hook project: two sentences grounded in the metrics, "
            "signals and receipts below.\n" + WHY_RULES
            + json.dumps(ctx, indent=1, default=str)[:12000])


def prose_estimate(user: str) -> float:
    """The worst case Claude._json checks against the budget before a what_line / why_blurb call with this prompt."""
    from ..costs import anthropic_estimate
    return anthropic_estimate(CLAUDE_BLURB_MODEL, len(user) + len(SYSTEM), PROSE_MAX_TOKENS)
