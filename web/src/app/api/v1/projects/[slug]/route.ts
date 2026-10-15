// GET /api/v1/projects/{slug}: one project in full (hooks, pools, daily volume, top wallets, receipts). A retired slug
// redirects (308) to its current one. Spam-flagged projects are 404, as on the site.
import { getProject, slugForAlias } from "@/db/queries";
import { respondJson } from "@/lib/http";
import { projectV1 } from "@/lib/api-v1";
import { apiError, apiHeaders, corsPreflight, rateLimited, SITE } from "@/lib/api-v1-http";

export const dynamic = "force-dynamic";

export function OPTIONS() {
  return corsPreflight();
}

export async function GET(req: Request, { params }: { params: Promise<{ slug: string }> }) {
  const limited = rateLimited(req);
  if (limited) return limited;
  const { slug } = await params;
  if (!/^[a-z0-9][a-z0-9-]{0,80}$/.test(slug)) return apiError(req, 400, "bad_slug", "A slug is lowercase letters, digits and dashes.");
  const p = await getProject(slug);
  if (!p) {
    const canonical = await slugForAlias(slug);
    if (canonical && canonical !== slug) return Response.redirect(new URL(`/api/v1/projects/${canonical}`, req.url), 308);
    return apiError(req, 404, "not_found", `No listed project with slug "${slug}".`);
  }
  return respondJson(req, { api_version: "1", project: projectV1(p, SITE) }, { headers: await apiHeaders() });
}
