// slug -> hook addresses for the list's 0x search. Fetched once, only when a search starts with "0x"; the rows the
// page ships no longer carry addresses (random hex, half the home page's transfer). Never throws: loadAll falls back to [].
// Next compresses pages but not route handlers, so the body is gzipped here (~140 KB -> ~58 KB for 1,658 projects).
import { gzipSync } from "node:zlib";
import { getHookIndex } from "@/db/queries";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const body = JSON.stringify(await getHookIndex());
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "Cache-Control": "public, max-age=600",
    Vary: "Accept-Encoding",
  };
  if (/\bgzip\b/.test(req.headers.get("accept-encoding") ?? "")) {
    return new Response(new Uint8Array(gzipSync(body)), { headers: { ...headers, "Content-Encoding": "gzip" } });
  }
  return new Response(body, { headers });
}
