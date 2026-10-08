import Link from "next/link";

export function Footer({ sample = false }: { sample?: boolean }) {
  return (
    <p className="foot">
      {sample && <b>Sample figures (BAIT_FIXTURES). </b>}
      Built from GeckoTerminal pools, the Uniswap v4 subgraphs and the Uniswap hooklist, refreshed nightly; live trades from GeckoTerminal. Swap buttons open the pool on
      app.uniswap.org; Bait never holds funds. Hooks are third-party code and nothing here is audited by Bait. The routable badge is a rule, not a guarantee.{" "}
      <Link href="/about">About</Link> · <Link href="/building">Building</Link> · <Link href="/leaders">Leaders</Link> · <Link href="/week">Weekly</Link> · <a href="/feed.xml">RSS</a> ·{" "}
      <a href="https://github.com/Uniswap/hooklist" target="_blank" rel="noopener">
        hooklist
      </a>
    </p>
  );
}
