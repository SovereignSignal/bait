import { defineConfig } from "@playwright/test";
import { resolve } from "node:path";

const representative = process.env.BAIT_E2E_REPRESENTATIVE === "1";
const PORT = representative ? 3101 : 3100;
const outputDir = process.env.BAIT_E2E_OUTPUT_DIR ?? `test-results/${representative ? "representative" : "smoke"}`;

// The caller builds first. Never reuse an unknown server or inherit a database URL: candidate QA must
// exercise this checkout's build and the explicitly selected local fixture, not a production endpoint.
export default defineConfig({
  testDir: "e2e",
  testMatch: representative ? "representative.spec.ts" : "smoke.spec.ts",
  timeout: 30_000,
  retries: process.env.CI ? 1 : 0,
  outputDir: resolve(outputDir, "artifacts"),
  reporter: [["list"], ["json", { outputFile: resolve(outputDir, "results.json") }]],
  metadata: { candidateSha: process.env.BAIT_E2E_SHA ?? "working-tree", fixture: representative ? "dated-source-replay-and-synthetic-regressions" : "october-8-sample" },
  use: { baseURL: `http://127.0.0.1:${PORT}`, screenshot: "only-on-failure", trace: "retain-on-failure" },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    { name: "phone", use: { viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true } },
  ],
  webServer: {
    command: `npm run start -- -p ${PORT} -H 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}/about`, // /api/health and /api/ready answer 503 without a database
    env: { DATABASE_URL: "", BAIT_FIXTURES: "1", BAIT_E2E_FIXTURE_PATH: representative ? resolve(__dirname, "e2e/fixtures/representative.json") : "" },
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
