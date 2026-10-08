// Reads for the dev radar (/building) and the "Just launched" feed (v4_pools from the on-chain indexer).
// Both tables are newer than the rest and may be missing or empty: every read goes through safe() and renders empty.
// Small in-process caches (single replica): launches 30s, radar 5 min (it changes nightly).
import { resolveSymbols } from "@/lib/symbols";
import { and, desc, eq, gte, isNull, ne, or, sql } from "drizzle-orm";
import { getDb } from "./client";
import { safe, usingFixtures } from "./queries";
import * as t from "./schema";
import { SAMPLE_RADAR, sampleLaunches } from "@/fixtures/radar-sample";
import { isRadarKind, type LaunchView, type RadarItemView } from "@/lib/types";

const LAUNCH_TTL_MS = 30_000;
const RADAR_TTL_MS = 300_000;
const MIN_SCORE = 0.5;
const REGISTRY_LIMIT = 200;
const OTHER_LIMIT = 400;

const memo = new Map<string, { at: number; value: Promise<unknown> }>();
function cachedFor<T>(key: string, ttl: number, fn: () => Promise<T>): Promise<T> {
  const hit = memo.get(key);
  const now = Date.now();
  if (hit && now - hit.at < ttl) return hit.value as Promise<T>;
  const value = fn();
  memo.set(key, { at: now, value });
  // An empty or failed read is retried sooner than a full one.
  value.then(
    (v) => {
      if (Array.isArray(v) && v.length === 0) memo.set(key, { at: now - ttl + 10_000, value });
    },
    () => memo.delete(key),
  );
  return value;
}

const iso = (d: Date | string | null | undefined): string | null => (d == null ? null : new Date(d).toISOString());

/** Latest hooked pool initializations across every chain the indexer covers. */
export async function getLaunches(n = 20): Promise<LaunchView[]> {
  const db = getDb();
  if (!db) return usingFixtures() ? sampleLaunches().slice(0, n) : [];
  return safe(
    "launches",
    () =>
      cachedFor(`launches:${n}`, LAUNCH_TTL_MS, async () => {
        const rows = await db
          .select({
            chainId: t.v4Pools.chainId,
            poolId: t.v4Pools.poolId,
            hook: t.v4Pools.hookAddress,
            currency0: t.v4Pools.currency0,
            currency1: t.v4Pools.currency1,
            fee: t.v4Pools.fee,
            blockTime: t.v4Pools.blockTime,
            txHash: t.v4Pools.txHash,
            projectSlug: t.projects.slug,
            projectName: t.projects.name,
            token0: t.pools.token0,
            token1: t.pools.token1,
          })
          .from(t.v4Pools)
          .leftJoin(t.hooks, and(eq(t.hooks.address, t.v4Pools.hookAddress), eq(t.hooks.chainId, t.v4Pools.chainId)))
          .leftJoin(t.projects, eq(t.projects.id, t.hooks.projectId))
          .leftJoin(t.pools, and(eq(t.pools.poolId, t.v4Pools.poolId), eq(t.pools.chainId, t.v4Pools.chainId)))
          .orderBy(sql`${t.v4Pools.blockTime} desc nulls last`, desc(t.v4Pools.blockNumber))
          .limit(n);
        const views = rows.map(
          (r): LaunchView => ({
            chainId: r.chainId,
            poolId: r.poolId,
            hook: r.hook.toLowerCase(),
            projectSlug: r.projectSlug ?? null,
            projectName: r.projectName ?? null,
            currency0: r.currency0,
            currency1: r.currency1,
            symbol0: symbol(r.token0),
            symbol1: symbol(r.token1),
            fee: r.fee,
            blockTime: iso(r.blockTime),
            txHash: r.txHash,
          }),
        );
        // Indexer-only pools carry addresses, not symbols: resolve via DefiLlama, then ERC-20 symbol() on-chain.
        const sym = await resolveSymbols(
          views.flatMap((v) => [
            ...(v.symbol0 ? [] : [{ chainId: v.chainId, address: v.currency0 }]),
            ...(v.symbol1 ? [] : [{ chainId: v.chainId, address: v.currency1 }]),
          ]),
        );
        return views.map((v) => ({
          ...v,
          symbol0: v.symbol0 ?? sym({ chainId: v.chainId, address: v.currency0 }),
          symbol1: v.symbol1 ?? sym({ chainId: v.chainId, address: v.currency1 }),
        }));
      }),
    [],
  );
}

function symbol(tok: t.TokenJson | null | undefined): string | null {
  const s = tok?.symbol;
  return typeof s === "string" && s.trim() && s.length <= 16 ? s.trim() : null;
}

/** Radar items: relevant web results (score >= 0.5) and every GitHub row; registry capped so it cannot crowd out the rest. */
export async function getRadar(): Promise<RadarItemView[]> {
  const db = getDb();
  if (!db) return usingFixtures() ? SAMPLE_RADAR : [];
  return safe(
    "radar",
    () =>
      cachedFor("radar", RADAR_TTL_MS, async () => {
        const relevant = or(isNull(t.radarItems.score), gte(t.radarItems.score, MIN_SCORE));
        const order = [sql`${t.radarItems.publishedAt} desc nulls last`, desc(t.radarItems.lastSeen)];
        const [registry, rest] = await Promise.all([
          db.select().from(t.radarItems).where(and(eq(t.radarItems.kind, "registry_submission"), relevant)).orderBy(...order).limit(REGISTRY_LIMIT),
          db.select().from(t.radarItems).where(and(ne(t.radarItems.kind, "registry_submission"), relevant)).orderBy(...order).limit(OTHER_LIMIT),
        ]);
        return [...registry, ...rest].flatMap((r): RadarItemView[] =>
          isRadarKind(r.kind)
            ? [
                {
                  url: r.url,
                  kind: r.kind,
                  title: r.title || r.repo || r.url,
                  summary: r.summary,
                  source: r.source,
                  repo: r.repo,
                  stars: r.stars,
                  chainId: r.chainId,
                  hookAddress: r.hookAddress,
                  projectSlug: r.projectSlug,
                  publishedAt: iso(r.publishedAt),
                  lastSeen: iso(r.lastSeen) ?? new Date(0).toISOString(),
                },
              ]
            : [],
        );
      }),
    [],
  );
}
