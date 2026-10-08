"use client";
import { StarIcon } from "./Icons";
import { useTerminal } from "./TerminalProvider";
import s from "./Detail.module.css";

/** Star toggle on the project header; the list lives in localStorage (TerminalProvider). */
export function WatchStar({ slug, name }: { slug: string; name: string }) {
  const t = useTerminal();
  const on = t.watchlist.includes(slug);
  return (
    <button
      type="button"
      className={s.star}
      aria-pressed={on}
      aria-label={on ? `Remove ${name} from watchlist` : `Add ${name} to watchlist`}
      title={on ? "On your watchlist" : "Add to watchlist"}
      onClick={() => t.toggleWatch(slug)}
    >
      <StarIcon filled={on} />
    </button>
  );
}
