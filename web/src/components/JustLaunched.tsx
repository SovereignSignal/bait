"use client";
// "Just launched": the latest hooked pool initializations (v4_pools) across chains. Server-rendered with the
// latest 20, then refreshed from /api/launches every 60s while on screen and the tab is visible.
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { anyChainColor, anyChainName, anyExplorerTxUrl } from "@/lib/chains";
import { NONE, paletteColor, since } from "@/lib/format";
import { collapseLaunches, launchFee, launchLabel, launchPair } from "@/lib/radar";
import type { LaunchView } from "@/lib/types";
import s from "./Radar.module.css";

const REFRESH_MS = 60_000;

export function JustLaunched({
  initial,
  now: serverNow,
  compact = false,
  chainId,
}: {
  initial: LaunchView[];
  /** server render time, so the first client render matches */
  now: number;
  /** the New tab's list column: tighter box and a link to /building */
  compact?: boolean;
  /** the terminal's chain filter, when set */
  chainId?: number;
}) {
  const [all, setLaunches] = useState(initial);
  const launches = chainId ? all.filter((l) => l.chainId === chainId) : all;
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(serverNow);
  const box = useRef<HTMLDivElement>(null);
  const onScreen = useRef(true);
  const lastLoad = useRef(Date.now());

  useEffect(() => setLaunches(initial), [initial]);

  const load = useCallback(async () => {
    lastLoad.current = Date.now();
    try {
      const res = await fetch("/api/launches");
      const body = (await res.json()) as { launches?: LaunchView[] };
      if (Array.isArray(body.launches)) setLaunches(body.launches);
    } catch {
      /* keep what we have */
    }
    setNow(Date.now());
  }, []);

  useEffect(() => {
    setNow(Date.now());
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
    <div ref={box} className={compact ? s.compact : `panel ${s.launches}`} aria-label="Just launched">
      <div className={s.lHead}>
        <span className={s.liveDot} aria-hidden="true" />
        Just launched · new hooked pools
        {compact && <Link href="/building#launches">All launches →</Link>}
      </div>
      {launches.length === 0 && all.length > 0 ? (
        <div className={s.note}>None of the latest {all.length} hooked pool launches are on this chain.</div>
      ) : launches.length === 0 ? (
        <div className={s.note}>
          No hooked pool launches indexed yet. New pools appear here as the on-chain indexer picks up PoolManager
          Initialize events, refreshed every minute.
        </div>
      ) : (
        <ul className={`${s.lList} ${open ? s.open : ""}`}>
          {collapseLaunches(launches).map(({ l, more }) => (
            <LaunchRow key={`${l.chainId}:${l.poolId}`} l={l} more={more} now={now} />
          ))}
        </ul>
      )}
      {!compact && launches.length > 5 && (
        <button type="button" className={s.showAll} onClick={() => setOpen((o) => !o)}>
          {open ? "Show fewer" : `All ${launches.length} launches →`}
        </button>
      )}
    </div>
  );
}

function LaunchRow({ l, more, now }: { l: LaunchView; more: number; now: number }) {
  const label = launchLabel(l);
  const tx = l.txHash ? anyExplorerTxUrl(l.chainId, l.txHash) : null;
  const chain = anyChainName(l.chainId);
  return (
    <li>
      <div className={s.lRow}>
        <span className={s.lAv} style={{ background: paletteColor(l.projectName ?? l.hook) }} aria-hidden="true">
          {(l.projectName ?? "0x").slice(0, l.projectName ? 1 : 2)}
          <span className={s.dot} style={{ background: anyChainColor(l.chainId) }} />
        </span>
        <span className={s.lMeta}>
          {l.projectSlug ? (
            <Link className={s.lName} href={`/p/${l.projectSlug}`}>
              {label}
            </Link>
          ) : (
            <span className={`${s.lName} mono`} title={l.hook}>
              {label}
            </span>
          )}
          <span className={s.lSub}>
            {launchPair(l)} · {launchFee(l.fee)} · {chain}
            {more > 0 && ` · +${more} more pool${more === 1 ? "" : "s"}`}
          </span>
        </span>
        <span className={s.lRight}>
          <span className="num mut">{l.blockTime ? since(l.blockTime, now) : NONE}</span>
          {tx && (
            <a href={tx} target="_blank" rel="noopener" title={`Open the initialize transaction on ${chain}`}>
              tx ↗
            </a>
          )}
        </span>
      </div>
    </li>
  );
}
