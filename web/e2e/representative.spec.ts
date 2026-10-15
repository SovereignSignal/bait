import { expect, test, type Page } from "@playwright/test";
import fixture from "./fixtures/representative.json";
import { usd } from "../src/lib/format";
import type { PackedRows } from "../src/lib/rows";
import { unpackRows } from "../src/lib/rows";

const sourceProjects = fixture.projects.filter((p) => !p.slug.startsWith("qa-"));
const list = (page: Page) => page.getByRole("complementary", { name: "Hook projects" });
const phone = () => test.info().project.name === "phone";

test.beforeEach(async ({ page }) => {
  // No provider calls and no trades. Empty activity is a deliberate local UI fixture.
  await page.route("**/api/trades/**", (route) => route.fulfill({ json: { trades: [] } }));
});

function errors(page: Page) {
  const found: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") found.push(m.text()); });
  page.on("pageerror", (e) => found.push(e.message));
  return found;
}

async function fit(page: Page) {
  const overflow = await page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    return { page: document.documentElement.scrollWidth - width, panels: Array.from(document.querySelectorAll("main .panel")).filter((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.right > width + 1; }).map((el) => el.textContent?.slice(0, 70)) };
  });
  expect(overflow.page, "document overflow").toBeLessThanOrEqual(0);
  expect(overflow.panels, "panel outside viewport").toEqual([]);
}

async function search(page: Page, value: string) {
  if (phone()) await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.getByRole("searchbox", { name: "Search hooks, projects or addresses" }).fill(value);
}

async function hydratedGoto(page: Page, path: string) {
  const rows = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/rows");
  await page.goto(path);
  await rows; // this request starts in the mounted list effect, after its controls have hydrated
}

function projectRow(page: Page, slug: string) {
  // List project links have a query suffix when a tab/filter is selected; filter links never use /p/.
  return list(page).locator(`a[href="/p/${slug}"], a[href^="/p/${slug}?"]`);
}

test("dated representative API rows retain their source totals without duplicate project identities", async ({ request }) => {
  const res = await request.get("/api/rows");
  expect(res.ok()).toBe(true);
  const rows = unpackRows(await res.json() as PackedRows);
  expect(new Set(rows.map((p) => p.slug)).size).toBe(rows.length);
  for (const source of sourceProjects) {
    const actual = rows.filter((p) => p.slug === source.slug);
    expect(actual).toHaveLength(1);
    expect(actual[0].vol7d).toBe(source.metrics.vol7d);
    expect(actual[0].vol24h).toBe(source.usage.vol24h);
    expect(actual[0].tvlUsd).toBe(source.metrics.tvlUsd);
  }
  expect(rows.find((p) => p.slug === "qa-dormant")?.isDormant).toBe(true);
  expect(rows.find((p) => p.slug === "qa-unpriced")?.isDormant).toBe(false);
  for (const slug of ["qa-dust", "qa-missing-history", "bunnihook"]) expect(rows.some((p) => p.slug === slug)).toBe(false);
});

for (const p of sourceProjects) {
  test(`${p.slug}: source replay keeps list/detail totals, period and evidence links aligned`, async ({ page }, testInfo) => {
    const found = errors(page);
    await page.goto("/?tab=all");
    const item = projectRow(page, p.slug);
    await expect(item).toBeVisible();
    await expect(item).toContainText(`${usd(p.metrics.vol7d)}7d`);
    await item.click();
    await expect(page).toHaveURL(new RegExp(`/p/${p.slug}(?:\\?|$)`));
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(p.name);
    const detail = page.getByRole("region", { name: "Project", exact: true });
    await expect(detail.getByText("7d volume", { exact: true }).locator("..")).toContainText(usd(p.metrics.vol7d));
    await expect(page.getByTestId("freshness")).toContainText("Figures through Oct 13 (UTC)");
    if (p.chains.includes(8453)) await expect(page.getByTestId("partial-chain")).toContainText("Base still indexing: figures partial");
    await expect(detail.getByText("Usage describes the available seven-day window ending Oct 13 UTC.", { exact: false })).toBeVisible();
    await expect(page.getByTestId("badges").getByText("Dormant in reported window", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Project context and evidence", exact: true })).toBeVisible();
    await page.getByText("What these badges mean", { exact: true }).click();
    await expect(detail.getByText(/Current Uniswap routing and quote availability have not been verified|Bait has not checked that approval|Insufficient pool or fee data/).first()).toBeVisible();
    if (p.signals.auditUrl) await expect(page.getByTestId("badges").getByRole("link", { name: /Audit linked/ })).toHaveAttribute("href", new URL(p.signals.auditUrl).href);
    if (p.website) await expect(detail.getByRole("link", { name: "Website", exact: true })).toHaveAttribute("href", new URL(p.website).href);
    await expect(page.getByRole("link", { name: "Swap on Uniswap", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: /^Swap$|Connect wallet|Approve/i })).toHaveCount(0);
    if (p.pools.length) await expect(page.getByRole("link", { name: "View pool on Uniswap", exact: true })).toHaveCount(1);
    await fit(page);
    await page.screenshot({ path: testInfo.outputPath(`${p.slug}.png`), fullPage: true });
    expect(found).toEqual([]);
  });
}

test("Aegis sampled deployments keep chain and pool identities distinct; synthetic alias redirects once", async ({ page, request }) => {
  const aegis = sourceProjects.find((p) => p.slug === "aegis")!;
  const v3 = sourceProjects.find((p) => p.slug === "aegis-v3-hook-unichain")!;
  // These were separate canonical records in the capture. Do not invent a merger or add both into one total.
  const keys = [...aegis.pools, ...v3.pools].map((p) => `${p.chainId}:${p.poolId}`);
  expect(new Set(keys).size).toBe(keys.length);
  const alias = await request.get("/p/qa-retired-aegis", { maxRedirects: 0 });
  expect(alias.status()).toBe(308);
  expect(alias.headers().location).toBe("/p/aegis");
  await page.goto("/p/qa-retired-aegis");
  await expect(page).toHaveURL(/\/p\/aegis$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Aegis");
  await expect(page.getByRole("region", { name: "Project", exact: true }).getByText("Dynamic fee · Base · Ethereum · Ink · Unichain · Robinhood", { exact: true })).toBeAttached();
  await fit(page);
});

test("historical dormant project stays in All, search, direct details and persisted watchlist", async ({ page }) => {
  const found = errors(page);
  await hydratedGoto(page, "/?tab=all");
  await expect(projectRow(page, "qa-dormant")).toContainText("Dormant");
  await search(page, "QA dormant");
  await expect(projectRow(page, "qa-dormant")).toBeVisible();
  await projectRow(page, "qa-dormant").click();
  await expect(page.getByTestId("badges").getByText("Dormant in reported window", { exact: true })).toBeVisible();
  await expect(page.getByText("Retained for research and search.", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Add QA dormant history to watchlist", exact: true }).first().click();
  await hydratedGoto(page, "/");
  await page.getByRole("button", { name: "Watchlist, 1 saved", exact: true }).click();
  await expect(projectRow(page, "qa-dormant")).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: "Watchlist, 1 saved", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(projectRow(page, "qa-dormant")).toBeVisible();
  await fit(page);
  expect(found).toEqual([]);
});

test("dormant and wash controls stay out of promotion; unknown stays out of Trending/New; revival can rank", async ({ page }) => {
  for (const tab of ["trending", "new", "verified"]) {
    await page.goto(tab === "trending" ? "/" : `/?tab=${tab}`);
    for (const slug of ["qa-dormant", "qa-wash", "qa-past-wash", ...(tab === "verified" ? [] : ["qa-unknown"])]) await expect(projectRow(page, slug)).toHaveCount(0);
  }
  await page.goto("/");
  await expect(projectRow(page, "qa-revived")).toBeVisible();
  await page.goto("/?tab=all");
  await expect(projectRow(page, "qa-unknown")).toContainText("Unknown");
  await expect(projectRow(page, "qa-wash")).toContainText("Wash");
  await expect(projectRow(page, "qa-past-wash")).toContainText("Past wash");
  await page.goto("/p/qa-wash");
  await expect(page.getByTestId("badges").getByText("Wash-like volume", { exact: true })).toBeVisible();
});

test("unknown and positive unpriced swaps never look like observed zero activity", async ({ page }) => {
  await page.goto("/p/qa-dormant");
  const detail = page.getByRole("region", { name: "Project", exact: true });
  await expect(detail.getByText("7d volume", { exact: true }).locator("..")).toContainText("$0");
  for (const slug of ["qa-unknown", "qa-unpriced"]) {
    await page.goto(`/p/${slug}`);
    await expect(detail.getByText("7d volume", { exact: true }).locator("..")).toContainText("–");
    await expect(page.getByTestId("badges").getByText("Dormant in reported window", { exact: true })).toHaveCount(0);
    await expect(detail.getByText("The last 7 days' volume", { exact: false })).toContainText("unknown");
  }
  await expect(detail.locator("span:visible").filter({ hasText: /^27 swaps\/7d(?: \(USD unknown\))?$/ }).first()).toBeVisible();
  await page.goto("/p/qa-unknown");
  await expect(page.getByTestId("badges").getByText("Recent activity unknown", { exact: true })).toBeVisible();
  await expect(page.getByText("Missing recent coverage is not evidence of zero activity.", { exact: false })).toBeVisible();
});

test("true dust, missing historical eligibility and absent Bunni are not manufactured as dormant", async ({ page }) => {
  for (const slug of ["qa-dust", "qa-missing-history", "bunnihook"]) {
    expect((await page.goto(`/p/${slug}`))?.status()).toBe(404);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(`No project called '${slug}'.`);
  }
});

test("chain/category filters, history and address search use canonical representative records", async ({ page }) => {
  const found = errors(page);
  await hydratedGoto(page, "/?tab=all");
  await page.getByRole("combobox", { name: "Chain" }).selectOption("base");
  await expect(page).toHaveURL(/chain=base/);
  await expect(projectRow(page, "clanker-static-fee-hook-v2-base")).toBeVisible();
  await expect(projectRow(page, "hookrmodularhookv6")).toHaveCount(0);
  await page.getByRole("group", { name: "Category" }).getByRole("link", { name: "Launchpad", exact: true }).click();
  await expect(page).toHaveURL(/category=launchpad/);
  await expect(projectRow(page, "angstrom")).toHaveCount(0);
  await page.goBack();
  await expect(projectRow(page, "angstrom")).toBeVisible();
  await hydratedGoto(page, "/");
  await search(page, "0xb429d62f8f3bffb98cdb9569533ea23bf0ba28cc");
  await expect(projectRow(page, "clanker-static-fee-hook-v2-base")).toBeVisible();
  await page.getByRole("searchbox", { name: "Search hooks, projects or addresses" }).press("Enter");
  await expect(page).toHaveURL(/\/p\/clanker-static-fee-hook-v2-base/);
  await fit(page);
  expect(found).toEqual([]);
});

test("failed full-list fetch offers retry and recovers without losing the requested filter", async ({ page }) => {
  let failures = 0;
  await page.route("**/api/rows?*", async (route) => {
    if (failures++ === 0) await route.fulfill({ status: 503, json: { error: "test-only unavailable" } });
    else await route.continue();
  });
  await hydratedGoto(page, "/");
  await page.getByRole("navigation", { name: "Feed views" }).getByRole("link", { name: "All", exact: true }).click();
  await expect(list(page).getByText("Couldn't load the project list.")).toBeVisible();
  await list(page).getByRole("button", { name: "Try again", exact: true }).click();
  await expect(projectRow(page, "qa-dormant")).toBeVisible();
  await expect(page).toHaveURL(/\?tab=all$/);
});

test("representative pages fit narrow phones and compact desktop", async ({ page }) => {
  for (const width of phone() ? [360, 375] : [1094, 1280]) {
    await page.setViewportSize({ width, height: phone() ? 812 : 900 });
    for (const slug of ["aegis", "clanker-static-fee-hook-v2-base", "arrakisprivatehook", "qa-unknown"]) {
      await page.goto(`/p/${slug}`);
      await fit(page);
    }
  }
});
