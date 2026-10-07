import { CHAINS, poolDeepLink } from "@/lib/chains";
import { feeLabel, int, pair, usd } from "@/lib/format";
import type { PoolView } from "@/lib/types";
import s from "./PoolsTable.module.css";

export function PoolsTable({ pools, total }: { pools: PoolView[]; total: number }) {
  if (pools.length === 0) return <p className="muted">No pools indexed yet.</p>;
  return (
    <div className={s.wrap}>
      <table className={s.table}>
        <thead>
          <tr>
            <th>Pool</th>
            <th className={s.n}>TVL</th>
            <th className={s.n}>Vol 7d</th>
            <th className={s.n}>Txs 7d</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {pools.map((p) => {
            const c = CHAINS[p.chainId];
            return (
              <tr key={`${p.chainId}:${p.poolId}`}>
                <td>
                  <div className={s.pair}>
                    {pair(p.token0Symbol, p.token1Symbol, p.token0, p.token1)}
                    <span className={s.fee}>{feeLabel(p.fee)}</span>
                  </div>
                  <div className={s.chain}>
                    <span className={s.dot} style={{ background: c.color, border: c.lightDot ? "1px solid var(--border)" : undefined }} />
                    {c.name}
                  </div>
                </td>
                <td className={s.n}>{usd(p.tvlUsd)}</td>
                <td className={s.n}>{usd(p.vol7d)}</td>
                <td className={s.n}>{int(p.tx7d)}</td>
                <td className={s.n}>
                  <a className={s.open} href={poolDeepLink(p.chainId, p.poolId)} target="_blank" rel="noopener">
                    Open
                  </a>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {total > pools.length && <div className={s.more}>Showing top {pools.length} of {int(total)} pools by TVL.</div>}
    </div>
  );
}
