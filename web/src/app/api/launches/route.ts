// Latest hooked pool initializations (v4_pools) for the "Just launched" feed's 60s client refresh.
// Never throws: a missing table, an empty table or a bad query is an empty list. getLaunches caches 30s in-process.
import { getLaunches } from "@/db/radar";
import { respondJson } from "@/lib/http";
import type { LaunchView } from "@/lib/types";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  let launches: LaunchView[] = [];
  try {
    launches = await getLaunches(20);
  } catch (e) {
    console.error("[launches] failed, serving empty:", (e as Error).message);
  }
  return respondJson(req, { launches }, { headers: { "Cache-Control": "public, max-age=30, s-maxage=30" } });
}
