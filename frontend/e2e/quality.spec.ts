import { expect, type Page, test } from "@playwright/test";

/*
 * Evidence Quality. Every expected count is read from the API at test time, never written into the test. A click on a
 * count must land on exactly the records that count names: the Evidence Review queue for a status, the quality record
 * list for everything else.
 */

type Metric = { id: string; label: string; count: number; denominator: number; links: { href: string; page: { href: string } | null } };
type Overview = { metrics: Metric[]; breakdown: { by: string; groups: { value: string; label: string; counts: Record<string, [number, number]> }[] } };

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

async function overview(page: Page): Promise<Overview> {
  const res = await page.request.get("/api/quality");
  expect(res.ok()).toBeTruthy();
  return (await res.json()) as Overview;
}

const metricOf = (o: Overview, id: string) => {
  const m = o.metrics.find((x) => x.id === id);
  expect(m, id).toBeTruthy();
  return m as Metric;
};

async function openQuality(page: Page, path = "/quality") {
  await page.goto(path);
  await expect(page.getByRole("heading", { level: 1, name: "Evidence Quality" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "Review status" })).toBeVisible({ timeout: 30_000 });
}

function metricRow(page: Page, id: string) {
  return page.locator(`tr[data-metric="${id}"]`);
}

test("each dimension is its own section, with no overall score, and completeness is not efficacy", async ({ page }) => {
  await openQuality(page);
  for (const name of ["Review status", "Completeness", "Source authority", "Methodological reliability signals", "Precision", "Freshness", "Breakdown", "Evidence-completeness assessment"]) {
    await expect(page.getByRole("heading", { level: 2, name, exact: true })).toBeVisible();
  }
  await expect(page.getByText(/does not mean the drug works or is safe/).first()).toBeVisible();
  await expect(page.getByText(/There is no combined quality score/)).toBeVisible();
  // Every chart offers its table.
  const charts = page.locator("figure");
  expect(await charts.count()).toBeGreaterThan(0);
  for (let i = 0; i < (await charts.count()); i++) await expect(charts.nth(i).getByRole("button", { name: "View as table" })).toBeVisible();
});

test("a review-status count opens exactly those records in Evidence Review", async ({ page }) => {
  const o = await overview(page);
  const pending = metricOf(o, "status_pending_human_review");
  expect(pending.count).toBeGreaterThan(0);
  expect(pending.links.href).toBe("/review?status=PENDING_HUMAN_REVIEW");
  await openQuality(page);
  await metricRow(page, "status_pending_human_review").getByRole("link", { name: new RegExp(`^${count(pending.count)} pending human review`) }).click();
  await expect(page).toHaveURL(/\/review\?status=PENDING_HUMAN_REVIEW$/);
  await expect(page.getByRole("navigation", { name: "Pages of records" }).getByRole("status")).toContainText(`of ${count(pending.count)} records`, { timeout: 30_000 });
});

test("a completeness count opens the list of exactly the counted records", async ({ page }) => {
  const o = await overview(page);
  const noCi = metricOf(o, "result_no_ci");
  expect(noCi.count).toBeGreaterThan(0);
  await openQuality(page);
  await metricRow(page, "result_no_ci").getByRole("link", { name: new RegExp(`^${count(noCi.count)} results without a confidence interval`) }).click();
  await expect(page).toHaveURL(/\/quality\/records\?metric=result_no_ci$/);
  await expect(page.getByRole("heading", { level: 1, name: "Results without a confidence interval" })).toBeVisible();
  await expect(page.getByTestId("records-count")).toContainText(`${count(noCi.count)} of ${count(noCi.denominator)}`, { timeout: 30_000 });
  await expect(page.getByRole("navigation", { name: "Pages of records" }).getByRole("status")).toContainText(`of ${count(noCi.count)} records`);
  // Each listed record opens its dossier in Evidence Review.
  const first = page.getByRole("table", { name: /Records counted by/ }).getByRole("link", { name: "Open the record in Evidence Review" }).first();
  await expect(first).toHaveAttribute("href", /^\/review\?type=result&key=[0-9a-f]{64}$/);
});

test("a breakdown cell opens the records of that molecule only, with the true total", async ({ page }) => {
  const o = await overview(page);
  expect(o.breakdown.by).toBe("drug");
  const group = o.breakdown.groups.find((g) => g.value === "tirzepatide");
  expect(group).toBeTruthy();
  const [n, of] = group!.counts.result_no_ci;
  expect(n).toBeGreaterThan(0);
  await openQuality(page);
  const table = page.getByRole("table", { name: "Completeness by molecule" });
  await expect(table).toBeVisible();
  await table.getByRole("link", { name: `${count(n)} of ${count(of)}: results without a confidence interval, ${group!.label}` }).click();
  await expect(page).toHaveURL(/\/quality\/records\?metric=result_no_ci&drug=tirzepatide$/);
  await expect(page.getByTestId("records-count")).toContainText(`${count(n)} of ${count(of)}`, { timeout: 30_000 });
  await expect(page.getByTestId("records-summary")).toContainText("Tirzepatide");
  const api = await (await page.request.get("/api/quality/records?metric=result_no_ci&drug=tirzepatide&limit=500")).json();
  expect(api.total).toBe(n);
  expect(api.rows.every((r: { drugs: string[] }) => r.drugs.includes("tirzepatide"))).toBeTruthy();
});

test("changing the breakdown re-counts by the chosen dimension", async ({ page }) => {
  await openQuality(page);
  await page.getByLabel("Break down by", { exact: true }).selectOption("tier");
  await page.getByLabel("Quality dimension", { exact: true }).selectOption("review_status");
  await expect(page).toHaveURL(/by=tier/);
  const table = page.getByRole("table", { name: "Review status by evidence tier" });
  await expect(table).toBeVisible({ timeout: 30_000 });
  const res = await (await page.request.get("/api/quality?by=tier")).json();
  for (const g of (res as Overview).breakdown.groups) await expect(table.getByRole("rowheader", { name: new RegExp(g.label.replace(/[()]/g, "\\$&")) })).toBeVisible();
});

test("the assessment of one molecule shows each dimension separately with its rules and counts", async ({ page }) => {
  await openQuality(page);
  await page.getByLabel("Molecule", { exact: true }).selectOption("tirzepatide");
  await expect(page).toHaveURL(/assess=drug%3Atirzepatide|assess=drug:tirzepatide/);
  const panel = page.getByTestId("assessment");
  await expect(panel).toBeVisible({ timeout: 30_000 });
  for (const name of ["Completeness", "Source authority", "Methodological reliability signals", "Precision", "Review status", "Freshness"]) {
    await expect(panel.getByRole("heading", { level: 3, name, exact: true })).toBeVisible();
  }
  await expect(panel).not.toContainText(/overall score|quality score:|grade [A-F]\b/i);
  const api = await (await page.request.get("/api/quality/assessment?drug=tirzepatide")).json();
  const completeness = api.sections.find((s: { id: string }) => s.id === "completeness");
  const noCi = completeness.metrics.find((m: Metric) => m.id === "result_no_ci");
  await expect(panel.locator('[data-metric="result_no_ci"]')).toContainText(`${count(noCi.count)} of ${count(noCi.denominator)}`);
});

test("on a phone the quality page and its record list do not scroll sideways", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const path of ["/quality?assess=drug:tirzepatide", "/quality/records?metric=risk_high"]) {
    await page.goto(path, { waitUntil: "networkidle" });
    await expect(page.locator("h1")).toHaveCount(1);
    await expect(page.locator('[role="status"][aria-label^="Loading"], .skeleton')).toHaveCount(0, { timeout: 30_000 });
    await page.waitForLoadState("networkidle");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, `horizontal overflow on ${path}`).toBeLessThanOrEqual(1);
  }
  // The counts stay reachable and readable on a phone.
  await page.goto("/quality");
  await expect(metricRow(page, "result_no_ci").getByTestId("metric-count")).toBeVisible({ timeout: 30_000 });
});
