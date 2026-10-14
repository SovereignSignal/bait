// Every listed project as a packed row (lib/rows.ts) for the terminal list's search, watchlist and tab / category /
// chain filtering on the client. Pages ship only the rows they show and hand the client `?v=<version>`, a hash of the
// set: a reply that matches its version is cached for a day, anything else (a newer set, a failed load served empty)
// for a minute, so a bad moment cannot stick in a browser under a good version.
import { getRowSet } from "@/db/queries";
import { respondJson } from "@/lib/http";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const { packed, version } = await getRowSet();
  const exact = new URL(req.url).searchParams.get("v") === version;
  return respondJson(req, packed, { headers: { "Cache-Control": exact ? "public, max-age=86400" : "public, max-age=60" } });
}
