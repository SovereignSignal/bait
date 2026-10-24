import { getFeed, listDigests } from "@/db/queries";
import { usd } from "@/lib/format";
import { respond } from "@/lib/http";
import { escXml as esc } from "@/lib/xml";

export const dynamic = "force-dynamic";

const SITE = process.env.NEXT_PUBLIC_SITE_URL || "http://localhost:3000";   // empty = unset, as in layout.tsx

export async function GET(req: Request) {
  const [digests, fresh] = await Promise.all([listDigests(12), getFeed({ tab: "new" })]);
  const newest = fresh.slice(0, 50);
  const items: string[] = [];

  for (const d of digests) {
    const body = [d.intro, "", ...d.items.map((i) => `${i.kind === "new" ? "New" : "Trending"}: ${i.name}. ${i.line}`)].join("\n");
    items.push(
      `<item><title>${esc(d.title)}</title><link>${SITE}/week/${d.week}</link><guid isPermaLink="true">${SITE}/week/${d.week}</guid>` +
        `<pubDate>${new Date(d.createdAt).toUTCString()}</pubDate><description>${esc(body)}</description></item>`,
    );
  }
  for (const p of newest) {
    const body = `${p.whatLine}\n\n${p.whyBlurb}\n\n7d volume ${usd(p.metrics.vol7d)}, TVL ${usd(p.metrics.tvlUsd)}, ${p.metrics.poolsN} pools.`;
    items.push(
      `<item><title>${esc(`New: ${p.name}`)}</title><link>${SITE}/p/${p.slug}</link><guid isPermaLink="true">${SITE}/p/${p.slug}</guid>` +
        `<pubDate>${new Date(p.firstSeenAt).toUTCString()}</pubDate><description>${esc(body)}</description></item>`,
    );
  }

  const xml =
    `<?xml version="1.0" encoding="UTF-8"?>` +
    `<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>` +
    `<title>Bait</title><link>${SITE}</link>` +
    `<description>One feed for Uniswap v4 hooks. Weekly digests and new projects.</description>` +
    `<language>en</language><lastBuildDate>${new Date().toUTCString()}</lastBuildDate>` +
    `<atom:link href="${SITE}/feed.xml" rel="self" type="application/rss+xml"/>` +
    items.join("") +
    `</channel></rss>`;

  return respond(req, xml, { headers: { "Content-Type": "application/rss+xml; charset=utf-8", "Cache-Control": "public, max-age=3600" } });
}
