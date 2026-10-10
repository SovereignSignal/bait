const DAY = 86_400_000;

/** The one glyph for a missing value. */
export const NONE = "–";
const trim = (x: number) => x.toFixed(1).replace(/\.0$/, "");

/** Compact USD: $1.2B, $184.2M, $22.8K, $1K, $812. */
export function usd(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return NONE;
  const abs = Math.abs(n), sign = n < 0 ? "-" : "";
  if (abs >= 1e9) return `${sign}$${trim(abs / 1e9)}B`;
  if (abs >= 1e6) return `${sign}$${trim(abs / 1e6)}M`;
  if (abs >= 1e3) return `${sign}$${trim(abs / 1e3)}K`;
  return `${sign}$${Math.round(abs).toLocaleString("en-US")}`;
}

/** Whole dollars under $1M ("$22,126"), compact above: for rows whose values sit close together. */
export function usdWhole(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return NONE;
  const r = Math.round(Math.abs(n));
  return r >= 1e6 ? usd(n) : `${n < 0 && r > 0 ? "-" : ""}$${r.toLocaleString("en-US")}`;
}

/** "+24%" / "-8%": the one week-over-week format. Past +999% the figure only says "from almost nothing" (a $1.5K week
 * then a $4M one reads +297195%), so it is shown as ">999%". */
export function fmtDelta(d: number): string {
  if (Math.round(d) > 999) return ">999%";
  return `${d >= 0 ? "+" : "-"}${Math.abs(Math.round(d))}%`;
}

/** The size of a week-over-week change for prose: "24%", "more than 999%". */
export function deltaWords(d: number): string {
  const a = Math.abs(Math.round(d)); // the same rounding as fmtDelta, so tile and prose agree
  return a > 999 ? "more than 999%" : `${a}%`;
}

const DIGEST_DEADLINE_HOUR_UTC = 8; // the Monday 06:00 UTC run writes it; health expects the run done by 08:00

/** The digest's next run day from `now` (UTC): "today" on a Monday before the deadline, else "Monday, Oct 12". After the
 * deadline an empty week means that run did not write one, so the next chance is a week out. */
export function nextDigestDay(now = Date.now()): string {
  const d = new Date(now);
  let ahead = (8 - d.getUTCDay()) % 7; // days until Monday (getUTCDay: 0 = Sunday)
  if (ahead === 0 && d.getUTCHours() >= DIGEST_DEADLINE_HOUR_UTC) ahead = 7;
  if (ahead === 0) return "today";
  const mon = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + ahead));
  return `Monday, ${mon.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" })}`;
}

/** "2026-10-10" -> "Oct 10" (the day is UTC). */
export function dayLabel(day: string): string {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

/** At most `max` chars including the ellipsis, cut at the last word boundary. */
export function clip(text: string, max: number): string {
  const t = text.trim();
  if (t.length <= max) return t;
  const head = t.slice(0, max - 1), sp = head.lastIndexOf(" ");
  return `${(sp > max / 2 ? head.slice(0, sp) : head).replace(/[\s,;:.-]+$/, "")}…`;
}

export function int(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return NONE;
  return Math.round(n).toLocaleString("en-US");
}

/** "+112%" / "-8%" / "new" when there is no meaningful base. */
export function pctDelta(cur: number, prev: number, isNew = false): { text: string; dir: "up" | "down" | "new" } {
  if (isNew || prev < 1000) return { text: "new", dir: "new" };
  const d = (cur / prev - 1) * 100;
  const rounded = Math.abs(d) >= 10 ? Math.round(d) : Math.round(d * 10) / 10;
  return { text: `${d >= 0 ? "+" : ""}${rounded}%`, dir: d >= 0 ? "up" : "down" };
}

export function signedPct(d: number, digits = 1): string {
  return `${d >= 0 ? "+" : ""}${d.toFixed(digits)}%`;
}

/** "today", "2d", "16d", "3mo" */
export function age(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "";
  const days = Math.floor((now - new Date(iso).getTime()) / DAY);
  if (days <= 0) return "today";
  if (days < 60) return `${days}d`;
  if (days < 365) return `${Math.floor(days / 30)}mo`;
  return `${Math.floor(days / 365)}y`;
}

/** "today", "2d ago", "16d ago" */
export function ago(iso: string | null | undefined, now = Date.now()): string {
  const a = age(iso, now);
  return a === "today" || a === "" ? a : `${a} ago`;
}

export function daysSince(iso: string, now = Date.now()): number {
  return Math.floor((now - new Date(iso).getTime()) / DAY);
}

export function shortAddr(a: string): string {
  return a.length > 12 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a;
}

/** PoolKey.fee -> "0.05%" or "dynamic" (0x800000). */
export function feeLabel(fee: number): string {
  if (fee >= 0x800000) return "dynamic";
  return `${(fee / 10_000).toFixed(fee % 100 === 0 ? 2 : 3).replace(/0+$/, "").replace(/\.$/, "")}%`;
}

export function pair(t0: string | null, t1: string | null, a0: string, a1: string): string {
  return `${t0 ?? shortAddr(a0)} / ${t1 ?? shortAddr(a1)}`;
}

export function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function longDate(iso: string): string {
  return new Date(iso + (iso.length === 10 ? "T00:00:00Z" : "")).toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });
}

/** Source label for a receipt: "x.com", "github", or the bare host. */
export function sourceLabel(url: string, source?: string | null): string {
  if (source) return source;
  try {
    const h = new URL(url).hostname.replace(/^www\./, "");
    if (h === "twitter.com") return "x.com";
    return h;
  } catch {
    return "link";
  }
}

/** "12s", "4m", "3h", "2d" for live trades. */
export function since(iso: string, now = Date.now()): string {
  const s = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86_400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86_400)}d`;
}

/** Deterministic avatar color from a wallet address or name. */
export function addrColor(a: string): string {
  let x = 0;
  for (let i = 0; i < a.length; i++) x = (x * 31 + a.charCodeAt(i)) | 0;
  return `hsl(${Math.abs(x) % 360} 62% 52%)`;
}

const PALETTE = ["#5b2fff", "#ff007a", "#1b8a5a", "#ff6a00", "#2563eb", "#0ea5e9", "#c2410c", "#7c3aed", "#0f766e", "#b45309"];
/** Mock's token / project palette pick by string hash. */
export function paletteColor(s: string): string {
  let x = 0;
  for (let i = 0; i < s.length; i++) x = (x * 31 + s.charCodeAt(i)) | 0;
  return PALETTE[Math.abs(x) % PALETTE.length];
}
