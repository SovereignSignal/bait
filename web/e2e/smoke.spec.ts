import { expect, test, type Page } from "@playwright/test";

/** Console errors and uncaught page errors for the whole test (the favicon is served now, so none are expected). */
function watch(page: Page): string[] {
  const errs: string[] = [];
  page.on("console", (m) => m.type() === "error" && errs.push(m.text()));
  page.on("pageerror", (e) => errs.push(e.message));
  return errs;
}
const phone = () => test.info().project.name === "phone";
async function noHorizontalScroll(page: Page) {
  // the document must not be wider than the viewport (scrollWidth vs clientWidth, which excludes any scrollbar)
  const { sw, cw } = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
  expect(sw, "page scrolls horizontally").toBeLessThanOrEqual(cw);
  // and nothing inside the Top wallets / Pools panels may poke out of the client width
  const offenders = await page.evaluate(() => {
    const cw = document.documentElement.clientWidth;
    const out: string[] = [];
    for (const h of Array.from(document.querySelectorAll("h2"))) {
      if (!/^(Top wallets|Pools)/.test(h.textContent ?? "")) continue;
      const panel = h.closest(".panel") ?? h.parentElement!;
      for (const el of [panel, ...Array.from(panel.querySelectorAll("*"))]) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.right > cw + 0.5) out.push(`${h.textContent}: <${el.tagName.toLowerCase()} class="${el.getAttribute("class")}"> right=${Math.round(r.right)}`);
      }
    }
    return out;
  });
  expect(offenders, "elements past the viewport edge").toEqual([]);
}

test("feed: tab labels, category chips, one h1", async ({ page }) => {
  const errs = watch(page);
  await page.goto("/");
  const tabs = page.getByRole("navigation", { name: "Feed views" });
  for (const label of ["Trending", "New", "In Uniswap app", "All"]) await expect(tabs.getByRole("link", { name: label, exact: true })).toBeVisible();
  const heights = await page.getByRole("group", { name: "Category" }).getByRole("link").evaluateAll((els) => els.map((e) => e.getBoundingClientRect().height));
  expect(heights.length).toBeGreaterThan(2);
  for (const h of heights) expect(h).toBeGreaterThanOrEqual(24);
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
  if (phone()) await noHorizontalScroll(page);
  expect(errs).toEqual([]);
});

test("phone pages never overflow the viewport", async ({ page }) => {
  test.skip(!phone(), "phone only");
  for (const width of [375, 360]) {
    await page.setViewportSize({ width, height: 812 }); // 360 is the narrow Android case (the feed tabs once overflowed there by 4 px)
    for (const path of ["/", "/p/stablepairhook", "/p/v4permissionedswaps"]) {
      await page.goto(path);
      await noHorizontalScroll(page);
    }
  }
});

test("In Uniswap app tab lists only the eligible fixture", async ({ page }) => {
  const errs = watch(page);
  await page.goto("/?tab=verified");
  const list = page.getByRole("complementary", { name: "Hook projects" }); // the movers strip is unfiltered
  await expect(list.getByRole("link", { name: /V4PermissionedSwaps/ })).toBeVisible();
  await expect(list.getByRole("link", { name: /StablePairHook/ })).toHaveCount(0);
  if (phone()) await noHorizontalScroll(page);
  expect(errs).toEqual([]);
});

for (const p of [{ slug: "stablepairhook", routing: "Not in app", swap: false }, { slug: "v4permissionedswaps", routing: "In Uniswap app", swap: true }]) {
  test(`/p/${p.slug}: badges and CTA`, async ({ page }) => {
    const errs = watch(page);
    await page.goto(`/p/${p.slug}`);
    const badges = page.getByTestId("badges");
    await expect(badges.getByText("✓ Hooklist", { exact: true })).toBeVisible();
    await expect(badges.getByText("Not in hooklist")).toHaveCount(0);
    await expect(badges.getByText(p.routing, { exact: true })).toBeVisible();
    // the card CTA (desktop) and the sticky bar CTA (phone) are one value; the hidden one is not in the role tree
    await expect(page.getByRole("link", { name: "Swap on Uniswap" })).toHaveCount(p.swap ? 1 : 0);
    if (!p.swap) await expect(page.getByRole("link", { name: "View pool on Uniswap" })).toHaveCount(1);
    await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
    if (phone()) await noHorizontalScroll(page);
    expect(errs).toEqual([]);
  });
}

test("unknown project: 404 with a message", async ({ page }) => {
  expect((await page.goto("/p/no-such-hook"))?.status()).toBe(404);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("No project called 'no-such-hook'.");
});

for (const [path, h1] of [["/leaders", "Leaders"], ["/building", "Building"], ["/week", "Weekly digest"], ["/about", "How this works"]]) {
  test(`${path} renders`, async ({ page }) => {
    const errs = watch(page);
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(h1);
    await expect(page.getByRole("main")).toHaveCount(1);
    if (phone()) await noHorizontalScroll(page);
    expect(errs).toEqual([]);
  });
}

test("/feed.xml is RSS", async ({ request }) => {
  const res = await request.get("/feed.xml");
  expect(res.status()).toBe(200);
  expect(await res.text()).toContain("<rss");
});

test("/api/ready answers 503 without a database", async ({ request }) => {
  const res = await request.get("/api/ready");
  expect(res.status()).toBe(503);
  expect((await res.json()).ok).toBe(false);
});

test("phone touch targets", async ({ page }) => {
  test.skip(!phone(), "phone only");
  await page.goto("/");
  const tab = page.getByRole("navigation", { name: "Feed views" }).getByRole("link").first();
  expect((await tab.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  const dock = await page.getByRole("navigation", { name: "Sections" }).locator("> *").evaluateAll((els) => els.map((e) => e.getBoundingClientRect().height));
  for (const h of dock) expect(h).toBeGreaterThanOrEqual(44);
});
