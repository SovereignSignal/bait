import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Terminal } from "@/components/Terminal";
import { getAllSlugs, getProject, getRows, usingFixtures } from "@/db/queries";
import { SAMPLE_NOW } from "@/fixtures/sample";
import { movers } from "@/lib/feed";

export const revalidate = 3600;
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
  const [p, rows] = await Promise.all([getProject(slug), getRows()]);
  if (!p) notFound();
  const sample = usingFixtures();
  return <Terminal rows={rows} moverRows={movers(rows)} project={p} view="detail" now={sample ? SAMPLE_NOW : Date.now()} sample={sample} />;
}
