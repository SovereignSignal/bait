"use client";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef } from "react";
import { CHAINS } from "@/lib/chains";
import { feedQs, parseFeedQuery } from "@/lib/feed";
import type { ChainId } from "@/lib/types";
import { Mark, SearchIcon } from "./Icons";
import { useTerminal } from "./TerminalProvider";
import s from "./Terminal.module.css";

function isTerminalPath(p: string | null): boolean {
  return p === "/" || !!p?.startsWith("/p/");
}

/** Brand, search (⌘K), chain select, Submit a hook. The same bar on every page. */
export function TopBar({ chains }: { chains: ChainId[] }) {
  const t = useTerminal();
  const pathname = usePathname();
  const router = useRouter();
  const input = useRef<HTMLInputElement>(null);
  const onTerminal = isTerminalPath(pathname);

  useEffect(() => {
    if (t.focusTick > 0) input.current?.focus();
  }, [t.focusTick]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        t.openSearch();
        input.current?.focus();
      } else if (e.key === "Escape" && document.activeElement === input.current) {
        input.current?.blur();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [t]);

  const onChange = (v: string) => {
    t.setQ(v);
    // The list lives on the terminal; searching from another page goes there.
    if (!onTerminal && v.trim()) router.push(`/${window.location.search}`);
  };

  return (
    <header className={s.top} data-search={t.searchOpen ? "open" : undefined}>
      <Link className={s.brand} href="/" aria-label="Bait home">
        <Mark />
        bait
      </Link>
      <label className={s.search}>
        <SearchIcon />
        <input
          ref={input}
          type="search"
          value={t.q}
          onChange={(e) => onChange(e.target.value)}
          placeholder="Search hooks, projects or addresses"
          aria-label="Search hooks, projects or addresses"
          autoComplete="off"
          spellCheck={false}
        />
        <span className={s.kbd} aria-hidden="true">
          ⌘K
        </span>
      </label>
      <nav className={s.links} aria-label="Pages">
        <Link href="/leaders" aria-current={pathname === "/leaders" ? "page" : undefined}>
          Leaders
        </Link>
        <Link href="/week" aria-current={pathname?.startsWith("/week") ? "page" : undefined}>
          Weekly
        </Link>
      </nav>
      <Suspense fallback={<ChainSelectView chains={chains} value="" onChange={() => {}} />}>
        <ChainSelect chains={chains} />
      </Suspense>
      <a className={`${s.pill} ${s.pink}`} href="https://github.com/Uniswap/hooklist" target="_blank" rel="noopener">
        Submit a hook
      </a>
    </header>
  );
}

function ChainSelect({ chains }: { chains: ChainId[] }) {
  const sp = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();
  const q = parseFeedQuery(sp);
  const value = q.chain && CHAINS[q.chain] ? CHAINS[q.chain].slug : "";
  return (
    <ChainSelectView
      chains={chains}
      value={value}
      onChange={(slug) => {
        const chain = Object.values(CHAINS).find((c) => c.slug === slug)?.id;
        const base = pathname?.startsWith("/p/") ? pathname : "/";
        router.push(`${base}${feedQs({ ...q, chain })}`);
      }}
    />
  );
}

function ChainSelectView({ chains, value, onChange }: { chains: ChainId[]; value: string; onChange: (slug: string) => void }) {
  return (
    <select className={`${s.pill} ${s.chainSel}`} aria-label="Chain" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">All chains</option>
      {chains
        .filter((id) => CHAINS[id])
        .map((id) => (
          <option key={id} value={CHAINS[id].slug}>
            {CHAINS[id].name}
          </option>
        ))}
    </select>
  );
}
