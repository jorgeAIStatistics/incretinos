import { type APIRequestContext, expect, test } from "@playwright/test";

/*
 * The Conflicts page against the freshly seeded e2e database. Groups are found through the API by what they report
 * (study, arm, endpoint, time point), never by a hard-coded identifier, and every count is read from the API.
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

type Summary = { group_id: string; class: string; arm: string; quantity: string; time_point: string | null; study: { nct_id: string } };
type ListBody = { total: number; rows: Summary[]; counts: Record<string, number> };

async function groups(request: APIRequestContext, query: string): Promise<ListBody> {
  const res = await request.get(`/api/conflicts?limit=500&${query}`);
  expect(res.ok()).toBeTruthy();
  return (await res.json()) as ListBody;
}

async function groupOf(request: APIRequestContext, nct: string, arm: string, weeks: number): Promise<Summary> {
  const body = await groups(request, `study=${nct}&evidence_type=result&endpoint_category=WEIGHT_PCT_CHANGE`);
  const found = body.rows.filter((g) => g.arm === arm && g.time_point === `week ${weeks}`);
  expect(found, `${nct} ${arm} week ${weeks}`).toHaveLength(1);
  return found[0];
}

test("DEMO C: an estimand difference is explained, never labelled a contradiction", async ({ page, request }) => {
  // SURMOUNT-1, 15 mg, week 72: the registry posts the efficacy estimand, the NEJM article the treatment-regimen estimand.
  const group = await groupOf(request, "NCT04184622", "15 mg Tirzepatide", 72);
  expect(group.class).toBe("EXPLAINABLE_DIFFERENCE");
  await page.goto(`/conflicts?group=${group.group_id}`);
  const inspector = page.getByTestId("conflict-inspector");
  await expect(inspector.getByRole("heading", { level: 2 }).first()).toContainText("SURMOUNT-1");
  await expect(inspector.getByTestId("conflict-class").first()).toHaveText("Explainable difference");
  await expect(inspector.getByTestId("group-explanation")).toContainText("that is not a contradiction");
  // Every classification on the page is the explained one: nothing calls the difference a conflict.
  for (const label of await inspector.getByTestId("conflict-class").allInnerTexts()) expect(label).toBe("Explainable difference");
  await expect(inspector).not.toContainText("True conflict");

  // The values side by side, as printed, with their units, precision and estimands.
  const table = inspector.getByRole("region", { name: /stored values side by side/ });
  await expect(table.getByTestId("side-by-side-value")).toHaveText([/−20\.9\s*%/, /−22\.5\s*percent change/]);
  await expect(table).toContainText("1 decimal place");
  await expect(table).toContainText("Efficacy estimand");
  await expect(table).toContainText("Treatment-regimen estimand");
  for (const href of await table.getByTestId("review-link").evaluateAll((links) => links.map((a) => a.getAttribute("href")))) expect(href).toMatch(/^\/review\?type=result&key=[0-9a-f]{64}$/);

  // The field that explains it, and the hierarchy shown only as context.
  const comparison = inspector.getByTestId("comparison").first();
  await expect(comparison.getByTestId("comparison-explanation")).toContainText("not a contradiction");
  await comparison.getByRole("button", { name: "Field by field" }).click();
  await expect(comparison.getByRole("row", { name: /^Estimand/ })).toContainText("Differs: explains the difference");
  await expect(inspector.getByTestId("hierarchy-note")).toContainText("Context only");
  await expect(inspector.getByTestId("next-actions")).toContainText("No correction is needed");
});

test("the list counts each class from the API, filters by class, and opens a true conflict with a documented formulation", async ({ page, request }) => {
  const all = await groups(request, "");
  await page.goto("/conflicts");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Conflicts");
  for (const cls of ["TRUE_CONFLICT", "INSUFFICIENT_INFORMATION", "EXPLAINABLE_DIFFERENCE", "CONSISTENT"]) {
    await expect(page.getByTestId(`class-count-${cls}`)).toHaveText(String(all.counts[cls]));
  }
  await expect(page.getByTestId("class-count-ALL")).toHaveText(String(all.total));

  // Filter to true conflicts: the address keeps the filter and every listed group carries that class, in words.
  await page.getByTestId("class-filter").getByRole("button", { name: /True conflict/ }).click();
  await expect(page).toHaveURL(/class=TRUE_CONFLICT/);
  await expect(page.getByTestId("class-filter").getByRole("button", { name: /True conflict/ })).toHaveAttribute("aria-pressed", "true");
  const shown = Math.min(25, all.counts.TRUE_CONFLICT);
  await expect(page.getByRole("region", { name: /Groups of rows/ }).getByTestId("conflict-class")).toHaveCount(shown);
  for (const label of await page.getByRole("region", { name: /Groups of rows/ }).getByTestId("conflict-class").allInnerTexts()) expect(label).toBe("True conflict");

  // ATTAIN-1, 36 mg, week 72: NEJM -11.2 against the label's -11.1, the label naming the bioequivalent 17.2 mg tablet.
  const attain = await groupOf(request, "NCT05869903", "36 mg Orforglipron", 72);
  await page.getByLabel("Trial").selectOption("NCT05869903");
  await page.getByRole("row").filter({ hasText: "percent change in body weight" }).filter({ hasText: "36 mg Orforglipron" }).getByTestId("open-group").click();
  await expect(page).toHaveURL(new RegExp(`group=${attain.group_id}`));
  const inspector = page.getByTestId("conflict-inspector");
  await expect(inspector.getByTestId("conflict-class").first()).toHaveText("True conflict");
  const values = inspector.getByRole("region", { name: /stored values side by side/ });
  await expect(values).toContainText("−11.2");
  await expect(values).toContainText("−11.1");
  await expect(values).toContainText("17.2 mg");
  // Both kinds of pair are present and each says why.
  await expect(inspector.getByTestId("comparison").filter({ hasText: "True conflict" }).first()).toContainText("differ beyond the precision printed");
  await expect(inspector.getByTestId("comparison").filter({ hasText: "Explainable difference" }).first()).toContainText("estimand");
  await expect(inspector.getByTestId("next-actions")).toContainText("label's analysis population");
  await expect(inspector.getByTestId("possible-reasons")).toContainText("one unit in the last printed digit");

  // From a row into the review workbench, which shows the same classification.
  await values.getByTestId("review-link").first().click();
  await expect(page).toHaveURL(/\/review\?type=result&key=[0-9a-f]{64}/);
  await expect(page.getByTestId("source-conflict")).toContainText(/True conflict|Explainable difference|Consistent/);
  await expect(page.getByTestId("source-conflict").getByRole("link", { name: "Open the comparison" })).toHaveAttribute("href", `/conflicts?group=${attain.group_id}`);
});

test("a publication that does not name its estimand is insufficient information, with the missing detail named", async ({ page, request }) => {
  const group = await groupOf(request, "NCT04660643", "Tirzepatide MTD", 88);
  expect(group.class).toBe("INSUFFICIENT_INFORMATION");
  await page.goto(`/conflicts?group=${group.group_id}`);
  const inspector = page.getByTestId("conflict-inspector");
  await expect(inspector.getByTestId("conflict-class").first()).toHaveText("Insufficient information");
  await expect(inspector.getByTestId("missing-details")).toContainText("The estimand of this value is not named");
  await expect(inspector.getByTestId("next-actions")).toContainText("Confirm the estimand of the publication value");
  // The change from study entry against the change from randomization is a stated baseline difference.
  await expect(inspector.getByTestId("comparison").filter({ hasText: "Explainable difference" }).first()).toContainText("baseline");
});
