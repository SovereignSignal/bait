// Project detail column (server): header, stats, daily volume, pools, activity, top wallets, why.
import { CHAINS, chainSlug, explorerAddressUrl, knownChain, poolDeepLink } from "@/lib/chains";
import { weekDelta } from "@/lib/feed";
import { ago, age, feeLabel, int, pair, shortAddr, since, sourceLabel, usd } from "@/lib/format";
import { categoryLine, chainNames } from "@/lib/present";
import type { PoolView, ProjectDetail } from "@/lib/types";
import { ProjectAvatar, WalletAvatar } from "./Avatar";
import { LiveTrades } from "./LiveTrades";
import { WatchStar } from "./WatchStar";
import s from "./Detail.module.css";

const DAY = 86_400_000;
const POOLS_SHOWN = 8;

/** Most active pool (24h volume), else the deepest. */
export function busiestPool(p: ProjectDetail): PoolView | null {
  const withVol = p.pools.filter((x) => x.vol24h != null && x.vol24h > 0);
  if (withVol.length) return withVol.reduce((a, b) => ((b.vol24h ?? 0) > (a.vol24h ?? 0) ? b : a));
  return p.pools[0] ?? null;
}

function poolPair(x: PoolView): string {
  return pair(x.token0Symbol, x.token1Symbol, x.token0, x.token1);
}

export function Detail({ p, now }: { p: ProjectDetail; now: number }) {
  const d = weekDelta(p.metrics);
  const u = p.usage;
  const swaps = u.buys24h != null && u.sells24h != null ? { k: "Swaps 24h", v: u.buys24h + u.sells24h } : { k: "Txs 24h", v: u.tx24h };
  const stats: Array<{ k: string; v: string; cls?: string }> = [
    { k: "7d volume", v: usd(p.metrics.vol7d) },
    { k: "vs last week", v: d == null ? (p.isNew ? "new" : "—") : `${d >= 0 ? "+" : ""}${Math.round(d)}%`, cls: d == null ? undefined : d >= 0 ? "up" : "down" },
    { k: "TVL", v: usd(p.metrics.tvlUsd) },
    { k: "Pools", v: int(p.metrics.poolsN || p.pools.length) },
    { k: "Wallets 24h", v: int(u.wallets24h) },
    { k: swaps.k, v: int(swaps.v) },
  ];
  const busy = busiestPool(p);
  const why =
    p.whyBlurb ||
    `${usd(p.metrics.vol7d)} traded through this hook in the last 7 days across ${int(p.metrics.poolsN)} pool${p.metrics.poolsN === 1 ? "" : "s"}${
      d != null ? `, ${d >= 0 ? "up" : "down"} ${Math.abs(Math.round(d))}% on the week` : ""
    }.`;

  return (
    <>
      <div className="panel">
        <div className={s.head}>
          <ProjectAvatar name={p.name} color={p.color} chain={p.chains[0]} size="lg" />
          <div className={s.title}>
            <div className={s.titleLine}>
              <h1>{p.name}</h1>
              <WatchStar slug={p.slug} name={p.name} />
            </div>
            <div className={s.badges}>
              {p.hooklisted ? <span className="b b-v">✓ Hooklist</span> : <span className="b b-n">Not in hooklist</span>}
              {p.signals.routable === "eligible" && <span className="b b-g">Routes in Uniswap app</span>}
              {p.signals.routable === "ineligible" && <span className="b b-n">Not routable in app</span>}
              {p.signals.routable === "unknown" && <span className="b b-n">Routing unknown</span>}
              {p.signals.auditCount > 0 ? <span className="b b-g">Audit linked</span> : <span className="b b-n">No audit linked</span>}
              {p.upgradeable && <span className="b b-a">Upgradeable</span>}
              {p.isNew && <span className="b b-p">New · {ago(p.firstSeenAt, now)}</span>}
              {!p.isNew && p.rank && <span className="b b-a">#{p.rank} trending</span>}
              {u.smartWallets != null && u.smartWallets > 0 && (
                <span className="b b-p">
                  ◆ {u.smartWallets} smart-money wallet{u.smartWallets === 1 ? "" : "s"}
                </span>
              )}
            </div>
          </div>
          <div className={s.where}>
            {categoryLine(p)}
            <br />
            {chainNames(p.chains, " · ")}
          </div>
        </div>
        <div className={s.stats}>
          {stats.map((x) => (
            <div key={x.k} className={s.stat}>
              <div className={s.k}>{x.k}</div>
              <div className={`${s.val} num ${x.cls ?? ""}`}>{x.v}</div>
            </div>
          ))}
        </div>
      </div>

      <div className={`panel ${s.chart}`}>
        <h3>Daily volume</h3>
        <Bars series={p.series} />
      </div>

      <div className={s.two}>
        <div className={`panel ${s.blk}`}>
          <h3>Pools</h3>
          <PoolsTable pools={p.pools} total={p.metrics.poolsN} />
        </div>
        <div className={`panel ${s.blk}`}>
          <h3>Activity</h3>
          {busy && knownChain(busy.chainId) && (
            <LiveTrades chainId={busy.chainId} chainSlug={chainSlug(busy.chainId)} poolId={busy.poolId} pair={`${poolPair(busy)} on ${CHAINS[busy.chainId]?.name ?? ""}`} />
          )}
          {busy && <div className={s.sub}>Signals</div>}
          <div className={s.feed}>
            {events(p, busy, now).map((e, i) => (
              <div key={i} className={s.ev}>
                <span className={`b ${e.cls}`}>{e.label}</span>
                <span className={s.evX}>{e.text}</span>
                {e.at && <span className={s.evT}>{ago(e.at, now)}</span>}
              </div>
            ))}
          </div>
        </div>
      </div>

      <TopWallets p={p} now={now} />

      <div className={`panel ${s.blk}`}>
        <h3>Why people care</h3>
        <div className={s.why}>{why}</div>
        {p.receipts.length > 0 && (
          <div className={s.recs}>
            {p.receipts.slice(0, 6).map((r) => (
              <a key={r.url + r.title} href={r.url} target="_blank" rel="noopener">
                <span>{r.title}</span>
                <span className="mut3">
                  {sourceLabel(r.url, r.source)}
                  {r.publishedAt ? ` · ${age(r.publishedAt, now)}` : ""}
                </span>
              </a>
            ))}
          </div>
        )}
        {(p.website || p.github || p.xHandle) && (
          <div className={s.links}>
            {p.website && (
              <a href={p.website} target="_blank" rel="noopener">
                Website
              </a>
            )}
            {p.github && (
              <a href={p.github} target="_blank" rel="noopener">
                GitHub
              </a>
            )}
            {p.xHandle && (
              <a href={`https://x.com/${p.xHandle}`} target="_blank" rel="noopener">
                @{p.xHandle}
              </a>
            )}
          </div>
        )}
      </div>
    </>
  );
}

function Bars({ series }: { series: (number | null)[] }) {
  if (series.length < 2) return <div className={s.chartEmpty}>Daily history starts with the next snapshot.</div>;
  const W = 640;
  const H = 170;
  const pl = 4;
  const pr = 4;
  const pt = 12;
  const pb = 22;
  const n = series.length;
  const max = Math.max(0, ...series.filter((v): v is number => v != null)) || 1;
  const bw = (W - pl - pr) / n;
  return (
    <svg className={s.bars} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Daily volume, last ${n} days, peak ${usd(max)}`}>
      <line className={s.gl} x1={pl} x2={W - pr} y1={pt} y2={pt} />
      <line className={s.gl} x1={pl} x2={W - pr} y1={H - pb} y2={H - pb} />
      {series.map((v, i) => {
        if (v == null) return null; // volume unknown that day: a gap, not a $0 bar
        const bh = ((H - pt - pb) * v) / max;
        return (
          <rect
            key={i}
            x={(pl + i * bw + bw * 0.18).toFixed(1)}
            y={(H - pb - bh).toFixed(1)}
            width={(bw * 0.64).toFixed(1)}
            height={Math.max(bh, 1).toFixed(1)}
            rx={3}
            fill={i === n - 1 ? "#ff007a" : "rgba(255,0,122,.42)"}
          >
            <title>{`${n - 1 - i === 0 ? "Today" : `${n - 1 - i}d ago`}: ${usd(v)}`}</title>
          </rect>
        );
      })}
      <text className={s.ax} x={pl} y={H - 6}>
        {n}d ago
      </text>
      <text className={s.ax} x={W - pr} y={H - 6} textAnchor="end">
        today
      </text>
      <text className={s.ax} x={W - pr} y={pt - 2} textAnchor="end">
        peak {usd(max)}
      </text>
    </svg>
  );
}

function PoolsTable({ pools, total }: { pools: PoolView[]; total: number }) {
  const shown = pools.filter((x) => knownChain(x.chainId)).slice(0, POOLS_SHOWN);
  if (!shown.length) return <div className="mut">No pools indexed yet.</div>;
  return (
    <>
      <div className={s.tw}>
        <table className={s.table}>
          <thead>
            <tr>
              <th>Pool</th>
              <th>Chain</th>
              <th>TVL</th>
              <th>24h vol</th>
              <th>Wallets</th>
              <th>Fee</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((x) => (
              <tr key={`${x.chainId}:${x.poolId}`}>
                <td>
                  <a href={poolDeepLink(x.chainId, x.poolId)} target="_blank" rel="noopener">
                    {poolPair(x)}
                  </a>
                </td>
                <td>{CHAINS[x.chainId]?.name ?? x.chainId}</td>
                <td className="num">{usd(x.tvlUsd)}</td>
                <td className="num">{usd(x.vol24h)}</td>
                <td className="num">{int(x.wallets24h)}</td>
                <td>{feeLabel(x.fee)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {Math.max(total, pools.length) > shown.length && (
        <div className={s.more}>
          Top {shown.length} of {int(Math.max(total, pools.length))} pools by TVL.
        </div>
      )}
    </>
  );
}

type Ev = { cls: string; label: string; text: string; at?: string };

/** The mock's derived events: the fallback when live trades are empty, and context under them. */
function events(p: ProjectDetail, busy: PoolView | null, now: number): Ev[] {
  const out: Ev[] = [];
  for (const x of p.pools.slice(0, 20)) {
    if (x.createdAt && (now - Date.parse(x.createdAt)) / DAY <= 14) {
      out.push({ cls: "b-p", label: "New pool", text: `${poolPair(x)} on ${CHAINS[x.chainId]?.name ?? x.chainId}`, at: x.createdAt });
    }
  }
  const d = weekDelta(p.metrics);
  if (d != null && Math.abs(d) >= 10) {
    out.push({
      cls: d > 0 ? "b-g" : "b-r",
      label: d > 0 ? "Volume up" : "Volume down",
      text: `${Math.abs(Math.round(d))}% week over week, ${usd(p.metrics.vol7d)} in 7 days`,
    });
  }
  if (busy && busy.vol24h != null) {
    out.push({
      cls: "b-n",
      label: "Top pool",
      text: `${poolPair(busy)}: ${usd(busy.vol24h)}${busy.wallets24h != null ? ` from ${int(busy.wallets24h)} wallets` : ""} in 24h`,
    });
  }
  if (p.upgradeable) out.push({ cls: "b-a", label: "Upgradeable", text: "The hook owner can change its code; read it before you trade." });
  out.push(
    p.hooklisted
      ? { cls: "b-v", label: "Registry", text: `Listed in the Uniswap hooklist${p.signals.auditCount > 0 ? " with an audit link" : ""}` }
      : { cls: "b-n", label: "Discovered on-chain", text: "Not in the Uniswap hooklist; found from its pools. Name and flags come from the chain." },
  );
  return out;
}

function TopWallets({ p, now }: { p: ProjectDetail; now: number }) {
  return (
    <div className={`panel ${s.blk}`}>
      <div className={s.h3row}>
        <h3>Top wallets</h3>
        {p.topWallets.length > 0 && <span>by 24h volume in the busiest pools</span>}
      </div>
      {p.topWallets.length === 0 ? (
        <div className="mut">Wallet breakdown appears after the next activity run.</div>
      ) : (
        <div className={s.tw}>
          <table className={s.table}>
            <thead>
              <tr>
                <th>Wallet</th>
                <th>Volume</th>
                <th>Trades</th>
                <th>Buys / sells</th>
                <th>Last</th>
              </tr>
            </thead>
            <tbody>
              {p.topWallets.map((w) => {
                const href = explorerAddressUrl(w.chainId, w.address);
                return (
                  <tr key={`${w.chainId}:${w.address}`}>
                    <td>
                      <span className={s.wallet}>
                        <WalletAvatar address={w.address} size="sm" />
                        {href ? (
                          <a className="mono" href={href} target="_blank" rel="noopener">
                            {shortAddr(w.address)}
                          </a>
                        ) : (
                          <span className="mono">{shortAddr(w.address)}</span>
                        )}
                        <span className={s.tTags}>
                          {w.smartMoney && <span className="b b-p">◆ Smart money</span>}
                          {w.labels.slice(0, w.smartMoney ? 1 : 2).map((l) => (
                            <span key={l} className="b b-n">
                              {l}
                            </span>
                          ))}
                        </span>
                      </span>
                    </td>
                    <td className="num">{usd(w.volumeUsd)}</td>
                    <td className="num">{int(w.trades)}</td>
                    <td className="num">
                      {w.buys != null || w.sells != null ? (
                        <span className={s.bs}>
                          <span className="up">{int(w.buys ?? 0)}</span>/<span className="down">{int(w.sells ?? 0)}</span>
                        </span>
                      ) : (
                        "–"
                      )}
                    </td>
                    <td className="num mut3">{w.lastTradeAt ? `${since(w.lastTradeAt, now)} ago` : "–"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function EmptyDetail({ text }: { text: string }) {
  return <div className={`panel ${s.emptyDetail}`}>{text}</div>;
}
