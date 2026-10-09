import { defineConfig } from "@playwright/test";

const PORT = 3100;

// The caller builds first (CI runs `npm run build`); this starts `next start` on sample data.
export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  use: { baseURL: `http://127.0.0.1:${PORT}` },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    { name: "phone", use: { viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true } },
  ],
  webServer: {
    command: `npm run start -- -p ${PORT} -H 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}/about`, // /api/health and /api/ready answer 503 without a database
    env: { BAIT_FIXTURES: "1" },
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
