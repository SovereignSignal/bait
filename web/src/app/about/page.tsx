import type { Metadata } from "next";
import { Footer } from "@/components/Footer";

export const metadata: Metadata = { title: "About", description: "How Bait finds, groups, scores and labels Uniswap v4 hooks, and how it flags wash-like volume." };

export default function AboutPage() {
  return (
    <>
      <main className="col" id="main" tabIndex={-1}>
        <div className="lead">
          <h1>How this works</h1>
          <p>A terminal for Uniswap v4 hooks. Projects, not hook addresses. Figures refreshed nightly; trades live.</p>
        </div>

        <section className="block">
          <h2>What you are looking at</h2>
          <p>
            A Uniswap v4 pool can name a hook: a contract that runs before or after swaps and liquidity changes and can change
            fees, block trades, take a cut or run an auction. Thousands of hooks are deployed; most see no use. Bait finds every
            hook with a live pool on the chains it covers, groups hooks that belong to one team into a project, and shows how
            much people trade through it, how many wallets, and whether the volume looks like real use.
          </p>
          <p>
            Every figure is a closed UTC day, refreshed nightly; a project page says which day. A dash means unknown, never zero.
            Badges explain themselves when you tap &quot;What these badges mean&quot;. Nothing here is an audit, an endorsement or
            trading advice, and Bait never executes trades.
          </p>
        </section>

        <section className="block">
          <h2>Method</h2>
          <p>
            Identity comes from the public <a className="inline" href="https://github.com/Uniswap/hooklist" target="_blank" rel="noopener">hooklist</a> registry and from the chain:
            a hook that is not in the registry is found from its pools, and its permissions come from the bits in its address. Usage comes from
            Bait&apos;s own indexer, which reads every hooked Uniswap v4 pool and the last 14 days of swaps on 16 chains through Envio
            HyperSync, valued at current DefiLlama prices. Where the indexer has not caught up, or a hook keeps its own accounting,
            GeckoTerminal&apos;s daily figures stand in. GeckoTerminal also supplies pool TVL and rolling 24-hour figures, and public RPC nodes
            resolve hooks and read their bytecode. The feed covers ten of those chains; the rest appear under Just launched. Figures are
            closed UTC days, refreshed by a nightly run; a project page says which day its figures run through. Contract names come
            from Blockscout, Sourcify and Routescan. Audit links come from the registry. Until October 24, 2026, Parallel also found team
            sites, GitHub, X and audit links for new projects, and Exa found posts about them; what was found then still shows, and nothing
            is added. The one-line &quot;what it does&quot; is written by Claude from the registry entry, the hook&apos;s permissions and any
            verified source code; the two-sentence &quot;why people care&quot; from the figures, GitHub activity and those posts, rewritten only
            when the signals change; the page shows when. The Building tab&apos;s audit, launch, hackathon and incubator posts come from Exa
            searches, classified by Claude.
          </p>
          <h3>Activity</h3>
          <p>
            Buys and sells are GeckoTerminal&apos;s rolling 24-hour counts. Top wallets come from GeckoTerminal&apos;s latest ~300 trades in each
            of Bait&apos;s 30 busiest pools, once a day. Distinct wallet counts come from the indexer where it has caught up, otherwise from those
            trades; a &quot;≤&quot; marks an upper bound. The live trade feed on a project page reads GeckoTerminal&apos;s recent trades for the project&apos;s
            busiest pool, hides trades under $10, and refreshes every minute while you watch. &quot;Wallets 7d&quot; and the top wallet&apos;s share of swaps come from the indexer
            and are shown at any size, with no threshold; they are unknown unless every chain the project is on has caught up through the week. A project page names the wallet that deployed
            each hook (from the registry, or the explorer&apos;s contract creator) and the share of the week&apos;s swaps those wallets made.
            The same figures are available as JSON from the read API: /api/v1/projects, /api/v1/projects/[slug] and /api/v1/hooks/[address].
            The field reference is in the repository under docs/api.md.
          </p>
          <h3>Grouping</h3>
          <p>
            Thousands of hook addresses collapse into projects. Hooks that share a hooklist name are one project, unless unrelated teams share
            that name (deployer clashes on one chain and no shared bytecode). A verified contract name is only a candidate: those hooks join on
            evidence, the same bytecode (not a shared proxy) or the same deployer (not a factory). Hooks with generic names, such as template
            defaults, stay apart. The few left with only a description are classified once by a small model. Factories that deploy one hook per pool are one card with the hook count as a stat. Renamed and merged projects keep their
            old links.
          </p>
          <h3>Categories</h3>
          <p className="muted">Dynamic fee · Launchpad · Orders (limit, TWAMM) · LP management · Compliance · Oracle · MEV / LVR · Lending · Gaming / NFT</p>
          <p>
            A category comes from the generated description where one exists. Until then it is guessed from what the chain and the registry
            already say: a dynamic-fee property or pool, a factory or a hook with many pools, words in the hook&apos;s own name or description
            (limit order, lending, auction, oracle, KYC, vault, NFT, or launch in its name), or the launch pattern: a hook that decides who
            creates its pools, with one pool per coin against a shared token, that either does nothing else or takes its own fee from swaps
            in pools whose LP fee is zero. A category written from the description wins; where the description found none, the guess
            applies. A project none of this places shows no category, rather than one called Other.
          </p>
        </section>

        <section className="block" id="scoring">
          <h2>Scoring</h2>
          <ul>
            <li>
              <b>New</b>: project first seen within 7 days, and either a pool above $1K TVL or at least 10 swaps in the week.
            </li>
            <li>
              <b>Trending</b>: 7-day volume of at least $10K, at least $1K the week before, first seen over a week ago, and no wash-like volume. Score is <code>log1p(vol_7d) × clamp(vol_7d / max(vol_7d_prev, 1000), 0.5, 10)</code>,
              so growth counts more than size; tie-break on TVL. Top 20.
            </li>
            <li>
              <b>Routable</b>: at least one hook is on Uniswap&apos;s routing allowlist, or one pool meets the automatic rule below, and no hook is on its denylist. Audit status is a separate badge.
            </li>
            <li>
              <b>Hidden from All</b>: no pools, or every pool under $100 TVL with fewer than 10 swaps lifetime, unless the project moved $1M or more.
            </li>
          </ul>
        </section>

        <section className="block" id="wash">
          <h2>Wash-like volume</h2>
          <p>
            A project gets the red &quot;Wash-like volume&quot; badge when pools carrying at least half its volume match one of these rules. It stays in All
            with its page, and never reaches Trending, New, Routable, movers, Leaders or the weekly digest.
          </p>
          <ul>
            <li><b>Few wallets</b>: $250K or more in a day through 25 wallets or fewer, or $1M in a week through 10 or fewer, when the pool also shows 7-day volume over 50 times a known TVL, no liquidity, or symmetric trading; or $1M carried by 10 swaps or fewer.</li>
            <li><b>Concentrated</b>: $1M or more in a week carried by at most two wallets.</li>
            <li><b>Volume far above liquidity</b>: 7-day volume over 300 times a known TVL, with fewer than 50 wallets.</li>
            <li><b>No liquidity</b>: $1M or more in a week through a pool with at most $1K of known TVL and fewer than 50 wallets.</li>
            <li><b>Symmetric</b>: the top wallets each buy and sell equally and carry most of the pool&apos;s volume (bots trading with themselves).</li>
            <li><b>Uniform</b>: the top wallets each made one trade of the same size, and the pool averages that size per wallet.</li>
            <li><b>Unverified</b>: $1M or more through pools too small to list, with no wallet data to check who moved it.</li>
            <li><b>Lookalike token</b>: the volume runs through a pool with a token posing as USDC, USDT or the chain&apos;s wrapped coin, by copying its symbol or the start and end of its address.</li>
            <li><b>Round trips</b>: at least half the pool&apos;s week came and went inside single transactions, swaps that net to nothing on both tokens. Nothing was bought or sold, so this needs no wallet or TVL check.</li>
          </ul>
          <p className="muted">
            Wallet counts are lower bounds and unknown TVL is never treated as zero. A private or RFQ hook with few counterparties and real
            liquidity is not flagged unless one or two wallets carry it. TVL is GeckoTerminal&apos;s valuation of a pool at its own prices, so Bait
            shows it as unknown when it cannot be liquidity: a pool with a lookalike token, or $1M or more that traded under 0.1% of itself in a
            fully measured week (usually a launch&apos;s own supply).
          </p>
        </section>

        <section className="block">
          <h2>Routable</h2>
          <p>
            The Uniswap app only routes through hooked pools whose hook is allowlisted. Per Uniswap&apos;s published rule, a hook is allowed automatically unless its address
            starts with <code>0x91</code>, or it uses <code>beforeSwapReturnsDelta</code>, <code>afterSwapReturnsDelta</code> or a dynamic fee. Bait computes this from the
            permission bits in the hook address (the chain enforces them) and also treats <code>afterAddLiquidityReturnsDelta</code> and <code>afterRemoveLiquidityReturnsDelta</code> as blocking, which is stricter than the
            published rule. Uniswap also publishes the hooks it approved by hand and the hooks it never routes through, in its{" "}
            <a href="https://github.com/Uniswap/uniroute-public/tree/main/src/lib/poolCaching/util">uniroute-public</a> allowlist and denylist. Bait reads
            both every few hours: a denylisted hook never counts as routable, an allowlisted one always does, and every other hook is judged by the rule.
            The Routable tab lists projects with at least one hook or pool that passes. Hooks without a known pool show Routing unknown. Not in hooklist means we found the hook from its pools, not from Uniswap&apos;s registry; its name and permissions come from the chain. The badge is a rule, not a guarantee: Bait has not checked current Uniswap routing or quote availability, and it does not execute trades.
          </p>
        </section>

        <section className="block">
          <h2>Disclaimers</h2>
          <ul>
            <li>Hooks are third-party code in the swap path. Nothing here is audited by us.</li>
            <li>Flags and properties are shown as published in the hooklist registry. We do not verify them against bytecode.</li>
            <li>&quot;Audit linked&quot; means an audit link is published. It is not a statement about the audit&apos;s scope or findings.</li>
            <li>Figures come from Bait&apos;s indexer and public APIs and can lag or be wrong when a source stalls; a project page says which day its figures run through. Metrics are USD estimates.</li>
            <li>Bait is a personal project, not affiliated with Uniswap Labs or the Uniswap Foundation.</li>
          </ul>
        </section>
        <section className="block">
          <h2>Submit a hook</h2>
          <p>
            To get a hook listed, open a pull request on the{" "}
            <a className="inline" href="https://github.com/Uniswap/hooklist" target="_blank" rel="noopener">
              Uniswap hooklist
            </a>{" "}
            repository.
          </p>
        </section>
        <Footer />
      </main>
    </>
  );
}
