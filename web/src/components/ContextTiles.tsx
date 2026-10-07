import type { ContextStats } from "@/lib/types";
import { int, signedPct, usd } from "@/lib/format";
import s from "./ContextTiles.module.css";

export function ContextTiles({ c }: { c: ContextStats }) {
  return (
    <section className={s.grid} aria-label="Context">
      <div className={s.tile}>
        <div className={s.k}>Hooks tracked</div>
        <div className={s.v}>{int(c.hooksTracked)}</div>
        <div className={`${s.d} ${c.hooksDelta7d > 0 ? "" : s.flat}`}>{c.hooksDelta7d > 0 ? `+${int(c.hooksDelta7d)} this week` : "no change"}</div>
      </div>
      <div className={s.tile}>
        <div className={s.k}>Projects</div>
        <div className={s.v}>{int(c.projects)}</div>
        <div className={`${s.d} ${c.projectsNew7d > 0 ? "" : s.flat}`}>{c.projectsNew7d > 0 ? `+${int(c.projectsNew7d)} new` : "no new"}</div>
      </div>
      <div className={s.tile}>
        <div className={s.k}>TVL in hooked pools</div>
        <div className={s.v}>{usd(c.tvlUsd)}</div>
        <div className={`${s.d} ${c.tvlDelta7dPct >= 0 ? "" : s.flat}`}>{signedPct(c.tvlDelta7dPct)} 7d</div>
      </div>
    </section>
  );
}
