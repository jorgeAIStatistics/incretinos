import { type APIRequestContext, expect, type Page, test } from "@playwright/test";
import { E2E_REVIEWER_TOKEN } from "../playwright.config";

/*
 * The Evidence Review workbench, end to end, against the isolated e2e database that playwright.config seeds for
 * every run. Decisions recorded here go through the real decision API with the e2e reviewer token and never reach
 * the working database. No test approves anything: the only status-changing decision is one rejection (DEMO A).
 * Records are found through the API by what they are, never by a hard-coded row id.
 */

const REVIEWER = "E2E Reviewer Person";

let consoleProblems: string[] = [];
test.beforeEach(({ page }) => {
  consoleProblems = [];
  page.on("pageerror", (error) => consoleProblems.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    // A refused decision (401, 409) is answered with a 4xx by design; the browser logs the failed request.
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) consoleProblems.push(message.text().slice(0, 300));
  });
});
test.afterEach(() => {
  expect(consoleProblems, "browser console errors").toEqual([]);
});

type Row = { evidence_type: string; id: number; evidence_key: string; label: string; study_nct: string | null; fingerprint: string };
type Detail = {
  id: number;
  evidence_key: string;
  fingerprint: string;
  label: string;
  status: { verification_status: string };
  source: { title: string } | null;
  anchoring: { quote: string | null };
  values: Record<string, unknown>;
  endpoint: { title: string; category: string } | null;
  arms: { label: string | null }[];
  study: { nct_id: string; display_name: string } | null;
  decisions: { decision: string; reviewer: string; rationale: string }[];
  audit: { action: string; actor: string }[];
};

async function pendingCurated(request: APIRequestContext, query: string): Promise<Row[]> {
  const res = await request.get(`/api/review/evidence?evidence_type=result&origin=CURATED&status=PENDING_HUMAN_REVIEW&${query}&limit=10`);
  expect(res.ok()).toBeTruthy();
  const body = (await res.json()) as { rows: Row[] };
  return body.rows;
}

async function detailOf(request: APIRequestContext, key: string): Promise<Detail> {
  const res = await request.get(`/api/review/evidence/by-key/result/${key}`);
  expect(res.ok()).toBeTruthy();
  return (await res.json()) as Detail;
}

/** Opens one record through the queue: search by its evidence key, then follow the link. */
async function openThroughQueue(page: Page, key: string, label: string) {
  await page.goto("/review");
  await page.getByLabel("Search", { exact: true }).fill(key);
  await expect(page.getByTestId("review-queue")).toContainText("1–1 of 1 records");
  await page.getByTestId("queue-record-link").filter({ hasText: label }).click();
  await expect(page.getByTestId("record-view")).toHaveAttribute("data-evidence-key", key);
}

async function enterToken(page: Page) {
  await page.getByLabel("Your name").fill(REVIEWER);
  await page.getByLabel("Reviewer token", { exact: true }).fill(E2E_REVIEWER_TOKEN);
  await page.getByRole("button", { name: "Use token" }).click();
  await expect(page.getByTestId("review-mode")).toContainText("Reviewer mode");
}

/**
 * The result ids the compare picker offers for one trial and endpoint, once it has loaded them. The findings API is
 * the oracle for "loaded": the picker must offer exactly what /api/findings returns for the same trial and endpoint.
 */
async function pickerResultIds(page: Page, request: APIRequestContext, nct: string, category: string): Promise<string[]> {
  const api = (await (await request.get(`/api/findings?study=${nct}&category=${category}&limit=2000`)).json()) as { rows: { result_id: number }[] };
  const expected = api.rows.map((r) => String(r.result_id)).sort();
  await page.goto("/compare");
  const trial = page.getByLabel("Trial", { exact: true }).first();
  await expect(trial.locator(`option[value="${nct}"]`)).toHaveCount(1);
  await trial.selectOption(nct);
  await page.getByLabel("Endpoint", { exact: true }).first().selectOption(category);
  const picker = page.getByLabel(/^Reported result/).first();
  const offered = () => picker.locator("option").evaluateAll((options) => options.map((o) => (o as HTMLOptionElement).value).filter(Boolean).sort());
  await expect.poll(offered).toEqual(expected);
  return offered();
}

test("DEMO A: a pending curated result is checked against its source and rejected; it leaves the compare picker and stays in the workbench", async ({ page, request }) => {
  const [target] = await pendingCurated(request, "q=ACHIEVE-3&endpoint_category=HBA1C_CHANGE");
  expect(target, "a pending curated ACHIEVE-3 HbA1c result in the e2e seed").toBeTruthy();
  const before = await detailOf(request, target.evidence_key);
  expect(before.status.verification_status).toBe("PENDING_HUMAN_REVIEW");
  expect(before.decisions).toHaveLength(0);
  const nct = before.study?.nct_id ?? "";
  const category = before.endpoint?.category ?? "";

  // Before the decision the compare picker offers the result.
  expect(await pickerResultIds(page, request, nct, category)).toContain(String(before.id));

  // The record, opened from the queue: its source passage and its extracted fields.
  await openThroughQueue(page, target.evidence_key, target.label);
  const source = page.getByTestId("source-panel");
  await expect(source.getByTestId("source-title")).toContainText(before.source?.title ?? "");
  if (before.anchoring.quote) await expect(source.getByTestId("quote-text")).toHaveText(before.anchoring.quote);
  await expect(source.getByTestId("anchoring")).toBeVisible();
  const evidence = page.getByTestId("evidence-panel");
  await expect(evidence.getByTestId("stored-value")).toContainText(String(before.values.value_text ?? before.values.value).replace(/^-/, "−"));
  await expect(evidence.getByTestId("endpoint")).toContainText(before.endpoint?.title ?? "");
  await expect(evidence.getByTestId("arms")).toContainText(before.arms[0]?.label ?? "");
  await expect(evidence).toContainText(before.study?.display_name ?? "");
  await expect(evidence.getByTestId("missing-information")).toBeVisible();
  await expect(page.getByTestId("record-status")).toHaveAttribute("data-status", "PENDING_HUMAN_REVIEW");
  await expect(page.getByTestId("record-status")).not.toContainText("Human-verified");

  // Record the decision with the e2e token. Attestation is required; without it nothing is sent.
  await enterToken(page);
  await page.getByTestId("review-actions").getByRole("button", { name: /^Reject/ }).click();
  const dialog = page.getByRole("dialog", { name: /Reject: record a decision/ });
  const rationale = "Checked the press release table: the HbA1c value is a topline figure without the estimand or analysis population stated.";
  await dialog.getByLabel(/Rationale/).fill(rationale);
  let posted = 0;
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().includes("/decisions")) posted += 1;
  });
  await dialog.getByRole("button", { name: "Record: Reject" }).click();
  await expect(dialog.getByText(/Confirm that you checked the original source/)).toBeVisible();
  expect(posted).toBe(0);
  await dialog.getByLabel(/I, a person, read the original source/).check();
  await dialog.getByRole("button", { name: "Record: Reject" }).click();
  await expect(dialog).toHaveCount(0);

  // Status and history update on this exact record.
  await expect(page.getByTestId("record-view")).toHaveAttribute("data-evidence-key", target.evidence_key);
  await expect(page.getByTestId("record-status")).toHaveAttribute("data-status", "REJECTED");
  await expect(page.getByTestId("excluded-note")).toBeVisible();
  const history = page.getByTestId("decision-history");
  await expect(history.getByTestId("decision-entry")).toHaveCount(1);
  await expect(history).toContainText(new RegExp(`Reject\\s+by ${REVIEWER}`));
  await expect(history).toContainText(rationale);
  await expect(page.getByTestId("audit-trail").getByTestId("audit-entry").filter({ hasText: "review.evidence_decision" })).toContainText(REVIEWER);

  const after = await detailOf(request, target.evidence_key);
  expect(after.status.verification_status).toBe("REJECTED");
  expect(after.decisions.map((d) => [d.decision, d.reviewer])).toEqual([["REJECT", REVIEWER]]);
  expect(after.fingerprint).toBe(before.fingerprint);

  // The token stayed in memory: not in the page, not in the address, not in browser storage.
  expect(await page.content()).not.toContain(E2E_REVIEWER_TOKEN);
  expect(page.url()).not.toContain(E2E_REVIEWER_TOKEN);
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }) + document.cookie);
  expect(stored).not.toContain(E2E_REVIEWER_TOKEN);

  // The rejected result is gone from the compare picker…
  expect(await pickerResultIds(page, request, nct, category)).not.toContain(String(before.id));
  // …and is still inspectable in the workbench, labelled, by its lasting key and in the rejected queue.
  await page.goto(`/review?type=result&key=${target.evidence_key}`);
  await expect(page.getByTestId("record-status")).toHaveAttribute("data-status", "REJECTED");
  await expect(page.getByTestId("record-status")).toContainText("Rejected in review");
  await page.goto(`/review?status=REJECTED&q=${target.evidence_key}`);
  await expect(page.getByTestId("queue-record-link").filter({ hasText: target.label })).toBeVisible();
});

test("without a token the workbench is in read-only public mode and the API refuses a decision", async ({ page, request }) => {
  const [row] = await pendingCurated(request, "q=ATTAIN-1");
  await page.goto(`/review?type=result&key=${row.evidence_key}`);
  await expect(page.getByTestId("review-mode")).toContainText("Read-only public mode");
  await expect(page.getByTestId("actions-disabled-reason")).toContainText("Read-only public mode");
  for (const name of [/^Approve/, /^Reject/, /^Request information/, /^Flag wrong attribution/, /^Propose a correction/]) {
    await expect(page.getByTestId("review-actions").getByRole("button", { name })).toBeDisabled();
  }
  // A shortcut does not open a decision dialog either.
  await page.locator("h1").click();
  await page.keyboard.press("a");
  await expect(page.getByRole("dialog")).toHaveCount(0);

  // The service itself refuses a decision without the token.
  const refused = await request.post(`/api/review/evidence/result/${row.id}/decisions`, {
    data: { decision: "REQUEST_INFO", reviewer: REVIEWER, rationale: "Checking that a request without the token is refused.", expected_fingerprint: row.fingerprint },
  });
  expect(refused.status()).toBe(401);
  expect((await detailOf(request, row.evidence_key)).decisions).toHaveLength(0);
});

test("a decision on a stale version is refused with 409 and the workbench explains it", async ({ page, request }) => {
  const rows = await pendingCurated(request, "q=ACHIEVE-3&endpoint_category=HBA1C_CHANGE");
  const row = rows[0];
  const stale = "0".repeat(64);

  // The API: an expected fingerprint that is not the current one is a conflict, and nothing is recorded.
  const direct = await request.post(`/api/review/evidence/result/${row.id}/decisions`, {
    headers: { "X-Reviewer-Token": E2E_REVIEWER_TOKEN },
    data: { decision: "REQUEST_INFO", reviewer: REVIEWER, rationale: "Simulated stale version: this request must be refused.", attest_human_review: false, expected_fingerprint: stale },
  });
  expect(direct.status()).toBe(409);
  expect(((await direct.json()) as { detail: { current_fingerprint: string } }).detail.current_fingerprint).toBe(row.fingerprint);

  // The workbench: the version changes between opening the record and recording (simulated by sending a stale
  // fingerprint to the real API), and the reviewer is told to reload.
  await page.goto(`/review?type=result&key=${row.evidence_key}`);
  await enterToken(page);
  await page.route("**/api/review/evidence/*/*/decisions", async (route) => {
    const body = JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>;
    await route.continue({ postData: JSON.stringify({ ...body, expected_fingerprint: stale }) });
  });
  await page.getByTestId("review-actions").getByRole("button", { name: /^Request information/ }).click();
  const dialog = page.getByRole("dialog", { name: /Request information: record a decision/ });
  await dialog.getByLabel(/Rationale/).fill("Asking for the analysis population behind this topline HbA1c value.");
  await dialog.getByRole("button", { name: "Record: Request information" }).click();
  await expect(dialog.getByTestId("decision-error")).toHaveAttribute("data-kind", "changed");
  await expect(dialog).toContainText("The evidence changed after you opened it");
  await expect(dialog).toContainText("Nothing was recorded");
  await page.unroute("**/api/review/evidence/*/*/decisions");
  await dialog.getByRole("button", { name: "Reload the current version" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByTestId("review-actions").getByRole("button", { name: /^Request information/ })).toBeEnabled();
  expect((await detailOf(request, row.evidence_key)).decisions).toHaveLength(0);
});

test("keyboard: j/k move through the queue and through records, Enter opens, ? shows help, Escape goes back", async ({ page, request }) => {
  const res = await request.get("/api/review/evidence?evidence_type=result&status=PENDING_HUMAN_REVIEW&limit=4");
  const { rows, total } = (await res.json()) as { rows: Row[]; total: number };
  expect(rows.length).toBeGreaterThanOrEqual(4);
  await page.goto("/review?etype=result&status=PENDING_HUMAN_REVIEW");
  const links = page.getByTestId("queue-record-link");
  await expect(links.first()).toHaveAttribute("aria-current", "true");
  await page.locator("h1").click();

  await page.keyboard.press("j");
  await page.keyboard.press("j");
  await expect(links.nth(2)).toHaveAttribute("aria-current", "true");
  await expect(page).toHaveURL(/pos=2/);
  await page.keyboard.press("k");
  await expect(links.nth(1)).toHaveAttribute("aria-current", "true");

  await page.keyboard.press("Enter");
  await expect(page.getByTestId("record-view")).toHaveAttribute("data-evidence-key", rows[1].evidence_key);
  await expect(page.getByTestId("record-position")).toHaveText(`Record 2 of ${total} in this queue`);
  await page.keyboard.press("j");
  await expect(page.getByTestId("record-view")).toHaveAttribute("data-evidence-key", rows[2].evidence_key);
  await expect(page.getByTestId("record-position")).toHaveText(`Record 3 of ${total} in this queue`);
  await page.keyboard.press("k");
  await expect(page.getByTestId("record-view")).toHaveAttribute("data-evidence-key", rows[1].evidence_key);

  await page.keyboard.press("?");
  const help = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  await expect(help).toBeVisible();
  await expect(help).toContainText("Next record");
  await page.keyboard.press("Escape");
  await expect(help).toHaveCount(0);
  await expect(page.getByTestId("record-view")).toBeVisible(); // Escape closed the dialog only

  await page.keyboard.press("Escape");
  await expect(page.getByTestId("review-queue")).toBeVisible();
  await expect(links.nth(1)).toHaveAttribute("aria-current", "true");
});

test("on a phone the record is shown in tabs without sideways scrolling", async ({ page, request }) => {
  const [row] = await pendingCurated(request, "q=ATTAIN-1");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/review?type=result&key=${row.evidence_key}`);
  await expect(page.getByTestId("record-status")).toBeVisible();
  const tabs = page.getByRole("tablist", { name: "Parts of this record" });
  await expect(tabs).toBeVisible();
  for (const name of ["Source", "Extracted evidence", "Checks and history"]) {
    await tabs.getByRole("tab", { name }).click();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, `horizontal overflow on the ${name} tab`).toBeLessThanOrEqual(1);
  }
  await page.goto("/review");
  await expect(page.getByTestId("queue-record-link").first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
});
