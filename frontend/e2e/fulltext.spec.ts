import { type APIRequestContext, expect, test } from "@playwright/test";

/*
 * Full-text evidence in the review workbench: a row read from a document shows the document and its licence, where in
 * the document the value is, the table as extracted with the used cells marked (or the sentence), the footnotes, the
 * parsing method and the quality assessment. Records are found through the API by what they are, never by row id.
 * Nothing is decided here.
 */

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

type Row = { evidence_type: string; evidence_key: string; label: string; verification_status: string; origin: string | null };

async function fullTextRows(request: APIRequestContext): Promise<Row[]> {
  const res = await request.get("/api/review/evidence?origin=FULLTEXT&evidence_type=analysis&limit=200");
  expect(res.ok()).toBeTruthy();
  return ((await res.json()) as { rows: Row[] }).rows;
}

test("an FDA-review table row shows the document, licence basis, locator, extracted table with the used cells, footnotes, method and quality", async ({ page, request }) => {
  const rows = await fullTextRows(request);
  const row = rows.find((r) => r.label.includes("ATTAIN-1") && r.label.includes("FDA primary analysis") && r.label.includes("OFG 6 mg N=723"));
  expect(row, "the FDA Table 23 difference for orforglipron 6 mg").toBeTruthy();
  expect(row!.verification_status).toBe("PENDING_HUMAN_REVIEW");

  await page.goto(`/review?type=analysis&key=${row!.evidence_key}`);
  const panel = page.getByTestId("fulltext-panel");
  await expect(panel).toBeVisible();
  await expect(page.getByTestId("fulltext-licence")).toContainText("No licence returned");
  await expect(page.getByTestId("fulltext-stored")).toHaveText("No");
  await expect(page.getByTestId("fulltext-document")).toContainText("17 U.S.C.");
  await expect(page.getByTestId("fulltext-locator-text")).toHaveText("FDA Table 23, PDF page 135 (printed page 112)");
  const table = page.getByTestId("fulltext-table");
  await expect(table).toContainText("Difference from placebo, (95% CI)");
  await expect(table.locator('td[data-used="true"]')).toHaveText(["-5.3 (-6.1, -4.5)", "<0.001"]);
  await expect(page.getByTestId("fulltext-footnotes")).toContainText("Analysis of covariance model");
  const quality = page.getByTestId("fulltext-quality");
  await expect(quality).toContainText("pdf-positioned-text/pypdf");
  await expect(quality).toContainText("Arm match");
  await expect(quality).toContainText("UNIQUE");
  await expect(page.getByTestId("fulltext-confidence")).toHaveText("MEDIUM");
  await expect(quality).toContainText("not evidence that the value is right");
});

test("a sentence read from an open-access article shows its licence, the stored full text and the sentence", async ({ page, request }) => {
  const rows = await fullTextRows(request);
  const row = rows.find((r) => r.label.includes("SURMOUNT-5") && r.label.includes("HbA1c at Week 72"));
  expect(row, "the SURMOUNT-5 prediabetes HbA1c treatment difference").toBeTruthy();

  await page.goto(`/review?type=analysis&key=${row!.evidence_key}`);
  await expect(page.getByTestId("fulltext-licence")).toHaveText("cc by");
  await expect(page.getByTestId("fulltext-stored")).toHaveText("Yes");
  await expect(page.getByTestId("fulltext-extracted")).toContainText("estimated treatment difference [95% confidence interval]");
  await expect(page.getByTestId("fulltext-locator-text")).toContainText("Results > Changes in glycemic parameters");
  await expect(page.getByTestId("fulltext-quality")).toContainText("SUBGROUP_POST_HOC");
});
