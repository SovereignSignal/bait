// Publication rules for research views. Provider strings are evidence candidates, never trusted URLs.
import type { ProjectDetail, Eligibility } from './types';
import type { RoutingSource } from './uniswap-routing';

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
  const isDormant = retainedDormant(p);
  const unidentified = !p.hooklisted && (/^unlisted-hook(?:-|$)/.test(p.slug) || /^(?:unlisted hook|.* hook(?: family)? · 0x)/i.test(p.name));
  return {
    ...p,
    isDormant,
    isNew: !isDormant && p.isNew,
    scoreTrending: isDormant ? 0 : p.scoreTrending,
    website: unidentified ? null : webUrl(p.website),
    github: unidentified ? null : githubUrl(p.github),
    xHandle: unidentified ? null : xHandle(p.xHandle),
    // A search on an address-family placeholder can return an unrelated team's site. Keep the observed label and metrics.
    whatLine: unidentified ? 'On-chain hook activity. Project identity and purpose have not been independently confirmed.' : p.whatLine,
    whyBlurb: unidentified ? '' : p.whyBlurb,
    receipts: (unidentified ? [] : p.receipts).filter(r => webUrl(r.url)).map(r => ({...r, url: webUrl(r.url)!})),
    signals: {...p.signals, auditUrl: unidentified ? null : webUrl(p.signals.auditUrl)},
  };
}

/** Routing as Uniswap publishes it: its hook denylist and allowlist (github.com/Uniswap/uniroute-public) first, then the
 * automatic rule. `listsChecked` is false when the lists could not be read, so the rule stands alone. Never proves a quote. */
export function routingNote(state: Eligibility, source: RoutingSource = 'rule', listsChecked = false): { label: string; detail: string } {
  if (source === 'denylist') return {label: 'Uniswap will not route', detail: 'Uniswap’s published routing denylist names this hook: its router never sends swaps through it. Read the reason in Uniswap’s list before trading through it directly.'};
  if (source === 'allowlist') return {label: 'On Uniswap’s routing allowlist', detail: 'Uniswap Labs approved this hook for routing in its published allowlist. Whether a swap routes here still depends on liquidity and price.'};
  if (state === 'eligible') return {label: 'Automatic routing criteria', detail: 'Meets the automatic routing rule Uniswap publishes (no swap-delta permission, not a 0x91 address, a static-fee pool). Current routing and quote availability have not been verified.'};
  if (state === 'ineligible') {
    return listsChecked
      ? {label: 'Not on Uniswap’s routing allowlist', detail: 'Not on Uniswap’s published allowlist, and fails the automatic routing rule (a dynamic fee or a returns-delta permission), so the Uniswap app is unlikely to route through it. Trading through the pool directly still works.'}
      : {label: 'Routing review needed', detail: 'Does not meet the automatic routing rule, and Uniswap’s published allowlist could not be read just now, so a manual approval is unchecked.'};
  }
  return {label: 'Routing unknown', detail: 'Insufficient pool or fee data to assess automatic routing. Check current availability in Uniswap.'};
}

/** Ratio is allowed above 100%; only its decorative progress bar is capped. */
export function activityPace(rolling: number | null, week: number | null): number | null {
  if (rolling == null || week == null || week <= 0 || !Number.isFinite(rolling) || !Number.isFinite(week)) return null;
  return Math.max(0, Math.round(rolling * 7 / week * 100));
}
