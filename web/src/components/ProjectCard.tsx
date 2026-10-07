import Link from "next/link";
import { CHAINS, poolDeepLink } from "@/lib/chains";
import { age, ago, int, pctDelta, sourceLabel, usd } from "@/lib/format";
import { CATEGORY_LABEL, type ProjectSummary } from "@/lib/types";
import { ArrowDown, ArrowRight, Chevron } from "./Icons";
import s from "./ProjectCard.module.css";

export function categoryLine(p: ProjectSummary): string {
  const parts = [CATEGORY_LABEL[p.category], ...p.secondaryCategories.map((c) => CATEGORY_LABEL[c])];
  if (p.isFactory) parts.push("Factory");
  return parts.join(" · ");
}

export function badge(p: ProjectSummary, now: number): { text: string; kind: "new" | "hot" | "plain" } | null {
  if (p.isNew) return { text: `New · ${age(p.firstSeenAt, now)}`, kind: "new" };
  if (p.rank) return { text: `#${p.rank} trending`, kind: "hot" };
  return null;
}

export function sourceLine(p: ProjectSummary): string {
  const parts = [p.verified ? "Verified" : "Unverified"];
  if (p.signals.auditCount > 0) parts.push(`${p.signals.auditCount} audit${p.signals.auditCount === 1 ? "" : "s"}`);
  if (p.upgradeable) parts.push("upgradeable");
  return parts.join(" · ");
}

export function aliveLabel(p: ProjectSummary, now: number): { text: string; on: boolean } {
  const c = p.signals.lastCommitAt ? (now - +new Date(p.signals.lastCommitAt)) / 86_400_000 : Infinity;
  const e = p.signals.lastExaHitAt ? (now - +new Date(p.signals.lastExaHitAt)) / 86_400_000 : Infinity;
  if (c <= 30) return { text: `Alive · commit ${ago(p.signals.lastCommitAt, now)}`, on: true };
  if (e <= 30) return { text: `Alive · mentioned ${ago(p.signals.lastExaHitAt, now)}`, on: true };
  return { text: "Quiet · 30d", on: false };
}

export function ctaFor(p: ProjectSummary): { href: string; label: string; primary: boolean; external: boolean } {
  if (p.signals.routable && p.topPool) {
    return { href: poolDeepLink(p.topPool.chainId, p.topPool.poolId), label: "Swap on Uniswap", primary: true, external: true };
  }
  if (p.website) return { href: p.website, label: `Open ${p.name}`, primary: false, external: true };
  if (p.github) return { href: p.github, label: `Open ${p.name}`, primary: false, external: true };
  return { href: `/p/${p.slug}`, label: `Open ${p.name}`, primary: false, external: false };
}

export function ProjectCard({ p, variant = "feed", now = Date.now() }: { p: ProjectSummary; variant?: "feed" | "header"; now?: number }) {
  const b = badge(p, now);
  const delta = pctDelta(p.metrics.vol7d, p.metrics.vol7dPrev, p.isNew);
  const alive = aliveLabel(p, now);
  const cta = ctaFor(p);
  const money = p.signals.smartMoneyLps;
  const title = variant === "feed" ? <Link href={`/p/${p.slug}`}>{p.name}</Link> : p.name;
  const Heading = variant === "feed" ? "h2" : "h1";

  return (
    <article className={`${s.card} ${variant === "feed" ? s.feed : ""}`}>
      <div className={s.head}>
        <div className={s.title}>
          <span className={s.logo} style={{ background: p.color }} aria-hidden="true">
            {p.name.slice(0, 1).toUpperCase()}
          </span>
          <div>
            <Heading className={s.name}>{title}</Heading>
            <div className={s.cat}>{categoryLine(p)}</div>
          </div>
        </div>
        {b && <span className={`${s.rank} ${b.kind === "new" ? s.new : b.kind === "hot" ? s.hot : ""}`}>{b.text}</span>}
      </div>

      <div className={s.well}>
        <div className={s.label}>What it does</div>
        <div className={s.big}>{p.whatLine}</div>
        <div className={s.foot}>
          <div className={s.chains}>
            {p.chains.map((id, i) => {
              const c = CHAINS[id];
              return (
                <span key={id} className={s.token}>
                  <span className={s.dot} style={{ background: c.color, border: c.lightDot ? "1px solid var(--border)" : undefined }} />
                  {c.name}
                  {i === 0 && p.chains.length > 1 && <Chevron size={10} />}
                </span>
              );
            })}
          </div>
          {p.flagLabels.length > 0 && <span className={s.flags}>{p.flagLabels.join(" · ")}</span>}
        </div>
      </div>

      <div className={s.arrow}>
        <span>
          <ArrowDown />
        </span>
      </div>

      <div className={s.well}>
        <div className={s.label}>Why people care</div>
        <div className={s.body}>{p.whyBlurb}</div>
        <div className={s.signals}>
          {money !== null &&
            (money > 0 ? (
              <span className={`${s.sig} ${s.money}`}>◆ {money} smart money LP{money === 1 ? "" : "s"}</span>
            ) : (
              <span className={`${s.sig} ${s.off}`}>◇ No smart money yet</span>
            ))}
          <span className={`${s.sig} ${alive.on ? s.on : s.off}`}>
            {alive.on ? "●" : "○"} {alive.text}
          </span>
          {p.signals.routable ? (
            <span className={`${s.sig} ${s.on}`}>● Routes in Uniswap app</span>
          ) : (
            <span className={`${s.sig} ${s.off}`}>○ Not routable in app</span>
          )}
          {p.signals.auditCount > 0 ? <span className={`${s.sig} ${s.on}`}>✓ Audited</span> : <span className={`${s.sig} ${s.off}`}>○ No audit linked</span>}
        </div>
      </div>

      <details className={s.details}>
        <summary className={s.rate}>
          <span>
            7d volume <b>{usd(p.metrics.vol7d)}</b>{" "}
            {delta.dir === "down" ? <span className={s.down}>{delta.text}</span> : <span className={s.up}>{delta.text}</span>}
          </span>
          <span className={s.toggle}>
            Details <Chevron />
          </span>
        </summary>
        <div className={s.grid}>
          <span>TVL</span>
          <b>{usd(p.metrics.tvlUsd)}</b>
          <span>Pools</span>
          <b>{int(p.metrics.poolsN)}</b>
          <span>Txs 7d</span>
          <b>{int(p.metrics.swaps7d)}</b>
          {p.isFactory && p.topPool ? (
            <>
              <span>Top pool</span>
              <b>
                {p.topPool.token0Symbol ?? "?"} / {p.topPool.token1Symbol ?? "?"} · {CHAINS[p.topPool.chainId].name}
              </b>
            </>
          ) : (
            <>
              <span>Hooks under this project</span>
              <b>{int(p.metrics.hooksN)}</b>
            </>
          )}
          <span>Source</span>
          <b>{sourceLine(p)}</b>
        </div>
      </details>

      {cta.external ? (
        <a className={`${s.cta} ${cta.primary ? "" : s.alt}`} href={cta.href} target="_blank" rel="noopener">
          {cta.label} {cta.primary && <ArrowRight />}
        </a>
      ) : (
        <Link className={`${s.cta} ${s.alt}`} href={cta.href}>
          {cta.label}
        </Link>
      )}

      {p.receipts.length > 0 && (
        <div className={s.receipts}>
          {p.receipts.slice(0, 2).map((r) => (
            <a key={r.url + r.title} href={r.url} target="_blank" rel="noopener">
              <span>{r.title}</span>
              <span className={s.src}>
                {sourceLabel(r.url, r.source)} · {age(r.publishedAt, now)}
              </span>
            </a>
          ))}
        </div>
      )}
    </article>
  );
}
