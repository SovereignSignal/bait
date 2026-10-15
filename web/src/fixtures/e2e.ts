// Explicit local candidate-test input. Never read when a database is configured or fixture mode is off.
import { readFileSync } from "node:fs";
import type { ProjectDetail } from "@/lib/types";
import type { Freshness } from "@/lib/health";

export interface E2eFixtures {
  observedAt: string;
  source: string;
  projects: ProjectDetail[];
  aliases?: Record<string, string>;
  freshness?: Freshness;
}

export function loadE2eFixtures(env: Record<string, string | undefined> = process.env): E2eFixtures | null {
  if (env.BAIT_FIXTURES !== "1" || env.DATABASE_URL || !env.BAIT_E2E_FIXTURE_PATH) return null;
  const value = JSON.parse(readFileSync(env.BAIT_E2E_FIXTURE_PATH, "utf8")) as E2eFixtures;
  if (!value || !Number.isFinite(Date.parse(value.observedAt)) || typeof value.source !== "string"
      || !Array.isArray(value.projects) || value.projects.some(p => !p || typeof p.slug !== "string"
        || !Array.isArray(p.hooks) || !Array.isArray(p.pools) || !p.metrics || !p.usage)
      || (value.aliases && (typeof value.aliases !== "object" || Object.values(value.aliases).some(v => typeof v !== "string")))) {
    throw new Error("Invalid BAIT_E2E_FIXTURE_PATH dataset");
  }
  return value;
}
