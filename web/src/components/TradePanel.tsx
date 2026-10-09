// Right column: pool action card (links out to Uniswap), buys vs sells, hook facts.
import { CHAINS, explorerAddressUrl, knownChain, poolDeepLink } from "@/lib/chains";
import { feeLabel, int, NONE, paletteColor, shortAddr } from "@/lib/format";
import { pace } from "@/lib/present";
import { eligibility } from "@/lib/routable";
import type { ProjectDetail } from "@/lib/types";
import { WatchStar } from "./WatchStar";
import s from "./Trade.module.css";

function Token({ sym }: { sym: string }) {
  return (
    <span className={s.tok}>
      <span className={s.dot} style={{ background: paletteColor(sym) }}>
        {sym.slice(0, 1)}
      </span>
      {sym}
    </span>
  );
}

export function TradePanel({ p }: { p: ProjectDetail }) {
  const pool = p.pools.find((x) => knownChain(x.chainId)) ?? null;
  const hookProps = (addr: string | null | undefined) => p.hooks.find((h) => h.address.toLowerCase() === (addr ?? "").toLowerCase())?.properties ?? {};
  const top = pool
    ? { poolId: pool.poolId, chainId: pool.chainId, s0: pool.token0Symbol, s1: pool.token1Symbol, fee: feeLabel(pool.fee), hook: pool.hookAddress, eligibility: pool.eligibility, tvlUsd: pool.tvlUsd as number | null }
    : p.topPool && knownChain(p.topPool.chainId)
      ? {
          poolId: p.topPool.poolId, chainId: p.topPool.chainId, s0: p.topPool.token0Symbol, s1: p.topPool.token1Symbol, fee: "", hook: p.topPool.hookAddress,
          eligibility: p.topPool.hookAddress ? eligibility(p.topPool.hookAddress, {}, hookProps(p.topPool.hookAddress), null) : p.signals.routable,
          tvlUsd: p.metrics.tvlUsd as number | null,
        }
      : null;
  const s0 = top?.s0 ?? "?";
  const s1 = top?.s1 ?? "?";
  const link = top ? poolDeepLink(top.chainId, top.poolId) : null;
  const chainName = top ? (CHAINS[top.chainId]?.name ?? "") : "";
  const hook = (top?.hook ?? p.hooks[0]?.address ?? "").toLowerCase();
  const hookChain = top?.chainId ?? p.hooks[0]?.chainId;
  const hookHref = hook && hookChain != null ? explorerAddressUrl(hookChain, hook) : null;
  const cta =
    link && top
      ? (() => {
          const elig = top.eligibility;
          const tvl = top.tvlUsd ?? 0;
          const ok = elig === "eligible" && tvl >= 1000 && s0 !== "?" && s1 !== "?";
          const reason =
            elig === "ineligible"
              ? "This hook does not route in the Uniswap app; trading happens through the project's own interface."
              : elig === "unknown"
                ? "Routing in the Uniswap app is unknown for this pool."
                : tvl < 1000
                  ? `No liquidity in this pool (${tvl < 1 ? "$0" : "under $1K"} TVL). Volume may not be organic.`
                  : "One of the tokens could not be identified.";
          return { ok, reason, label: ok ? "Swap on Uniswap" : "View pool on Uniswap" };
        })()
      : null;
  const share = pace(p.usage.vol24h, p.metrics.vol7d);
  const u = p.usage;
  const hasBs = u.buys24h != null && u.sells24h != null && u.buys24h + u.sells24h > 0;
  const buyPct = hasBs ? Math.round(((u.buys24h ?? 0) / ((u.buys24h ?? 0) + (u.sells24h ?? 0))) * 100) : 0;

  return (
    <>
      <div className={s.swap}>
        <div className={s.swapH}>
          <div className={s.swapT}>
            <span>Trade</span>
          </div>
        </div>
        <div className={s.well}>
          <div className={s.lbl}>Pool</div>
          <div className={s.ln}>
            <Token sym={s0} /> <span className="mut3">/</span> <Token sym={s1} />
          </div>
          <div className={s.wsub}>{top ? `${chainName}${top.fee ? ` · ${top.fee} fee` : ""}` : "No pool indexed yet"}</div>
        </div>
        <div className={s.route}>
          <span>Through</span>
          <b>{p.name}</b>
        </div>
        {cta && link && (
          <>
            <a className={`${s.cta} ${s.inCard} ${cta.ok ? "" : s.alt}`} href={link} target="_blank" rel="noopener">
              {cta.label}
            </a>
            {!cta.ok && <div className={s.note}>{cta.reason}</div>}
          </>
        )}
      </div>

      {hasBs && (
        <div className={`panel ${s.card}`}>
          <h2>Last 24h</h2>
          <div className={s.bsTop}>
            <span className="up num">{int(u.buys24h)} buys</span>
            <span className="down num">{int(u.sells24h)} sells</span>
          </div>
          <div className={s.bsBar} role="img" aria-label={`${buyPct}% buys, ${100 - buyPct}% sells`}>
            <i className={s.bsBuy} style={{ width: `${buyPct}%` }} />
            <i className={s.bsSell} style={{ width: `${100 - buyPct}%` }} />
          </div>
          <div className={s.bsSub}>
            <span className="num">{u.buyers24h != null ? `${int(u.buyers24h)} buyers` : ""}</span>
            <span className="num">{u.wallets24h != null ? `${int(u.wallets24h)} wallets` : ""}</span>
            <span className="num">{u.sellers24h != null ? `${int(u.sellers24h)} sellers` : ""}</span>
          </div>
        </div>
      )}

      <div className={`panel ${s.card}`}>
        <div className={s.kv}>
          <span>Hook</span>
          <b className="num mono" style={{ fontSize: 12 }}>
            {hook ? (
              hookHref ? (
                <a href={hookHref} target="_blank" rel="noopener" title={hook}>
                  {`${hook.slice(0, 10)}…${hook.slice(-6)}`}
                </a>
              ) : (
                shortAddr(hook)
              )
            ) : (
              NONE
            )}
          </b>
          <span>Flags</span>
          <b>{p.flagLabels.join(", ") || "none listed"}</b>
          <span>Hooks in project</span>
          <b className="num">{int(p.metrics.hooksN)}</b>
          <span>Today vs 7d pace</span>
          <b className="num">{p.usage.vol24h == null ? NONE : `${share}%`}</b>
        </div>
        <div className={s.bar}>
          <i style={{ width: `${share}%` }} />
        </div>
      </div>

      {cta && link && (
        <div className={s.sticky}>
          <WatchStar slug={p.slug} name={p.name} className={s.stickyStar} />
          <a className={`${s.cta} ${cta.ok ? "" : s.alt}`} href={link} target="_blank" rel="noopener">
            {cta.label}
          </a>
        </div>
      )}
    </>
  );
}
