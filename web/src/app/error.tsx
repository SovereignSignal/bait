"use client";
import Link from "next/link";

export default function ErrorPage({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <main className="col" id="main" tabIndex={-1}>
      <div className="lead">
        <h1>Something broke on our side</h1>
        <p>
          <button type="button" className="linkBtn" onClick={reset}>
            Try again
          </button>{" "}
          or <Link href="/">go back to the feed</Link>.
        </p>
      </div>
    </main>
  );
}
