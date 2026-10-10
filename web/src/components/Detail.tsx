// Project detail column (server): header, stats, daily volume, pools, activity, top wallets, why.
import { CHAINS, chainSlug, explorerAddressUrl, knownChain, poolDeepLink } from "@/lib/chains";
import { weekDelta } from "@/lib/feed";
import { ago, age, dayLabel, deltaWords, feeLabel, fmtDelta, int, NONE, pair, shortAddr, since, sourceLabel, usd, usdWhole } from "@/lib/format";
import { FRESH_NOTE, type Freshness } from "@/lib/health";
import { activePool, categoryLine, chainNames, poolsNote, poolVolume, walletsText, washLine } from "@/lib/present";
import type { PoolView, ProjectDetail } from "@/lib/types";
import { ProjectAvatar, WalletAvatar } from "./Avatar";
import { LiveTrades } from "./LiveTrades";
import { ProjectHeading } from "./ProjectHeading";
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

export function Detail({ p, now, fresh }: { p: ProjectDetail; now: number; fresh?: Freshness | null }) {
  const d = weekDelta(p.metrics);
  const u = p.usage;
  const stats: Array<{ k: string; v: string; cls?: string; title?: string }> = [
    { k: "7d volume", v: usd(p.metrics.vol7d) },
    { k: "vs last week", v: d == null ? (p.isNew ? "new" : NONE) : fmtDelta(d), cls: d == null ? undefined : d >= 0 ? "up" : "down" },
    { k: "TVL", v: usd(p.metrics.tvlUsd) },
    { k: "Pools", v: int(p.metrics.poolsN || p.pools.length) },
    {
      k: "Wallets 24h",
      v: walletsText(u.wallets24h, u.walletsExact),
      title: u.walletsExact === false ? "Upper bound: buyers + sellers per pool, so a wallet that bought and sold counts twice." : undefined,
    },
    // the closed UTC day, from score: no longer GeckoTerminal's rolling buys + sells
    { k: "Swaps 24h", v: int(u.tx24h) },
  ];
  const busy = busiestPool(p);
  const pools = `${int(p.metrics.poolsN)} pool${p.metrics.poolsN === 1 ? "" : "s"}`;
  const why =
    p.whyBlurb ||
    (p.metrics.vol7d == null ? `The last 7 days' volume through this hook's ${pools} is unknown.` : null) ||
    `${usd(p.metrics.vol7d)} traded through this hook in the last 7 days across ${int(p.metrics.poolsN)} pool${p.metrics.poolsN === 1 ? "" : "s"}${
      d != null ? `, ${d >= 0 ? "up" : "down"} ${deltaWords(d)} on the week` : ""
    }.`;

  return (
    <>
      <div className="panel">
        <div className={s.head}>
          <ProjectAvatar name={p.name} color={p.color} chain={p.chains[0]} size="lg" />
          <div className={s.title}>
            <div className={s.titleLine}>
              <ProjectHeading name={p.name} />
              <WatchStar slug={p.slug} name={p.name} />
            </div>
            {p.whatLine && <p className={s.what}>{p.whatLine}</p>}
            <div className={s.badges} data-testid="badges">
              {p.isWash && (
                <span className="b b-r" title={washLine(p.washReason)}>
                  Wash-like volume
                </span>
              )}
              {p.hooklisted ? (
                <span className="b b-v" title="Listed in the Uniswap hooklist, the public registry of v4 hooks.">✓ Hooklist</span>
              ) : (
                <span className="b b-n" title="Found on-chain from its pools; not in the hooklist registry.">Not in hooklist</span>
              )}
              {p.signals.routable === "eligible" && (
                <span className="b b-g" title="At least one pool should route in the Uniswap app. A rule, not a guarantee.">In Uniswap app</span>
              )}
              {p.signals.routable === "ineligible" && <span className="b b-n" title="The Uniswap app will not route swaps through this hook.">Not in app</span>}
              {p.signals.routable === "unknown" && <span className="b b-n" title="Not enough data to say whether the app routes through this hook.">Routing unknown</span>}
              {p.signals.auditCount > 0 ? (
                <span className="b b-g" title="An audit link is published. It says nothing about scope or findings.">Audit linked</span>
              ) : (
                <span className="b b-n" title="No audit link found.">No audit</span>
              )}
              {p.upgradeable && <span className="b b-a" title="The owner can change this hook's code. Read it before you trade.">Upgradeable</span>}
              {p.isNew && <span className="b b-p" title="First seen in the last 7 days.">New · {ago(p.firstSeenAt, now)}</span>}
              {!p.isNew && p.rank && <span className="b b-a" title="Rank by 7-day volume growth.">#{p.rank} trending</span>}
              {u.smartWallets != null && u.smartWallets > 0 && (
                <span className="b b-p" title="Wallets Nansen labels as smart money traded here in the last day.">
                  ◆ {u.smartWallets} smart wallet{u.smartWallets === 1 ? "" : "s"}
                </span>
              )}
            </div>
            <div className={s.whereSub}>
              {categoryLine(p)} · {chainNames(p.chains, " · ")}
            </div>
          </div>
          <div className={s.where}>
            {categoryLine(p)}
            <br />
            {chainNames(p.chains, " · ")}
          </div>
        </div>
        <div className={s.statsBox}>
          <div className={s.stats}>
            {stats.map((x) => (
              <div key={x.k} className={s.stat} title={x.title}>
                <div className={s.k}>{x.k}</div>
                <div className={`${s.val} num ${x.cls ?? ""}`}>{x.v}</div>
              </div>
            ))}
          </div>
        </div>
        {fresh?.through && (
          <p className={s.fresh} data-testid="freshness">
            <span>Figures through {dayLabel(fresh.through)}, updated nightly</span>
            {fresh.note && <span className="b b-a">{FRESH_NOTE[fresh.note]}</span>}
          </p>
        )}
      </div>

      <div className={`panel ${s.chart}`}>
        <h2>Daily volume</h2>
        <Bars series={p.series} />
      </div>

      <div className={s.two}>
        <div className={`panel ${s.blk}`}>
          <h2>Pools</h2>
          <PoolsTable pools={p.pools} total={p.metrics.poolsN} />
        </div>
        <div className={`panel ${s.blk}`}>
          <h2>Activity</h2>
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
        <h2>Why people care</h2>
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
  const pb = 4;
  const n = series.length;
  const max = Math.max(0, ...series.filter((v): v is number => v != null)) || 1;
  const bw = (W - pl - pr) / n;
  return (
    <>
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
            <title>{`${n - 1 - i === 0 ? "Yesterday" : `${n - i}d ago`}: ${usd(v)}`}</title>
          </rect>
        );
      })}
    </svg>
    <div className={s.axis}>
      <span>{n}d ago</span>
      <span>peak {usd(max)}</span>
      <span>yesterday</span>
    </div>
    </>
  );
}

function PoolVolume({ x }: { x: PoolView }) {
  const v = poolVolume(x);
  return v.note ? (
    <span className="mut" title={v.note}>
      {v.text}
    </span>
  ) : (
    <>{v.text}</>
  );
}

function PoolsTable({ pools, total }: { pools: PoolView[]; total: number }) {
  const known = pools.filter((x) => knownChain(x.chainId));
  const active = known.filter(activePool);
  const shown = (active.length ? active : known).slice(0, POOLS_SHOWN);
  const note = poolsNote(shown.length, active.length, known.length, total);
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
                <td className="num">
                  <PoolVolume x={x} />
                </td>
                <td className="num">{walletsText(x.wallets24h, x.walletsExact)}</td>
                <td>{feeLabel(x.fee)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className={s.stack}>
        {shown.map((x) => (
          <li key={`${x.chainId}:${x.poolId}`}>
            <a href={poolDeepLink(x.chainId, x.poolId)} target="_blank" rel="noopener">
              {poolPair(x)}
            </a>
            <span className="mut">
              {" "}
              · {CHAINS[x.chainId]?.name ?? x.chainId} · {feeLabel(x.fee)}
            </span>
            <div className={`${s.stackSub} num`}>
              TVL {usd(x.tvlUsd)} · 24h <PoolVolume x={x} /> · {walletsText(x.wallets24h, x.walletsExact)} wallets
            </div>
          </li>
        ))}
      </ul>
      {note && <div className={s.more}>{note}</div>}
    </>
  );
}

type Ev = { cls: string; label: string; text: string; at?: string };

/** The mock's derived events: the fallback when live trades are empty, and context under them. */
function events(p: ProjectDetail, busy: PoolView | null, now: number): Ev[] {
  const out: Ev[] = [];
  if (p.isWash) out.push({ cls: "b-r", label: "Wash-like", text: washLine(p.washReason) });
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
      text: `${deltaWords(d)} week over week, ${usd(p.metrics.vol7d)} in 7 days`,
    });
  }
  if (busy && busy.vol24h != null) {
    out.push({
      cls: "b-n",
      label: "Top pool",
      text: `${poolPair(busy)}: ${usd(busy.vol24h)}${busy.wallets24h != null ? ` from ${walletsText(busy.wallets24h, busy.walletsExact)} wallets` : ""} in 24h`,
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

function WalletWho({ w }: { w: ProjectDetail["topWallets"][number] }) {
  const href = explorerAddressUrl(w.chainId, w.address);
  return (
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
  );
}

function TopWallets({ p, now }: { p: ProjectDetail; now: number }) {
  return (
    <div className={`panel ${s.blk}`}>
      <div className={s.h3row}>
        <h2>Top wallets</h2>
        {p.topWallets.length > 0 && <span>by volume in the latest ~300 trades of each busy pool</span>}
      </div>
      {p.topWallets.length === 0 ? (
        <div className="mut">No wallet data for this project yet. It usually fills in within a day.</div>
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
                return (
                  <tr key={`${w.chainId}:${w.address}`}>
                    <td>
                      <WalletWho w={w} />
                    </td>
                    <td className="num">{usdWhole(w.volumeUsd)}</td>
                    <td className="num">{int(w.trades)}</td>
                    <td className="num">
                      {w.buys != null || w.sells != null ? (
                        <span className={s.bs}>
                          <span className="up">{int(w.buys ?? 0)}</span>/<span className="down">{int(w.sells ?? 0)}</span>
                        </span>
                      ) : (
                        NONE
                      )}
                    </td>
                    <td className="num mut3">{w.lastTradeAt ? `${since(w.lastTradeAt, now)} ago` : NONE}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {p.topWallets.length > 0 && (
        <ul className={s.stack}>
          {p.topWallets.map((w) => (
            <li key={`${w.chainId}:${w.address}`}>
              <WalletWho w={w} />
              <div className={`${s.stackSub} num`}>
                {usdWhole(w.volumeUsd)} · {int(w.trades)} trade{w.trades === 1 ? "" : "s"} · {int(w.buys ?? 0)}/{int(w.sells ?? 0)} · {w.lastTradeAt ? `${since(w.lastTradeAt, now)} ago` : NONE}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function EmptyDetail({ text }: { text: string }) {
  return <div className={`panel ${s.emptyDetail}`}>{text}</div>;
}
