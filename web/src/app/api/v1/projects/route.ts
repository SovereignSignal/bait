// GET /api/v1/projects?sort=vol7d|trending|new|tvl&limit=100&offset=0&chain=<slug or id>&category=<slug>
// The listed projects (the same set as the All tab) as stable snake_case summaries. See docs/api.md.
import { getAllProjects } from "@/db/queries";
import { respondJson } from "@/lib/http";
import { listProjects, parseListQuery, projectSummaryV1 } from "@/lib/api-v1";
import { apiHeaders, corsPreflight, rateLimited, SITE } from "@/lib/api-v1-http";

export const dynamic = "force-dynamic";

export function OPTIONS() {
  return corsPreflight();
}

export async function GET(req: Request) {
  const limited = rateLimited(req);
  if (limited) return limited;
  const q = parseListQuery(new URL(req.url).searchParams);
  const { total, rows } = listProjects(await getAllProjects(), q);
  const headers = await apiHeaders();
  return respondJson(req, {
    api_version: "1",
    data_day: headers["X-Bait-Data-Day"] ?? null,
    query: q,
    total,
    count: rows.length,
    projects: rows.map((p) => projectSummaryV1(p, SITE)),
  }, { headers });
}
