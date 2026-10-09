"use client";
// The terminal's left column and the movers strip. Rows ship to the client once;
// tab / category / chain come from the URL, search and watchlist from TerminalProvider.
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { CHAINS } from "@/lib/chains";
import { applyTab, availableCategories, feedHref, moversFor, parseFeedQuery, projectHref, searchRows } from "@/lib/feed";
import { ago, NONE, usd } from "@/lib/format";
import { categoryShort, chainNames, deltaText, rowMetric } from "@/lib/present";
import { CATEGORY_SHORT, TABS, type FeedQuery, type LaunchView, type RowView } from "@/lib/types";
import { ProjectAvatar } from "./Avatar";
import { StarIcon } from "./Icons";
import { JustLaunched } from "./JustLaunched";
import { useTerminal } from "./TerminalProvider";
import s from "./Terminal.module.css";

const DEFAULT_QUERY: FeedQuery = { tab: "trending" };

export function useFeedQuery(): FeedQuery {
  return parseFeedQuery(useSearchParams());
}

function Delta({ r }: { r: RowView }) {
  if (r.isNew) return <span className="b b-p">New</span>;
  const d = deltaText(r);
  if (!d) return <span className="mut3">{NONE}</span>;
  return <span className={d.up ? "up" : "down"}>{d.text}</span>;
}

// ---------- list ----------

type ListProps = { rows: RowView[]; selected?: string; now: number; launches?: LaunchView[]; launchesNow?: number };

export function ProjectList(props: ListProps) {
  return <ProjectListView {...props} query={useFeedQuery()} />;
}

/** Suspense fallback on statically rendered pages (no URL yet): the default tab. */
export function ProjectListFallback(props: { rows: RowView[]; selected?: string; now: number }) {
  return <ProjectListView {...props} query={DEFAULT_QUERY} />;
}

function ProjectListView({ rows, selected, now, query, launches, launchesNow }: ListProps & { query: FeedQuery }) {
  const t = useTerminal();
  const watching = t.watchOnly;
  const cats = availableCategories(rows);
  const searching = t.q.trim().length > 0;
  // Search spans every project (chain / category filters still apply), not just the current tab.
  let list: Array<RowView & { rank?: number }> = watching
    ? rows.filter((r) => t.watchlist.includes(r.slug)).sort((a, b) => b.vol7d - a.vol7d)
    : applyTab(rows, searching ? { ...query, tab: "all" } : query);
  list = searchRows(list, t.q);
  const tabForRows = watching || searching ? "all" : query.tab;

  return (
    <aside className={`panel ${s.list}`} aria-label="Hook projects">
      <div className={s.tabs} role="tablist">
        {TABS.map(([id, label]) => (
          <Link
            key={id}
            className={s.tab}
            role="tab"
            aria-selected={!watching && !searching && query.tab === id}
            href={feedHref({ ...query, tab: id })}
            onClick={() => t.setWatchOnly(false)}
          >
            {label}
          </Link>
        ))}
        <button
          type="button"
          className={`${s.tab} ${s.watchTab}`}
          aria-pressed={watching}
          aria-label={`Watchlist, ${t.watchlist.length} saved`}
          onClick={() => t.setWatchOnly(!watching)}
        >
          <StarIcon size={14} filled={watching} />
          <span className="num">{t.watchlist.length}</span>
        </button>
      </div>
      {!watching && (
        <div className={s.cats} role="group" aria-label="Category">
          <Link className={s.chip} aria-current={!query.category} href={feedHref({ ...query, category: undefined })}>
            All
          </Link>
          {cats.map((c) => (
            <Link key={c} className={s.chip} aria-current={query.category === c} href={feedHref({ ...query, category: c })}>
              {CATEGORY_SHORT[c]}
            </Link>
          ))}
        </div>
      )}
      {(watching || searching) && (
        <div className={s.listHead}>
          <span>{watching ? `Watchlist · ${list.length}` : `${list.length} match${list.length === 1 ? "" : "es"} for “${t.q.trim()}”`}</span>
          <button type="button" onClick={() => (watching ? t.setWatchOnly(false) : t.setQ(""))}>
            {watching ? "Show all" : "Clear"}
          </button>
        </div>
      )}
      <div className={s.rows}>
        {launches && query.tab === "new" && !watching && !searching && <JustLaunched initial={launches} now={launchesNow ?? now} chainId={query.chain} compact />}
        {list.length === 0 ? (
          <div className="empty">
            {watching ? "Star a project to keep it here. Saved on this device only." : rows.length ? "Nothing matches this filter." : "Nothing indexed yet."}
          </div>
        ) : (
          list.map((r, i) => {
            const m = rowMetric(r, tabForRows === "new");
            const sub = r.whatLine || [r.category !== "other" ? categoryShort(r.category) : "", chainNames(r.chains)].filter(Boolean).join(" · ");
            return (
              <Link key={r.slug} className={s.row} href={projectHref(r.slug, query)} aria-current={r.slug === selected ? "true" : undefined}>
                <ProjectAvatar name={r.name} color={r.color} chain={r.chains[0]} />
                <div className={s.meta}>
                  <div className={s.n}>
                    {tabForRows === "trending" && <span className={`${s.rank} num`}>{r.rank ?? i + 1}</span>}
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
          })
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
  const chain = query.chain && CHAINS[query.chain] ? query.chain : undefined;
  return (
    <Link className={s.back} href={feedHref({ ...query, chain })}>
      ← All hooks
    </Link>
  );
}
