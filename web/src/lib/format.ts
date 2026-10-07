const DAY = 86_400_000;

/** Compact USD: $1.21B, $184.2M, $22.8K, $812. */
export function usd(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "–";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1)}K`;
  return `${sign}$${Math.round(abs).toLocaleString("en-US")}`;
}

export function int(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "–";
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
