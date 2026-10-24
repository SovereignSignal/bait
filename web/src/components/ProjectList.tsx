"use client";
// The terminal's left column and the movers strip. A page ships only the rows its URL shows first (FeedSeed); the full
// set comes from /api/rows once per visit. Tab / category / chain live in the URL but change on the client: the list
// re-filters at once and history.pushState moves the URL, which useSearchParams follows, so links and the back button
// still work. Search and watchlist come from TerminalProvider.
import Link from "next/link";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { loadHookIndex } from "@/lib/hook-index";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { CHAINS } from "@/lib/chains";
import { emptyCopy, feedHref, feedHrefAt, feedQs, LIST_PAGE, listFor, moversFor, parseFeedQuery, projectHref, sameQuery, type FeedSeed } from "@/lib/feed";
import { ago, NONE, usd } from "@/lib/format";
import { categoryAndChains, deltaText, rowLabel, rowMetric } from "@/lib/present";
import { loadRows, peekRows } from "@/lib/rows";
import { CATEGORY_SHORT, TAB_SHORT, TABS, type FeedQuery, type HookIndex, type LaunchView, type RowView } from "@/lib/types";
import { ProjectAvatar } from "./Avatar";
import { StarIcon } from "./Icons";
import { JustLaunched } from "./JustLaunched";
import { useTerminal } from "./TerminalProvider";
import s from "./Terminal.module.css";

const DEFAULT_QUERY: FeedQuery = { tab: "trending" };

const subscribeNever = () => () => {};

/** The hook address index for 0x search: requested when a query starts with "0x", again on a later keystroke if the last
 * request failed. `loading` is true while an address query waits for it. */
function useHookIndex(q: string): { index?: HookIndex; loading: boolean } {
  const wanted = q.trim().toLowerCase().startsWith("0x");
  const [index, setIndex] = useState<HookIndex>();
  useEffect(() => {
    if (!wanted || index) return;
    let live = true;
    loadHookIndex()
      .then((x) => live && setIndex(x))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [wanted, index, q]);
  return { index, loading: wanted && !index };
}

/** Every listed project, for search, the watchlist and any view but the one the page seeded. Already here after a
 * client navigation (peekRows); otherwise fetched on mount, so it has landed by the time anyone clicks. */
function useAllRows(version: string): { rows?: RowView[]; failed: boolean; retry: () => void } {
  const [rows, setRows] = useState<RowView[] | undefined>(peekRows);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    loadRows(version).then(
      (r) => live && setRows(r),
      () => live && setFailed(true),
    );
    return () => {
      live = false;
    };
  }, [version, attempt]);
  const retry = useCallback(() => {
    setFailed(false);
    setAttempt((n) => n + 1);
  }, []);
  return { rows, failed, retry };
}

export function useFeedQuery(): FeedQuery {
  return parseFeedQuery(useSearchParams());
}

/** A plain left click; a modified one (new tab, new window, download) keeps the link's own behaviour. */
function plainClick(e: React.MouseEvent): boolean {
  return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;
}

function Delta({ r }: { r: RowView }) {
  if (r.isWash)
    return (
      <span className="b b-r" title={r.washHistorical ? "Historical wash-like volume: an earlier finding, not evidence of wash in the current window." : "Wash-like volume: listed here, never ranked."}>
        {r.washHistorical ? "Past wash" : "Wash"}
      </span>
    );
  if (r.isDormant) return <span className="b b-n">Dormant</span>;
  if (r.activityStatus === "unknown" && r.hasHistoricalUsage) return <span className="b b-n" title="Retained on historical usage; recent coverage cannot establish zero activity.">Unknown</span>;
  if (r.isNew) return <span className="b b-p">New</span>;
  const d = deltaText(r);
  if (!d) return <span className="mut3">{NONE}</span>;
  return <span className={d.up ? "up" : "down"}>{d.text}</span>;
}

// ---------- list ----------

type ListProps = { seed: FeedSeed; selected?: string; view: "list" | "detail"; now: number; launches?: LaunchView[]; launchesNow?: number };

export function ProjectList(props: ListProps) {
  return <ProjectListView {...props} query={useFeedQuery()} />;
}

/** Suspense fallback on statically rendered pages (no URL yet): the default tab. */
export function ProjectListFallback(props: { seed: FeedSeed; selected?: string; view: "list" | "detail"; now: number }) {
  return <ProjectListView {...props} query={DEFAULT_QUERY} />;
}

function ProjectListView({ seed, selected, view, now, query: urlQuery, launches, launchesNow }: ListProps & { query: FeedQuery }) {
  const t = useTerminal();
  const pathname = usePathname();
  const all = useAllRows(seed.version);
  // The view a click picked, shown in the same frame; Next applies the pushed URL in a transition a moment later.
  // Dropped once the URL catches up (not on any URL change: two quick clicks would flash the first) or on Back/Forward.
  const [picked, setPicked] = useState<FeedQuery | null>(null);
  const urlKey = feedQs(urlQuery);
  const [seenKey, setSeenKey] = useState(urlKey);
  if (urlKey !== seenKey) {
    setSeenKey(urlKey);
    if (picked && feedQs(picked) === urlKey) setPicked(null);
  }
  useEffect(() => {
    const drop = () => setPicked(null);
    window.addEventListener("popstate", drop);
    return () => window.removeEventListener("popstate", drop);
  }, []);
  const query = picked ?? urlQuery;

  // Letters typed before hydration reach `q` from the top bar's effect, which can run before this Suspense boundary
  // hydrates; the boundary must still render what the server did, so search and watchlist wait for the mount.
  const hydrated = useSyncExternalStore(subscribeNever, () => true, () => false);
  const watching = hydrated && t.watchOnly;
  // The provider can restore storage before this streamed boundary hydrates. Its first count must match SSR.
  const watchCount = hydrated ? t.watchlist.length : 0;
  const searching = hydrated && t.q.trim().length > 0;
  const hooks = useHookIndex(t.q);
  // Before /api/rows lands, the seeded view is the only one we can draw; any other waits a moment for the full set.
  const fromSeed = !all.rows && !watching && !searching && sameQuery(seed.query, query);
  const list: Array<RowView & { rank?: number }> | undefined = all.rows
    ? listFor(all.rows, query, { search: t.q, watchlist: watching ? t.watchlist : undefined, hooks: hooks.index })
    : fromSeed
      ? seed.rows
      : undefined;
  const total = all.rows ? (list?.length ?? 0) : fromSeed ? seed.total : 0;
  const count = all.rows?.length ?? seed.count;

  const topHref = searching && list?.[0] ? projectHref(list[0].slug, query) : null;
  const { setTopMatch } = t;
  useEffect(() => {
    setTopMatch(topHref);
    return () => setTopMatch(null);
  }, [topHref, setTopMatch]);
  const tabForRows = watching || searching ? "all" : query.tab;

  // "Show more" pages through the view; a different view starts again at one page, scrolled to the top.
  const viewKey = `${feedQs(query)}|${watching ? "w" : ""}|${t.q.trim()}`;
  const [more, setMore] = useState({ key: viewKey, n: LIST_PAGE });
  const limit = more.key === viewKey ? more.n : LIST_PAGE;
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (box.current) box.current.scrollTop = 0;
  }, [viewKey]);

  /** Tab, category and "clear" links: re-filter here and push the URL, no server round trip. */
  const pick = (e: React.MouseEvent, next: FeedQuery) => {
    if (watching) t.setWatchOnly(false);
    if (!plainClick(e)) return;
    e.preventDefault();
    if (sameQuery(next, query)) return; // the view on screen: no history entry
    setPicked(next);
    window.history.pushState(null, "", feedHrefAt(pathname, next));
  };
  const feedLink = (next: FeedQuery) => ({ href: feedHrefAt(pathname, next), prefetch: false, onClick: (e: React.MouseEvent) => pick(e, next) });

  return (
    <aside className={`panel ${s.list}`} aria-label="Hook projects">
      <nav className={s.tabs} aria-label="Feed views">
        {TABS.map(([id, label]) => (
          <Link
            key={id}
            className={s.tab}
            aria-current={!watching && !searching && query.tab === id ? "page" : undefined}
            aria-label={TAB_SHORT[id] && label}
            title={TAB_SHORT[id] && label}
            {...feedLink({ ...query, tab: id })}
          >
            {TAB_SHORT[id] ?? label}
          </Link>
        ))}
        <button
          type="button"
          className={`${s.tab} ${s.watchTab}`}
          aria-pressed={watching}
          aria-label={`Watchlist, ${watchCount} saved`}
          onClick={() => t.setWatchOnly(!watching)}
        >
          <StarIcon size={14} filled={watching} />
          <span className="num">{watchCount}</span>
        </button>
      </nav>
      {!watching && !searching && query.tab === "trending" && (
        <p className={s.rankNote}>
          Ranked by 7-day volume growth; wash-like volume never ranks. <Link href="/about#scoring">How</Link>
        </p>
      )}
      {!watching && (
        <div className={s.cats} role="group" aria-label="Category">
          <Link className={s.chip} aria-current={!query.category} {...feedLink({ ...query, category: undefined })}>
            All
          </Link>
          {seed.cats.map((c) => (
            <Link key={c} className={s.chip} aria-current={query.category === c} {...feedLink({ ...query, category: c })}>
              {CATEGORY_SHORT[c]}
            </Link>
          ))}
        </div>
      )}
      {(watching || searching) && (
        <div className={s.listHead}>
          <span>
            {watching
              ? `Watchlist · ${t.watchlist.length}`
              : !list || (hooks.loading && list.length === 0)
                ? `Looking up “${t.q.trim()}”…`
                : `${list.length} match${list.length === 1 ? "" : "es"} for “${t.q.trim()}”`}
          </span>
          <button type="button" onClick={() => (watching ? t.setWatchOnly(false) : t.setQ(""))}>
            {watching ? "Show all" : "Clear"}
          </button>
        </div>
      )}
      <div className={s.rows} ref={box} aria-busy={list ? undefined : true}>
        {view === "list" && query.tab === "new" && !watching && !searching && <JustLaunched initial={launches} now={launchesNow ?? now} chainId={query.chain} compact />}
        {watching && t.watchlist.length === 0 ? (
          <div className="empty">
            <p>Star a project to keep it here.</p>
            <p className="mut3">Saved on this device only.</p>
          </div>
        ) : !list ? (
          <div className="empty">
            {all.failed ? (
              <>
                <p>Couldn&apos;t load the project list.</p>
                <button type="button" className="linkBtn" onClick={all.retry}>
                  Try again
                </button>
              </>
            ) : (
              "Loading projects…"
            )}
          </div>
        ) : list.length === 0 ? (
          <div className="empty">
            {watching ? (
              "None of your starred projects is listed right now."
            ) : searching && hooks.loading ? (
              "Loading hook addresses…"
            ) : searching ? (
              "Try a token symbol or 0x address."
            ) : count ? (
              <>
                <p>{emptyCopy(query)}</p>
                <p>
                  {query.category && <Link {...feedLink({ ...query, category: undefined })}>Clear category</Link>}
                  {query.category && query.chain && " · "}
                  {query.chain && <Link {...feedLink({ ...query, chain: undefined })}>All chains</Link>}
                </p>
              </>
            ) : (
              "Nothing indexed yet."
            )}
          </div>
        ) : (
          <>
            {list.slice(0, limit).map((r, i) => {
              const m = rowMetric(r, tabForRows === "new");
              const sub = r.whatLine || categoryAndChains(r.category, r.chains);
              return (
                <Link
                  key={r.slug}
                  className={s.row}
                  href={projectHref(r.slug, query)}
                  aria-label={rowLabel(r, tabForRows === "trending" ? (r.rank ?? i + 1) : undefined, m)}
                  data-selected={r.slug === selected ? "true" : undefined}
                  aria-current={r.slug === selected && view === "detail" ? "page" : undefined}
                >
                  <ProjectAvatar name={r.name} color={r.color} chain={r.chains[0]} />
                  <div className={s.meta}>
                    <div className={s.n}>
                      {tabForRows === "trending" && <span className={`${s.rank} num`} aria-hidden="true">{r.rank ?? i + 1}</span>}
                      {r.name}
                    </div>
                    <div className={s.s}>{sub}</div>
                  </div>
                  <div className={s.r}>
                    <div className={`${s.v} num`}>
                      {usd(m.v)}
                      <span className={s.win}>{m.win}</span>
                    </div>
                    <div className={`${s.d} num ${tabForRows === "new" ? "mut" : ""}`}>{tabForRows === "new" ? ago(r.firstSeenAt, now) : <Delta r={r} />}</div>
                  </div>
                </Link>
              );
            })}
            {total > limit && (
              // past the seeded page before /api/rows lands: say so until it does
              <button type="button" className={s.more} disabled={limit > list.length} onClick={() => setMore({ key: viewKey, n: limit + LIST_PAGE })}>
                {limit > list.length ? "Loading…" : `Show ${Math.min(LIST_PAGE, total - limit)} more · ${total.toLocaleString("en-US")} in all`}
              </button>
            )}
          </>
        )}
      </div>
    </aside>
  );
}

// ---------- movers ----------

export function Movers({ rows }: { rows: RowView[] }) {
  return <MoversView rows={rows} query={useFeedQuery()} />;
}

export function MoversFallback({ rows }: { rows: RowView[] }) {
  return <MoversView rows={rows} query={DEFAULT_QUERY} />;
}

function MoversView({ rows, query }: { rows: RowView[]; query: FeedQuery }) {
  const shown = moversFor(rows, query.chain);
  if (!shown.length) return null;
  return (
    <section className={s.movers} aria-label="Top movers this week">
      {shown.map((r) => {
        const m = rowMetric(r, r.isNew);
        return (
          <Link key={r.slug} className={s.mover} href={projectHref(r.slug, query)}>
            <span className={s.who}>
              <ProjectAvatar name={r.name} color={r.color} chain={r.chains[0]} size="sm" />
              {r.name}
            </span>
            <span className={s.what}>
              <span className={`${s.big} num`}>
                {usd(m.v)}
                <span className={s.win}>{m.win}</span>
              </span>
              <span className={`${s.delta} num`}>
                <Delta r={r} />
              </span>
            </span>
          </Link>
        );
      })}
    </section>
  );
}

// ---------- back link (phone) ----------

export function BackLink() {
  return <BackLinkView query={useFeedQuery()} />;
}

export function BackLinkView({ query = DEFAULT_QUERY }: { query?: FeedQuery }) {
  const t = useTerminal();
  const router = useRouter();
  const chain = query.chain && CHAINS[query.chain] ? query.chain : undefined;
  return (
    <Link
      className={s.back}
      href={feedHref({ ...query, chain })}
      onClick={(e) => {
        if (t.fromFeed()) {
          e.preventDefault();
          router.back();
        }
      }}
    >
      ← {TABS.find(([id]) => id === query.tab)?.[1] ?? "Feed"}
    </Link>
  );
}
