// Right column: the classic Uniswap swap widget, buys vs sells, what it does, hook facts.
import { CHAINS, explorerAddressUrl, knownChain, poolDeepLink } from "@/lib/chains";
import { feeLabel, int, paletteColor, shortAddr } from "@/lib/format";
import { pace } from "@/lib/present";
import type { ProjectDetail } from "@/lib/types";
import { ArrowDown, Chevron } from "./Icons";
import s from "./Trade.module.css";

function Token({ sym }: { sym: string }) {
  return (
    <span className={s.tok}>
      <span className={s.dot} style={{ background: paletteColor(sym) }}>
        {sym.slice(0, 1)}
      </span>
      {sym} <Chevron size={10} />
    </span>
  );
}

export function TradePanel({ p }: { p: ProjectDetail }) {
  const pool = p.pools.find((x) => knownChain(x.chainId)) ?? null;
  const top = pool
    ? { poolId: pool.poolId, chainId: pool.chainId, s0: pool.token0Symbol, s1: pool.token1Symbol, fee: feeLabel(pool.fee), hook: pool.hookAddress }
    : p.topPool && knownChain(p.topPool.chainId)
      ? { poolId: p.topPool.poolId, chainId: p.topPool.chainId, s0: p.topPool.token0Symbol, s1: p.topPool.token1Symbol, fee: "", hook: p.topPool.hookAddress }
      : null;
  const s0 = top?.s0 ?? "?";
  const s1 = top?.s1 ?? "?";
  const link = top ? poolDeepLink(top.chainId, top.poolId) : null;
  const chainName = top ? (CHAINS[top.chainId]?.name ?? "") : "";
  const hook = (top?.hook ?? p.hooks[0]?.address ?? "").toLowerCase();
  const hookChain = top?.chainId ?? p.hooks[0]?.chainId;
  const hookHref = hook && hookChain != null ? explorerAddressUrl(hookChain, hook) : null;
  const share = pace(p.usage.vol24h, p.metrics.vol7d);
  const u = p.usage;
  const hasBs = u.buys24h != null && u.sells24h != null && u.buys24h + u.sells24h > 0;
  const buyPct = hasBs ? Math.round(((u.buys24h ?? 0) / ((u.buys24h ?? 0) + (u.sells24h ?? 0))) * 100) : 0;

  return (
    <>
      <div className={s.swap}>
        <div className={s.swapH}>
          <div className={s.swapT}>
            <span>Swap</span>
            <span>Limit</span>
          </div>
          <span className="mut3" aria-hidden="true">
            ⚙
          </span>
        </div>
        <div className={s.well}>
          <div className={s.lbl}>You pay</div>
          <div className={s.ln}>
            <input inputMode="decimal" placeholder="0" aria-label="Amount to pay" autoComplete="off" />
            <Token sym={s0} />
          </div>
          <div className={s.wsub}>{top ? `via ${s0} / ${s1} on ${chainName}` : "No pool indexed yet"}</div>
        </div>
        <div className={s.flip}>
          <span>
            <ArrowDown />
          </span>
        </div>
        <div className={s.well}>
          <div className={s.lbl}>You receive</div>
          <div className={s.ln}>
            <span className={s.q}>0</span>
            <Token sym={s1} />
          </div>
          <div className={s.wsub}>Quote on Uniswap</div>
        </div>
        <div className={s.route}>
          <span>Routes through</span>
          <b>
            {p.name}
            {top?.fee ? ` · ${top.fee} fee` : ""}
          </b>
        </div>
        {link &&
          (p.signals.routable ? (
            <a className={s.cta} href={link} target="_blank" rel="noopener">
              Swap on Uniswap
            </a>
          ) : (
            <>
              <a className={`${s.cta} ${s.alt}`} href={link} target="_blank" rel="noopener">
                View pool on Uniswap
              </a>
              <div className={s.note}>This hook does not route in the Uniswap app, so trading happens through the project’s own interface.</div>
            </>
          ))}
      </div>

      {hasBs && (
        <div className={`panel ${s.card}`}>
          <h3>Last 24h</h3>
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
        <h3>What it does</h3>
        <div className={s.what}>{p.whatLine || "No description in the registry."}</div>
      </div>

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
              "–"
            )}
          </b>
          <span>Flags</span>
          <b>{p.flagLabels.join(", ") || "none listed"}</b>
          <span>Hooks in project</span>
          <b className="num">{int(p.metrics.hooksN)}</b>
          <span>Today vs 7d pace</span>
          <b className="num">{p.usage.vol24h == null ? "–" : `${share}%`}</b>
        </div>
        <div className={s.bar}>
          <i style={{ width: `${share}%` }} />
        </div>
      </div>
    </>
  );
}
