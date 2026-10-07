import Link from "next/link";
import s from "./Footer.module.css";

export function Footer() {
  return (
    <p className={s.foot}>
      Hooks are third-party code in the swap path. Nothing here is audited by us. Flags are shown as published in the hooklist registry. The routable badge is a rule, not a guarantee.
      <span className={s.links}>
        <Link href="/about">About</Link>
        <Link href="/week">Weekly</Link>
        <a href="/feed.xml">RSS</a>
        <a href="https://github.com/Uniswap/hooklist" target="_blank" rel="noopener">
          hooklist
        </a>
      </span>
    </p>
  );
}
