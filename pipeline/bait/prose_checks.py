"""Deterministic checks on generated prose (what_line, why_blurb). Pure functions, no I/O.

The 2026-10-24 fact-check of the 30 busiest described projects found text that the facts Bait passed in did not
support: a hooklisted hook called "unlisted, purpose unknown", a verified one called "unverified", "growing" over a
7-day volume that had halved, dollar figures and counts that matched no metric, audits named with no audit link,
press cited with no receipts, and internal words ("the input", "the alive signal") on the page. The prompts in
clients/claude.py ask for none of that; these checks make sure of it twice: Claude.what_line and Claude.why_blurb clean
new text before it is stored, and enrich and signals write stored text again when it fails against today's facts.

A failing clause (what_line, split on ";") or sentence (why_blurb) is dropped and the rest is kept. An empty result is
stored as empty, and an empty line or blurb is written again after ENRICH_RETRY_DAYS. Both contexts carry the facts the
checks read (enrich.hook_facts, plus the blurb's metrics and receipts). Where a fact is missing anyway, a check either
skips or drops, by which way the error would go:

- Skips, because the text may be true: chain names without `chains`; "unlisted" unless `hooklisted`, or a hooklist
  description when that is null, says the hook is listed; "purpose unknown" unless a description, a source snippet or
  `verified_source` true documents it; press or coverage without a `receipts` key; TVL words without `tvl_usd` in the
  metrics; trend words without metrics; a number word ("three pools") when that count is unknown.
- Drops, because the claim needs the fact to stand: "unverified" unless `verified_source` is false; an audit unless
  `audit_urls` holds a web link; press or coverage when `receipts` is empty; a dollar amount, percentage or digit count
  that matches no metric passed in and is not quoted in the context (an unknown metric matches nothing); a size phrase
  ("hundreds of wallets") over an unknown count; a weekly trend when either week's volume is unknown; zero TVL when TVL
  is unknown.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Problem:
    kind: str      # chain | audit | coverage | figure | trend | status | leak
    text: str      # the clause or sentence that failed (or the phrase removed)
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


# --- chains ------------------------------------------------------------------------------------------------------
# Bait slugs (config.CHAINS) plus chains Bait does not index, which no project is on. Ambiguous names (Base, BNB, Ink,
# Robinhood) count only in chain phrasing: "on Base", "Base chain", a list ", Base", never "base fee" or "Robinhood
# stock tokens".
_ON = r"(?:on|to|across|from|via|and|or)\s+"
CHAIN_PATTERNS: dict[str, re.Pattern] = {slug: re.compile(rx) for slug, rx in {
    "ethereum": r"\bEthereum\b(?!\s+Foundation)",
    "base": rf"\b{_ON}Base\b|\bBase\s+(?:[Cc]hain|mainnet|network|L2)\b|(?<=, )Base\b",
    "optimism": r"\bOptimism\b|\bOP Mainnet\b",
    "bnb": r"\bon BNB\b(?!\s*/)|\bBNB\s+(?:Smart\s+)?Chain\b|\bBSC\b",
    "unichain": r"\b[Uu]nichain\b",
    "polygon": r"\bPolygon\b",
    "robinhood": rf"\bRobinhood\s+(?:[Cc]hain|L2|mainnet|network)\b|\b{_ON}Robinhood\b(?!\s+(?:[Ss]tock|app|Markets|tokens?))"
                 r"|(?<=, )Robinhood\b(?!\s+(?:[Ss]tock|app|Markets|tokens?))",
    "arbitrum": r"\bArbitrum\b(?!\s+Orbit)",
    "avalanche": r"\bAvalanche\b",
    "ink": rf"\b{_ON}Ink\b|\bInk\s+(?:[Cc]hain|mainnet)\b|(?<=, )Ink\b",
    "solana": r"\bSolana\b",
    "zksync": r"\b[Zz][Kk][Ss]ync\b",
    "linea": r"\bLinea\b",
    "scroll": r"\bon Scroll\b",
    "blast": r"\bon Blast\b",
    "mantle": r"\bon Mantle\b",
    "zora": r"\bon Zora\b|\bZora\s+(?:[Cc]hain|network)\b",
    "celo": r"\bCelo\b",
    "gnosis": r"\bGnosis\s+[Cc]hain\b|\bon Gnosis\b",
    "berachain": r"\bBerachain\b",
    "worldchain": r"\bWorld\s?[Cc]hain\b",
    "monad": r"\bMonad\b",
    "sonic": r"\bon Sonic\b",
    "hyperevm": r"\bHyperEVM\b",
    "soneium": r"\bSoneium\b",
    "testnet": r"\bSepolia\b|\bHolesky\b|\btestnet\b",
}.items()}


def chains_named(text: str) -> set[str]:
    return {slug for slug, rx in CHAIN_PATTERNS.items() if rx.search(text or "")}


def _chain_problems(part: str, ctx: dict) -> list[Problem]:
    chains = ctx.get("chains")
    if chains is None:
        return []
    allowed = {str(c.get("slug") if isinstance(c, dict) else c).lower() for c in chains}
    bad = sorted(chains_named(part) - allowed)
    if not bad:
        return []
    return [Problem("chain", part, f"names {', '.join(bad)}; the project is on {', '.join(sorted(allowed)) or 'no known chain'}")]


# --- audits and coverage -----------------------------------------------------------------------------------------
# Same reading as the web's auditMention (web/src/lib/present.ts): a mention counts unless one of the four words before
# it negates it, so "no audit is listed" is not a claim of an audit.
_AUDIT = re.compile(r"\baudit(?:ed|s|or|ors|ing)?\b|\bsecurity[- ]review(?:ed|s)?\b", re.I)
_COVERAGE = re.compile(r"\b(?:press|coverage|covered|outlets?|articles?|news|receipts?|write-?ups?|mentions?|mentioned)\b",
                       re.I)
_NEGATION = re.compile(r"\b(?:no|not|never|without|lacks?|lacking|missing|none|nor|nothing|neither)\b", re.I)
# "any audit are undocumented", "audit could not be identified": negated after the word
_NEGATED_AFTER = re.compile(r"^\W*(?:\w+\W+){0,4}?(?:are|is|was|were|remains?|could|can)\s+(?:still\s+)?"
                            r"(?:undocumented|unknown|unclear|missing|absent"
                            r"|not\s+(?:be\s+)?(?:listed|found|linked|known|documented|identified|available))\b", re.I)
_CONTRAST = re.compile(r".*(?:[;:]|\b(?:but|though|although|while|yet|whereas)\b)", re.I | re.S)


def _positive_mention(rx: re.Pattern, part: str) -> bool:
    """A mention not negated within the eight words before it, counted from the last "but"/"though" (so "no team,
    audit, website or press coverage" negates all four, and "no commits, though the team links audits" does not)."""
    for m in rx.finditer(part):
        before = _CONTRAST.sub("", part[: m.start()]).split()[-8:]
        if not _NEGATION.search(" ".join(before)) and not _NEGATED_AFTER.search(part[m.end():]):
            return True
    return False


def audit_links(ctx: dict) -> list[str]:
    """The audit links the context carries: the web URLs among the hooks' hooklist audit_urls, the links the web's
    audit badge shows (auditLinks / firstWebUrl in web/src/lib/present.ts)."""
    return [u for u in (ctx.get("audit_urls") or []) if isinstance(u, str) and re.match(r"https?://", u, re.I)]


def _audit_problems(part: str, ctx: dict) -> list[Problem]:
    if _positive_mention(_AUDIT, part) and not audit_links(ctx):
        return [Problem("audit", part, "mentions an audit, but no audit link was passed in")]
    return []


def _coverage_problems(part: str, ctx: dict) -> list[Problem]:
    """Press needs a receipt among the stored ones the blurb context lists (signals no longer searches for new ones)."""
    if "receipts" not in ctx:
        return []
    if _positive_mention(_COVERAGE, part) and not ctx["receipts"]:
        return [Problem("coverage", part, "cites press or coverage, but no receipts were passed in")]
    return []


# --- verification, registry, purpose -----------------------------------------------------------------------------
_UNVERIFIED = re.compile(r"\b(?:hook|contract|source|code)\s+(?:is|are|was|remains)\s+(?:still\s+|reported\s+as\s+|listed\s+as\s+)?"
                         r"(?:unverified|not verified)\b"
                         r"|\bunverified\s+(?:(?:Uniswap\s+)?v4\s+)?(?:hook|contract|source|code)s?\b|\bno verified source\b"
                         r"|\bsource (?:code )?(?:is )?not (?:verified|published)\b", re.I)
_UNLISTED = re.compile(r"\bunlisted\b|\bnot (?:in|on) the hook ?list\b|\bnot hooklisted\b", re.I)
_UNKNOWN_PURPOSE = re.compile(
    r"\bpurpose (?:is )?(?:unknown|unclear|not documented|undocumented|not (?:stated|known))\b"
    r"|\b(?:no|without) (?:a )?(?:documented|known|stated|clear) purpose\b"
    r"|\bwhat (?:the hook|it) does\b[^.;]{0,40}\bunclear\b|\blogic is (?:unknown|unclear|undocumented)\b", re.I)
# "Unverified hook ...", "Unlisted v4 hook ...": the adjective goes, the description stays.
_STATUS_ADJ = {
    "unverified": re.compile(r"\bunverified\s+(?=(?:(?:Uniswap\s+)?v4\s+|singleton\s+)?(?:hook|contract)s?\b)", re.I),
    "unlisted": re.compile(r"\bunlisted\s+(?=(?:(?:Uniswap\s+)?v4\s+|singleton\s+)?(?:hook|contract)s?\b)", re.I),
}


def _source_snippet(ctx: dict) -> str:
    src = (ctx.get("enrichment") or {}).get("source") if isinstance(ctx.get("enrichment"), dict) else None
    return str((src or {}).get("snippet") or "") if isinstance(src, dict) else ""


def _verified(ctx: dict) -> bool | None:
    """True / False when the context says (`verified_source`, true when any hook is: enrich.hook_facts), else True
    when a source snippet is there, else None: a chain whose explorer refuses bots leaves no source in the context, yet
    the hook may well be verified (2026-10-24, a Robinhood LaunchHook on Sourcify read "Unverified hook")."""
    if ctx.get("verified_source") is not None:
        return bool(ctx["verified_source"])
    return True if _source_snippet(ctx) else None


def _listed(ctx: dict) -> bool | None:
    """`hooklisted` when known (true when any hook is in the hooklist), else True when a hooklist description is there,
    else None. A hook dropped from the registry keeps its description but reads hooklisted false: "unlisted" is true."""
    if ctx.get("hooklisted") is not None:
        return bool(ctx["hooklisted"])
    return True if ctx.get("hooklist_descriptions") else None


def _documented(ctx: dict) -> bool:
    """A hooklist description, a source snippet, or a verified source (`verified_source` true: the blurb context does
    not carry the snippet, and SessionFeeHook's blurb called its fee logic unknown beside a verified SessionFeeHook.sol)."""
    return bool(ctx.get("hooklist_descriptions")) or bool(_source_snippet(ctx)) or ctx.get("verified_source") is True


def _status_problems(part: str, ctx: dict) -> list[Problem]:
    out = []
    if _UNVERIFIED.search(part) and _verified(ctx) is not False:
        out.append(Problem("status", part, "calls the hook unverified; nothing passed in says so"))
    if _UNLISTED.search(part) and _listed(ctx):
        out.append(Problem("status", part, "calls the hook unlisted; it has a hooklist entry"))
    if _UNKNOWN_PURPOSE.search(part) and _documented(ctx):
        out.append(Problem("status", part, "calls the purpose unknown; a hooklist description or verified source documents it"))
    return out


# --- internal words ----------------------------------------------------------------------------------------------
# Phrases removed in place ("no audit is listed in the input" -> "no audit is listed"), then words that sink the part.
_LEAK_PHRASE = re.compile(r",?\s+(?:in|from|per)\s+the\s+(?:input|provided data|data provided|context|metadata)\b", re.I)
_LEAK = re.compile(
    r"\bthe input\b|\balive signal\b|\b(?:flagged|marked|listed)\s+(?:as\s+)?not alive\b"
    r"|\bsignals? (?:reads?|is|are|shows?) (?:false|true|null)\b"
    r"|(?-i:\bnull\b)|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", re.I)


def _leak_problems(part: str, ctx: dict) -> list[Problem]:
    m = _LEAK.search(part)
    return [Problem("leak", part, f"internal wording: {m.group(0)!r}")] if m else []


# --- figures -----------------------------------------------------------------------------------------------------
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?(thousand|million|billion|bn|[KkMmBb])?\b")
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s?(?:%|percent\b)")
_LOWER = re.compile(r"\b(?:over|more than|above|at least|upwards of)\s*$", re.I)
_UPPER = re.compile(r"\b(?:under|less than|below|at most|up to|nearly|almost)\s*$", re.I)
_NOUNS = {"swap": "swaps", "swaps": "swaps", "trade": "swaps", "trades": "swaps", "transactions": "swaps",
          "wallet": "wallets", "wallets": "wallets", "trader": "wallets", "traders": "wallets", "addresses": "wallets",
          "users": "wallets", "pool": "pools", "pools": "pools", "hook": "hooks", "hooks": "hooks"}
_NOUN = r"(swaps?|trades?|transactions|wallets?|traders?|addresses|users|pools?|hooks?)"
_FILLER = r"(?:(?:distinct|unique|daily|active|different|separate|launch|hooked|live|v4|new|[A-Z][A-Z0-9]{1,11})\s+){0,2}"
_COUNT = re.compile(rf"(?<![$\d.,])\b(\d{{1,3}}(?:,\d{{3}})+|\d+)\s+{_FILLER}{_NOUN}\b")   # \b: "v4 hooks" is no count
_WORDS = {"a single": 1, "single": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
# Number words are checked for pools and hooks only: "a single wallet" is a concentration claim, not a wallet count.
_WORD_COUNT = re.compile(rf"\b((?i:a single|single|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve))\s+"
                         rf"{_FILLER}(pools?|hooks?)\b")
_MAGNITUDES = [  # phrase, lowest and highest count it fits
    (r"(?:a few|several) dozen|dozens of", 24, 99),
    (r"(?:about|around|roughly|nearly) a hundred", 70, 149),
    (r"(?:a few|several) hundred|hundreds of", 200, 999),
    (r"(?:about|around|roughly|nearly) a thousand", 700, 1499),
    (r"(?:a few|several) thousand|thousands of", 2000, 99_999),
    (r"tens of thousands of", 20_000, 999_999),
]
_MAGNITUDE = re.compile(rf"\b((?i:{'|'.join(p for p, _, _ in _MAGNITUDES)}))\s+{_FILLER}{_NOUN}\b")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _metrics(ctx: dict) -> dict:
    m = ctx.get("metrics")
    return m if isinstance(m, dict) else {}


def _known(*vals) -> list[float]:
    return [float(v) for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]


def _count_values(kind: str, ctx: dict) -> tuple[list[float], bool]:
    """Known values a count of `kind` may match, and whether they are upper bounds (wallets summed per pool)."""
    m = _metrics(ctx)
    if kind == "swaps":
        return _known(m.get("swaps_24h"), m.get("swaps_7d")), False
    if kind == "wallets":
        upper = str(m.get("wallets_24h_kind") or "").startswith("upper")
        return _known(m.get("wallets_24h"), m.get("wallets_7d")), upper
    if kind == "pools":
        return _known(m.get("pools_n"), m.get("pools"), ctx.get("pools_n")), False
    return _known(m.get("hooks_n"), ctx.get("hooks_n")), False


def _close(stated: float, actual: float, unit: float = 1.0) -> bool:
    return abs(stated - actual) <= max(unit / 2, 0.01 * abs(actual))


def _bounded(stated: float, actual: float, unit: float, before: str) -> bool:
    """"over $10M" fits 10.8M, not 156M: a bound is read as within a factor of two."""
    if _LOWER.search(before):
        return stated - unit / 2 <= actual <= 2 * stated
    if _UPPER.search(before):
        return stated / 2 <= actual <= stated + unit / 2
    return _close(stated, actual, unit)


def _ctx_text(ctx: dict) -> str:
    return json.dumps(ctx, default=str)


def _money_problems(part: str, ctx: dict) -> list[Problem]:
    m = _metrics(ctx)
    values = _known(m.get("tvl_usd"), m.get("vol_24h"), m.get("vol_7d"), m.get("vol_7d_prev"))
    out = []
    for hit in _MONEY.finditer(part):
        raw, suffix = hit.group(1), (hit.group(2) or "").lower()
        mult = _MULT.get(suffix, 1.0)
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        stated, unit = _num(raw) * mult, mult * 10 ** -decimals
        before = part[max(0, hit.start() - 16): hit.start()]
        if any(_bounded(stated, v, unit, before) for v in values):
            continue
        if re.search(rf"\$\s?{re.escape(raw)}(?![\d,])", _ctx_text(ctx)):   # quoted as given in a receipt title or fact
            continue
        known = ", ".join(f"{v:,.0f}" for v in values) or "none known"
        out.append(Problem("figure", part, f"{hit.group(0).strip()} matches no metric passed in ({known})"))
    return out


def _pct_values(ctx: dict) -> list[float]:
    m = _metrics(ctx)
    v7, prev, d1 = m.get("vol_7d"), m.get("vol_7d_prev"), m.get("vol_24h")
    out = []
    if _known(v7, prev) == [v7, prev] and prev:
        out.append(abs(v7 / prev - 1) * 100)
    if _known(v7, d1) == [v7, d1] and v7:
        out.append(d1 / v7 * 100)
    return out


def _pct_problems(part: str, ctx: dict) -> list[Problem]:
    text = _ctx_text(ctx)
    quoted = {_num(x) for x in _PCT.findall(text)}
    out = []
    for hit in _PCT.finditer(part):
        stated = _num(hit.group(1))
        if stated in quoted or any(abs(stated - v) <= max(1.0, 0.05 * v) for v in _pct_values(ctx)):
            continue
        out.append(Problem("figure", part, f"{hit.group(0)} matches no percentage passed in or the 7-day change"))
    return out


def _count_problems(part: str, ctx: dict) -> list[Problem]:
    out = []
    for hit in _COUNT.finditer(part):
        kind = _NOUNS[hit.group(2).lower()]
        stated = _num(hit.group(1))
        values, upper = _count_values(kind, ctx)
        before = part[max(0, hit.start() - 16): hit.start()]
        if any((stated <= v) if upper else _bounded(stated, v, 1.0, before) for v in values):
            continue
        out.append(Problem("figure", part, f"{hit.group(0)!r}: {kind} passed in {values or 'unknown'}"))
    for hit in _WORD_COUNT.finditer(part):
        kind = _NOUNS[hit.group(2).lower()]
        stated = _WORDS[hit.group(1).lower()]
        values, _ = _count_values(kind, ctx)
        if values and not any(stated == v for v in values):
            out.append(Problem("figure", part, f"{hit.group(0)!r}: {kind} passed in {values}"))
    for hit in _MAGNITUDE.finditer(part):
        phrase = hit.group(1).lower()
        lo, hi = next((lo, hi) for p, lo, hi in _MAGNITUDES if re.fullmatch(p, phrase, re.I))
        kind = _NOUNS[hit.group(2).lower()]
        values, upper = _count_values(kind, ctx)
        if not values:
            out.append(Problem("figure", part, f"{hit.group(0)!r}: {kind} unknown"))
        elif not any((lo <= v) if upper else (lo <= v <= hi) for v in values):
            out.append(Problem("figure", part, f"{hit.group(0)!r} is {lo:,}-{hi:,}; {kind} passed in {values}"))
    return out


# --- trend -------------------------------------------------------------------------------------------------------
_WEEK = re.compile(r"\b(?:week|weekly|weeks|7-day|seven-day|7 days|seven days)\b", re.I)
_VOLUME = re.compile(r"\b(?:volume|trading|traded|activity|usage|use|swaps|flow|interest)\b", re.I)
_UP = re.compile(r"\b(?:higher|up(?!\s+to\b)|grew|grown|grows|growing|rose|risen|rising|jump(?:ed|s)?|picked up|pickup"
                 r"|surg(?:ed|es|ing)|soar(?:ed|ing)|climb(?:ed|ing)|increas(?:ed|ing|e)|above|gain(?:ed|ing)|doubled|tripled"
                 r"|accelerat(?:ed|ing))\b", re.I)
_DOWN = re.compile(r"\b(?:lower|down|fell|fallen|falling|fad(?:ed|ing)|declin(?:ed|ing|e)|drop(?:ped|ping)?|below|slipp(?:ed|ing)"
                   r"|shr(?:ank|inking|unk)|cool(?:ed|ing)|slow(?:ed|ing)|halved|weaken(?:ed|ing))\b", re.I)
_STRONG = re.compile(r"\bsharp(?:ly)?\b|\bclearly\b|\b(?:far|well|much) (?:above|below|beyond|higher|lower|more|less)\b"
                     r"|\bjump(?:ed|s)?\b|\bsurg(?:ed|es|ing)\b|\bsoar(?:ed|ing)\b|\bdoubled\b|\btripled\b|\bhalved\b"
                     r"|\bsteep(?:ly)?\b|\bplung(?:ed|ing)\b", re.I)
# "growing rather than fading", "not fading": the negated word states no direction
_NEGATED_DIRECTION = re.compile(r"\b(?:rather than|instead of|not|never|no longer)\s+\w+", re.I)
_FROM_NOTHING = re.compile(
    r"\b(?:nothing|no volume|no trading|zero volume|no activity)\b[^.;]{0,60}\b(?:prior|previous|before)\b"
    r"|\b(?:prior|previous) (?:7 days|seven days|week)\b[^.;]{0,30}\b(?:no volume|nothing|zero)\b"
    r"|\bfrom (?:nothing|zero|a standing start)\b|\bnew this week\b", re.I)
_LATEST_DAY_MOST = re.compile(
    r"\b(?:latest|last|most recent) day\b[^.;]{0,40}\bmost of\b"
    r"|\bmost (?:of the )?(?:activity|volume|trading)\b[^.;]{0,40}\b(?:latest|last|most recent) day\b", re.I)


def _trend_problems(part: str, ctx: dict) -> list[Problem]:
    m = _metrics(ctx)
    if not m:
        return []
    v7, prev, d1 = m.get("vol_7d"), m.get("vol_7d_prev"), m.get("vol_24h")
    out = []
    if _FROM_NOTHING.search(part) and isinstance(prev, (int, float)) and prev > 0.01 * max(float(v7 or 0), 1.0):
        out.append(Problem("trend", part, f"says the prior week had nothing; vol_7d_prev is {prev:,.0f}"))
    if _LATEST_DAY_MOST.search(part) and _known(v7, d1) == [v7, d1] and v7 and d1 <= 0.5 * v7:
        out.append(Problem("trend", part, f"says most of the week landed on the latest day; {d1:,.0f} of {v7:,.0f}"))
    if out or not (_WEEK.search(part) and _VOLUME.search(part)):
        return out
    stated = _NEGATED_DIRECTION.sub(" ", part)
    # "new this week" is a direction too: "new and fading" says up on the week and down on the day, so it is not checked
    up, down = bool(_UP.search(stated) or _FROM_NOTHING.search(stated)), bool(_DOWN.search(stated))
    if up == down:
        return out                              # no direction, or both (up on the week, down on the day): not checked
    if _known(v7, prev) != [v7, prev]:
        return [Problem("trend", part, "states a weekly trend, but 7-day or prior 7-day volume is unknown")]
    change = float("inf") if prev == 0 else v7 / prev - 1
    need = 0.10 if _STRONG.search(stated) else 0.0
    if (up and not change > need) or (down and not change < -need):
        said = ("up" if up else "down") + (" sharply" if need else "")
        out.append(Problem("trend", part, f"says {said}; 7-day {v7:,.0f} vs prior {prev:,.0f}"))
    return out


_ZERO_TVL = re.compile(r"(?:\bzero\b|\$0\b)[^.;]{0,24}\bTVL\b|\bTVL\b[^.;]{0,24}(?:\bzero\b|\$0(?![\d.,]))", re.I)
_NO_TVL = re.compile(r"\bno (?:reported |known )?TVL\b|\bTVL\b[^.;]{0,24}\b(?:unknown|unreported|not reported)\b", re.I)


def _tvl_problems(part: str, ctx: dict) -> list[Problem]:
    m = _metrics(ctx)
    if "tvl_usd" not in m:
        return []
    tvl = m.get("tvl_usd")
    if tvl is None and _ZERO_TVL.search(part):
        return [Problem("figure", part, "calls unknown TVL zero")]
    if isinstance(tvl, (int, float)) and tvl > 0 and (_ZERO_TVL.search(part) or _NO_TVL.search(part)):
        return [Problem("figure", part, f"says no or zero TVL; tvl_usd is {tvl:,.0f}")]
    return []


# --- assembly ----------------------------------------------------------------------------------------------------
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")

WHAT_CHECKS = (_leak_problems, _chain_problems, _audit_problems, _status_problems)
BLURB_CHECKS = (_leak_problems, _chain_problems, _audit_problems, _coverage_problems, _status_problems,
                _money_problems, _pct_problems, _count_problems, _trend_problems, _tvl_problems)


def _scrub(text: str, ctx: dict) -> tuple[str, list[Problem]]:
    """In-place removals that keep the rest of the sentence: leaked "in the input", a wrong "unverified"/"unlisted"."""
    out = []
    for m in _LEAK_PHRASE.finditer(text):
        out.append(Problem("leak", m.group(0).strip(", "), "internal wording removed"))
    text = _LEAK_PHRASE.sub("", text)
    if _verified(ctx) is not False and _STATUS_ADJ["unverified"].search(text):
        out.append(Problem("status", "unverified", "nothing passed in says the hook is unverified; word removed"))
        text = _STATUS_ADJ["unverified"].sub("", text)
    if _listed(ctx) and _STATUS_ADJ["unlisted"].search(text):
        out.append(Problem("status", "unlisted", "the hook has a hooklist entry; word removed"))
        text = _STATUS_ADJ["unlisted"].sub("", text)
    return text, out


def _capitalize(s: str) -> str:
    s = s.strip(" ,;:-")
    return s[:1].upper() + s[1:] if s else s


def _finish(s: str) -> str:
    s = _capitalize(s)
    return s if not s or s[-1] in ".!?…" else s + "."


def problems(text: str, ctx: dict, checks) -> list[Problem]:
    return [p for part in _parts(text, checks is WHAT_CHECKS) for check in checks for p in check(part, ctx)]


def _parts(text: str, clauses: bool) -> list[str]:
    sentences = [s for s in _SENTENCE_END.split((text or "").strip()) if s.strip()]
    if not clauses:
        return sentences
    return [c for s in sentences for c in re.split(r";\s+", s) if c.strip()]


def what_line_problems(text: str, ctx: dict) -> list[Problem]:
    return problems(text, ctx, WHAT_CHECKS)


def blurb_problems(text: str, ctx: dict) -> list[Problem]:
    return problems(text, ctx, BLURB_CHECKS)


def clean_what_line(text: str, ctx: dict, min_len: int = 25) -> tuple[str, list[Problem]]:
    """The line with failing clauses dropped; "" when what is left is shorter than `min_len` (a fragment is no line)."""
    t, found = _scrub(text or "", ctx)
    kept_sentences = []
    for sentence in _parts(t, clauses=False):
        kept = []
        for clause in re.split(r";\s+", sentence):
            bad = [p for check in WHAT_CHECKS for p in check(clause, ctx)]
            found += bad
            if not bad and clause.strip(" .;"):
                kept.append(clause.strip().rstrip("."))
        if kept:
            kept_sentences.append(_finish("; ".join(kept)))
    out = " ".join(kept_sentences)
    if found and len(out) < min_len:
        out = ""
    return (out if found else (text or "").strip()), found


def clean_blurb(text: str, ctx: dict) -> tuple[str, list[Problem]]:
    """The blurb with failing sentences dropped ("" when none is left)."""
    t, found = _scrub(text or "", ctx)
    kept = []
    for sentence in _parts(t, clauses=False):
        bad = [p for check in BLURB_CHECKS for p in check(sentence, ctx)]
        found += bad
        if not bad:
            kept.append(_finish(sentence))
    return (" ".join(kept) if found else (text or "").strip()), found
