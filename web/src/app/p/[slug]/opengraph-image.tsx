import { ImageResponse } from "next/og";
import { getProject } from "@/db/queries";
import { CHAINS } from "@/lib/chains";
import { int, pctDelta, usd } from "@/lib/format";
import { categoryLine, sourceLine } from "@/lib/present";

export const revalidate = 3600;
export const alt = "Bait project card";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

// Night tokens from the terminal mock, inlined because satori has no CSS variables.
const T = {
  bg: "#0a0a13",
  surface: "#12121d",
  well: "#181826",
  border: "#24243a",
  fg: "#f5f6fc",
  fg2: "#9a9bb2",
  fg3: "#676880",
  pink: "#ff007a",
  pinkTint: "rgba(255,0,122,.15)",
  pinkTintFg: "#ff3393",
  green: "#2fd27a",
  red: "#ff6a52",
  greenTint: "rgba(47,210,122,.14)",
  amber: "#ffbd5c",
  amberTint: "rgba(255,189,92,.14)",
};

let interCache: Promise<ArrayBuffer | null> | null = null;
/**
 * Inter 600 as TrueType for satori (it cannot read woff2). Google Fonts picks the
 * format from the User-Agent: a non-browser UA gets a .ttf URL. Falls back to the
 * bundled default font if the network is unavailable.
 */
async function loadInter(): Promise<ArrayBuffer | null> {
  if (!interCache) {
    interCache = (async () => {
      try {
        const css = await fetch("https://fonts.googleapis.com/css2?family=Inter:wght@600&display=swap", {
          headers: { "User-Agent": "bait-og/1.0 (+satori)" },
        }).then((r) => r.text());
        const m = css.match(/src: url\(([^)]+\.ttf)\) format\('truetype'\)/);
        if (!m) return null;
        const res = await fetch(m[1]);
        if (!res.ok) return null;
        return await res.arrayBuffer();
      } catch {
        return null;
      }
    })();
  }
  return interCache;
}

export default async function Image({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const p = await getProject(slug);
  const inter = await loadInter();
  const fonts = inter ? [{ name: "Inter", data: inter, weight: 600 as const, style: "normal" as const }] : [];
  const font = inter ? "Inter" : "sans-serif";

  if (!p) return new Response("Not found", { status: 404 });

  const delta = pctDelta(p.metrics.vol7d ?? 0, p.metrics.vol7dPrev ?? 0, p.isNew);
  const badge = p.isNew ? { text: "New", bg: T.pinkTint, fg: T.pinkTintFg } : p.rank ? { text: `#${p.rank} trending`, bg: T.amberTint, fg: T.amber } : null;
  const sig = (on: boolean, text: string, money = false) => (
    <div
      style={{
        display: "flex",
        padding: "8px 16px",
        borderRadius: 999,
        fontSize: 20,
        background: money ? T.pinkTint : on ? T.greenTint : T.surface,
        color: money ? T.pinkTintFg : on ? T.green : T.fg3,
        border: money || on ? "1px solid transparent" : `1px solid ${T.border}`,
      }}
    >
      {text}
    </div>
  );
  const money = p.signals.smartMoneyLps;

  return new ImageResponse(
    <div style={{ width: "100%", height: "100%", display: "flex", background: T.bg, fontFamily: font, position: "relative" }}>
      <div style={{ position: "absolute", left: 300, top: -300, width: 600, height: 600, borderRadius: 300, background: "rgba(255,0,122,.22)", filter: "blur(80px)" }} />
      <div style={{ display: "flex", flexDirection: "column", margin: 40, width: 1120, height: 550, background: T.surface, border: `1px solid ${T.border}`, borderRadius: 36, padding: 24, gap: 8 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "4px 12px 8px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
            <div style={{ width: 48, height: 48, borderRadius: 24, background: p.color, color: "#fff", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 22 }}>
              {p.name.slice(0, 1).toUpperCase()}
            </div>
            <div style={{ display: "flex", flexDirection: "column" }}>
              <div style={{ fontSize: 32, color: T.fg, letterSpacing: -0.5 }}>{p.name}</div>
              <div style={{ fontSize: 20, color: T.fg2 }}>{categoryLine(p)}</div>
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            {badge && <div style={{ display: "flex", fontSize: 20, padding: "6px 14px", borderRadius: 999, background: badge.bg, color: badge.fg }}>{badge.text}</div>}
            <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 22, color: T.fg }}>
              <div style={{ width: 28, height: 28, borderRadius: 14, background: T.pink }} />
              Bait
            </div>
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", background: T.well, borderRadius: 28, padding: 24, gap: 12 }}>
          <div style={{ fontSize: 20, color: T.fg2 }}>What it does</div>
          <div style={{ fontSize: 34, lineHeight: 1.25, color: T.fg, letterSpacing: -0.5 }}>{p.whatLine}</div>
          <div style={{ display: "flex", gap: 10, marginTop: 4 }}>
            {p.chains.filter((id) => CHAINS[id]).slice(0, 4).map((id) => (
              <div key={id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 14px 6px 6px", borderRadius: 999, background: T.surface, border: `1px solid ${T.border}`, fontSize: 20, color: T.fg }}>
                <div style={{ width: 24, height: 24, borderRadius: 12, background: CHAINS[id].color }} />
                {CHAINS[id].name}
              </div>
            ))}
            {p.flagLabels.length > 0 && <div style={{ display: "flex", alignItems: "center", marginLeft: "auto", fontSize: 18, color: T.fg3 }}>{p.flagLabels.join(" · ")}</div>}
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", background: T.well, borderRadius: 28, padding: 24, gap: 14 }}>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            {money !== null && (money > 0 ? sig(true, `◆ ${money} smart money LP${money === 1 ? "" : "s"}`, true) : sig(false, "◇ No smart money yet"))}
            {sig(p.signals.routable === "eligible", p.signals.routable === "eligible" ? "● Routes in Uniswap app" : p.signals.routable === "ineligible" ? "○ Not routable in app" : "○ Routing unknown")}
            {sig(p.signals.auditCount > 0, p.signals.auditCount > 0 ? "✓ Audit linked" : "○ No audit linked")}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 16px 0", fontSize: 24, color: T.fg2 }}>
          <div style={{ display: "flex", gap: 10 }}>
            7d volume <span style={{ color: T.fg }}>{usd(p.metrics.vol7d)}</span> <span style={{ color: delta.dir === "down" ? T.red : T.green }}>{delta.text}</span>
          </div>
          <div style={{ display: "flex", gap: 24 }}>
            <span style={{ display: "flex", gap: 8 }}>
              TVL <span style={{ color: T.fg }}>{usd(p.metrics.tvlUsd)}</span>
            </span>
            <span style={{ display: "flex", gap: 8 }}>
              Pools <span style={{ color: T.fg }}>{int(p.metrics.poolsN)}</span>
            </span>
            <span>{sourceLine(p)}</span>
          </div>
        </div>
      </div>
    </div>,
    { ...size, fonts, headers: { "Cache-Control": "public, max-age=3600" } },
  );
}
