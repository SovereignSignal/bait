import Link from "next/link";
import { CATEGORY_LABEL, type Category, type ChainId, type Tab } from "@/lib/types";
import { feedHref } from "./Nav";
import s from "./Filters.module.css";

export function Filters({ tab, chain, category, available, note }: { tab: Tab; chain?: ChainId; category?: Category; available: Category[]; note?: string }) {
  return (
    <div className={s.row} role="group" aria-label="Category">
      <Link className={s.chip} aria-pressed={!category} href={feedHref({ tab, chain })}>
        All
      </Link>
      {available.map((c) => (
        <Link key={c} className={s.chip} aria-pressed={category === c} href={feedHref({ tab, chain, category: c })}>
          {CATEGORY_LABEL[c]}
        </Link>
      ))}
      {note && <span className={s.note}>{note}</span>}
    </div>
  );
}
