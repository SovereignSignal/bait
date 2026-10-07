import { longDate, usd } from "@/lib/format";
import s from "./Sparkline.module.css";

const W = 440;
const H = 72;
const PAD = 2;

/**
 * 30-day sparkline as inline SVG, drawn to scale: y runs from 0 to the series
 * max, so a flat line is flat and a doubling looks like a doubling. One series
 * per chart (no dual axis). Each day has an invisible hit rect with a native
 * <title> tooltip, so the chart stays interactive without client JS.
 */
export function Sparkline({ title, points, now }: { title: string; points: Array<{ day: string; value: number }>; now?: number }) {
  if (points.length === 0) return null;
  const max = Math.max(1, ...points.map((p) => p.value));
  const n = points.length;
  const x = (i: number) => (n === 1 ? W / 2 : PAD + (i / (n - 1)) * (W - PAD * 2));
  const y = (v: number) => H - PAD - (v / max) * (H - PAD * 2);
  const path = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  const area = `${path} L${x(n - 1).toFixed(1)},${(H - PAD).toFixed(1)} L${x(0).toFixed(1)},${(H - PAD).toFixed(1)} Z`;
  const last = points[n - 1];
  const first = points[0];
  const delta = first.value > 0 ? (last.value / first.value - 1) * 100 : null;
  const slot = (W - PAD * 2) / Math.max(1, n - 1);

  return (
    <figure className={s.wrap} style={{ margin: 0 }}>
      <figcaption className={s.head}>
        <span>{title}</span>
        <span>
          <b>{usd(last.value)}</b>
          {delta !== null && (
            <span className="num" style={{ marginLeft: 6 }}>
              {delta >= 0 ? "+" : ""}
              {Math.round(delta)}% 30d
            </span>
          )}
        </span>
      </figcaption>
      <svg className={s.svg} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}, last ${n} days, now ${usd(last.value)}`}>
        <line className={s.grid} x1={0} x2={W} y1={H - PAD} y2={H - PAD} />
        <path className={s.fill} d={area} />
        <path className={s.line} d={path} />
        <circle className={s.end} cx={x(n - 1)} cy={y(last.value)} r={4} />
        {points.map((p, i) => (
          <rect key={p.day} className={s.hit} x={x(i) - slot / 2} y={0} width={slot} height={H}>
            <title>{`${longDate(p.day)}: ${usd(p.value)}`}</title>
          </rect>
        ))}
      </svg>
      <div className={s.axis}>
        <span>{longDate(first.day)}</span>
        <span>max {usd(max)}</span>
        <span>{now ? "today" : longDate(last.day)}</span>
      </div>
    </figure>
  );
}
