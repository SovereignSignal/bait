// Project detail column (server): header, stats, daily volume, pools, activity, top wallets, why.
import { CHAINS, chainSlug, explorerAddressUrl, knownChain, poolDeepLink } from "@/lib/chains";
import { weekDelta } from "@/lib/feed";
import { ago, age, dayLabel, deltaWords, feeLabel, fmtDelta, int, NONE, pair, shortAddr, since, sourceLabel, usd, usdWhole } from "@/lib/format";
import { activityNote, routingNote } from "@/lib/research";
import { FRESH_NOTE, type Freshness } from "@/lib/health";
import { activePool, categoryLine, chainNames, dayWindow, poolsNote, poolTvlNote, poolVolume, seriesLabels, walletsText, washLine, auditMention, topWalletText, deployerShareText, deployersOf } from "@/lib/present";
import type { PoolView, ProjectDetail } from "@/lib/types";
import { variants } from "@/lib/variants";
import { ProjectAvatar, WalletAvatar } from "./Avatar";
import { indexerLine } from "@/lib/trades";
import { LiveTrades } from "./LiveTrades";
import { ProjectHeading } from "./ProjectHeading";
import { WatchStar } from "./WatchStar";
import s from "./Detail.module.css";

const DAY = 86_400_000;
const POOLS_SHOWN = 8;
const VARIANTS_SHOWN = 6;

/** Most active pool (closed-day volume), else the deepest. */
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
  // The day the data ends on, else the run receipt's: labels name that UTC day, never "24h" (see dayWindow).
  const through = p.through ?? fresh?.through ?? null;
  const win = dayWindow(through);
  const stats: Array<{ k: string; v: string; cls?: string; title?: string }> = [
    { k: "7d volume", v: usd(p.metrics.vol7d) },
    { k: "vs last week", v: d == null ? (p.isNew ? "new" : NONE) : fmtDelta(d), cls: d == null ? undefined : d >= 0 ? "up" : "down" },
    { k: "TVL", v: usd(p.metrics.tvlUsd) },
    { k: "Pools", v: int(p.metrics.poolsN || p.pools.length) },
    {
      k: `Wallets ${win.short}`,
      v: walletsText(u.wallets24h, u.walletsExact),
      title: u.walletsExact === false ? "Upper bound: buyers + sellers per pool, so a wallet that bought and sold counts twice." : win.note,
    },
    { k: "Wallets 7d", v: int(u.wallets7d), title: "Distinct wallets across the project's pools over the seven closed days, from the indexer. Unknown unless every chain the project is on has caught up through the week." },
    { k: "Top wallet", v: topWalletText(u.topWalletShare7d), title: "Share of the week's swaps made by the single busiest wallet. Of swaps, not volume. Shown at any size; the wash rules only act past $250K a day." },
    { k: "Deployer's swaps", v: deployerShareText(u.deployerSwapShare7d), title: "Share of the week's swaps made by the wallets that deployed this project's hooks (registry-supplied or the explorer's contract creator; CREATE2 factories excluded). Unknown when no deployer is known or coverage is incomplete. A team's own trades are not outside demand." },
    // the closed UTC day, from score; when a day is missing, the rolling buys + sells the trade card shows, marked "≈"
    (u.tx24h == null || (u.tx24h === 0 && (u.wallets24h ?? 0) > 0)) && u.buys24h != null && u.sells24h != null && u.buys24h + u.sells24h > 0
      ? { k: "Swaps 24h", v: `≈ ${int(u.buys24h + u.sells24h)}`, title: "Rolling 24h buys + sells from the pool feed; the closed-day count is not in yet." }
      : { k: `Swaps ${win.short}`, v: int(u.tx24h), title: win.note },
  ];
  const routing = routingNote(p.signals.routable);
  const activity = activityNote(p);
  const badges: Array<{ cls: string; label: string; why: string; href?: string }> = [
    ...(p.isWash ? [{ cls: "b-r", label: p.washHistorical ? "Historical wash-like volume" : "Wash-like volume", why: washLine(p.washReason, p.washHistorical) }] : []),
    p.hooklisted
      ? { cls: "b-v", label: "✓ Hooklist", why: "Listed in the Uniswap hooklist, the public registry of v4 hooks." }
      : { cls: "b-n", label: "Not in hooklist", why: "Found on-chain from its pools; not in the hooklist registry." },
    { cls: "b-n", label: routing.label, why: routing.detail },
    ...(activity ? [{ cls: "b-n", label: activity.label, why: activity.detail }] : []),
    // only with a link to follow: a count without a URL is not an audit anyone can read
    p.signals.auditUrl
      ? { cls: "b-g", label: "Audit linked", why: `Audit link: ${p.signals.auditUrl}. It says nothing about scope or findings.`, href: p.signals.auditUrl }
      : auditMention(p.whatLine, p.whyBlurb)
        ? { cls: "b-a", label: "Audit mentioned, not linked", why: "The description mentions an audit, but neither the registry nor the team's published details link one. Treat it as unverified." }
        : { cls: "b-n", label: "No audit link", why: "No audit link found." },
    ...(u.topWalletShare7d != null && u.topWalletShare7d >= 0.5 && (u.wallets7d ?? 0) > 0
      ? [{ cls: "b-a", label: `One wallet, ${topWalletText(u.topWalletShare7d)}`, why: "A single wallet made at least half of this week's swaps across the project's pools. Not a wash finding; small hooks often start this way. Check who it is before reading the volume as demand." }]
      : []),
    ...(u.deployerSwapShare7d != null && u.deployerSwapShare7d >= 0.5
      ? [{ cls: "b-a", label: `Deployer made ${topWalletText(u.deployerSwapShare7d)}`, why: "The wallets that deployed this project's hooks made at least half of this week's swaps. The team trading its own pools is not outside demand; the volume may still be real." }]
      : []),
    ...(p.upgradeable ? [{ cls: "b-a", label: "Upgradeable", why: "The owner can change this hook's code. Read it before you trade." }] : []),
    ...(p.isNew ? [{ cls: "b-p", label: `New · ${ago(p.firstSeenAt, now)}`, why: "First seen in the last 7 days." }] : []),
    ...(!p.isNew && p.rank ? [{ cls: "b-a", label: `#${p.rank} trending`, why: "Rank by 7-day volume growth, among projects older than a week with at least $10K this week and $1K the week before." }] : []),
    ...(u.smartWallets != null && u.smartWallets > 0
      ? [{ cls: "b-p", label: `◆ ${u.smartWallets} smart wallet${u.smartWallets === 1 ? "" : "s"}`, why: "Wallets Nansen labelled as smart money (labels collected until October 2026) traded here in the last day." }]
      : []),
  ];
  const partial = p.chains.filter((c) => fresh?.partialChains?.includes(c));
  const vs = variants(p.hooks);
  const deployers = deployersOf(p.hooks);
  const busy = busiestPool(p);
  const pools = `${int(p.metrics.poolsN)} pool${p.metrics.poolsN === 1 ? "" : "s"}`;
  const why =
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
              {badges.map((b) =>
                b.href ? (
                  <a key={b.label} className={`b ${b.cls}`} title={b.why} href={b.href} target="_blank" rel="noopener nofollow">
                    {b.label} ↗
                  </a>
                ) : (
                  <span key={b.label} className={`b ${b.cls}`} title={b.why}>
                    {b.label}
                  </span>
                ),
              )}
            </div>
            {vs.length > 0 && (
              <div className={s.variants} data-testid="variants" aria-label="Deployments">
                {vs.slice(0, VARIANTS_SHOWN).map((v) => (
                  <span key={v.label} className="b b-n" title={`${v.hooks} hook${v.hooks === 1 ? "" : "s"} registered as ${v.label}`}>
                    {v.label} · {chainNames(v.chains, ", ")}
                  </span>
                ))}
                {vs.length > VARIANTS_SHOWN && <span className="b b-n">+{vs.length - VARIANTS_SHOWN} more</span>}
              </div>
            )}
            {deployers.length > 0 && (
              <p className="mut3" data-testid="deployer">
                Deployer{deployers.length > 1 ? "s" : ""}:{" "}
                {deployers.slice(0, 2).map((d, i) => {
                  const href = explorerAddressUrl(d.chainId, d.address);
                  const label = `${shortAddr(d.address)} (${chainNames([d.chainId], "")}, ${d.source === "hooklist" ? "registry" : d.source === "explorer" ? "contract creator" : "source unknown"})`;
                  return (
                    <span key={`${d.chainId}:${d.address}`}>
                      {i > 0 ? ", " : ""}
                      {href ? <a href={href} target="_blank" rel="noopener nofollow" className="mono">{label} ↗</a> : <span className="mono">{label}</span>}
                    </span>
                  );
                })}
                {deployers.length > 2 ? ` +${deployers.length - 2} more` : ""}
              </p>
            )}
            {/* the titles above are hover-only; this works on touch and keyboard */}
            <details className={s.badgeHelp}>
              <summary>What these badges mean</summary>
              <dl>
                {badges.map((b) => (
                  <div key={b.label}>
                    <dt>{b.label}</dt>
                    <dd>{b.why}</dd>
                  </div>
                ))}
              </dl>
            </details>
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
        {through && (
          <p className={s.fresh} data-testid="freshness">
            <span>Figures through {dayLabel(through)} (UTC), updated nightly</span>
            {fresh?.note && <span className="b b-a">{FRESH_NOTE[fresh.note]}</span>}
            {partial.length > 0 && (
              <span className="b b-a" title="Some on-chain history is pending or freshness cannot be confirmed. Available provider data may be used instead; figures are not verified totals." data-testid="partial-chain">
                {chainNames(partial, ", ")} still indexing: figures partial
              </span>
            )}
          </p>
        )}
      </div>

      <div className={`panel ${s.blk}`}>
        <h2>Project context and evidence</h2>
        <div className={s.why}>{why}</div>
        <p className="mut3">Usage describes the available seven-day window{through ? ` ending ${dayLabel(through)} UTC` : ""}. Volume alone does not establish organic demand, safety, or returns.</p>
        {p.isDormant && <p className="mut">Retained for research and search. Excluded from New, Trending, Movers and automatic-routing discovery.</p>}
        {p.hasHistoricalUsage && p.activityStatus === "unknown" && <p className="mut">Retained on historical usage evidence. Missing recent coverage is not evidence of zero activity.</p>}
        {p.whyBlurb && (
          <details className={s.badgeHelp}>
            <summary>Historical generated context{p.whyBlurbAt ? ` · ${dayLabel(p.whyBlurbAt.slice(0, 10))}` : " · date unknown"}</summary>
            <p>{p.whyBlurb}</p>
            <p className="mut3">Generated research may be stale or incorrect. Its numbers refer to the generation date, not the current snapshot.</p>
          </details>
        )}
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

      <div className={`panel ${s.chart}`}>
        <h2>Daily volume</h2>
        <Bars series={p.series} through={through} />
      </div>

      <div className={s.two}>
        <div className={`panel ${s.blk}`}>
          <h2>Pools</h2>
          <PoolsTable pools={p.pools} total={p.metrics.poolsN} tvl={p.metrics.tvlUsd} win={win} />
        </div>
        <div className={`panel ${s.blk}`}>
          <h2>Activity</h2>
          {busy && knownChain(busy.chainId) && (
            <LiveTrades
              chainId={busy.chainId}
              chainSlug={chainSlug(busy.chainId)}
              poolId={busy.poolId}
              pair={`${poolPair(busy)} on ${CHAINS[busy.chainId]?.name ?? ""}`}
              indexer={indexerLine(busy)}
            />
          )}
          {busy && <div className={s.sub}>Signals</div>}
          <div className={s.feed}>
            {events(p, busy, now, win.short).map((e, i) => (
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


    </>
  );
}

function Bars({ series, through }: { series: (number | null)[]; through: string | null }) {
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
  const labels = seriesLabels(n, through);
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
            <title>{`${labels[i]}: ${usd(v)}`}</title>
          </rect>
        );
      })}
    </svg>
    <div className={s.axis}>
      <span>{labels[0]}</span>
      <span>peak {usd(max)}</span>
      <span>{labels[n - 1]}{through ? " (UTC)" : ""}</span>
    </div>
    </>
  );
}

/** In the table the reason is a tooltip; the phone stack has no hover, so it says it inline and drops the day label. */
function PoolVolume({ x, day, stacked = false }: { x: PoolView; day: string; stacked?: boolean }) {
  const v = poolVolume(x);
  if (!v.note) return <>{stacked ? `${day} ${v.text}` : v.text}</>;
  return (
    <span className="mut" title={v.note}>
      {v.text}
      {stacked && " (USD unknown)"}
    </span>
  );
}

function PoolsTable({ pools, total, tvl, win }: { pools: PoolView[]; total: number; tvl: number; win: { short: string; note: string } }) {
  const known = pools.filter((x) => knownChain(x.chainId));
  const active = known.filter(activePool);
  const shown = (active.length ? active : known).slice(0, POOLS_SHOWN);
  // the header TVL is score's sum over every pool: say where the part not in these rows is
  const note = [poolsNote(shown.length, active.length, known.length, total), poolTvlNote(tvl, shown)].filter(Boolean).join(" ") || null;
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
              <th title={win.note}>Vol {win.short}</th>
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
                  <PoolVolume x={x} day={win.short} />
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
              TVL {usd(x.tvlUsd)} · <PoolVolume x={x} day={win.short} stacked /> · {walletsText(x.wallets24h, x.walletsExact)} wallets
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
function events(p: ProjectDetail, busy: PoolView | null, now: number, day: string): Ev[] {
  const out: Ev[] = [];
  if (p.isWash) out.push({ cls: "b-r", label: p.washHistorical ? "Historical wash-like" : "Wash-like", text: washLine(p.washReason, p.washHistorical) });
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
  if (busy && busy.vol24h != null && busy.vol24h > 0) {
    out.push({
      cls: "b-n",
      label: "Top pool",
      text: `${poolPair(busy)}: ${usd(busy.vol24h)}${busy.wallets24h != null ? ` from ${walletsText(busy.wallets24h, busy.walletsExact)} wallets` : ""} ${day === "1d" ? "in the last closed day" : `on ${day}`}`,
    });
  }
  if (p.upgradeable) out.push({ cls: "b-a", label: "Upgradeable", text: "The hook owner can change its code; read it before you trade." });
  out.push(
    p.hooklisted
      ? { cls: "b-v", label: "Registry", text: `Listed in the Uniswap hooklist${p.signals.auditUrl ? " with an audit link" : ""}` }
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
        <div className="mut">No wallet data for this project yet.</div>
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
