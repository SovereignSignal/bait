"use client";
// The project h1. After a client-side navigation focus moves here (replaces the old aria-live region);
// the first paint of a session is a page load, not a navigation, so it is left alone.
import { useEffect, useRef } from "react";

let seen = false;

export function ProjectHeading({ name }: { name: string }) {
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (seen) ref.current?.focus({ preventScroll: true });
    seen = true;
  }, [name]);
  return (
    <h1 ref={ref} tabIndex={-1}>
      {name}
    </h1>
  );
}
