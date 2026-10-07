import type { Metadata } from "next";
import { ContextTiles } from "@/components/ContextTiles";
import { Filters } from "@/components/Filters";
import { Footer } from "@/components/Footer";
import { Nav } from "@/components/Nav";
import { ProjectCard } from "@/components/ProjectCard";
import { getContext, getFeed, usingFixtures } from "@/db/queries";
import { parseChainParam } from "@/lib/chains";
import { CATEGORIES, isCategory, type Tab } from "@/lib/types";
import { SAMPLE_NOW } from "@/fixtures/sample";

export const revalidate = 3600;

const TAB_COPY: Record<Tab, { h1: string; p: string }> = {
  trending: { h1: "What's moving in v4 hooks this week", p: "Grouped by project. Ranked by 7-day volume growth. Refreshed nightly." },
  new: { h1: "New hooks worth a look", p: "First seen in the last 7 days with at least $1K in a pool or 10 swaps. Refreshed nightly." },
  all: { h1: "Every hook project with real usage", p: "Grouped by project. Sorted by 7-day volume. Spam and empty hooks hidden." },
};

type Search = { tab?: string; chain?: string; category?: string };

export async function generateMetadata({ searchParams }: { searchParams: Promise<Search> }): Promise<Metadata> {
  const sp = await searchParams;
  const tab = parseTab(sp.tab);
  return { title: tab === "trending" ? "Hook Feed" : `${TAB_COPY[tab].h1}`, description: TAB_COPY[tab].p };
}

function parseTab(s: string | undefined): Tab {
  return s === "new" || s === "all" ? s : "trending";
}

export default async function FeedPage({ searchParams }: { searchParams: Promise<Search> }) {
  const sp = await searchParams;
  const tab = parseTab(sp.tab);
  const chain = parseChainParam(sp.chain);
  const category = isCategory(sp.category) ? sp.category : undefined;
  const [rows, ctx] = await Promise.all([getFeed({ tab, chain, category }), getContext()]);
  const fixtures = usingFixtures();
  const now = fixtures ? SAMPLE_NOW : Date.now();
  const unfiltered = chain || category ? await getFeed({ tab }) : rows;
  const available = CATEGORIES.filter((c) => unfiltered.some((p) => p.category === c || p.secondaryCategories.includes(c)));
  const copy = TAB_COPY[tab];

  return (
    <>
      <Nav tab={tab} chain={chain} category={category} />
      <main className="col">
        <div className="lead">
          <h1>{copy.h1}</h1>
          <p>{copy.p}</p>
        </div>
        <ContextTiles c={ctx} />
        <Filters tab={tab} chain={chain} category={category} available={available} note={fixtures ? "Sample figures" : undefined} />
        {rows.length === 0 && <p className="empty">Nothing here yet for this filter.</p>}
        {rows.map((p) => (
          <ProjectCard key={p.id} p={p} now={now} />
        ))}
        <Footer />
      </main>
    </>
  );
}
