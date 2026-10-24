"""prose_checks against the live text the 2026-10-24 fact-check found wrong (and right), with the live metrics of the
2026-10-23 data day. Each blurb was generated against that day's or an earlier day's numbers; these are the numbers it
sits beside on the page today."""
import json
import logging
from types import SimpleNamespace

import pytest

from bait.clients.claude import Claude, what_line_prompt, why_blurb_prompt
from bait.prose_checks import blurb_problems, chains_named, clean_blurb, clean_what_line, what_line_problems

KEYS = ("tvl_usd", "vol_24h", "vol_7d", "vol_7d_prev", "swaps_24h", "swaps_7d", "wallets_24h", "pools_n")
LIVE = {  # project_daily for 2026-10-23, in KEYS order
    "stablepairhook": (10840852, 155978543, 745713090, 714354016, 3023, 19452, 1574, 3),
    "fablesrampeth": (18571099, 35497153, 257025392, 400076250, 12605, 94474, 1478, 10),
    "fablesramp": (2740960, 3157580, 28142595, 59711072, None, None, 2406, 39),
    "angstrom": (5133525, 1931451, 83485503, 169792043, None, None, 63, 5),
    "arrakisprivatehook": (1062896, 456663, 3779817, 3763925, None, None, 243, 42),
    "uniswapv4kemhook": (721805, 909034, 6857917, 10307740, None, None, 618, 560),
    "sessionfeehook": (2634283, 2960287, 22633936, 13049869, 6353, 45280, 1061, 3),
    "standardlaunchhook": (460740, 1547967, 11419729, 2366055, None, None, 1549, 1075),
    "pangea-multichain-hook": (209898, 1131855, 11520889, 0, None, None, 1812, 994),
    "anchorhook-robinhood": (340141, 133410, 1141831, 358185, 1554, 10341, 198, 1),
    "volatilepairhook": (3508109, 8375875, 28509740, 3501705, 1969, 11313, 534, 1),
    "dualpoolhook": (None, 6872, 3413738, 34994, None, None, 18, 95),
    "aegis": (5968356, 381948, 3208912, 2478024, None, None, 442, 227),
    "launchhook-0x963f-25cc": (None, None, 1277005, 0, None, None, 257, 6),
    "fablesrwa": (2263762, 1932188, 23884894, 20730776, 6882, 56422, 1233, 17),
    "launchhook": (165546, 195657, 3887786, 13490310, None, None, 1021, 55524),
    "claus": (331589, 282043, 2959024, 9639389, 437, 4357, 173, 1),
    "programmablelaunchfeehook": (396348, 228380, 2637313, 3880003, 922, 6117, 453, 1),
}


CHAINS = {"fablesrampeth": ["robinhood"], "fablesramp": ["robinhood"]}   # the rest: Ethereum, unless a test is about chains


def bctx(slug, receipts=(), audit_urls=(), chains=None, hooklisted=None, verified_source=None, descriptions=(),
         hooks_n=1, wallets_7d=None, **extra):
    """A why_blurb context shaped like signals._blurb_ctx: the hook facts (enrich.hook_facts; registry flags unknown
    unless given), the 2026-10-23 metrics, signals and the stored receipts."""
    m = dict(zip(KEYS, LIVE[slug]), wallets_24h_kind="distinct", wallets_7d=wallets_7d)
    return {"name": slug, "category": "other", "what_line": None, "hooks_n": hooks_n,
            "chains": list(chains if chains is not None else CHAINS.get(slug, ["ethereum"])),
            "hooklisted": hooklisted, "verified_source": verified_source, "hooklist_descriptions": list(descriptions),
            "audit_urls": list(audit_urls), "metrics": m, "signals": {},
            "receipts": [{"title": t} for t in receipts], **extra}


def wctx(chains, descriptions=(), audit_urls=(), **extra):
    """A what_line context shaped like enrich.what_context (registry flags unknown unless given)."""
    return {"name": "x", "is_factory": False, "hooks_n": 1, "chains": list(chains), "hooklisted": None,
            "verified_source": None, "hooklist_descriptions": list(descriptions), "audit_urls": list(audit_urls),
            "flags_union": ["beforeSwap"], "dynamic_fee": False, "enrichment": {}, **extra}


def kinds(problems):
    return sorted({p.kind for p in problems})


# --- figures: dollar amounts and counts must match the metrics passed in --------------------------------------------

STABLEPAIR = ("StablePair, built by Uniswap Labs, handled about $657M in 7-day volume across 3 pools with $7.5M TVL, though "
              "that is down from about $866M the prior week. It drew press coverage in September and October 2026 as a "
              "Uniswap move into stablecoin trading, and its GitHub repo had a commit on Sep 10.")


def test_stale_dollar_figures_drop_the_sentence_and_keep_the_rest():
    out, found = clean_blurb(STABLEPAIR, bctx("stablepairhook", receipts=["StablePair Hook ... - BlockBeats"]))
    assert out == ("It drew press coverage in September and October 2026 as a Uniswap move into stablecoin trading, and "
                   "its GitHub repo had a commit on Sep 10.")
    details = " ".join(p.detail for p in found)
    assert "$657M" in details and "$7.5M" in details and "$866M" in details
    assert "trend" in kinds(found)            # "down from" while 7-day volume is up 4%


def test_figures_that_match_the_metrics_within_rounding_pass():
    ok = "It handled about $746M in 7-day volume across 3 pools with $10.8M TVL, up from $714M the prior week."
    assert blurb_problems(ok, bctx("stablepairhook")) == []
    assert blurb_problems("It holds over $10M of TVL.", bctx("stablepairhook")) == []
    assert kinds(blurb_problems("It holds over $12M of TVL.", bctx("stablepairhook"))) == ["figure"]


def test_counts_must_match_known_metrics_and_unknown_counts_fail():
    ramp_eth = ("FablesRampETH handles native ETH pools on Robinhood Chain with a dynamic fee, holding about $25.5M TVL "
                "across 3 pools and 22,874 swaps in the past 7 days. Seven-day volume was about $431.9M, down from about "
                "$547.5M the prior week.")
    out, found = clean_blurb(ramp_eth, bctx("fablesrampeth"))
    assert out == ""
    details = " ".join(p.detail for p in found)
    assert "'3 pools'" in details and "'22,874 swaps'" in details
    ramp = ("FablesRamp holds about $4.23M TVL across 2 pools and handled roughly $56.5M in 7-day volume over 16,635 "
            "swaps.")
    assert "'16,635 swaps': swaps passed in unknown" in " ".join(p.detail for p in blurb_problems(ramp, bctx("fablesramp")))
    # "v4 hooks" is not a count of four hooks
    assert blurb_problems("It is one of the busiest Uniswap v4 hooks this week.", bctx("fablesramp")) == []


def test_a_percentage_must_be_the_weekly_change_or_one_passed_in():
    kem = ("It handled about $6.96M in 7-day volume across 5,195 swaps in 4 pools with roughly $1.04M TVL, down about 3% "
           "from the prior week's $7.18M. Kyber Network built it so swaps need a signed off-chain quote, and output above "
           "the quoted rate is kept as claimable ERC-6909 tokens.")
    out, found = clean_blurb(kem, bctx("uniswapv4kemhook"))
    assert out.startswith("Kyber Network built it")
    assert any("3%" in p.detail for p in found)
    assert blurb_problems("Volume fell about 33% from the prior week.", bctx("uniswapv4kemhook")) == []
    assert blurb_problems("It charges a flat 1% fee.", bctx("uniswapv4kemhook", what_line="Charges a flat 1% fee.")) == []


def test_size_words_must_fit_the_metric():
    angstrom = ("Weekly volume is clearly higher than the week before, and daily swaps come from a few hundred distinct "
                "wallets across only three pools, so activity is growing but narrow.")
    details = " ".join(p.detail for p in blurb_problems(angstrom, bctx("angstrom")))
    assert "'three pools'" in details and "'a few hundred distinct wallets' is 200-999" in details
    anchor = "Most activity landed on the latest day, spread across a few dozen distinct wallets."
    details = " ".join(p.detail for p in blurb_problems(anchor, bctx("anchorhook-robinhood")))
    assert "'a few dozen distinct wallets' is 24-99" in details
    pangea = "Volume is spread across a very large number of pools and thousands of distinct wallets."
    assert kinds(blurb_problems(pangea, bctx("pangea-multichain-hook"))) == ["figure"]       # 1,812 is not thousands
    right = "Activity comes from hundreds of distinct wallets in a single pool."
    assert blurb_problems(right, bctx("volatilepairhook")) == []
    assert blurb_problems("Trading in the single CLAUS pool is broad.", bctx("claus")) == []


def test_unknown_tvl_is_never_zero_and_known_tvl_is_never_none():
    anchor = "It is a solo developer project with no audit listed, no recent commits, no press coverage, and no reported TVL."
    assert any("no or zero TVL" in p.detail for p in blurb_problems(anchor, bctx("anchorhook-robinhood")))
    assert blurb_problems("TVL is unknown, so liquidity depth is unclear.", bctx("dualpoolhook")) == []
    assert any("unknown TVL zero" in p.detail for p in blurb_problems("TVL is zero, so nothing backs it.", bctx("dualpoolhook")))


# --- trend: growing / fading must follow vol_7d against vol_7d_prev ---------------------------------------------------

def test_trend_words_must_follow_the_weekly_volumes():
    angstrom = "Weekly volume is clearly higher than the week before, so activity is growing."
    assert kinds(blurb_problems(angstrom, bctx("angstrom"))) == ["trend"]                  # 83.5M vs 169.8M
    arrakis = "Volume over the last week is clearly up on the prior week, and activity is spread across many pools."
    assert kinds(blurb_problems(arrakis, bctx("arrakisprivatehook"))) == ["trend"]        # +0.4% is not "clearly up"
    assert blurb_problems("Volume over the last week is up slightly on the prior week.", bctx("arrakisprivatehook")) == []
    dualpool = ("Weekly volume rose sharply from the prior week, though the latest day's volume is small by comparison, so "
                "the pickup looks uneven rather than steady.")
    assert blurb_problems(dualpool, bctx("dualpoolhook")) == []
    volatile = ("Volume over the last 7 days is far above the prior 7 days, and activity comes from hundreds of distinct "
                "wallets in a single pool, so usage looks like it is growing rather than fading.")
    assert blurb_problems(volatile, bctx("volatilepairhook")) == []
    unknown = bctx("angstrom")
    unknown["metrics"]["vol_7d_prev"] = None
    assert kinds(blurb_problems("Weekly volume is higher than the week before.", unknown)) == ["trend"]


def test_nothing_last_week_and_most_on_the_latest_day_are_checked():
    session = ("Volume and swap activity are new this week, with nothing recorded in the prior seven days, so it is growing "
               "from a standing start across three pools and a broad set of distinct wallets.")
    assert any("vol_7d_prev is 13,049,869" in p.detail for p in blurb_problems(session, bctx("sessionfeehook")))
    standard = ("Activity is growing fast: the prior 7 days recorded no volume, while the latest day alone accounts for "
                "most of the past week's trading, spread across many distinct wallets and a large set of launch pools.")
    details = " ".join(p.detail for p in blurb_problems(standard, bctx("standardlaunchhook")))
    assert "vol_7d_prev is 2,366,055" in details and "1,547,967 of 11,419,729" in details
    pangea = "Volume is new, with nothing recorded in the prior 7 days, and it is spread across a very large number of pools."
    assert blurb_problems(pangea, bctx("pangea-multichain-hook")) == []                    # prior week really was 0


# --- audits and coverage ---------------------------------------------------------------------------------------------

AEGIS = ("Weekly volume has grown sharply against the prior week across a wide set of pools, so use looks broad rather "
         "than tied to one market. Development looks quieter, with no GitHub commits in the last 30 days, though the team "
         "links audit reports from Spearbit via Cantina and Trail of Bits.")


def test_an_audit_needs_an_audit_link():
    out, found = clean_blurb(AEGIS, bctx("aegis"))
    assert kinds(found) == ["audit"]
    assert out.startswith("Weekly volume has grown sharply") and "audit" not in out
    # the hooks' hooklist audit link, the one the web's audit badge shows
    assert blurb_problems(AEGIS, bctx("aegis", audit_urls=["https://example.org/aegis-audit.pdf"])) == []
    # text that is not a web URL is no link, as on the web (firstWebUrl)
    assert kinds(blurb_problems(AEGIS, bctx("aegis", audit_urls=["see the docs"]))) == ["audit"]


def test_statements_that_there_is_no_audit_pass():
    for text in ("No team, audit, website or press coverage is listed.",
                 "The hook's purpose, team and any audit are undocumented.",
                 "It runs behind an upgradeable proxy and no audit is listed."):
        assert blurb_problems(text, bctx("sessionfeehook")) == [], text


def test_coverage_needs_receipts():
    rwa = ("It matters to anyone trading tokenized equities on v4 because fees follow US market sessions, though no audit "
           "is listed and the press receipts are general v4 hook coverage.")
    assert kinds(blurb_problems(rwa, bctx("fablesrwa"))) == ["coverage"]
    launch = ("It charges an anti-snipe fee that decays over 20 seconds, and an HTX Research report on stock-linked memecoin "
              "issuance and AMM liquidity appeared in recent coverage.")
    assert "coverage" in kinds(blurb_problems(launch, bctx("launchhook")))
    claus = "Recent listing and exchange-zone news plus the project's own journal posts suggest it is still drawing attention."
    assert blurb_problems(claus, bctx("claus", receipts=["Two rules in the same pool | Claus Journal"])) == []
    assert blurb_problems("There is no press coverage yet.", bctx("claus")) == []


# --- internal words --------------------------------------------------------------------------------------------------

def test_internal_wording_is_removed_or_dropped():
    volatile = ("Uniswap Labs built it and launched it on Ethereum mainnet, which several outlets covered this month, though "
                "no audit is listed in the input.")
    out, found = clean_blurb(volatile, bctx("volatilepairhook", receipts=["Uniswap VolatilePair Hook"]))
    assert out.endswith("though no audit is listed.") and kinds(found) == ["leak"]
    pangea = ("The alive signal is false, no outside coverage or team information was found, and swap counts are unknown, "
              "so what the hook does and whether the activity will last are unclear.")
    assert "leak" in kinds(blurb_problems(pangea, bctx("pangea-multichain-hook")))
    prog = "The project is flagged not alive, the GitHub repo has had no commits in the last 30 days, and no audit link is listed."
    assert kinds(blurb_problems(prog, bctx("programmablelaunchfeehook"))) == ["leak"]
    line = "Runs custom logic before and after swaps on a Uniswap v4 pool; the input does not specify what it does."
    out, found = clean_what_line(line, wctx(["robinhood"]))
    assert out == "Runs custom logic before and after swaps on a Uniswap v4 pool." and kinds(found) == ["leak"]
    assert what_line_problems("Takes a fixed 0.50% hook fee on each swap in one ETH/NULL pool.", wctx(["ethereum"])) == []


# --- verification, registry, purpose ---------------------------------------------------------------------------------

PANGEA_DESC = ("Fee hook for Pangea pangea.trade multichain token launch pools. Takes the whole trading fee in the quote "
               "asset at swap time: beforeSwap on buys, afterSwap on sells, grossed up for exact-out. Pools use fee 0.")


def test_a_hooklisted_hook_is_not_unlisted_or_of_unknown_purpose():
    line = ("Unlisted v4 hook that runs on pool init, add liquidity and swaps, and can modify swap amounts via return "
            "deltas; purpose unknown.")
    out, found = clean_what_line(line, wctx(["bnb", "ethereum", "robinhood"], [PANGEA_DESC]))
    assert out == "V4 hook that runs on pool init, add liquidity and swaps, and can modify swap amounts via return deltas."
    assert [p.kind for p in found] == ["status", "status"]
    session = ("Unlisted hook on Robinhood Chain with beforeInitialize and beforeSwap callbacks; pools using it appear to "
               "be in dynamic-fee mode.")
    assert clean_what_line(session, wctx(["robinhood"])) == (session, [])                  # no hooklist entry: true


def test_unverified_is_not_said_unless_the_context_says_so():
    line = "Unverified hook on Robinhood Chain that runs on pool init, swaps, and liquidity changes; its purpose is not documented."
    out, found = clean_what_line(line, wctx(["robinhood"]))
    assert out == "Hook on Robinhood Chain that runs on pool init, swaps, and liquidity changes; its purpose is not documented."
    assert kinds(found) == ["status"]
    blurb = ("The hook is unverified with no documented purpose or audit, and the only outside mentions are IMD Explorer "
             "launch listings that don't clearly tie to this address.")
    ctx = bctx("launchhook-0x963f-25cc", receipts=["IMD Explorer listing"])
    assert kinds(blurb_problems(blurb, ctx)) == ["status"]
    assert blurb_problems(blurb, {**ctx, "verified_source": False}) == []
    assert clean_what_line(line, wctx(["robinhood"], verified_source=False)) == (line, [])


# --- chains ----------------------------------------------------------------------------------------------------------
# No live line or blurb in the top 200 named a chain the project is not on (2026-10-24); the check guards the rule.

def test_a_chain_the_project_is_not_on_drops_the_clause():
    line = "Dynamic-fee hook for same-price pairs like USDC/USDT on Base; fee starts high and decays each block to favor LPs."
    out, found = clean_what_line(line, wctx(["arbitrum", "ethereum"]))
    assert out == "Fee starts high and decays each block to favor LPs." and kinds(found) == ["chain"]
    assert what_line_problems(line, wctx(["arbitrum", "base", "ethereum"])) == []
    eth = "Sets a per-pool flat swap fee and supports native ETH pools on Robinhood Chain."
    assert what_line_problems(eth, wctx(["robinhood"])) == []
    assert kinds(what_line_problems(eth, wctx(["ethereum"]))) == ["chain"]


def test_chain_words_in_other_senses_are_not_chains():
    assert chains_named("Charges a base fee in the base token, for Robinhood stock tokens.") == set()
    assert chains_named("Same address on Ethereum, BNB Chain, Base and Robinhood.") == {"ethereum", "bnb", "base", "robinhood"}
    assert chains_named("A Robinhood Chain hook, also tested on Sepolia.") == {"robinhood", "testnet"}
    # the blurb context carries chains now (enrich.hook_facts); a context without them is not checked rather than stripped
    assert blurb_problems("It launched on Ethereum mainnet this month.", bctx("volatilepairhook")) == []
    assert kinds(blurb_problems("It launched on Base this month.", bctx("volatilepairhook", chains=["ethereum"]))) == ["chain"]
    no_chains = {k: v for k, v in bctx("volatilepairhook").items() if k != "chains"}
    assert blurb_problems("It launched on Base this month.", no_chains) == []


# --- facts the contexts gained (enrich.hook_facts, metrics.wallets_7d) -----------------------------------------------

def test_the_hooklisted_flag_settles_unlisted_with_or_without_a_description():
    line = "Unlisted hook that takes the whole trading fee on launch pools."
    out, found = clean_what_line(line, wctx(["robinhood"], hooklisted=True))        # listed, no description
    assert out == "Hook that takes the whole trading fee on launch pools." and kinds(found) == ["status"]
    # dropped from the registry: it keeps its description, and "unlisted" is now true
    assert clean_what_line(line, wctx(["robinhood"], [PANGEA_DESC], hooklisted=False)) == (line, [])
    blurb = "It is an unlisted hook, yet volume is spread across a very large number of pools."
    assert kinds(blurb_problems(blurb, bctx("pangea-multichain-hook", hooklisted=True))) == ["status"]
    assert blurb_problems(blurb, bctx("pangea-multichain-hook")) == []               # registry status unknown


def test_purpose_unknown_in_a_blurb_is_checked_against_the_descriptions():
    pangea = "Swap counts are unknown, so what the hook does and whether the activity will last are unclear."
    assert kinds(blurb_problems(pangea, bctx("pangea-multichain-hook", descriptions=[PANGEA_DESC]))) == ["status"]
    assert blurb_problems(pangea, bctx("pangea-multichain-hook")) == []              # no description: may be true
    # SessionFeeHook: no hooklist entry, but a verified source documents the fee logic
    session = "Its fee-setting logic is unknown, so the hook's role in the pools is unclear."
    assert kinds(blurb_problems(session, bctx("sessionfeehook", verified_source=True))) == ["status"]
    assert blurb_problems(session, bctx("sessionfeehook")) == []


def test_hook_counts_and_seven_day_wallets_are_checked_against_the_metrics():
    assert blurb_problems("It runs 3 hooks across many pools.", bctx("aegis", hooks_n=3)) == []
    assert kinds(blurb_problems("It runs 5 hooks across many pools.", bctx("aegis", hooks_n=3))) == ["figure"]
    assert kinds(blurb_problems("It spans five hooks.", bctx("aegis", hooks_n=3))) == ["figure"]
    weekly = "Over the week 1,500 wallets traded it, a broad base."
    assert blurb_problems(weekly, bctx("angstrom", wallets_7d=1_500)) == []          # 63 that day, 1,500 over the week
    assert kinds(blurb_problems(weekly, bctx("angstrom"))) == ["figure"]              # weekly count unknown


def test_verified_source_from_the_hooks_settles_unverified():
    line = "Unverified hook on Robinhood Chain that runs on pool init and swaps."
    assert clean_what_line(line, wctx(["robinhood"], verified_source=False)) == (line, [])     # no hook verified
    out, found = clean_what_line(line, wctx(["robinhood"], verified_source=True))
    assert out == "Hook on Robinhood Chain that runs on pool init and swaps." and kinds(found) == ["status"]
    blurb = "The hook is unverified, so its logic cannot be read."
    assert blurb_problems(blurb, bctx("launchhook-0x963f-25cc", verified_source=False)) == []
    assert kinds(blurb_problems(blurb, bctx("launchhook-0x963f-25cc", verified_source=True))) == ["status"]


# --- right text passes untouched -------------------------------------------------------------------------------------

@pytest.mark.parametrize("slug,text", [
    ("dualpoolhook", "Weekly volume rose sharply from the prior week, though the latest day's volume is small by comparison, "
                     "so the pickup looks uneven rather than steady. It runs across many pools, with a modest set of distinct "
                     "wallets on the latest day, and the Uniswap Labs repo had a commit on the latest day."),
    ("pangea-multichain-hook", "Volume is new, with nothing recorded in the prior 7 days, and it is spread across a very "
                               "large number of pools, which points to broad use rather than a few traders."),
])
def test_correct_blurbs_are_returned_as_written(slug, text):
    assert clean_blurb(text, bctx(slug)) == (text, [])


# --- wired into the Claude client ------------------------------------------------------------------------------------

def _client(payload):
    class M:
        def create(self, **kw):
            text = SimpleNamespace(type="text", text=json.dumps(payload))
            return SimpleNamespace(stop_reason="end_turn", usage=None, content=[text])
    m = M()
    return SimpleNamespace(messages=m, beta=SimpleNamespace(messages=m))


def test_claude_returns_checked_prose_and_logs_what_it_dropped(caplog):
    with caplog.at_level(logging.WARNING, logger="bait"):
        blurb = Claude(client=_client({"why_blurb": STABLEPAIR})).why_blurb(bctx("stablepairhook", receipts=["BlockBeats"]))
        line = "Unlisted v4 hook that takes the trading fee on swaps; purpose unknown."
        res = Claude(client=_client({"what_line": line, "category": "other"})).what_line(wctx(["ethereum"], [PANGEA_DESC]))
    assert blurb.startswith("It drew press coverage") and "$657M" not in blurb
    assert res == {"what_line": "V4 hook that takes the trading fee on swaps.", "category": "other"}
    assert "why_blurb project=stablepairhook dropped: figure" in caplog.text and "what_line project=x dropped: status" in caplog.text


def test_prompts_carry_the_rules_before_the_facts():
    w, b = what_line_prompt({"name": "x"}), why_blurb_prompt({"name": "x"})
    assert w.index("Never add a mechanism") < w.index('"name"') and "Name a chain only if it is in chains" in w
    assert "Mention an audit only when audit_urls is not empty" in w
    assert b.index("Say growing only when vol_7d is above vol_7d_prev") < b.index('"name"')
    assert "Do not quote dollar amounts" in b and "never mention the input" in b
    for p in (w, b):        # the contexts carry the registry flags now; nothing researched reaches the prompts
        assert "unverified only when verified_source is false" in p and "unlisted only when hooklisted is false" in p
        assert "researched" not in p and "website" not in p and "under enrichment" not in p
    assert "an audit only when audit_urls is not empty" in b and "Name a chain only if it is in chains" in b
