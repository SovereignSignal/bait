// Server-only: route handler responses, gzipped when the client accepts it.
// Temporary: Next 16.4 never compresses a route handler (NodeNextResponse.appendHeader stores Content-Type as an array
// and the compression filter rejects non-strings; pages are compressed). Remove when route handlers come back from Next
// with Content-Encoding on their own; Next skips bodies already encoded, so this cannot double-compress meanwhile.
import { gzipSync } from "node:zlib";

const MIN_GZIP_BYTES = 1024; // below this gzip's header outweighs the saving (a 39-byte trades reply grows to 57)

export function respond(req: Request, body: string, init: { status?: number; headers: Record<string, string> }): Response {
  const headers: Record<string, string> = { ...init.headers, Vary: "Accept-Encoding" };
  if (body.length >= MIN_GZIP_BYTES && /\bgzip\b/i.test(req.headers.get("accept-encoding") ?? "")) {
    return new Response(new Uint8Array(gzipSync(body)), { status: init.status, headers: { ...headers, "Content-Encoding": "gzip" } });
  }
  return new Response(body, { status: init.status, headers });
}

export function respondJson(req: Request, data: unknown, init: { status?: number; headers?: Record<string, string> } = {}): Response {
  return respond(req, JSON.stringify(data), { status: init.status, headers: { "Content-Type": "application/json", ...init.headers } });
}
