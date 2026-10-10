"use client";
// fomo-style live trade feed for the project's busiest pool. Polls /api/trades every
// 60s while the panel is on screen and the tab is visible.
import { useCallback, useEffect, useRef, useState } from "react";
import { explorerTxUrl } from "@/lib/chains";
import { shortAddr, since, usd } from "@/lib/format";
import { liveDotOn, tradesNote, type TradesStatus } from "@/lib/trades";
import type { ChainId, TradeView } from "@/lib/types";
import { WalletAvatar } from "./Avatar";
import s from "./Detail.module.css";

const REFRESH_MS = 60_000;

export function LiveTrades({ chainId, chainSlug, poolId, pair }: { chainId: ChainId; chainSlug: string; poolId: string; pair: string }) {
  const [feed, setFeed] = useState<{ trades: TradeView[]; status: TradesStatus } | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const box = useRef<HTMLDivElement>(null);
  const onScreen = useRef(true);
  const lastLoad = useRef(0);

  const load = useCallback(async () => {
    lastLoad.current = Date.now();
    try {
      const res = await fetch(`/api/trades/${chainSlug}/${poolId}`);
      const body = (await res.json()) as { trades?: TradeView[]; status?: TradesStatus };
      const trades = Array.isArray(body.trades) ? body.trades : [];
      const failed = body.status === "rate_limited" || body.status === "upstream_error";
      setFeed((cur) => (failed && cur?.trades.length ? { ...cur, status: body.status ?? "upstream_error" } : { trades, status: body.status ?? "ok" }));
    } catch {
      setFeed((cur) => (cur?.trades.length ? { ...cur, status: "upstream_error" } : { trades: [], status: "upstream_error" }));
    }
    setNow(Date.now());
  }, [chainSlug, poolId]);

  useEffect(() => {
    setFeed(null);
    load();
    const visible = () => document.visibilityState === "visible" && onScreen.current;
    const tick = setInterval(() => {
      if (visible()) load();
    }, REFRESH_MS);
    const clock = setInterval(() => setNow(Date.now()), 15_000);
    const onVis = () => {
      if (visible() && Date.now() - lastLoad.current > REFRESH_MS) load();
    };
    document.addEventListener("visibilitychange", onVis);
    let io: IntersectionObserver | undefined;
    if (box.current && "IntersectionObserver" in window) {
      io = new IntersectionObserver((entries) => {
        onScreen.current = entries.some((e) => e.isIntersecting);
        onVis();
      });
      io.observe(box.current);
    }
    return () => {
      clearInterval(tick);
      clearInterval(clock);
      document.removeEventListener("visibilitychange", onVis);
      io?.disconnect();
    };
  }, [load]);

  return (
    <div ref={box} className={s.trades} aria-live="off">
      <div className={s.liveHead}>
        <span className={`${s.liveDot} ${feed && liveDotOn(feed.status, feed.trades, now) ? "" : s.liveDotOff}`} aria-hidden="true" />
        Live trades · {pair}
      </div>
      {feed == null ? (
        <div className={`${s.tradesNote} mut`}>Loading trades…</div>
      ) : tradesNote(feed.status, feed.trades.length) ? (
        <div className={`${s.tradesNote} mut`}>{tradesNote(feed.status, feed.trades.length)}</div>
      ) : (
        <ul className={s.tradeList}>
          {feed.trades.map((x, i) => {
            const href = explorerTxUrl(chainId, x.tx);
            const label = x.labels?.[0];
            const body = (
              <>
                <WalletAvatar address={x.wallet} />
                <span className={s.tWho}>
                  <span className={`${s.tAddr} mono`}>{shortAddr(x.wallet)}</span>
                  <span className={s.tTags}>
                    {x.smartMoney && <span className="b b-p">◆ Smart money</span>}
                    {label && <span className="b b-n">{label}</span>}
                  </span>
                </span>
                <span className={`b ${x.kind === "buy" ? "b-g" : "b-r"}`}>{x.kind === "buy" ? "Buy" : "Sell"}</span>
                <span className={`${s.tUsd} num`}>{usd(x.usd)}</span>
                <span className={`${s.tAge} num mut3`}>{since(x.ts, now)}</span>
              </>
            );
            return (
              <li key={`${i}:${x.tx}`}>
                {href ? (
                  <a className={s.trade} href={href} target="_blank" rel="noopener" title="Open transaction">
                    {body}
                  </a>
                ) : (
                  <span className={s.trade}>{body}</span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
