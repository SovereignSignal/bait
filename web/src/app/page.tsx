import type { Metadata } from "next";
import { Terminal } from "@/components/Terminal";
import { getProject, getRowSet, usingFixtures } from "@/db/queries";
import { getFreshness } from "@/db/health";
import { getLaunches } from "@/db/radar";
import { SAMPLE_NOW } from "@/fixtures/sample";
import { feedSeed, movers, parseFeedQuery } from "@/lib/feed";
import type { Tab } from "@/lib/types";

export const revalidate = 3600;

const TAB_COPY: Record<Tab, { title: string; description: string }> = {
  trending: { title: "Bait", description: "What's moving in Uniswap v4 hooks this week, grouped by project and ranked by 7-day volume growth." },
  new: { title: "New hooks", description: "Uniswap v4 hook projects first seen in the last 7 days with real usage." },
  verified: { title: "Hooks in the Uniswap app", description: "Uniswap v4 hook projects with at least one pool that should route in the Uniswap app." },
  all: { title: "All hooks", description: "Every Uniswap v4 hook project with real usage, sorted by 7-day volume." },
};

type Search = Record<string, string | string[] | undefined>;

export async function generateMetadata({ searchParams }: { searchParams: Promise<Search> }): Promise<Metadata> {
  const q = parseFeedQuery(await searchParams);
  const c = TAB_COPY[q.tab];
  return { title: q.tab === "trending" ? { absolute: "Bait · v4 hook terminal" } : c.title, description: c.description };
}

export default async function TerminalPage({ searchParams }: { searchParams: Promise<Search> }) {
  const q = parseFeedQuery(await searchParams);
  // The New tab carries the "Just launched" feed (v4_pools); other tabs skip the read.
  const [{ rows, version }, launches, fresh] = await Promise.all([getRowSet(), q.tab === "new" ? getLaunches(20) : Promise.resolve(undefined), getFreshness()]);
  // Only this URL's first rows ride in the page; the client fetches the full set (/api/rows) for search and filters.
  const seed = feedSeed(rows, q, version);
  const first = seed.rows[0];
  const project = first ? await getProject(first.slug) : null;
  const sample = usingFixtures();
  return (
    <Terminal
      seed={seed}
      moverRows={movers(rows, 60)}
      project={project}
      view="list"
      now={sample ? SAMPLE_NOW : Date.now()}
      sample={sample}
      emptyText={rows.length ? "Pick a project from the list to see its pools, wallets and live trades." : "Nothing indexed yet. The first pipeline run fills this in."}
      launches={launches}
      launchesNow={Date.now()}
      fresh={fresh}
    />
  );
}
