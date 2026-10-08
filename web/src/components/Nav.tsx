import Link from "next/link";
import { CHAINS, CHAIN_ORDER } from "@/lib/chains";
import type { ChainId, Tab } from "@/lib/types";
import { Chevron, Mark } from "./Icons";
import s from "./Nav.module.css";

const TABS: Array<[Tab, string]> = [
  ["new", "New"],
  ["trending", "Trending"],
  ["all", "All"],
];

export function feedHref(q: { tab?: Tab; chain?: ChainId; category?: string }): string {
  const p = new URLSearchParams();
  if (q.tab && q.tab !== "trending") p.set("tab", q.tab);
  if (q.chain) p.set("chain", CHAINS[q.chain].slug);
  if (q.category) p.set("category", q.category);
  const qs = p.toString();
  return qs ? `/?${qs}` : "/";
}

export function Nav({ tab, chain, category }: { tab?: Tab; chain?: ChainId; category?: string }) {
  const current = chain ? CHAINS[chain] : null;
  return (
    <header className={s.nav}>
      <Link className={s.brand} href="/" aria-label="Bait home">
        <Mark />
        Bait
      </Link>
      <nav className={s.tabs} role="tablist" aria-label="Feed">
        {TABS.map(([id, label]) => (
          <Link key={id} className={s.tab} role="tab" aria-selected={tab === id} href={feedHref({ tab: id, chain, category })}>
            {label}
          </Link>
        ))}
      </nav>
      <div className={s.right}>
        <details className={s.sel}>
          <summary className={s.pill} aria-label={`Chain: ${current ? current.name : "all chains"}`}>
            {current ? (
              <span className={s.dot} style={{ background: current.color, border: current.lightDot ? "1px solid var(--border)" : undefined }} />
            ) : (
              <span className={s.dot}>∀</span>
            )}
            {current ? current.name : "All chains"}
            <Chevron />
          </summary>
          <div className={s.menu}>
            <Link href={feedHref({ tab, category })} aria-current={!chain}>
              <span className={s.dot} style={{ background: "var(--well)" }} />
              All chains
            </Link>
            {CHAIN_ORDER.map((id) => {
              const c = CHAINS[id];
              return (
                <Link key={id} href={feedHref({ tab, chain: id, category })} aria-current={chain === id}>
                  <span className={s.dot} style={{ background: c.color, border: c.lightDot ? "1px solid var(--border)" : undefined }} />
                  {c.name}
                </Link>
              );
            })}
          </div>
        </details>
        <a className={s.cta} href="https://github.com/Uniswap/hooklist" target="_blank" rel="noopener">
          Submit a hook
        </a>
      </div>
    </header>
  );
}
