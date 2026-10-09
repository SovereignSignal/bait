"use client";
// Client state shared by the top bar, the list and the dock across navigations:
// search text, the mobile search toggle, and the localStorage watchlist.
import { usePathname } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

const KEY = "bait:watchlist";

interface TerminalState {
  q: string;
  setQ: (q: string) => void;
  /** mobile: the search row is shown */
  searchOpen: boolean;
  /** bumps on every request to focus the search box */
  focusTick: number;
  openSearch: () => void;
  closeSearch: () => void;
  watchOnly: boolean;
  setWatchOnly: (on: boolean) => void;
  /** came from the feed page (for the back link) */
  fromFeed: () => boolean;
  topMatch: string | null;
  setTopMatch: (href: string | null) => void;
  watchlist: string[];
  toggleWatch: (slug: string) => void;
}

const Ctx = createContext<TerminalState | null>(null);

function readList(): string[] {
  try {
    const raw = window.localStorage.getItem(KEY);
    const v: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function writeList(list: string[]) {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(list));
  } catch {
    /* private mode or blocked storage: the watchlist lives for this tab only */
  }
}

export function TerminalProvider({ children }: { children: React.ReactNode }) {
  const [q, setQ] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [focusTick, setFocusTick] = useState(0);
  const [watchOnly, setWatchOnlyState] = useState(false);
  const pathname = usePathname();
  const last = useRef<string | null>(null);
  const fromFeedRef = useRef(false);
  useEffect(() => {
    fromFeedRef.current = last.current === "/";
    last.current = pathname;
    if (pathname?.startsWith("/p/")) setSearchOpen(false);
  }, [pathname]);
  const fromFeed = useCallback(() => fromFeedRef.current, []);
  useEffect(() => setWatchOnlyState(new URLSearchParams(window.location.search).get("w") === "1"), []);
  const setWatchOnly = useCallback((on: boolean) => {
    setWatchOnlyState(on);
    const u = new URL(window.location.href);
    if (on) u.searchParams.set("w", "1");
    else u.searchParams.delete("w");
    window.history.replaceState(null, "", u);
  }, []);
  const [topMatch, setTopMatch] = useState<string | null>(null);
  const [watchlist, setWatchlist] = useState<string[]>([]);

  useEffect(() => {
    setWatchlist(readList());
  }, []);

  const toggleWatch = useCallback((slug: string) => {
    setWatchlist((cur) => {
      const next = cur.includes(slug) ? cur.filter((s) => s !== slug) : [...cur, slug];
      writeList(next);
      return next;
    });
  }, []);

  const openSearch = useCallback(() => {
    setSearchOpen(true);
    setFocusTick((n) => n + 1);
  }, []);
  const closeSearch = useCallback(() => setSearchOpen(false), []);

  const value = useMemo(
    () => ({ q, setQ, searchOpen, focusTick, openSearch, closeSearch, watchOnly, setWatchOnly, fromFeed, topMatch, setTopMatch, watchlist, toggleWatch }),
    [q, searchOpen, focusTick, openSearch, closeSearch, watchOnly, setWatchOnly, fromFeed, topMatch, watchlist, toggleWatch],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTerminal(): TerminalState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useTerminal outside TerminalProvider");
  return v;
}
