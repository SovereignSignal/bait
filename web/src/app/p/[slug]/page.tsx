import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { Footer } from "@/components/Footer";
import { HookList } from "@/components/HookList";
import { Nav } from "@/components/Nav";
import { PoolsTable } from "@/components/PoolsTable";
import { ProjectCard } from "@/components/ProjectCard";
import { Sparkline } from "@/components/Sparkline";
import { getAllSlugs, getProject, usingFixtures } from "@/db/queries";
import { SAMPLE_NOW } from "@/fixtures/sample";
import { ago, age, longDate, sourceLabel } from "@/lib/format";
import { CHAINS } from "@/lib/chains";

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
    openGraph: { title: `${p.name} · Hook Feed`, description: p.whatLine, type: "article" },
    twitter: { card: "summary_large_image", title: `${p.name} · Hook Feed`, description: p.whatLine },
  };
}

export default async function ProjectPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const p = await getProject(slug);
  if (!p) notFound();
  const now = usingFixtures() ? SAMPLE_NOW : Date.now();
  const tvl = p.history.map((h) => ({ day: h.day, value: h.tvlUsd }));
  const vol = p.history.map((h) => ({ day: h.day, value: h.vol7d }));

  return (
    <>
      <Nav />
      <main className="col">
        <ProjectCard p={p} variant="header" now={now} />

        <section className="block" aria-labelledby="pools">
          <h2 id="pools">Pools</h2>
          <PoolsTable pools={p.pools} total={p.metrics.poolsN} />
        </section>

        {p.history.length > 1 && (
          <section className="block" aria-labelledby="history">
            <h2 id="history">Last 30 days</h2>
            <Sparkline title="TVL" points={tvl} />
            <Sparkline title="7d volume" points={vol} />
          </section>
        )}

        <section className="block" aria-labelledby="hooks">
          <h2 id="hooks">
            Hooks <span className="muted">· {p.metrics.hooksN.toLocaleString("en-US")}</span>
          </h2>
          <HookList hooks={p.hooks} total={p.metrics.hooksN} />
        </section>

        <section className="block" aria-labelledby="signals">
          <h2 id="signals">Signals</h2>
          <ul>
            <li>
              Smart money:{" "}
              {p.signals.smartMoneyLps === null
                ? `not covered by Nansen on ${p.chains.map((c) => CHAINS[c].name).join(", ")}`
                : `${p.signals.smartMoneyLps} labelled LP${p.signals.smartMoneyLps === 1 ? "" : "s"} in the top pools`}
            </li>
            <li>GitHub: {p.signals.lastCommitAt ? `last commit ${ago(p.signals.lastCommitAt, now)}` : "no repo linked"}</li>
            <li>Web: {p.signals.lastExaHitAt ? `last mention ${ago(p.signals.lastExaHitAt, now)}` : "no mentions in 30d"}</li>
            <li>Routable: {p.signals.routable ? "yes, by the published rule" : "no, needs manual allowlist"}</li>
            <li>Audits linked: {p.signals.auditCount}</li>
            <li>First seen: {longDate(p.firstSeenAt.slice(0, 10))}</li>
          </ul>
          <p className="muted" style={{ fontSize: 13 }}>
            {[
              p.website && (
                <a key="w" className="inline" href={p.website} target="_blank" rel="noopener">
                  Website
                </a>
              ),
              p.github && (
                <a key="g" className="inline" href={p.github} target="_blank" rel="noopener">
                  GitHub
                </a>
              ),
              p.xHandle && (
                <a key="x" className="inline" href={`https://x.com/${p.xHandle}`} target="_blank" rel="noopener">
                  @{p.xHandle}
                </a>
              ),
            ]
              .filter(Boolean)
              .flatMap((el, i) => (i === 0 ? [el] : [" · ", el]))}
          </p>
        </section>

        <section className="block" aria-labelledby="receipts">
          <h2 id="receipts">Receipts</h2>
          {p.receipts.length === 0 ? (
            <p className="muted">No links in the last 30 days.</p>
          ) : (
            <ul>
              {p.receipts.map((r) => (
                <li key={r.url + r.title}>
                  <a className="inline" href={r.url} target="_blank" rel="noopener">
                    {r.title}
                  </a>{" "}
                  <span className="muted num">
                    {sourceLabel(r.url, r.source)} · {age(r.publishedAt, now)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="block">
          <p className="muted" style={{ fontSize: 13 }}>
            Not audited by us. Flags shown as published. Routable badge is a rule, not a guarantee. <Link href="/about" className="inline">How this works</Link>.
          </p>
        </section>
        <Footer />
      </main>
    </>
  );
}
