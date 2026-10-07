import type { Metadata } from "next";
import Link from "next/link";
import { Footer } from "@/components/Footer";
import { Nav } from "@/components/Nav";
import { listDigests } from "@/db/queries";
import s from "./week.module.css";

export const revalidate = 3600;
export const metadata: Metadata = { title: "Weekly", description: "Weekly digest of new and trending Uniswap v4 hook projects." };

export default async function WeeksPage() {
  const all = await listDigests(26);
  return (
    <>
      <Nav />
      <main className="col">
        <div className="lead">
          <h1>Weekly digest</h1>
          <p>
            Every Monday. New projects that cleared the bar and the week&apos;s top trending hooks. Also as <a href="/feed.xml">RSS</a>.
          </p>
        </div>
        <section className="block">
          {all.length === 0 ? (
            <p className="muted">No digests yet.</p>
          ) : (
            <div className={s.weeks}>
              {all.map((d) => (
                <Link key={d.week} href={`/week/${d.week}`}>
                  <span className={s.t}>{d.title}</span>
                  <span className={s.i}>{d.intro}</span>
                </Link>
              ))}
            </div>
          )}
        </section>
        <Footer />
      </main>
    </>
  );
}
