"use client";
// Client state shared by the top bar, the list and the dock across navigations:
// search text, the mobile search toggle, and the localStorage watchlist.
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

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
  const [watchOnly, setWatchOnly] = useState(false);
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
    () => ({ q, setQ, searchOpen, focusTick, openSearch, closeSearch, watchOnly, setWatchOnly, watchlist, toggleWatch }),
    [q, searchOpen, focusTick, openSearch, closeSearch, watchOnly, watchlist, toggleWatch],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTerminal(): TerminalState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useTerminal outside TerminalProvider");
  return v;
}
