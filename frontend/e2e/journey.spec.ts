import { expect, test } from "@playwright/test";

// Demonstration F (docs/examples/session-2-demonstrations.md): one researcher's path through the product, from a drug to an
// exported, cited report. Each step is reached by what the previous page offers (a link, a row, a button); the only address
// typed in is the first one and the review workbench, which the evidence key from the API opens directly.

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

const ENDPOINT = "Percent Change From Baseline in Body Weight (Primary Treatment Period)";

test("drug → trial → endpoint → source evidence → verification status → comparison → limitations → cited report", async ({ page, request }) => {
  // 1. Drug.
  await page.goto("/molecules?drug=tirzepatide");
  await expect(page.getByRole("heading", { name: "Tirzepatide" }).first()).toBeVisible();

  // 2. Trial: from the molecule's tracked trials.
  await page.getByRole("link", { name: "SURMOUNT-1", exact: true }).first().click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("SURMOUNT-1");

  // 3. Endpoint: the primary weight endpoint and its stored arm-level values.
  const group = page.getByTestId("endpoint-group").filter({ has: page.getByText(ENDPOINT, { exact: true }) }).first();
  const toggle = group.getByRole("button").first();
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  const rows = group.getByTestId("result-row");
  const tirzepatide = rows.filter({ hasText: "15 mg Tirzepatide" }).first();
  const placebo = rows.filter({ hasText: /^Placebo/ }).first();
  await expect(tirzepatide).toBeVisible();
  const a = await tirzepatide.getAttribute("data-result-id");
  const b = await placebo.getAttribute("data-result-id");
  expect(a && b && a !== b).toBeTruthy();

  // 4. Source evidence and 5. verification status, as the trial page prints them…
  await expect(tirzepatide).toContainText("Registry-posted results (sponsor-submitted)");
  await expect(tirzepatide.getByTestId("result-qualifiers")).toContainText("Machine-extracted");
  // …and in the evidence review workbench, where the record's source, its location and its status are inspected.
  const dossier = await request.get(`/api/review/evidence/result/${a}`);
  expect(dossier.ok()).toBeTruthy();
  const record = (await dossier.json()) as { evidence_key: string; status: { verification_status: string }; source: { key: string } };
  expect(record.status.verification_status).toBe("MACHINE_EXTRACTED");
  expect(record.source.key).toBe("ctgov:NCT04184622");
  await page.goto(`/review?type=result&key=${record.evidence_key}`);
  await expect(page.getByTestId("record-status")).toHaveAttribute("data-status", "MACHINE_EXTRACTED");
  await expect(page.getByTestId("source-title")).toContainText("NCT04184622");
  await expect(page.getByTestId("stored-value").first()).toContainText("−22.5"); // printed with a true minus sign

  // 6. Compare two findings: the 15 mg arm against placebo, randomized within the same trial.
  await page.goto(`/trials/NCT04184622`);
  const again = page.getByTestId("endpoint-group").filter({ has: page.getByText(ENDPOINT, { exact: true }) }).first();
  if ((await again.getByRole("button").first().getAttribute("aria-expanded")) !== "true") await again.getByRole("button").first().click();
  await again.getByTestId("result-row").filter({ hasText: "15 mg Tirzepatide" }).first().getByRole("link", { name: "Compare →" }).click();
  await expect(page).toHaveURL(new RegExp(`/compare\\?a=${a}`));
  await page.goto(`/compare?a=${a}&b=${b}`);
  const result = page.getByTestId("comparison-result");
  await expect(page.getByTestId("classification")).toHaveText("Direct randomized comparison");
  await expect(result.getByTestId("reported-analysis").first()).toContainText("95% CI");

  // 7. Methodological limitations, in the comparison's own words.
  await expect(result).toContainText("Not assessable from stored sources: endpoint hierarchy and multiplicity.");

  // 8. The exported, cited report.
  const href = await page.getByRole("link", { name: /Export cited report/ }).getAttribute("href");
  expect(href).toBe(`/api/reports/comparison?a=${a}&b=${b}`);
  const report = await request.get(href!);
  expect(report.ok()).toBeTruthy();
  const markdown = await report.text();
  expect(markdown).toContain("**DIRECT RANDOMIZED COMPARISON**");
  expect(markdown).toContain("Not assessable from stored sources: endpoint hierarchy and multiplicity.");
  expect(markdown).toContain("**Does not establish:**");
  expect(markdown).toContain("## Sources");
  expect(markdown).toContain("https://clinicaltrials.gov/study/NCT04184622");
  expect(markdown).toContain("content hash:");
});
