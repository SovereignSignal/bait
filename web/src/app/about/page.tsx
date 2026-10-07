import type { Metadata } from "next";
import { Footer } from "@/components/Footer";
import { Nav } from "@/components/Nav";

export const metadata: Metadata = { title: "About", description: "How Hook Feed finds, groups, scores and labels Uniswap v4 hooks." };

export default function AboutPage() {
  return (
    <>
      <Nav />
      <main className="col">
        <div className="lead">
          <h1>How this works</h1>
          <p>One feed for Uniswap v4 hooks. Cards are grouped by project, not by hook address. Refreshed nightly.</p>
        </div>

        <section className="block">
          <h2>Method</h2>
          <p>
            Identity comes from the public <a className="inline" href="https://github.com/Uniswap/hooklist" target="_blank" rel="noopener">hooklist</a> registry. Usage comes from the
            Uniswap v4 subgraphs on The Graph, one per chain: every pool with a non-zero hook address, its TVL, cumulative volume and
            transaction count, snapshotted daily. Source verification comes from Blockscout. Smart-money labels come from Nansen, on the
            LPs and swappers of each project&apos;s top pools, and only on chains Nansen covers (not Unichain or Ink). Links come from Exa,
            last 30 days. Team, site and audit details for new projects come from Parallel. The one-line &quot;what it does&quot; and the
            two-sentence &quot;why people care&quot; are written by Claude from those sources and regenerated when the signals change.
          </p>
          <h3>Grouping</h3>
          <p>
            Thousands of hook addresses collapse into a few hundred projects. Same normalized name, same project. Same bytecode hash, same
            project. The long tail is classified once by a small model into a project and a category. Factories that deploy one hook per pool
            (EulerSwap, Saturn) are one card with the hook count as a stat.
          </p>
          <h3>Categories</h3>
          <p className="muted">Dynamic fee · Launchpad · Orders (limit, TWAMM) · LP management · Compliance · Oracle · MEV / LVR · Lending · Gaming / NFT · Other</p>
        </section>

        <section className="block">
          <h2>Scoring</h2>
          <ul>
            <li>
              <b>New</b>: project first seen within 7 days, and either a pool above $1K TVL or at least 10 swaps in the week.
            </li>
            <li>
              <b>Trending</b>: 7-day volume of at least $10K. Score is <code>log1p(vol_7d) × clamp(vol_7d / max(vol_7d_prev, 1000), 0.5, 10)</code>, tie-break on TVL.
              Top 20.
            </li>
            <li>
              <b>Alive</b>: a GitHub commit or a web mention in the last 30 days.
            </li>
            <li>
              <b>Hidden from All</b>: no pools, or every pool under $100 TVL with fewer than 10 swaps lifetime.
            </li>
            <li>
              <b>Txs 7d</b> counts swaps plus liquidity modifications. The subgraph&apos;s pool counter increments on both, so we label it as transactions rather than
              swaps.
            </li>
          </ul>
        </section>

        <section className="block">
          <h2>Routable</h2>
          <p>
            The Uniswap app only routes through hooked pools whose hook is allowlisted. Per Uniswap&apos;s published rule, a hook is allowed automatically unless its address
            starts with <code>0x91</code>, or it uses <code>beforeSwapReturnsDelta</code>, <code>afterSwapReturnsDelta</code> or a dynamic fee. Hook Feed computes this from the
            published flags and also treats <code>afterAddLiquidityReturnsDelta</code> and <code>afterRemoveLiquidityReturnsDelta</code> as blocking, which is stricter than the
            published rule. Hooks that Uniswap Labs reviewed and allowlisted by hand will still show as not routable here, because no public list of manual approvals exists.
            The badge is a rule, not a guarantee. The pink &quot;Swap on Uniswap&quot; button only appears when the rule says a pool should route.
          </p>
        </section>

        <section className="block">
          <h2>Disclaimers</h2>
          <ul>
            <li>Hooks are third-party code in the swap path. Nothing here is audited by us.</li>
            <li>Flags and properties are shown as published in the hooklist registry. We do not verify them against bytecode.</li>
            <li>&quot;Audited&quot; means an audit link is published. It is not a statement about the audit&apos;s scope or findings.</li>
            <li>Smart-money counts are Nansen labels on addresses that interacted with a project&apos;s top pools. They are not investment signals.</li>
            <li>Figures are derived from public subgraphs and can lag or be wrong when a subgraph halts. Metrics are USD estimates.</li>
            <li>Hook Feed is a personal project, not affiliated with Uniswap Labs or the Uniswap Foundation.</li>
          </ul>
        </section>
        <Footer />
      </main>
    </>
  );
}
