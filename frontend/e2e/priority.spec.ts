import { expect, type Locator, test } from "@playwright/test";

/*
 * The two priority pages: the Research Command Center and the Research Assistant.
 * Command center: every count is a link to the page that lists exactly what it counts, and that page shows the same
 * number. Assistant: [C#] markers, source numbers, review links and follow-ups resolve to what the API returned.
 * Counts and keys are read from the API, never written into a test. These tests write no files.
 */

const count = (n: number) => new Intl.NumberFormat("en-US").format(n);
const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

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

/** The number a count link carries, read from its accessible name ("267 evidence records …"). */
async function linkCount(scope: Locator, href: string): Promise<number> {
  const name = (await scope.locator(`a[href="${href}"]`).first().getAttribute("aria-label")) ?? "";
  return Number(name.split(" ")[0].replace(/,/g, ""));
}

/* ------------------------------------------------------------------ command center */

test("command center: each attention count opens the list that holds exactly that many records", async ({ page, request }) => {
  const summary = await (await request.get("/api/review/summary")).json();
  const conflicts = await (await request.get("/api/conflicts?limit=1")).json();
  const meta = await (await request.get("/api/meta")).json();
  await page.goto("/");
  const attention = page.getByRole("list", { name: "Unresolved review work" });
  await expect(attention.getByTestId("attention-pending")).toContainText(count(summary.by_status.PENDING_HUMAN_REVIEW));

  // Pending human review → the review queue filtered to that status.
  const pending = summary.by_status.PENDING_HUMAN_REVIEW as number;
  expect(pending).toBeGreaterThan(0);
  await attention.getByRole("link", { name: /evidence records pending human review/ }).click();
  await expect(page).toHaveURL(/\/review\?status=PENDING_HUMAN_REVIEW$/);
  await expect(page.getByText(new RegExp(`^${esc(count(pending))} records of`))).toBeVisible({ timeout: 30_000 });

  // True conflicts and unreconcilable differences → the conflict list filtered to that class.
  for (const [cls, name] of [["TRUE_CONFLICT", /true source conflicts/], ["INSUFFICIENT_INFORMATION", /insufficient information/]] as const) {
    const n = conflicts.counts[cls] as number;
    await page.goto("/");
    if (n === 0) {
      await expect(attention.getByRole("link", { name })).toHaveCount(0);
      continue;
    }
    await attention.getByRole("link", { name }).click();
    await expect(page).toHaveURL(new RegExp(`/conflicts\\?class=${cls}$`));
    await expect(page.getByText(new RegExp(`^${n} groups? of ${esc(count(conflicts.all_groups))}`))).toBeVisible({ timeout: 30_000 });
  }

  // Manual review-queue items → the queue on Sources & Audit.
  await page.goto("/");
  const queue = meta.counts.open_review_items as number;
  if (queue > 0) {
    await attention.getByRole("link", { name: /open items in the manual review queue/ }).click();
    await expect(page).toHaveURL(/\/sources\?tab=changes$/);
    await expect(page.getByText(new RegExp(`^${esc(count(queue))} open items?`))).toBeVisible({ timeout: 30_000 });
  }

  // A zero is shown as zero and is not a link to an empty list.
  await page.goto("/");
  for (const [key, value] of [["held", summary.held_total ?? 0], ["orphaned", summary.orphaned_decisions_total ?? 0], ["recheck", summary.by_status.SOURCE_CHANGED_RECHECK_REQUIRED ?? 0]] as const) {
    const row = attention.getByTestId(`attention-${key}`);
    await expect(row).toContainText(count(value));
    if (value === 0) await expect(row.getByRole("link", { name: /open the list/ })).toHaveCount(0);
  }
});

test("command center: trial counts open the trial atlas filtered to the same trials", async ({ page, request }) => {
  const studies = (await (await request.get("/api/studies?limit=1000")).json()).rows as { phase: string | null; overall_status: string | null; has_results: boolean }[];
  const overview = await (await request.get("/api/overview")).json();
  await page.goto("/");
  const table = page.getByTestId("phase-status");
  await expect(table).toBeVisible({ timeout: 30_000 });
  const hrefs = await table.locator("a").evaluateAll((links) => links.map((a) => a.getAttribute("href") ?? ""));
  expect(hrefs.length).toBeGreaterThan(4);

  // Counts are read once (the page is not reloaded per link, which would spend the API's request budget). The first
  // link is followed by a click; the rest by their address. Each lands on the atlas with the same count.
  const counts = new Map<string, number>();
  for (const href of hrefs) counts.set(href, await linkCount(table, href));
  for (const [i, href] of hrefs.entries()) {
    const n = counts.get(href) ?? -1;
    const params = new URL(href, "http://x").searchParams;
    const expected = studies.filter(
      (s) =>
        (!params.get("phase") || (s.phase ?? "none") === params.get("phase")) &&
        (!params.get("status") || (s.overall_status ?? "none") === params.get("status")) &&
        (!params.get("results") || s.has_results === (params.get("results") === "yes")),
    ).length;
    expect(n, href).toBe(expected);
    if (i === 0) await table.locator(`a[href="${href}"]`).first().click();
    else await page.goto(href);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Every tracked trial");
    await expect(page.getByText(new RegExp(`^${n} of ${studies.length} trials( match the filters)?\\.`))).toBeVisible({ timeout: 30_000 });
  }
  await page.goto("/");

  // Completed without posted results: the list on the page and the filtered atlas agree.
  const unpublished = overview.completed_without_registry_results.length as number;
  await expect(page.getByTestId("completed-without-results").getByRole("listitem")).toHaveCount(unpublished);
  await page.getByRole("link", { name: `All ${unpublished} in the trial atlas` }).click();
  await expect(page).toHaveURL(/\/trials\?status=COMPLETED&results=no$/);
  await expect(page.getByText(new RegExp(`^${unpublished} of ${studies.length} trials match the filters\\.`))).toBeVisible({ timeout: 30_000 });
  await expect(page.getByLabel("Recruitment status")).toHaveValue("COMPLETED");
  await expect(page.getByLabel("Registry results")).toHaveValue("no");
});

test("command center: gap, quality and ledger counts open their filtered lists", async ({ page, request }) => {
  const overview = await (await request.get("/api/overview")).json();
  const quality = await (await request.get("/api/quality")).json();
  await page.goto("/");

  // Gaps by nature → the gap tracker filtered to that nature.
  const natures = Object.entries(overview.gaps_by_nature as Record<string, number>).filter(([, n]) => n > 0);
  for (const [nature, n] of natures.slice(0, 3)) {
    await page.goto("/");
    await page.getByTestId("gaps-by-nature").locator(`a[href="/gaps?nature=${nature}"]`).click();
    await expect(page).toHaveURL(new RegExp(`/gaps\\?nature=${nature}$`));
    await expect(page.getByText(new RegExp(`of ${n} open questions?`))).toBeVisible({ timeout: 30_000 });
  }

  // Quality figures → exactly the records they count.
  const metrics = new Map((quality.metrics as { id: string; count: number }[]).map((m) => [m.id, m.count]));
  for (const id of ["result_no_ci", "status_pending_human_review"]) {
    const n = metrics.get(id) ?? 0;
    expect(n).toBeGreaterThan(0);
    await page.goto("/");
    await page.getByTestId(`quality-metric-${id}`).getByRole("link").click();
    await expect(page).toHaveURL(new RegExp(`/quality/records\\?metric=${id}$`));
    await expect(page.getByText(new RegExp(`of ${esc(count(n))} records`))).toBeVisible({ timeout: 30_000 });
  }
  // Six separate dimensions, never one score.
  await page.goto("/");
  await expect(page.locator('[data-testid^="quality-dimension-"]')).toHaveCount((quality.dimensions as unknown[]).length);
  await expect(page.getByText("There is no overall score", { exact: false })).toBeVisible();

  // Ledger: trials in depth → the atlas.
  await page.getByTestId("ledger-trials").getByRole("link").click();
  await expect(page.getByText(new RegExp(`^${overview.meta.counts.studies} of ${overview.meta.counts.studies} trials\\.`))).toBeVisible({ timeout: 30_000 });
});

test("command center: portfolio states regulatory status, its review state and the derived development stage", async ({ page, request }) => {
  const overview = await (await request.get("/api/overview")).json();
  await page.goto("/");
  const table = page.getByRole("table", { name: "Portfolio molecules" });
  await expect(table.locator("tbody tr")).toHaveCount(overview.drugs.length);
  const retatrutide = table.getByRole("row", { name: /Retatrutide/ });
  await expect(retatrutide).toContainText("Investigational");
  await expect(retatrutide).toContainText(/Phase \d/);
  const drug = overview.drugs.find((d: { slug: string }) => d.slug === "retatrutide");
  if (drug.regulatory_statement_status === "PENDING_HUMAN_REVIEW") await expect(retatrutide).toContainText("Pending review");
  await expect(retatrutide.getByRole("link", { name: /tracked trials with Retatrutide/ })).toHaveAttribute("href", "/trials?drug=retatrutide");

  // A status withheld in review reads as withheld, never as a status (synthetic answer, in the browser only).
  await page.route("**/api/overview", async (route) => {
    const body = await (await route.fetch()).json();
    const d = body.drugs.find((x: { slug: string }) => x.slug === "retatrutide");
    Object.assign(d, { regulatory_status: "UNKNOWN", regulatory_status_available: false, regulatory_status_note: "The statement was rejected in review.", regulatory_source: null, regulatory_as_of: null });
    await route.fulfill({ json: body });
  });
  await page.goto("/");
  const withheld = page.getByRole("table", { name: "Portfolio molecules" }).getByRole("row", { name: /Retatrutide/ });
  await expect(withheld).toContainText("Status withheld in review");
  await expect(withheld).not.toContainText("Investigational");
  await page.unroute("**/api/overview");
});

test("command center: a failed block reports itself and the rest of the page still works", async ({ page }) => {
  await page.route("**/api/review/summary", (route) => route.fulfill({ status: 500, json: { detail: "Simulated failure" } }));
  await page.route("**/api/quality", (route) => route.fulfill({ status: 500, json: { detail: "Simulated failure" } }));
  await page.goto("/");
  await expect(page.getByRole("alert").filter({ hasText: "The review counts could not be loaded" })).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "The quality counts could not be loaded" })).toBeVisible();
  await expect(page.getByTestId("attention-pending")).toContainText("Not loaded"); // no count is invented
  await expect(page.getByRole("table", { name: "Portfolio molecules" })).toBeVisible();
  await expect(page.getByTestId("phase-status")).toBeVisible();
});

/* ------------------------------------------------------------------ research assistant */

const QUESTION = "What evidence differentiates retatrutide from tirzepatide?";

test("assistant: markers, sources, review records and follow-ups resolve to what the API returned", async ({ page, request }) => {
  const api = await (await request.post("/api/assistant/ask", { data: { question: QUESTION } })).json();
  // The question is in the address, so the answer can be linked to.
  await page.goto(`/assistant?q=${encodeURIComponent(QUESTION)}`);
  const answer = page.getByTestId("assistant-answer");
  await expect(answer).toBeVisible({ timeout: 30_000 });
  await expect(page.getByLabel("Research question")).toHaveValue(QUESTION);

  // Every block is rendered, in the API's order, with its kind in words.
  await expect(answer.getByTestId("answer-block")).toHaveCount(api.blocks.length);
  const kinds = await answer.getByTestId("answer-block").evaluateAll((els) => els.map((e) => e.getAttribute("data-kind")));
  expect(kinds).toEqual(api.blocks.map((b: { kind: string }) => b.kind));

  // Each [C#] marker links to its own block, and the marker index resolves every marker the API listed.
  for (const id of Object.keys(api.markers)) {
    await expect(page.locator(`#card-${id}`)).toHaveCount(1);
    await expect(page.locator(`#card-${id}`).getByTestId("block-marker")).toHaveAttribute("href", `#card-${id}`);
  }
  const index = page.getByTestId("marker-index");
  await expect(index.getByRole("row")).toHaveCount(Object.keys(api.markers).length + 1);

  // The result block: source number, review record by evidence key, the finding and the trial.
  const i = api.blocks.findIndex((b: { result_ids: number[] }) => b.result_ids.length > 0);
  const block = api.blocks[i];
  const card = page.locator(`#card-${block.card_id}`);
  const n = api.sources.findIndex((s: { id: number }) => s.id === block.citations[0]) + 1;
  await expect(card.getByTestId("source-ref").first()).toHaveAttribute("href", `#source-${n}`);
  await expect(page.locator(`#source-${n}`).getByRole("link").first()).toHaveAttribute("href", `/sources?id=${block.citations[0]}`);
  const key = block.evidence[0].evidence_key as string;
  expect(key).toMatch(/^[0-9a-f]{64}$/);
  await expect(card.getByTestId("review-link")).toHaveAttribute("href", `/review?type=${block.evidence[0].evidence_type}&key=${key}`);
  await expect(card.getByTestId("open-finding")).toHaveAttribute("href", `/compare?a=${block.result_ids[0]}`);
  const nct = /NCT\d{8}/.exec(block.text)?.[0];
  if (nct) await expect(card.getByTestId("open-trial").first()).toHaveAttribute("href", `/trials/${nct}`);

  // A marker in the index scrolls to its block and marks it as the target.
  await index.getByRole("link", { name: new RegExp(`Marker ${block.card_id}:`) }).click();
  await expect(page).toHaveURL(new RegExp(`#card-${block.card_id}$`));
  await expect(card).toBeInViewport();

  // Comparing the two reported findings opens the comparison laboratory with its classification.
  const results = Array.from(new Set(api.blocks.flatMap((b: { result_ids: number[] }) => b.result_ids)));
  await expect(page.getByTestId("compare-findings")).toHaveAttribute("href", `/compare?a=${results[0]}&b=${results[1]}`);
  await page.getByTestId("compare-findings").click();
  await expect(page.getByTestId("classification")).toBeVisible({ timeout: 30_000 });

  // The review link opens exactly that record in Evidence Review.
  await page.goto(`/assistant?q=${encodeURIComponent(QUESTION)}`);
  await page.locator(`#card-${block.card_id}`).getByTestId("review-link").click();
  await expect(page.getByTestId("record-view")).toHaveAttribute("data-evidence-key", key, { timeout: 30_000 });
});

test("assistant: works with no model, by keyboard, and declines a recommendation", async ({ page }) => {
  await page.goto("/assistant");
  await expect(page.getByTestId("model-status")).toContainText("No language model is configured");
  const example = page.getByRole("button", { name: QUESTION, exact: true });
  await example.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("assistant-answer")).toBeVisible({ timeout: 30_000 });
  await expect(page).toHaveURL(/\/assistant\?q=/);
  await expect(page.getByTestId("answer-limitations")).toContainText("not evidence that it does not exist");
  await expect(page.getByTestId("model-withheld")).toHaveCount(0);

  const field = page.getByLabel("Research question");
  await field.fill("x");
  await field.press("Enter");
  await expect(page.getByRole("alert").filter({ hasText: "at least three characters" })).toBeVisible();

  await field.fill("Should I take tirzepatide or retatrutide?");
  await field.press("Enter");
  const refusal = page.getByTestId("answer-block").first();
  await expect(refusal).toHaveAttribute("data-kind", "REFUSAL");
  await expect(refusal.getByRole("heading", { level: 3 })).toHaveText("Not answered");
});

test("assistant: a withheld model draft shows the verdicts and reasons, never the draft", async ({ page, request }) => {
  const api = await (await request.post("/api/assistant/ask", { data: { question: QUESTION } })).json();
  const marker = Object.keys(api.markers)[1];
  // A synthetic withheld report in the shape of the API's (verifier.withheld_report), injected in the browser only.
  await page.route("**/api/assistant/ask", (route) =>
    route.fulfill({
      json: {
        ...api,
        llm: {
          provider: "fake",
          used: false,
          rejected: true,
          cards: { offered: 9, available: 9, truncated: false },
          citation_check: {
            passed: false,
            problems: ["Sentence 1 (contradicted): the value does not match the cited record."],
            sentences_checked: 1,
            verdicts: { CONTRADICTED: 1 },
            method: "deterministic structured-claim verification; no language model takes part in the decision",
            draft_withheld: true,
            sentences: [{ index: 1, verdict: "CONTRADICTED", cited: [marker], reasons: ["The value does not match the cited record."], claims: [{ verdict: "CONTRADICTED", bound_card: marker, mismatches: [{ field: "value", claimed: -30, actual: -25, kind: "value" }] }] }],
          },
        },
      },
    }),
  );
  await page.goto(`/assistant?q=${encodeURIComponent(QUESTION)}`);
  const report = page.getByTestId("model-withheld");
  await expect(report).toContainText("Model-drafted summary withheld");
  const table = page.getByTestId("withheld-sentences");
  await expect(table).toContainText("Contradicted by the record");
  await expect(table).toContainText("The value does not match the cited record.");
  await expect(table).toContainText("the draft says -30; the record says -25");
  await expect(table.getByRole("link", { name: new RegExp(`Marker ${marker}:`) }).first()).toHaveAttribute("href", `#card-${marker}`);
  await expect(page.locator('[data-kind="MODEL_SUMMARY"]')).toHaveCount(0);
  await page.unroute("**/api/assistant/ask");
});

test("assistant: a failed question is an error with a retry, not an empty answer", async ({ page }) => {
  await page.route("**/api/assistant/ask", (route) => route.fulfill({ status: 500, json: { detail: "Simulated failure" } }));
  await page.goto(`/assistant?q=${encodeURIComponent(QUESTION)}`);
  const alert = page.getByRole("alert").filter({ hasText: "The question could not be answered" });
  await expect(alert).toBeVisible();
  await expect(page.getByTestId("assistant-answer")).toHaveCount(0);
  await page.unroute("**/api/assistant/ask");
  await alert.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("assistant-answer")).toBeVisible({ timeout: 30_000 });
});
