"use client";
// Body of the root 404. Reads the URL so /p/<slug> and /week/<date> each say what was missing.
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect } from "react";
import { longDate } from "@/lib/format";
import { useTerminal } from "./TerminalProvider";

function seg(path: string, prefix: string): string | null {
  if (!path.startsWith(prefix)) return null;
  const raw = path.slice(prefix.length).split("/")[0];
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

export function MissingPage({ top }: { top: Array<{ slug: string; name: string }> }) {
  const path = usePathname() ?? "";
  const { setQ } = useTerminal();
  const slug = seg(path, "/p/");
  const week = seg(path, "/week/");
  const guess = slug ? slug.replace(/-/g, " ") : "";
  useEffect(() => {
    if (guess) setQ(guess); // the search box arrives prefilled; "/" shows the matches
  }, [guess, setQ]);
  const when = week && /^\d{4}-\d{2}-\d{2}$/.test(week) ? longDate(week) : week;
  return (
    <>
      <div className="lead">
        <h1>{slug ? `No project called '${slug}'.` : week ? `No digest for the week of ${when}.` : "Not found"}</h1>
        <p>
          {slug ? (
            <Link href="/">Search for “{guess}” in the feed</Link>
          ) : week ? (
            <Link href="/">This week&apos;s trending hooks</Link>
          ) : (
            <>
              No such page. <Link href="/">Back to the feed</Link>.
            </>
          )}
        </p>
      </div>
      {top.length > 0 && (
        <section className="block" aria-label="Trending now">
          <h2>Trending now</h2>
          <ul>
            {top.map((p) => (
              <li key={p.slug}>
                <Link className="inline" href={`/p/${p.slug}`}>
                  {p.name}
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
