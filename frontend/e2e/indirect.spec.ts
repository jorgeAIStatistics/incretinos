import { type Locator, type Page, expect, test } from "@playwright/test";

// The anchored indirect comparison (Bucher) in the comparison laboratory: one pair that meets every requirement and is
// calculated (SURMOUNT-1 × ATTAIN-1, week 72, efficacy estimand), and one that is refused (SURMOUNT-2, type 2 diabetes,
// × ATTAIN-1, no diabetes). Numbers are read from the API's answer, never typed into the test.
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

/** Choose the first option whose text matches, once the engine's list has loaded. */
async function pick(select: Locator, pattern: RegExp): Promise<string> {
  await expect.poll(async () => (await select.locator("option").allTextContents()).some((t) => pattern.test(t)), { timeout: 15_000 }).toBe(true);
  const label = (await select.locator("option").allTextContents()).find((t) => pattern.test(t)) as string;
  await select.selectOption({ label });
  return label;
}

async function choosePair(page: Page, a: RegExp, b: RegExp) {
  const section = page.getByTestId("indirect-section");
  await pick(section.getByLabel("Drug A", { exact: true }), /^Tirzepatide/);
  await pick(section.getByLabel("Drug B", { exact: true }), /^Orforglipron/);
  await pick(section.getByLabel("Common comparator C", { exact: true }), /^Placebo$/);
  await pick(section.getByLabel(/^Analysis A/), a);
  await pick(section.getByLabel(/^Analysis B/), b);
  return section;
}

test("DEMO E: SURMOUNT-1 × ATTAIN-1 is calculated as an anchored indirect estimate, with its assumptions", async ({ page, request }) => {
  await page.goto("/compare");
  const section = await choosePair(page, /^SURMOUNT-1 · 15 mg Tirzepatide vs Placebo · .* · week 72 · Efficacy estimand/, /^ATTAIN-1 · 36 mg Orforglipron vs Placebo · .* · week 72 · Efficacy estimand/);
  await expect(section.getByRole("heading", { name: "Anchored indirect comparison" })).toBeVisible();
  const result = section.getByTestId("indirect-result");
  await expect(result).toBeVisible();
  await expect(section.getByTestId("indirect-outcome")).toHaveAttribute("data-status", "CALCULATED");
  await expect(result).toContainText("Anchored indirect estimate (Bucher) — not a randomized comparison");
  await expect(result).toContainText("Not a randomized comparison · not a ranking");

  // The number on the page is the API's, from the stored rows.
  const a = Number(await section.getByLabel(/^Analysis A/).inputValue());
  const b = Number(await section.getByLabel(/^Analysis B/).inputValue());
  const api = await (await request.post("/api/indirect/assess", { data: { a, b } })).json();
  expect(api.status).toBe("CALCULATED");
  // The page prints two decimals without trailing zeros and a true minus sign (lib/api.ts fmt).
  const fmt = (v: number) => v.toFixed(2).replace(/\.?0+$/, "").replace("-", "−");
  await expect(section.getByTestId("indirect-estimate")).toContainText(fmt(api.result.estimate));
  await expect(section.getByTestId("indirect-ci")).toContainText(`95% CI ${fmt(api.result.ci_low)} to ${fmt(api.result.ci_high)}`);
  await expect(section.getByTestId("indirect-formula")).toContainText("d_AB = d_AC − d_BC");

  // Every requirement is shown; what cannot be verified is shown as an unverified assumption.
  for (const key of ["network", "comparator", "estimand", "outcome", "effect_scale", "follow_up", "reliability", "variance"]) {
    await expect(section.getByTestId(`indirect-check-${key}`)).toHaveAttribute("data-status", "PASS");
  }
  await expect(section.getByTestId("indirect-check-transitivity")).toHaveAttribute("data-status", "CANNOT_ASSESS");
  await expect(section.getByTestId("indirect-unverified")).toContainText("Transitivity");
  await expect(section.getByTestId("indirect-modifiers")).toContainText("Mean baseline BMI");
  await expect(section.getByTestId("indirect-modifiers")).toContainText("not stored");
  await expect(section.getByTestId("indirect-inputs")).toContainText("SURMOUNT-1");
  await expect(section.getByTestId("indirect-inputs")).toContainText("ATTAIN-1");
  // The network has a text equivalent naming both trials and saying A and B were never randomized against each other.
  await expect(section.getByTestId("indirect-network-text")).toContainText("NCT04184622");
  await expect(section.getByTestId("indirect-network-text")).toContainText("NCT05869903");
  await expect(section.getByTestId("indirect-network-text")).toContainText("never randomized against each other");
  await expect(section.getByRole("img", { name: /never randomized against each other/ })).toBeVisible();

  // The three terms are explained where they are used.
  await section.getByRole("button", { name: "anchored indirect comparison: what this means" }).click();
  await expect(page.getByRole("dialog")).toContainText("Bucher method");
  await page.keyboard.press("Escape");
  await expect(section.getByRole("button", { name: "transitivity: what this means" }).first()).toBeVisible();
  await expect(section.getByRole("button", { name: "effect modifier: what this means" }).first()).toBeVisible();
  await section.screenshot({ path: "test-results/indirect/demo-e-calculated.png", animations: "disabled" }); // git-ignored, for review
});

test("DEMO E: SURMOUNT-2 (type 2 diabetes) × ATTAIN-1 (no diabetes) is refused, with the reasons", async ({ page }) => {
  await page.goto("/compare");
  const section = await choosePair(page, /^SURMOUNT-2 · 15 mg Tirzepatide vs Placebo · .* · week 72/, /^ATTAIN-1 · 36 mg Orforglipron vs Placebo · .* · week 72 · Efficacy estimand/);
  await expect(section.getByTestId("indirect-outcome")).toHaveAttribute("data-status", "REFUSED");
  await expect(section.getByTestId("indirect-result")).toHaveCount(0);
  await expect(section.getByTestId("indirect-estimate")).toHaveCount(0);
  const reasons = section.getByTestId("indirect-refusal-reasons");
  await expect(reasons).toContainText("type 2 diabetes");
  await expect(reasons).toContainText("The estimand is not stated for SURMOUNT-2");
  await expect(section.getByTestId("indirect-check-population")).toHaveAttribute("data-status", "FAIL");
  await expect(section.getByTestId("indirect-check-estimand")).toHaveAttribute("data-status", "CANNOT_ASSESS");
  // The checklist and the network are still shown, so the refusal can be audited.
  await expect(section.getByTestId("indirect-network-text")).toContainText("NCT04657003");
  await section.screenshot({ path: "test-results/indirect/demo-e-refused.png", animations: "disabled" }); // git-ignored, for review
});

test("a direct randomized comparison states its reported effect without going beyond the stored fields", async ({ page }) => {
  await page.goto("/compare");
  await page.getByRole("link", { name: /Tirzepatide 15 mg vs placebo within SURMOUNT-1/ }).click();
  const effect = page.getByTestId("reported-effect").first();
  await expect(effect).toContainText("Difference: 15 mg Tirzepatide − Placebo");
  await expect(effect).toContainText("95% CI −21.2 to −19, two-sided");
  await expect(effect).toContainText("Efficacy (inferred from analysis description)");
  await expect(effect).toContainText("n analysed: 625 versus 635");
  await expect(effect).toContainText("Multiplicity control");
  await expect(page.getByTestId("reported-effect-reading").first()).toContainText("Whether it was inside multiplicity control is not stated.");
});
