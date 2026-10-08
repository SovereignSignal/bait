// The /building list column (server): kind chips, then radar items grouped by kind.
import Link from "next/link";
import { anyChainColor, anyChainName } from "@/lib/chains";
import { ago, int, since } from "@/lib/format";
import { groupRadar, RADAR_LABEL, RADAR_PILL, radarHref } from "@/lib/radar";
import { RADAR_KINDS, type RadarItemView, type RadarKind } from "@/lib/types";
import s from "./Radar.module.css";
import t from "./Terminal.module.css";

const GROUP_PREVIEW = 8;
const KIND_MAX = 200;

export function RadarList({ items, kind, now }: { items: RadarItemView[]; kind?: RadarKind; now: number }) {
  const counts = new Map<RadarKind, number>();
  for (const x of items) counts.set(x.kind, (counts.get(x.kind) ?? 0) + 1);
  const groups = groupRadar(items, kind);

  return (
    <aside className={`panel ${t.list}`} aria-label="Hooks being built">
      <div className={s.head}>
        <h1>Building</h1>
        <span>Refreshed nightly</span>
      </div>
      <div className={t.cats} role="group" aria-label="Kind">
        <Link className={t.chip} aria-current={!kind} href={radarHref()}>
          All
        </Link>
        {RADAR_KINDS.map(([k, label]) => (
          <Link key={k} className={t.chip} aria-current={kind === k} href={radarHref(k)}>
            {label}
            {counts.get(k) ? <span className="num mut3"> {counts.get(k)}</span> : null}
          </Link>
        ))}
      </div>
      <div className={t.rows}>
        {groups.length === 0 ? (
          <div className="empty">
            {items.length
              ? `Nothing in ${RADAR_LABEL[kind!]} yet.`
              : "Nothing on the radar yet. Hooklist registry submissions, new v4 hook repos, hackathon and incubator projects, audits and launch posts appear here after the next nightly pipeline run (about 06:00 UTC)."}
          </div>
        ) : (
          groups.map(([k, xs]) => {
            const shown = kind ? xs.slice(0, KIND_MAX) : xs.slice(0, GROUP_PREVIEW);
            return (
              <section key={k} className={s.group} aria-label={RADAR_LABEL[k]}>
                {!kind && (
                  <div className={s.groupHead}>
                    <span>
                      {RADAR_LABEL[k]} · <span className="num">{int(xs.length)}</span>
                    </span>
                    {xs.length > shown.length && <Link href={radarHref(k)}>All {int(xs.length)} →</Link>}
                  </div>
                )}
                {shown.map((x) => (
                  <RadarRow key={x.url} x={x} now={now} />
                ))}
              </section>
            );
          })
        )}
      </div>
    </aside>
  );
}

/** "5h ago" inside a day, then "3d ago", "2mo ago". */
function relTime(iso: string, now: number): string {
  const ms = now - Date.parse(iso);
  if (!Number.isFinite(ms)) return "";
  return ms < 86_400_000 ? `${since(iso, now)} ago` : ago(iso, now);
}

function RadarRow({ x, now }: { x: RadarItemView; now: number }) {
  const when = x.publishedAt ?? x.lastSeen;
  const pill = RADAR_PILL[x.kind];
  return (
    <article className={s.item}>
      <div className={s.itemTop}>
        <span className={`b ${pill === "bc" ? s.bc : pill}`}>{RADAR_LABEL[x.kind]}</span>
        <a className={s.itemTitle} href={x.url} target="_blank" rel="noopener" title={x.title}>
          {x.title} ↗
        </a>
      </div>
      {x.summary && <div className={s.itemSum} title={x.summary}>{x.summary}</div>}
      <div className={s.itemMeta}>
        <span>{x.source ?? "web"}</span>
        <span aria-hidden="true">·</span>
        <span className="num">{relTime(when, now)}</span>
        {x.kind === "repo" && x.stars != null && (
          <>
            <span aria-hidden="true">·</span>
            <span className="num" aria-label={`${x.stars} stars`}>
              ★ {int(x.stars)}
            </span>
          </>
        )}
        {x.chainId != null && (
          <span className={s.chainPill}>
            <span className={s.dot} style={{ background: anyChainColor(x.chainId) }} aria-hidden="true" />
            {anyChainName(x.chainId)}
          </span>
        )}
        {x.projectSlug && (
          <Link className={`b ${s.live}`} href={`/p/${x.projectSlug}`}>
            Live on Bait
          </Link>
        )}
      </div>
    </article>
  );
}
