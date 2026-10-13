import { expect, test } from "@playwright/test";

const SHOTS = "../docs/screenshots";
// Animations are fast-forwarded and the dev-server badge hidden so captures are complete and repeatable.
const shot = (page: import("@playwright/test").Page, name: string) =>
  page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true, animations: "disabled", style: "nextjs-portal { display: none !important; }" });

// Every workflow must run without script errors or React warnings (render loops, duplicate keys) in the browser console.
// Failed-request lines are excluded: several workflows exercise refusals that the API answers with a 4xx by design.
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

test("command center shows portfolio status and validation", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("What the public evidence supports");
  await expect(page.getByText(/15 \/ 15/)).toBeVisible();
  const retatrutide = page.getByRole("row", { name: /Retatrutide/ });
  await expect(retatrutide).toContainText("Investigational");
  await expect(retatrutide).toContainText("Triple GIP, GLP-1 and glucagon receptor agonist");
  await expect(page.getByRole("row", { name: /Eloralintide\s*LY3841136/ })).toContainText("Selective amylin receptor agonist");
  await shot(page, "01-command-center");
});

test("vertical slice: trial → publication → results → comparison → cited report", async ({ page, request }) => {
  await page.goto("/trials");
  await page.getByPlaceholder("Name, title or NCT number").fill("SURMOUNT-1");
  await page.getByRole("link", { name: "SURMOUNT-1", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("SURMOUNT-1");
  await expect(page.getByText("PMID 35658024").first()).toBeVisible(); // linked primary publication
  await expect(page.getByText(/Registry reference/i).first()).toBeVisible(); // how the link was established
  // The chart is a list of rows (each row is a keyboard-reachable control), no longer a single image.
  await expect(page.getByRole("list", { name: /Chart of 4 rows/ })).toBeVisible();
  await expect(page.getByText(/Machine-extracted|Quote-anchored/).first()).toBeVisible();
  await shot(page, "03-trial-detail");

  await page.goto("/compare");
  await page.getByRole("link", { name: /Retatrutide 12 mg \(TRIUMPH-1\) vs tirzepatide 15 mg/ }).click();
  await expect(page.getByTestId("classification")).toHaveText("Descriptive cross-study comparison");
  const result = page.getByTestId("comparison-result");
  await expect(result).toContainText("there is no randomization between these groups");
  await expect(result).toContainText("TRIUMPH-5");
  await expect(result).toContainText("Do not state or imply that");
  await expect(result).toContainText("Confidence interval: not available");
  await shot(page, "04-comparison-laboratory");

  const href = await page.getByRole("link", { name: /Export cited report/ }).getAttribute("href");
  const report = await request.get(href!);
  expect(report.ok()).toBeTruthy();
  const markdown = await report.text();
  expect(markdown).toContain("DESCRIPTIVE CROSS-STUDY COMPARISON");
  expect(markdown).toContain("## Sources");
  expect(markdown).toContain("pubmed.ncbi.nlm.nih.gov/35658024");
  expect(markdown).toContain("content hash:");
});

test("within-trial comparison shows the registry analysis; estimand mismatch is refused", async ({ page }) => {
  await page.goto("/compare");
  await page.getByRole("link", { name: /Tirzepatide 15 mg vs placebo within SURMOUNT-1/ }).click();
  await expect(page.getByTestId("classification")).toHaveText("Direct randomized comparison");
  await expect(page.getByTestId("comparison-result")).toContainText("Reported between-arm analysis");
  await expect(page.getByTestId("comparison-result")).toContainText("95% CI");

  await page.goto("/compare");
  await page.getByRole("link", { name: /ATTAIN-1 36 mg: efficacy estimand vs treatment-regimen estimand/ }).click();
  await expect(page.getByTestId("classification")).toHaveText("Not suitable for efficacy comparison");
  await expect(page.getByTestId("comparison-result")).toContainText("They answer different questions");
  await expect(page.getByTestId("comparison-result")).toContainText("Not computed");
});

test("molecule explorer harmonizes Foundayo doses only through documented pairs", async ({ page }) => {
  await page.goto("/molecules?drug=orforglipron");
  await expect(page.getByRole("heading", { name: "Orforglipron" })).toBeVisible();
  await expect(page.getByText("FDA-approved").first()).toBeVisible();
  await expect(page.getByRole("row", { name: /36 mg 17\.2 mg/ })).toContainText("Documented bioequivalence");
  await page.getByRole("button", { name: "Look up documented equivalent" }).click();
  await expect(page.getByTestId("harmonize-result")).toContainText("36 mg corresponds to 17.2 mg");
  await page.getByLabel("Dose").fill("20");
  await page.getByRole("button", { name: "Look up documented equivalent" }).click();
  await expect(page.getByTestId("harmonize-result")).toContainText("No documented equivalent");
  await shot(page, "02-molecule-explorer");

  await page.goto("/molecules?drug=retatrutide");
  await expect(page.getByText("Investigational").first()).toBeVisible();
  await page.goto("/molecules?drug=eloralintide");
  await expect(page.getByText("Selective amylin receptor agonist").first()).toBeVisible();
  await expect(page.getByText("Not a GLP-1 receptor agonist").first()).toBeVisible();
});

test("estimand inspector keeps ATTAIN-1 estimands apart", async ({ page }) => {
  await page.goto("/estimands?study=NCT05869903");
  const row = page.getByRole("row", { name: /percent change in body weight\s*orforglipron 36 mg\s*wk 72/i }).first();
  await expect(row).toContainText("−11.2%");
  await expect(row).toContainText("−12.35%");
  await expect(page.getByText("never merged").first()).toBeVisible();
  await row.getByRole("button", { name: /−11\.2%/ }).first().click();
  await expect(page.getByText("Evidence inspector")).toBeVisible();
  await expect(page.getByText(/treatment-regimen estimand/).first()).toBeVisible();
  await shot(page, "05-estimand-inspector");
});

test("benefit–risk workbench qualifies EloraTZP and labels scores and scenarios", async ({ page }) => {
  await page.goto("/benefit-risk?nct=NCT06603571");
  await expect(page.getByText(/EloraTZP Phase 2b: Type 2 diabetes · Phase 2/)).toBeVisible();
  // The range disclosure is read from the API (adverse-event rows with their quote), not written into the page.
  await expect(page.getByText("Only a range across arms was disclosed for 2 groups of arms")).toBeVisible();
  await expect(page.getByText(/EloraTZP combination arms \(range across arms; arm-level values not disclosed\): 10\.8% to 27%/)).toBeVisible();
  await expect(page.getByText(/10\.8% to 27\.0% with EloraTZP, 0% to 10\.8% with eloralintide/)).toBeVisible();
  await expect(page.getByText(/Efficacy estimand · Sponsor press release/).first()).toBeVisible();
  await shot(page, "06-benefit-risk-eloratzp");

  await page.goto("/benefit-risk?nct=NCT04184622");
  await page.getByRole("button", { name: "Compute summary" }).click();
  await expect(page.getByTestId("utility-result")).toContainText("not a validated clinical endpoint");
  await page.getByRole("button", { name: "Run scenario" }).click();
  await expect(page.getByTestId("scenario-result")).toContainText("HYPOTHETICAL SCENARIO");
  await expect(page.getByTestId("scenario-result")).toContainText("Individual participant data that would be required");
});

test("safety intelligence separates trial incidence from spontaneous reports", async ({ page }) => {
  await page.goto("/safety?drug=tirzepatide");
  await expect(page.getByText("Why these numbers are not incidence — and not evidence of causation")).toBeVisible();
  await expect(page.getByText(/number of people exposed to the drug is unknown/)).toBeVisible();
  await expect(page.getByText("Reports received per quarter")).toBeVisible();
  await expect(page.getByText(/Boxed warning/).first()).toBeVisible();
  await expect(page.getByText(/Denominator: participants at risk/)).toBeVisible();
  await shot(page, "07-safety-intelligence");
  await page.goto("/safety?drug=retatrutide");
  await expect(page.getByText("No FDA labeling is on record for this agent.")).toBeVisible();
  await expect(page.getByText("No spontaneous-report series is stored for this agent.")).toBeVisible();
});

test("evidence gap tracker explains why each question is open", async ({ page }) => {
  await page.goto("/gaps");
  const question = page.getByRole("button", { name: "How does retatrutide compare with tirzepatide in a randomized head-to-head trial?" });
  await expect(question).toBeVisible();
  await question.click();
  await expect(page.getByText("Evidence already available")).toBeVisible();
  await expect(page.getByText(/TRIUMPH-5 \(NCT06662383\)/).first()).toBeVisible();
  await expect(page.getByText(/not confirmation that the sponsor has no data/).first()).toBeVisible();
  await shot(page, "08-evidence-gaps");
});

test("protocol design workbench calculates and labels synthetic output", async ({ page }) => {
  await page.goto("/design");
  await page.getByRole("button", { name: "Calculate" }).click();
  await expect(page.getByTestId("power-result")).toContainText("85 + 85");
  await expect(page.getByTestId("power-result")).toContainText("not a forecast");
  await page.getByRole("tab", { name: "Synthetic simulation and sensitivity" }).click();
  await page.getByRole("button", { name: "Run synthetic simulation" }).click();
  await expect(page.getByTestId("simulation-result")).toContainText("SYNTHETIC SIMULATION — not trial data");
  await expect(page.getByTestId("simulation-result")).toContainText("seed 20261013");
  await shot(page, "09-protocol-design");
  await page.getByRole("tab", { name: "Eligibility and amendments" }).click();
  await expect(page.getByText("Synthetic demonstration — not a real registry change")).toBeVisible();
});

test("research assistant cites evidence and declines recommendations", async ({ page }) => {
  await page.goto("/assistant");
  await expect(page.getByText("No language model is configured — answers are fully deterministic")).toBeVisible();
  await page.getByRole("button", { name: "What evidence differentiates retatrutide from tirzepatide?" }).click();
  const answer = page.getByTestId("assistant-answer");
  await expect(answer).toContainText("Reported evidence");
  await expect(answer).toContainText("No randomized head-to-head result");
  await expect(answer).toContainText("Hypothesis — untested");
  await expect(answer.getByText(/Sources cited \(\d+\)/)).toBeVisible();
  await shot(page, "10-research-assistant");
  await page.getByLabel("Research question").fill("Should I take tirzepatide or retatrutide?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.getByTestId("assistant-answer")).toContainText("does not give treatment recommendations");
});

test("source and audit explorer shows provenance, the change summary and validation", async ({ page }) => {
  await page.goto("/sources");
  await page.getByPlaceholder("Title, NCT, PMID…").fill("NCT04184622");
  await page.getByRole("button", { name: /ClinicalTrials\.gov NCT04184622/ }).click();
  await expect(page.getByText("Content hash (SHA-256)")).toBeVisible();
  await expect(page.getByText(/Yes \(public-domain/)).toBeVisible();
  await shot(page, "11-source-explorer");
  await page.goto("/sources?tab=changes");
  await expect(page.getByText("Synthetic demonstration", { exact: true })).toBeVisible();
  await expect(page.getByRole("cell", { name: "Overall recruitment status" }).first()).toBeVisible();
  await expect(page.getByText(/Manual review queue/)).toBeVisible();
  await page.goto("/sources?tab=validation");
  await expect(page.getByText(/15 of 15/)).toBeVisible();
});
