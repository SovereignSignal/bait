// GET /api/v1/hooks/{0xaddress}?chain=<slug or id>: the project(s) that own a hook at this address, busiest first.
// One address can be a hook on several chains and so belong to several projects; `chain` narrows it to one.
import { getAllProjects } from "@/db/queries";
import { respondJson } from "@/lib/http";
import { chainParam, findByHook, isHexAddress, projectSummaryV1 } from "@/lib/api-v1";
import { apiError, apiHeaders, corsPreflight, rateLimited, SITE } from "@/lib/api-v1-http";

export const dynamic = "force-dynamic";

export function OPTIONS() {
  return corsPreflight();
}

export async function GET(req: Request, { params }: { params: Promise<{ address: string }> }) {
  const limited = rateLimited(req);
  if (limited) return limited;
  const { address } = await params;
  if (!isHexAddress(address)) return apiError(req, 400, "bad_address", "A hook address is 0x followed by 40 hex characters.");
  const { chain, bad } = chainParam(new URL(req.url).searchParams);
  if (bad) return apiError(req, 400, "bad_chain", "Unknown chain; use a chain id or slug from GET /api/v1/projects (chains[].slug).");
  const matches = findByHook(await getAllProjects(), address, chain);
  if (!matches.length) return apiError(req, 404, "not_found", `No listed project has a hook at ${address.toLowerCase()}${chain ? ` on chain ${chain}` : ""}. Bait lists hooks with at least one pool; a deployed hook with no pool is not here yet.`);
  const headers = await apiHeaders();
  return respondJson(req, {
    api_version: "1",
    data_day: headers["X-Bait-Data-Day"] ?? null,
    address: address.toLowerCase(),
    matches: matches.map(({ project, hooks }) => ({
      project: projectSummaryV1(project, SITE),
      hooks: hooks.map((h) => ({ address: h.address.toLowerCase(), chain_id: h.chainId, name: h.name, hooklisted: h.hooklisted, deployer: h.deployer ? h.deployer.toLowerCase() : null, deployer_source: h.deployerSource ?? null })),
    })),
  }, { headers });
}
