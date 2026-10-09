"use client";
// Phone-only bottom dock: Feed, Search, Building, Watchlist, Leaders.
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { DockSearchIcon, HomeIcon, LeadersIcon, RadarIcon, StarIcon } from "./Icons";
import { useTerminal } from "./TerminalProvider";
import s from "./Terminal.module.css";

export function Dock() {
  const t = useTerminal();
  const pathname = usePathname();
  const router = useRouter();
  const onFeed = pathname === "/";

  /** Back to the list, keeping the current tab / chain / category. */
  const toFeed = () => {
    const qs = typeof window !== "undefined" && (onFeed || pathname?.startsWith("/p/")) ? window.location.search : "";
    if (!onFeed) router.push(`/${qs}`);
  };

  const current = pathname === "/leaders" ? "leaders" : pathname === "/building" ? "building" : t.searchOpen ? "search" : onFeed && t.watchOnly ? "watch" : onFeed ? "feed" : null;

  return (
    <nav className={s.dock} aria-label="Sections">
      <button
        type="button"
        aria-current={current === "feed" ? "true" : undefined}
        onClick={() => {
          t.setWatchOnly(false);
          t.closeSearch();
          toFeed();
        }}
      >
        <HomeIcon />
        Feed
      </button>
      <button
        type="button"
        aria-current={current === "search" ? "true" : undefined}
        onClick={() => {
          if (t.searchOpen) return t.closeSearch();
          t.openSearch();
          if (!onFeed) toFeed();
        }}
      >
        <DockSearchIcon />
        Search
      </button>
      <Link href="/building" aria-current={current === "building" ? "page" : undefined} onClick={() => t.closeSearch()}>
        <RadarIcon />
        Radar
      </Link>
      <button
        type="button"
        aria-current={current === "watch" ? "true" : undefined}
        onClick={() => {
          t.closeSearch();
          t.setWatchOnly(true);
          toFeed();
        }}
      >
        <StarIcon />
        Watchlist
      </button>
      <Link href="/leaders" aria-current={current === "leaders" ? "page" : undefined} onClick={() => t.closeSearch()}>
        <LeadersIcon />
        Leaders
      </Link>
    </nav>
  );
}
