import { chainColor } from "@/lib/chains";
import { addrColor } from "@/lib/format";
import type { ChainId } from "@/lib/types";

/** Project initial on its brand color, with the primary chain's dot. */
export function ProjectAvatar({ name, color, chain, size }: { name: string; color: string; chain?: ChainId; size?: "sm" | "md" | "lg" }) {
  return (
    <span className={`av${size ? ` av-${size}` : ""}`} style={{ background: color }} aria-hidden="true">
      {name.slice(0, 1).toUpperCase()}
      {chain != null && <span className="cd" style={{ background: chainColor(chain) }} />}
    </span>
  );
}

/** Wallet circle colored from the address. */
export function WalletAvatar({ address, size = "md" }: { address: string; size?: "sm" | "md" }) {
  return (
    <span
      className={`av av-${size}`}
      style={{ background: `radial-gradient(circle at 30% 30%, ${addrColor(address.slice(2, 10))}, ${addrColor(address.slice(-8))})` }}
      aria-hidden="true"
    />
  );
}
