import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { Footer } from "@/components/Footer";
import { getDigest, listDigests } from "@/db/queries";
import { longDate, usd } from "@/lib/format";
import s from "../week.module.css";
import { isUnnamed } from "@/lib/unlisted";

export const revalidate = 600; // demo window: a first visit is at most ten minutes behind the list
export const dynamicParams = true;

export async function generateStaticParams() {
  if (!process.env.DATABASE_URL) return [];
  const d = await listDigests(12);
  return d.map((x) => ({ date: x.week }));
}

/** A digest week is a real Monday (UTC). Anything else is a 404 before it can reach the database. */
function isWeekStart(date: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return false;
  const d = new Date(date + "T00:00:00Z");
  return !Number.isNaN(d.getTime()) && d.toISOString().startsWith(date) && d.getUTCDay() === 1;
}

export async function generateMetadata({ params }: { params: Promise<{ date: string }> }): Promise<Metadata> {
  const { date } = await params;
  if (!isWeekStart(date)) return { title: "Not found" };
  const d = await getDigest(date);
  return d ? { title: d.title, description: d.intro } : { title: "Not found" };
}

export default async function WeekPage({ params }: { params: Promise<{ date: string }> }) {
  const { date } = await params;
  if (!isWeekStart(date)) notFound();
  const [d, all] = await Promise.all([getDigest(date), listDigests(12)]);
  if (!d) notFound();
  const idx = all.findIndex((x) => x.week === d.week);
  const newer = idx > 0 ? all[idx - 1] : null;
  const older = idx >= 0 && idx < all.length - 1 ? all[idx + 1] : null;
  // as the New tab: skip hooks nobody named and launches with no volume yet (the digest itself keeps them)
  const fresh = d.items.filter((i) => i.kind === "new" && !isUnnamed(i.name) && i.vol7d !== 0);
  const hot = d.items.filter((i) => i.kind === "trending");

  return (
    <>
      <main className="col" id="main" tabIndex={-1}>
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
