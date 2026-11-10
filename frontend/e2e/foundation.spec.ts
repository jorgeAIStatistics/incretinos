import { expect, type Page, test } from "@playwright/test";

/*
 * The shared frontend foundation: shell, document titles, responsive layout, request states, paging, and the
 * evidence-state language. These tests write no files. Counts are read from the API, never written into a test.
 */

const ROUTES: [path: string, title: RegExp][] = [
  ["/", /^Command Center · IncretinOS$/],
  ["/molecules", /^Molecule Explorer · IncretinOS$/],
  ["/trials", /^Clinical Trial Atlas · IncretinOS$/],
  ["/trials/NCT05869903", /^ATTAIN-1 \(NCT05869903\) · IncretinOS$/],
  ["/compare", /^Comparison Laboratory · IncretinOS$/],
  ["/estimands", /^Estimand Inspector · IncretinOS$/],
  ["/benefit-risk", /^Benefit–Risk Workbench · IncretinOS$/],
  ["/safety", /^Safety Intelligence · IncretinOS$/],
  ["/gaps", /^Evidence Gap Tracker · IncretinOS$/],
  ["/design", /^Protocol Design · IncretinOS$/],
  ["/assistant", /^Research Assistant · IncretinOS$/],
  ["/sources", /^Sources & Audit · IncretinOS$/],
  ["/review", /^Evidence Review · IncretinOS$/],
  ["/quality", /^Evidence Quality · IncretinOS$/],
  ["/quality/records?metric=result_no_ci", /^Quality Records · IncretinOS$/],
  ["/conflicts", /^Conflicts · IncretinOS$/],
];

const count = (n: number) => new Intl.NumberFormat("en-US").format(n);

let consoleProblems: string[] = [];
test.beforeEach(({ page }) => {
  consoleProblems = [];
  page.on("pageerror", (error) => consoleProblems.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) consoleProblems.push(message.text().slice(0, 300));
  });
});
test.afterEach(() => {
  expect(consoleProblems, "browser console errors").toEqual([]);
});

/** Waits until the page has its heading and no loading placeholder is left. */
async function settled(page: Page) {
  await expect(page.locator("h1")).toHaveCount(1);
  await expect(page.locator('[role="status"][aria-label^="Loading"], .skeleton')).toHaveCount(0);
}

test("every route has its own document title and exactly one page heading", async ({ page }) => {
  for (const [path, title] of ROUTES) {
    await page.goto(path);
    await expect(page, `title of ${path}`).toHaveTitle(title);
    await expect(page.locator("h1"), `h1 on ${path}`).toHaveCount(1);
    await expect(page.getByRole("main")).toHaveCount(1);
  }
});

/**
 * Measured once the page has finished loading — after the network is idle and no placeholder is left, so panels
 * that render late (a risk contrast, a chart) are included — and again in every tab state of the page.
 */
async function sidewaysOverflow(page: Page): Promise<number> {
  await page.waitForLoadState("networkidle");
  await settled(page);
  await page.waitForLoadState("networkidle");
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

for (const viewport of [
  { name: "narrow phone", width: 320, height: 640 },
  { name: "phone", width: 390, height: 844 },
  { name: "tablet", width: 834, height: 1112 },
]) {
  test(`no route scrolls sideways at ${viewport.name} width, in any tab`, async ({ page }) => {
    test.setTimeout(420_000);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    for (const [path] of ROUTES) {
      await page.goto(path, { waitUntil: "networkidle" });
      expect(await sidewaysOverflow(page), `horizontal overflow on ${path} at ${viewport.width}px`).toBeLessThanOrEqual(1);
      const tabs = page.getByRole("tab");
      const names = await tabs.allInnerTexts();
      for (const [i, name] of names.entries()) {
        if ((await tabs.nth(i).getAttribute("aria-selected")) === "true") continue;
        await tabs.nth(i).click();
        expect(await sidewaysOverflow(page), `horizontal overflow on ${path}, tab "${name.trim()}", at ${viewport.width}px`).toBeLessThanOrEqual(1);
      }
    }
  });
}

test("phone navigation: the menu is a dialog that Escape closes, returning focus, and a link closes it", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/gaps");
  const menu = page.getByRole("button", { name: "Open navigation" });
  await expect(menu).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("navigation", { name: "Primary" })).toHaveCount(0); // the desktop sidebar is not exposed on a phone
  await menu.click();
  const drawer = page.getByRole("dialog", { name: "Navigation" });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole("link", { name: "Evidence Gap Tracker" })).toHaveAttribute("aria-current", "page");
  for (const label of ["Evidence Review", "Evidence Quality", "Conflicts", "Sources & Audit"]) await expect(drawer.getByRole("link", { name: label })).toBeVisible();
  await expect(drawer.getByText(/Not medical advice/)).toBeVisible(); // the research-use notice is reachable on a phone

  await page.keyboard.press("Escape");
  await expect(drawer).toHaveCount(0);
  await expect(menu).toBeFocused();

  await menu.click();
  await page.getByRole("dialog", { name: "Navigation" }).getByRole("link", { name: "Clinical Trial Atlas" }).click();
  await expect(page).toHaveURL(/\/trials$/);
  await expect(page.getByRole("dialog", { name: "Navigation" })).toHaveCount(0);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Every tracked trial");
});

test("keyboard: the skip link is the first stop and moves focus to the content", async ({ page }) => {
  await page.goto("/trials");
  await settled(page);
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to content" });
  await expect(skip).toBeFocused();
  await expect(skip).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(page.locator("main#main")).toBeFocused();
  // The desktop sidebar marks the current page for assistive technology as well as visually.
  await expect(page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Clinical Trial Atlas" })).toHaveAttribute("aria-current", "page");
});

test("a failed request shows an error with a retry and never an empty state", async ({ page }) => {
  await page.route("**/api/gaps**", (route) => route.abort());
  await page.goto("/gaps");
  const alert = page.getByRole("alert").filter({ hasText: "The open questions could not be loaded" });
  await expect(alert).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible(); // the page header stays
  await expect(page.getByLabel("Molecule", { exact: true })).toBeVisible(); // and so do the filters
  await expect(page.getByText(/No open question/)).toHaveCount(0);
  await page.unroute("**/api/gaps**");
  await alert.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByRole("table", { name: /Open evidence questions/ })).toBeVisible();
  await expect(alert).toHaveCount(0);
});

test("the command center never shows a count it did not receive", async ({ page }) => {
  await page.route("**/api/validation", (route) => route.abort());
  await page.goto("/");
  await expect(page.getByRole("alert").filter({ hasText: "The validation rules could not be loaded" })).toBeVisible();
  await expect(page.getByText(/passing/)).toHaveCount(0); // no "— passing" placeholder
  await expect(page.getByRole("table", { name: "Portfolio molecules" })).toBeVisible(); // the rest of the page still works
});

test("an unknown trial keeps the page header and selector and reports the problem", async ({ page }) => {
  await page.goto("/estimands?study=NCT99999999");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("One arm, two estimands");
  await expect(page.getByLabel("Trial", { exact: true })).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "The estimands for this trial could not be loaded" })).toContainText("NCT99999999 is not on record");
});

test("verification statuses are counted separately and only a human decision reads as verified", async ({ page, request }) => {
  const meta = await (await request.get("/api/meta")).json();
  const stored: Record<string, number> = meta.result_verification;
  const pending = (stored.PENDING_HUMAN_REVIEW ?? 0) + (stored.CURATED_PENDING_REVIEW ?? 0);
  await page.goto("/");
  const table = page.getByRole("table", { name: "Stored results by verification status" });
  await expect(table.getByRole("row", { name: /Human-verified/ })).toContainText(count(stored.HUMAN_VERIFIED ?? 0));
  await expect(table.getByRole("row", { name: /Machine-extracted/ })).toContainText(count(stored.MACHINE_EXTRACTED ?? 0));
  await expect(table.getByRole("row", { name: /Quote-anchored/ })).toContainText(count(stored.QUOTE_ANCHORED ?? 0));
  await expect(table.getByRole("row", { name: /Pending review/ })).toContainText(count(pending));
  await expect(table.getByRole("row")).toHaveCount(7); // header row plus all six statuses, including those at zero
  // Quote-anchored must not borrow the look of a verification: no check mark, no "ok" colouring.
  const anchored = table.getByRole("row", { name: /Quote-anchored/ }).locator("button span").first();
  await expect(anchored).not.toHaveClass(/bg-ok-soft|text-ok/);
  await expect(table.getByRole("row", { name: /Human-verified/ }).locator("button span").first()).toHaveClass(/text-ok/);
  // Its explanation says what was and was not checked, and Escape dismisses it.
  await table.getByRole("button", { name: "Quote-anchored" }).focus();
  const tip = page.getByRole("tooltip");
  await expect(tip).toContainText("no person has checked that");
  await page.keyboard.press("Escape");
  await expect(tip).toHaveCount(0);
});

test("lists state how many rows exist and every row can be reached", async ({ page, request }) => {
  const meta = await (await request.get("/api/meta")).json();
  const sources: number = meta.counts.sources;
  const open: number = meta.counts.open_review_items;

  await page.goto("/sources");
  const pages = page.getByRole("navigation", { name: "Pages of sources" });
  await expect(pages).toContainText(`1–25 of ${count(sources)} sources`);
  await expect(page.getByRole("table", { name: "Evidence sources" }).locator("tbody tr")).toHaveCount(Math.min(25, sources));
  await pages.getByRole("button", { name: "Next page of sources" }).click();
  await expect(pages).toContainText(`26–${Math.min(50, sources)} of ${count(sources)} sources`);

  await page.goto("/sources?tab=changes");
  const queue = page.getByRole("navigation", { name: "Pages of review items" });
  await expect(queue).toContainText(`of ${count(open)} review items`);
  await expect(queue).toContainText(`Page 1 of ${Math.max(1, Math.ceil(open / 25))}`);
  // Walk to the last page: the final row number must equal the number of open items.
  const next = queue.getByRole("button", { name: "Next page of review items" });
  for (let i = 1; i < Math.ceil(open / 25); i++) await next.click();
  await expect(queue).toContainText(`–${count(open)} of ${count(open)} review items`);
  await expect(next).toBeDisabled();
});

test("lists work with the paged API envelope {total, limit, offset, rows}", async ({ page, request }) => {
  const meta = await (await request.get("/api/meta")).json();
  const sources: number = meta.counts.sources;
  const studies: number = meta.counts.studies;
  // The list endpoints are moving to explicit paging. These handlers re-shape the real answers into the new
  // envelope in the browser; nothing is stored and no value is invented.
  const pageSizes: number[] = [];
  await page.route(/\/api\/sources\?/, async (route) => {
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get("limit") ?? 50);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    url.searchParams.set("limit", "2000");
    url.searchParams.delete("offset");
    const full = await (await route.fetch({ url: url.toString() })).json();
    const rows = full.rows.slice(offset, offset + limit);
    pageSizes.push(rows.length);
    await route.fulfill({ json: { ...full, total: full.rows.length, limit, offset, rows } });
  });
  await page.goto("/sources");
  const pages = page.getByRole("navigation", { name: "Pages of sources" });
  await expect(pages).toContainText(`1–25 of ${count(sources)} sources`);
  await pages.getByRole("button", { name: "Next page of sources" }).click();
  await expect(pages).toContainText(`26–${Math.min(50, sources)} of ${count(sources)} sources`);
  await expect(page.getByRole("table", { name: "Evidence sources" }).locator("tbody tr")).toHaveCount(Math.min(25, sources - 25));
  expect(Math.max(...pageSizes), "one page per request, not the whole list").toBeLessThanOrEqual(25);

  // A list that is filtered in the browser reads every page of a paged endpoint before it shows a total.
  await page.route(/\/api\/studies(\?.*)?$/, async (route) => {
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get("limit") ?? 20);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    // The whole list, whichever shape the service answers in (a bare array, or the envelope with its largest page).
    const whole = await (await route.fetch({ url: `${url.origin}${url.pathname}?limit=1000` })).json();
    const all: unknown[] = Array.isArray(whole) ? whole : whole.rows;
    expect(all.length, "the re-shaped list is the whole list").toBe(Array.isArray(whole) ? all.length : whole.total);
    await route.fulfill({ json: { total: all.length, limit, offset, rows: all.slice(offset, offset + limit) } });
  });
  await page.goto("/trials");
  await expect(page.getByRole("navigation", { name: "Pages of trials" })).toContainText(`of ${count(studies)} trials`);
  await expect(page.getByRole("table", { name: "Trials ingested in depth" }).locator("tbody tr")).toHaveCount(Math.min(50, studies));
});

test("a status the interface does not know is still counted, and legacy names are folded in", async ({ page }) => {
  // Synthetic counts, injected into the response in the browser only, to exercise the summary. Nothing is stored.
  await page.route("**/api/overview", async (route) => {
    const body = await (await route.fetch()).json();
    body.meta.result_verification = { MACHINE_EXTRACTED: 10, PENDING_HUMAN_REVIEW: 3, CURATED_PENDING_REVIEW: 2, SOURCE_CHANGED_RECHECK_REQUIRED: 1, REJECTED: 1, SOMETHING_NEW: 4 };
    await route.fulfill({ json: body });
  });
  await page.goto("/");
  const table = page.getByRole("table", { name: "Stored results by verification status" });
  await expect(table.getByRole("row", { name: /Pending review/ })).toContainText("5");
  await expect(table.getByRole("row", { name: /Recheck required/ })).toContainText("1");
  await expect(table.getByRole("row", { name: /Rejected/ })).toContainText("1");
  await expect(table.getByRole("row", { name: /Something new/ })).toContainText("4");
  await expect(table.getByRole("row", { name: /Human-verified/ })).toContainText("0.0%");
  await expect(table.getByRole("row")).toHaveCount(8); // header, six known statuses, one unknown
});

test("sortable column headers report their state and sort the rows", async ({ page }) => {
  await page.goto("/trials");
  const table = page.getByRole("table", { name: "Trials ingested in depth" });
  const header = table.getByRole("columnheader", { name: "Enrolled" });
  await expect(header).toHaveAttribute("aria-sort", "none");
  await header.getByRole("button").click();
  await expect(header).toHaveAttribute("aria-sort", "ascending");
  const enrolled = async () => (await table.locator("tbody tr td.r").allInnerTexts()).map((t) => Number(t.split("\n")[0].replace(/,/g, ""))).filter((n) => !Number.isNaN(n));
  const ascending = await enrolled();
  expect(ascending.length).toBeGreaterThan(5);
  expect(ascending).toEqual([...ascending].sort((a, b) => a - b));
  await header.getByRole("button").click();
  await expect(header).toHaveAttribute("aria-sort", "descending");
  const descending = await enrolled();
  expect(descending).toEqual([...descending].sort((a, b) => b - a));
});

test("filters live in the address, and tabs follow the arrow keys", async ({ page }) => {
  await page.goto("/trials");
  await page.getByLabel("Molecule", { exact: true }).selectOption({ label: "Retatrutide" });
  await expect(page).toHaveURL(/drug=retatrutide/);
  await page.reload();
  await expect(page.getByLabel("Molecule", { exact: true })).toHaveValue("retatrutide");
  await expect(page.getByRole("button", { name: "Clear filters" })).toBeVisible();

  await page.goto("/sources");
  await page.getByRole("tab", { name: "Sources" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Changes and review queue" })).toHaveAttribute("aria-selected", "true");
  await expect(page).toHaveURL(/tab=changes/);
  await expect(page.getByRole("heading", { name: "Manual review queue" })).toBeVisible();
});

test("a technical term explains itself and Escape returns focus to it", async ({ page }) => {
  await page.goto("/estimands?study=NCT05869903");
  const term = page.getByRole("button", { name: "treatment-regimen estimand: what this means" });
  await term.click();
  const explanation = page.getByRole("dialog").filter({ hasText: "Treatment-regimen estimand" });
  await expect(explanation).toContainText("whether or not they stayed on it");
  await page.keyboard.press("Escape");
  await expect(explanation).toHaveCount(0);
  await expect(term).toBeFocused();
});

test("reduced motion: overlays and placeholders do not animate", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/trials");
  await page.getByRole("button", { name: "Open navigation" }).click();
  const drawer = page.getByRole("dialog", { name: "Navigation" });
  await expect(drawer).toBeVisible();
  const seconds = await drawer.evaluate((el) => Number.parseFloat(getComputedStyle(el).animationDuration));
  expect(seconds).toBeLessThan(0.001);
});
