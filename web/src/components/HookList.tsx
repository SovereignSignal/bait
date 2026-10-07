import { CHAINS, explorerAddressUrl } from "@/lib/chains";
import { activeFlags, isRoutable } from "@/lib/routable";
import type { HookView } from "@/lib/types";
import s from "./HookList.module.css";

// Flags that make a hook not routable without a manual allowlist review.
const ROUTE_BLOCKING = new Set(["beforeSwapReturnsDelta", "afterSwapReturnsDelta", "afterAddLiquidityReturnsDelta", "afterRemoveLiquidityReturnsDelta"]);

export function HookList({ hooks, total }: { hooks: HookView[]; total: number }) {
  const shown = hooks.slice(0, 12);
  return (
    <div className={s.list}>
      {shown.map((h) => {
        const c = CHAINS[h.chainId];
        const explorer = explorerAddressUrl(h.chainId, h.address);
        const flags = activeFlags(h.flags);
        const routable = isRoutable(h.address, h.flags, h.properties);
        return (
          <div key={`${h.chainId}:${h.address}`} className={s.hook}>
            <div className={s.top}>
              <span className={s.name}>{h.name}</span>
              <span className={s.chain}>
                <span className={s.dot} style={{ background: c.color, border: c.lightDot ? "1px solid var(--border)" : undefined }} />
                {c.name}
              </span>
            </div>
            <div className={s.addr}>
              {explorer ? (
                <a href={explorer} target="_blank" rel="noopener">
                  {h.address}
                </a>
              ) : (
                h.address
              )}
            </div>
            {h.description && <div className={s.desc}>{h.description}</div>}
            <div className={s.flags}>
              {h.properties.dynamicFee && <span className={`${s.flag} ${s.warn}`}>dynamicFee</span>}
              {flags.map((f) => (
                <span key={f} className={`${s.flag} ${ROUTE_BLOCKING.has(f) ? s.warn : ""}`}>
                  {f}
                </span>
              ))}
              {h.address.toLowerCase().startsWith("0x91") && <span className={`${s.flag} ${s.warn}`}>0x91 prefix</span>}
            </div>
            <div className={s.meta}>
              <span>{h.verifiedSource ? "Source verified" : "Source unverified"}</span>
              <span>{h.properties.upgradeable ? "Upgradeable" : "Immutable"}</span>
              <span>{routable ? "Routable by rule" : "Needs allowlist"}</span>
              {h.auditUrl && (
                <a href={h.auditUrl} target="_blank" rel="noopener">
                  Audit
                </a>
              )}
            </div>
          </div>
        );
      })}
      {total > shown.length && <div className={s.meta}>Showing {shown.length} of {total.toLocaleString("en-US")} hooks under this project.</div>}
    </div>
  );
}
