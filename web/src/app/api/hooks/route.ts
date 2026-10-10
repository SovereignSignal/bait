// slug -> hook addresses for the list's 0x search. Fetched once, only when a search starts with "0x"; the rows the
// page ships no longer carry addresses (random hex, half the home page's transfer). Never throws: loadAll falls back to [].
import { getHookIndex } from "@/db/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  return Response.json(await getHookIndex(), { headers: { "Cache-Control": "public, max-age=600" } });
}
