// The terminal shell shared by "/" and "/p/[slug]": movers, list | detail | trade, footer.
// List, movers and the back link read the URL on the client; on the statically rendered
// project page they suspend and the fallback renders the default tab.
import Link from "next/link";
import { Suspense } from "react";
import type { FeedSeed } from "@/lib/feed";
import type { Freshness } from "@/lib/health";
import type { LaunchView, ProjectDetail, RowView } from "@/lib/types";
import { Detail, EmptyDetail } from "./Detail";
import { Footer } from "./Footer";
import { BackLink, BackLinkView, Movers, MoversFallback, ProjectList, ProjectListFallback } from "./ProjectList";
import { TradePanel } from "./TradePanel";
import s from "./Terminal.module.css";

export function Terminal({
  seed,
  moverRows,
  project,
  view,
  now,
  sample,
  emptyText,
  launches,
  launchesNow,
  fresh,
}: {
  /** the list rows this URL shows first; the full set comes from /api/rows */
  seed: FeedSeed;
  moverRows: RowView[];
  project: ProjectDetail | null;
  /** phone: "list" on "/", "detail" on "/p/[slug]" */
  view: "list" | "detail";
  now: number;
  sample: boolean;
  emptyText?: string;
  /** "Just launched" panel on top of the New tab's list (only passed when the tab is New) */
  launches?: LaunchView[];
  launchesNow?: number;
  /** age of the figures, from the run receipt and indexer state; null without a database */
  fresh?: Freshness | null;
}) {
  return (
    <div className={s.shell} data-view={view}>
      {view === "list" && (
        <p className={s.intro}>
          Uniswap v4 hooks are add-on contracts that change how a pool trades. Bait shows which ones people actually use,
          grouped by project, with figures from its own on-chain indexer. <Link href="/about">How it works</Link>
        </p>
      )}
      <Suspense fallback={<MoversFallback rows={moverRows} />}>
        <Movers rows={moverRows} />
      </Suspense>
      <main className={s.app} id="main" tabIndex={-1}>
        {view === "list" && <h1 className={s.listTitle}>Hook projects</h1>}
        <Suspense fallback={<ProjectListFallback seed={seed} selected={project?.slug} view={view} now={now} />}>
          <ProjectList seed={seed} selected={project?.slug} view={view} now={now} launches={launches} launchesNow={launchesNow} />
        </Suspense>
        <section className={s.detail} aria-label="Project">
          {view === "detail" && (
            <Suspense fallback={<BackLinkView />}>
              <BackLink />
            </Suspense>
          )}
          {project ? <Detail p={project} now={now} fresh={fresh} /> : <EmptyDetail text={emptyText ?? "Nothing indexed yet."} />}
        </section>
        <aside className={s.side} aria-label="Trade">
          {project && <TradePanel p={project} />}
        </aside>
      </main>
      <Footer sample={sample} through={fresh?.through} />
    </div>
  );
}
