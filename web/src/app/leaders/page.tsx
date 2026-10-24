import type { Metadata } from "next";
import Link from "next/link";
import { ProjectAvatar } from "@/components/Avatar";
import { Footer } from "@/components/Footer";
import { getFreshness } from "@/db/health";
import { getLeaders, usingFixtures } from "@/db/queries";
import { int, usd } from "@/lib/format";
import { categoryAndChains } from "@/lib/present";
import type { LeaderEntry } from "@/lib/types";
import s from "./leaders.module.css";

// Dynamic like "/": a build-time prerender would be empty (no DATABASE_URL in the image) for up to an hour after each deploy.
// The heavy read is behind the 1h in-process cache in queries.ts.
export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Leaders", description: "Usage leaderboards across Uniswap v4 hooks: wallets, volume and TVL." };

function Board({ title, note, entries, fmt, empty }: { title: string; note: string; entries: LeaderEntry[]; fmt: (n: number) => string; empty: string }) {
  return (
    <section className={`panel ${s.board}`} aria-label={title}>
      <div className={s.bh}>
        <h2>{title}</h2>
        <span>{note}</span>
      </div>
      {entries.length === 0 ? (
        <div className={s.empty}>{empty}</div>
      ) : (
        <ol className={s.list}>
          {entries.map((e, i) => (
            <li key={e.row.slug}>
              <Link className={s.row} href={`/p/${e.row.slug}`}>
                <span className={`${s.medal} ${i < 3 ? s[`g${i + 1}`] : ""} num`} aria-label={`Rank ${i + 1}`}>
                  {i + 1}
                </span>
                <ProjectAvatar name={e.row.name} color={e.row.color} chain={e.row.chains[0]} />
                <span className={s.meta}>
                  <span className={s.n}>
                    {e.row.name}
                  </span>
                  <span className={s.s}>
                    {categoryAndChains(e.row.category, e.row.chains)}
                  </span>
                </span>
                <span className={`${s.val} num`}>{fmt(e.value)}</span>
              </Link>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export default async function LeadersPage() {
  const [L, fresh] = await Promise.all([getLeaders(), getFreshness()]);
  const t = L.totals;
  return (
    <>
      <main className={s.wrap} id="main" tabIndex={-1}>
        <div className={s.lead}>
          <h1>Leaders</h1>
          <p>Who is actually using v4 hooks. Grouped by project, refreshed nightly.</p>
        </div>
        <div className={s.totals}>
          <div className={s.tot}>
            <div className={s.k}>7d volume through hooked pools</div>
            <div className={`${s.v} num`}>{usd(t.vol7d)}</div>
          </div>
          <div className={s.tot}>
            <div className={s.k}>Wallets, last closed day (upper bound)</div>
            <div className={`${s.v} num`}>{int(t.wallets24h)}</div>
          </div>
          <div className={s.tot}>
            <div className={s.k}>Projects traded, last 7 days</div>
            <div className={`${s.v} num`}>{int(t.projects)}</div>
          </div>
          <div className={s.tot}>
            <div className={s.k}>Chains covered</div>
            <div className={`${s.v} num`}>{int(t.chains)}</div>
          </div>
        </div>
        <div className={s.boards}>
          <Board title="Most wallets" note="last closed UTC day, distinct where known" entries={L.wallets} fmt={int} empty="Wallet counts appear after the next pipeline run." />
          <Board title="Most volume" note="7 days" entries={L.volume} fmt={usd} empty="No volume indexed yet." />
          <Board title="Most TVL" note="now" entries={L.tvl} fmt={usd} empty="No TVL indexed yet." />
        </div>
      </main>
      <Footer sample={usingFixtures()} through={fresh?.through} />
    </>
  );
}
