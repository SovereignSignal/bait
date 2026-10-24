import type { Metadata } from "next";
import { notFound, permanentRedirect } from "next/navigation";
import { Terminal } from "@/components/Terminal";
import { getFreshness } from "@/db/health";
import { getAllSlugs, getProject, getProjectPage, slugForAlias, usingFixtures } from "@/db/queries";
import { SAMPLE_NOW } from "@/fixtures/sample";
import { feedSeed, movers } from "@/lib/feed";

export const revalidate = 600; // demo window: a first visit is at most ten minutes behind the list
export const dynamicParams = true;

export async function generateStaticParams() {
  if (!process.env.DATABASE_URL) return [];
  const slugs = await getAllSlugs();
  return slugs.map((slug) => ({ slug }));
}

export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }): Promise<Metadata> {
  const { slug } = await params;
  const p = await getProject(slug);
  if (!p) return { title: "Not found" };
  return {
    title: p.name,
    description: p.whatLine,
    openGraph: { title: `${p.name} · Bait`, description: p.whatLine, type: "article" },
    twitter: { card: "summary_large_image", title: `${p.name} · Bait`, description: p.whatLine },
  };
}

export default async function ProjectPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  // the detail and the sidebar from one list load, so ranks and figures agree within the page
  const [{ project: p, rows, version }, fresh] = await Promise.all([getProjectPage(slug), getFreshness()]);
  if (!p) {
    const moved = await slugForAlias(slug);   // renamed or merged since this link was shared
    if (moved) permanentRedirect(`/p/${moved}`);
    notFound();
  }
  const sample = usingFixtures();
  // eslint-disable-next-line react-hooks/purity -- a Server Component renders per request or revalidation, never in the browser
  const now = sample ? SAMPLE_NOW : Date.now();
  // Static: no URL query here, so the list seeds the default tab and the client re-filters from the URL.
  return <Terminal seed={feedSeed(rows, { tab: "trending" }, version)} moverRows={movers(rows, 60)} project={p} view="detail" now={now} sample={sample} fresh={fresh} />;
}
