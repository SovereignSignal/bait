// Publication rules for research views. Provider strings are evidence candidates, never trusted URLs.
import type { ProjectDetail, Eligibility } from './types';

export function webUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  if (!/^https?:\/\//i.test(text) || /[\s\u0000-\u001f\\]/u.test(text)) return null;
  try {
    const url = new URL(text);
    if (url.username || url.password || !url.hostname.includes('.') || url.hostname.endsWith('.local')) return null;
    return url.href;
  } catch { return null; }
}

export function githubUrl(value: unknown): string | null {
  const parsed = webUrl(value);
  if (!parsed) return null;
  const u = new URL(parsed);
  return u.hostname.toLowerCase() === 'github.com' && /^\/[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)?\/?$/.test(u.pathname) ? parsed : null;
}

export function xHandle(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  let text = value.trim().replace(/^@/, '');
  if (/^https?:\/\//i.test(text)) {
    const parsed = webUrl(text);
    if (!parsed) return null;
    const u = new URL(parsed);
    if (!['x.com', 'twitter.com', 'www.x.com', 'www.twitter.com'].includes(u.hostname.toLowerCase())) return null;
    text = u.pathname.replace(/^\//, '').replace(/\/$/, '');
    if (u.search || u.hash) return null;
  }
  if (!/^[A-Za-z0-9_]{1,15}$/.test(text) || ['none','unknown','null','undefined','search','home','intent','share','not_found','notidentified'].includes(text.toLowerCase())) return null;
  return text;
}

// A link to a Uniswap or Uniswap Foundation property shows only on a project with a hook in Uniswap's own hooklist
// registry (StablePair, VolatilePair, DualPool, the Liquidity Launcher strategies). The 2026-10-16 audit found an
// unlisted launch template (FoundationHookV2, 61 projects) linked to uniswapfoundation.org, the Foundation's GitHub and
// @UniswapFND by a name-only search.

/** A URL on uniswap.org or uniswapfoundation.org (any subdomain), a GitHub or X account whose name starts with
 * "uniswap", or such a bare X handle. */
export function isUniswapBrand(link: string | null | undefined): boolean {
  if (!link) return false;
  if (/^[A-Za-z0-9_]{1,15}$/.test(link)) return /^uniswap/i.test(link);
  try {
    const u = new URL(link);
    const host = u.hostname.toLowerCase().replace(/^www\./, '');
    if (/(^|\.)uniswap(foundation)?\.org$/.test(host)) return true;
    if (['github.com', 'x.com', 'twitter.com'].includes(host)) return /^\/uniswap/i.test(u.pathname);
    return false;
  } catch { return false; }
}

export function uniswapBuilt(p: Pick<ProjectDetail, 'hooks'>): boolean {
  return p.hooks.some((h) => h.hooklisted === true);
}

/** Only the pipeline can establish historical usage and complete recent zero-swap coverage. A zero USD value,
 * registry membership, verified source or an old frontend flag cannot establish dormancy or override spam. */
export function retainedDormant(p: ProjectDetail): boolean {
  return !p.isWash && p.hasHistoricalUsage === true && p.activityStatus === 'dormant'
    && p.metrics.swaps7d === 0 && (p.metrics.vol7d == null || p.metrics.vol7d === 0);
}

/** Evidence labels describe the scored period, not a project's current operating status or contract safety. */
export function activityNote(p: ProjectDetail): { label: string; detail: string } | null {
  if (retainedDormant(p)) return {
    label: 'Dormant in reported window',
    detail: `Prior qualifying usage is recorded. Complete swap coverage shows zero swaps over the seven closed UTC days${p.through ? ` ending ${p.through}` : ''}. This does not establish shutdown or contract safety.`,
  };
  if (p.hasHistoricalUsage && p.activityStatus === 'unknown') return {
    label: 'Recent activity unknown',
    detail: 'Prior qualifying usage is recorded. Recent swap coverage is incomplete or unavailable, so inactivity has not been established.',
  };
  return null;
}

export function researchProject(p: ProjectDetail): ProjectDetail {
  const official = uniswapBuilt(p);
  const brand = (v: string | null): string | null => (v && !official && isUniswapBrand(v) ? null : v);
  const isDormant = retainedDormant(p);
  const unidentified = !p.hooklisted && (/^unlisted-hook(?:-|$)/.test(p.slug) || /^(?:unlisted hook|.* hook(?: family)? · 0x)/i.test(p.name));
  return {
    ...p,
    isDormant,
    isNew: !isDormant && p.isNew,
    scoreTrending: isDormant ? 0 : p.scoreTrending,
    website: unidentified ? null : brand(webUrl(p.website)),
    github: unidentified ? null : brand(githubUrl(p.github)),
    xHandle: unidentified ? null : brand(xHandle(p.xHandle)),
    // A search on an address-family placeholder can return an unrelated team's site. Keep the observed label and metrics.
    whatLine: unidentified ? 'On-chain hook activity. Project identity and purpose have not been independently confirmed.' : p.whatLine,
    whyBlurb: unidentified ? '' : p.whyBlurb,
    receipts: (unidentified ? [] : p.receipts).filter(r => webUrl(r.url)).map(r => ({...r, url: webUrl(r.url)!})),
    signals: {...p.signals, auditUrl: unidentified ? null : brand(webUrl(p.signals.auditUrl))},
  };
}

/** The stored enum is an automatic-allowlist heuristic, not the result of an individual routing review. */
export function routingNote(state: Eligibility): { label: string; detail: string } {
  if (state === 'eligible') return {label: 'Automatic routing criteria', detail: 'Meets Bait’s conservative automatic-routing rule. Current Uniswap routing and quote availability have not been verified.'};
  if (state === 'ineligible') return {label: 'Routing review needed', detail: 'Does not meet Bait’s automatic-routing rule. Uniswap Labs can approve individual hooks; Bait has not checked that approval. This does not mean routing is unavailable.'};
  return {label: 'Routing unknown', detail: 'Insufficient pool or fee data to assess automatic routing. Check current availability in Uniswap.'};
}

/** Ratio is allowed above 100%; only its decorative progress bar is capped. */
export function activityPace(rolling: number | null, week: number | null): number | null {
  if (rolling == null || week == null || week <= 0 || !Number.isFinite(rolling) || !Number.isFinite(week)) return null;
  return Math.max(0, Math.round(rolling * 7 / week * 100));
}
