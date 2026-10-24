// Shared response plumbing for /api/v1: one in-process rate limit, caching and CORS headers, JSON errors.
// Public, read-only, single replica: the bucket is per process, which is the whole deployment today.
import { getFreshness } from "@/db/health";
import { respondJson } from "@/lib/http";
import { createTokenBucket } from "@/lib/token-bucket";
import { API_VERSION, apiSite, type ApiError } from "@/lib/api-v1";

const bucket = createTokenBucket({ capacity: 120, refillPerSec: 10 }); // 600 a minute sustained, 120 in a burst

export const SITE = apiSite(process.env.NEXT_PUBLIC_SITE_URL);

export async function apiHeaders(maxAge = 300): Promise<Record<string, string>> {
  const fresh = await getFreshness();
  return {
    "Cache-Control": `public, max-age=${maxAge}`,
    "Access-Control-Allow-Origin": "*",
    "X-Bait-Api-Version": API_VERSION,
    ...(fresh?.through ? { "X-Bait-Data-Day": fresh.through } : {}),
  };
}

export function apiError(req: Request, status: number, code: string, message: string, headers: Record<string, string> = {}): Response {
  const body: ApiError = { error: { code, message } };
  return respondJson(req, body, { status, headers: { "Cache-Control": "no-store", "Access-Control-Allow-Origin": "*", "X-Bait-Api-Version": API_VERSION, ...headers } });
}

/** null when the request may proceed, else the 429 to return. */
export function rateLimited(req: Request): Response | null {
  return bucket.take() ? null : apiError(req, 429, "rate_limited", "Too many requests; the API allows about 600 a minute.", { "Retry-After": "10" });
}

export function corsPreflight(): Response {
  return new Response(null, { status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "*", "Access-Control-Max-Age": "86400" } });
}
