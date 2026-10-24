import type { Metadata } from "next";
import Link from "next/link";
import { Footer } from "@/components/Footer";
import { JustLaunched } from "@/components/JustLaunched";
import { RadarList } from "@/components/RadarList";
import { getLaunches, getRadar } from "@/db/radar";
import { usingFixtures } from "@/db/queries";
import { SAMPLE_NOW } from "@/fixtures/sample";
import { int } from "@/lib/format";
import { radarHref } from "@/lib/radar";
import { isRadarKind, RADAR_KINDS } from "@/lib/types";
import r from "@/components/Radar.module.css";
import t from "@/components/Terminal.module.css";

// Reads searchParams (?kind=), so it renders per request; the reads are cached in-process (launches 30s, radar 5 min),
// and a build without DATABASE_URL never bakes an empty page.
export const revalidate = 60;
export const metadata: Metadata = {
  title: "Building",
  description: "Uniswap v4 hooks being built: registry submissions, new repos, hackathon and incubator projects, audits, launch posts and new hooked pools.",
};

type Search = Record<string, string | string[] | undefined>;

export default async function BuildingPage({ searchParams }: { searchParams: Promise<Search> }) {
  const sp = await searchParams;
  const raw = Array.isArray(sp.kind) ? sp.kind[0] : sp.kind;
  const kind = isRadarKind(raw) ? raw : undefined;
  const [items, launches] = await Promise.all([getRadar(), getLaunches(20)]);
  const sample = usingFixtures();
  // eslint-disable-next-line react-hooks/purity -- a Server Component renders per request or revalidation, never in the browser
  const clock = Date.now();
  const now = sample ? SAMPLE_NOW : clock;
  const counts = new Map(RADAR_KINDS.map(([k]) => [k, items.filter((x) => x.kind === k).length]));

  return (
    <div className={t.shell}>
      <main className={r.app} id="main" tabIndex={-1}>
        <RadarList items={items} kind={kind} now={now} />
        <section className={r.side} id="launches" aria-label="Just launched">
          <JustLaunched initial={launches} now={clock} />
          <div className={`panel ${r.about}`}>
            <h2>What is on the radar</h2>
            <p>
              Hooks before they have volume. Each night Bait reads new submissions to the Uniswap hooklist, GitHub repos building v4 hooks, and
              web posts about hackathon and incubator projects, audits and launches (web results are kept only when they are about a specific
              hook). When an item matches a project already trading, it links to that project. Not reviewed by us.
            </p>
            <div className={r.kinds}>
              {RADAR_KINDS.map(([k, label]) => (
                <Link key={k} className={r.kind} href={radarHref(k)}>
                  <span className="mut">{label}</span>
                  <b className="num">{int(counts.get(k) ?? 0)}</b>
                </Link>
              ))}
            </div>
          </div>
        </section>
      </main>
      <Footer sample={sample} />
    </div>
  );
}
