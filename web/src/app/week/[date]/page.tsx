import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { Footer } from "@/components/Footer";
import { Nav } from "@/components/Nav";
import { getDigest, listDigests } from "@/db/queries";
import { longDate, usd } from "@/lib/format";
import s from "../week.module.css";

export const revalidate = 3600;
export const dynamicParams = true;

export async function generateStaticParams() {
  if (!process.env.DATABASE_URL) return [];
  const d = await listDigests(12);
  return d.map((x) => ({ date: x.week }));
}

export async function generateMetadata({ params }: { params: Promise<{ date: string }> }): Promise<Metadata> {
  const { date } = await params;
  const d = await getDigest(date);
  return d ? { title: d.title, description: d.intro } : { title: "Not found" };
}

export default async function WeekPage({ params }: { params: Promise<{ date: string }> }) {
  const { date } = await params;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) notFound();
  const [d, all] = await Promise.all([getDigest(date), listDigests(12)]);
  if (!d) notFound();
  const idx = all.findIndex((x) => x.week === d.week);
  const newer = idx > 0 ? all[idx - 1] : null;
  const older = idx >= 0 && idx < all.length - 1 ? all[idx + 1] : null;
  const fresh = d.items.filter((i) => i.kind === "new");
  const hot = d.items.filter((i) => i.kind === "trending");

  return (
    <>
      <Nav />
      <main className="col">
        <div className="lead">
          <h1>{d.title}</h1>
          <p>{d.intro}</p>
        </div>
        {fresh.length > 0 && (
          <section className="block" aria-labelledby="new">
            <h2 id="new">New this week</h2>
            <ol className={s.list}>
              {fresh.map((i) => (
                <li key={i.slug}>
                  <Link href={`/p/${i.slug}`} className={s.name}>
                    {i.name}
                  </Link>
                  <span className={s.line}>{i.line}</span>
                  <span className={`${s.vol} num`}>{usd(i.vol7d)}</span>
                </li>
              ))}
            </ol>
          </section>
        )}
        {hot.length > 0 && (
          <section className="block" aria-labelledby="trending">
            <h2 id="trending">Trending</h2>
            <ol className={s.list}>
              {hot.map((i, n) => (
                <li key={i.slug}>
                  <Link href={`/p/${i.slug}`} className={s.name}>
                    <span className={`${s.rank} num`}>#{n + 1}</span> {i.name}
                  </Link>
                  <span className={s.line}>{i.line}</span>
                  <span className={`${s.vol} num`}>{usd(i.vol7d)}</span>
                </li>
              ))}
            </ol>
          </section>
        )}
        <nav className={s.pager} aria-label="Weeks">
          {older ? <Link href={`/week/${older.week}`}>← {longDate(older.week)}</Link> : <span />}
          <Link href="/week">All weeks</Link>
          {newer ? <Link href={`/week/${newer.week}`}>{longDate(newer.week)} →</Link> : <span />}
        </nav>
        <Footer />
      </main>
    </>
  );
}
