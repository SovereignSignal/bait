import { expect, test, type Page } from "@playwright/test";

// Candidate UI tests never ask the local trades proxy to fetch live provider data.
test.beforeEach(async ({ page }) => {
  await page.route("**/api/trades/**", (route) => route.fulfill({ json: { trades: [] } }));
});

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
  for (const label of ["Trending", "New", "Automatic routing", "All"]) await expect(tabs.getByRole("link", { name: label, exact: true })).toBeVisible();
  const heights = await page.getByRole("group", { name: "Category" }).getByRole("link").evaluateAll((els) => els.map((e) => e.getBoundingClientRect().height));
  expect(heights.length).toBeGreaterThan(2);
  for (const h of heights) expect(h).toBeGreaterThanOrEqual(24);
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
  if (phone()) await noHorizontalScroll(page);
  expect(errs).toEqual([]);
});

test("tabs and category chips filter on the client: no server round trip, the URL follows, Back undoes", async ({ page }) => {
  const errs = watch(page);
  // "/" with a tab or category, as a document or an RSC request; the tab and chip links never prefetch
  const feedFetches: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname === "/" && (u.searchParams.has("tab") || u.searchParams.has("category"))) feedFetches.push(r.url());
  });
  const rowsLoaded = page.waitForResponse((r) => r.url().includes("/api/rows"));
  await page.goto("/");
  await rowsLoaded;
  const list = page.getByRole("complementary", { name: "Hook projects" });
  await page.getByRole("navigation", { name: "Feed views" }).getByRole("link", { name: "All", exact: true }).click();
  await expect(page).toHaveURL(/\/\?tab=all$/);
  await expect(page.getByRole("navigation", { name: "Feed views" }).getByRole("link", { name: "All", exact: true })).toHaveAttribute("aria-current", "page");
  await expect(list.getByRole("link", { name: /StablePairHook/ })).toBeVisible();
  await page.getByRole("group", { name: "Category" }).getByRole("link", { name: "Compliance", exact: true }).click();
  await expect(page).toHaveURL(/tab=all&category=compliance/);
  await expect(list.getByRole("link", { name: /V4PermissionedSwaps/ })).toBeVisible();
  await expect(list.getByRole("link", { name: /StablePairHook/ })).toHaveCount(0);
  await page.goBack();
  await expect(page).toHaveURL(/\/\?tab=all$/);
  await expect(list.getByRole("link", { name: /StablePairHook/ })).toBeVisible();
  expect(feedFetches, "a tab or chip click went to the server").toEqual([]);
  expect(errs).toEqual([]);
});

test("search keeps letters typed before the page hydrated", async ({ page }) => {
  test.skip(phone(), "phones open the search box from the dock, after hydration");
  const errs = watch(page);
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  await page.route("**/_next/static/chunks/*.js", async (route) => {
    await gate; // hold the scripts only: the CSS shares this folder, and holding it would hold DOMContentLoaded too
    await route.continue();
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const box = page.getByRole("searchbox", { name: "Search hooks, projects or addresses" });
  await box.pressSequentially("stable");
  release();
  await expect(page.getByRole("complementary", { name: "Hook projects" }).getByText("1 match for “stable”")).toBeVisible();
  await expect(box).toHaveValue("stable");
  expect(errs).toEqual([]);
});

test("desktop at 1094px: the watchlist tab fits the tab bar, and an empty watchlist says what it is for", async ({ page }) => {
  test.skip(phone(), "the 320px list column is a desktop layout");
  await page.setViewportSize({ width: 1094, height: 800 });
  const rowsLoaded = page.waitForResponse((r) => r.url().includes("/api/rows"));
  await page.goto("/");
  await rowsLoaded; // fetched on mount, so the star's click handler is attached by now
  const list = page.getByRole("complementary", { name: "Hook projects" });
  const star = page.getByRole("button", { name: /^Watchlist, 0 saved$/ });
  const first = page.getByRole("navigation", { name: "Feed views" }).getByRole("link", { name: "Trending", exact: true });
  const [s, l, f] = [(await star.boundingBox())!, (await list.boundingBox())!, (await first.boundingBox())!];
  expect(s.x + s.width, "the star tab pokes past the list panel").toBeLessThanOrEqual(l.x + l.width);
  expect(s.y, "the star tab wrapped onto a second row").toBeLessThan(f.y + f.height / 2);
  await star.click();
  await expect(star).toHaveAttribute("aria-pressed", "true");
  await expect(list.getByText("Star a project to keep it here.")).toBeVisible();
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

for (const p of [{ slug: "stablepairhook", routing: "Routing review needed", swap: false }, { slug: "v4permissionedswaps", routing: "Automatic routing criteria", swap: true }]) {
  test(`/p/${p.slug}: badges and CTA`, async ({ page }) => {
    const errs = watch(page);
    await page.goto(`/p/${p.slug}`);
    const badges = page.getByTestId("badges");
    await expect(badges.getByText("✓ Hooklist", { exact: true })).toBeVisible();
    await expect(badges.getByText("Not in hooklist")).toHaveCount(0);
    await expect(badges.getByText(p.routing, { exact: true })).toBeVisible();
    // the card CTA (desktop) and the sticky bar CTA (phone) are one value; the hidden one is not in the role tree
    await expect(page.getByRole("link", { name: "Swap on Uniswap" })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "View pool on Uniswap" })).toHaveCount(1);
    await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
    if (phone()) await noHorizontalScroll(page);
    expect(errs).toEqual([]);
  });
}

test("0x search: an address prefix finds its project, and Enter pressed while the index loads still goes there", async ({ page }) => {
  const errs = watch(page);
  // hold /api/hooks for a moment so the Enter lands before the index does
  await page.route("**/api/hooks", async (route) => {
    await new Promise((r) => setTimeout(r, 800));
    await route.continue();
  });
  await page.goto("/");
  if (phone()) await page.getByRole("button", { name: "Search", exact: true }).click(); // phones open search from the dock
  const box = page.getByRole("searchbox", { name: "Search hooks, projects or addresses" });
  await box.fill("0xb3ca29");
  await box.press("Enter");
  await expect(page).toHaveURL(/\/p\/hookrmodularhookv6/);
  expect(errs).toEqual([]);
});

test("/p/angstrom: the audit badge links out, the summary says when it was written, the badges explain themselves", async ({ page }) => {
  const errs = watch(page);
  await page.goto("/p/angstrom");
  await expect(page.getByTestId("badges").getByRole("link", { name: /Audit linked/ })).toHaveAttribute("href", "https://github.com/Uniswap/hooklist");
  await expect(page.getByText("Historical generated context · Oct 8")).toBeVisible();
  await page.getByText("Historical generated context · Oct 8").click();
  await expect(page.getByText(/Generated research may be stale or incorrect/)).toBeVisible();
  await page.getByText("What these badges mean").click();
  await expect(page.getByText("Audit link: https://github.com/Uniswap/hooklist. It says nothing about scope or findings.").last()).toBeVisible();
  expect(errs).toEqual([]);
});

test("desktop at 1280px: the detail tables fit their pane, long wallet labels included", async ({ page }) => {
  test.skip(phone(), "tables are a desktop layout");
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/p/stablepairhook");   // its top wallet carries a long label
  const over = await page.locator("table").evaluateAll((ts) => ts.map((t) => t.scrollWidth - (t.parentElement?.clientWidth ?? 0)));
  expect(over.length).toBeGreaterThan(0);
  for (const o of over) expect(o, "a detail table scrolls sideways at 1280px").toBeLessThanOrEqual(0);
});

test("route handlers keep their content type and compress when asked", async ({ request }) => {
  const rss = await request.get("/feed.xml");
  expect(rss.headers()["content-type"]).toContain("application/rss+xml");
  const hooks = await request.get("/api/hooks", { headers: { "Accept-Encoding": "gzip" } });
  expect(hooks.headers()["content-type"]).toContain("application/json");
  expect(Object.keys(await hooks.json()).length).toBeGreaterThan(0);
  const rows = await request.get("/api/rows?v=x", { headers: { "Accept-Encoding": "gzip" } });
  expect(rows.headers()["content-type"]).toContain("application/json");
  const packed = await rows.json();
  expect(packed.keys).toContain("slug");
  expect(packed.rows.length).toBeGreaterThan(0);
});

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


test("research evidence precedes the chart and external actions never imply execution", async ({page}) => {
  await page.goto('/p/stablepairhook');
  const context = page.getByRole('heading',{name:'Project context and evidence'});
  const chart = page.getByRole('heading',{name:'Daily volume'});
  expect((await context.boundingBox())!.y).toBeLessThan((await chart.boundingBox())!.y);
  await expect(page.getByRole('link',{name:'Swap on Uniswap'})).toHaveCount(0);
  await expect(page.getByRole('link',{name:'View pool on Uniswap'})).toHaveCount(1);
  if (!phone()) await expect(page.getByText(/Bait does not execute trades/)).toBeVisible();
  await noHorizontalScroll(page);
});


test("read API: list, one project, hook lookup, and the errors a script can act on", async ({ request }) => {
  const list = await request.get("/api/v1/projects?limit=3&sort=vol7d");
  expect(list.status()).toBe(200);
  expect(list.headers()["x-bait-api-version"]).toBe("1");
  expect(list.headers()["access-control-allow-origin"]).toBe("*");
  const body = await list.json();
  expect(body.count).toBe(3);
  expect(body.projects[0]).toHaveProperty("metrics.vol_7d_usd");
  const one = await request.get(`/api/v1/projects/${body.projects[0].slug}`);
  expect(one.status()).toBe(200);
  const detail = (await one.json()).project;
  expect(Array.isArray(detail.hooks)).toBe(true);
  if (detail.hooks.length) {
    const hit = await request.get(`/api/v1/hooks/${detail.hooks[0].address}`);
    expect(hit.status()).toBe(200);
    expect((await hit.json()).matches.some((m: { project: { slug: string } }) => m.project.slug === detail.slug)).toBe(true);
  }
  expect((await request.get("/api/v1/projects/no-such-project-xyz")).status()).toBe(404);
  expect((await request.get("/api/v1/hooks/0xnothex")).status()).toBe(400);
});
