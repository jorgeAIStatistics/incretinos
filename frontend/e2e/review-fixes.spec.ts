import { type APIRequestContext, type Locator, type Page, expect, test } from "@playwright/test";

/**
 * Fixes for the second independent review of the merged Session 2 frontend (findings WEB-01 … WEB-17, SEC-08, FE-1
 * of that review; the numbering is the review's own and differs from the older WEB-xx tests in presentation.spec.ts).
 *
 * Expectations are computed from the API inside each test. Where a test makes a request fail, or adds a field to an
 * answer, it does so in the browser only (page.route); nothing is stored.
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

const json = async (request: APIRequestContext, path: string): Promise<any> => {
  const response = await request.get(`/api${path}`);
  expect(response.ok(), `GET ${path}`).toBeTruthy();
  return response.json();
};
const num = (v: number): string => String(v).replace("-", "−");
const count = (n: number) => new Intl.NumberFormat("en-US").format(n);
/** A percent change, as opposed to a share of participants or any other unit. Written out here, independently of the page's code. */
const isPercentChange = (unit: string | null | undefined): boolean => /^(%|percent(age)? change)$/i.test((unit ?? "").trim());
const overflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const openGroup = async (group: Locator) => {
  const toggle = group.locator("> button");
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
};

/* ------------------------------------------------------------------ WEB-01 / WEB-06: what a weight chart may plot */

test("WEB-01: a share of participants is never plotted, scored or averaged as weight change", async ({ page, request }) => {
  const profile = await json(request, "/benefit-risk/NCT05616013"); // BELIEVE
  const shares = profile.arms.filter((a: any) => a.efficacy.weight_pct && !isPercentChange(a.efficacy.weight_pct.unit));
  const percent = profile.arms.filter((a: any) => a.efficacy.weight_pct && isPercentChange(a.efficacy.weight_pct.unit));
  await page.goto("/benefit-risk?nct=NCT05616013");
  await page.waitForLoadState("networkidle");
  const chart = page.getByTestId("efficacy-chart");
  await expect(chart.getByTestId("chart-row")).toHaveCount(percent.length);
  for (const a of shares) await expect(chart).not.toContainText(`${num(a.efficacy.weight_pct.value)}% of participants`);
  if (shares.length) {
    await expect(page.getByTestId("efficacy-other-unit")).toContainText("not a percent change in body weight");
    await expect(page.getByTestId("utility-refused")).toBeVisible();
    await expect(page.getByRole("button", { name: "Compute summary" })).toBeDisabled();
    // No dose field is labelled with a share of participants: either there is no scenario, or none of its fields is.
    await expect(page.getByTestId("scenario-unavailable").or(page.getByTestId("scenario-inputs"))).toBeVisible();
    if (await page.getByTestId("scenario-inputs").count()) await expect(page.getByTestId("scenario-inputs")).not.toContainText("of participants");
  }

  // The trial page keeps such rows out of its weight chart and says so.
  const study = await json(request, "/studies/NCT05616013");
  const misfiled = study.endpoint_groups.filter((g: any) => g.category === "WEIGHT_PCT_CHANGE").flatMap((g: any) => g.results).filter((r: any) => r.verification_status !== "REJECTED" && !isPercentChange(r.unit));
  await page.goto("/trials/NCT05616013");
  const hero = page.getByTestId("hero-chart");
  await expect(hero.getByTestId("chart-row").first()).toBeVisible();
  for (const text of await hero.getByTestId("chart-row").allInnerTexts()) expect(text).not.toContain("of participants");
  if (misfiled.length) await expect(hero.getByTestId("hero-other-unit")).toContainText(`${misfiled.length} row`);
});

test("WEB-06: an arm role given by the API is used as given", async ({ page, request }) => {
  // The API's arm_role, injected in the browser for one placebo-flagged arm, outranks the placebo flag.
  const profile = await json(request, "/benefit-risk/NCT04184622");
  const placebo = profile.arms.find((a: any) => a.is_placebo && a.efficacy.weight_pct);
  expect(placebo).toBeTruthy();
  await page.route("**/api/benefit-risk/NCT04184622*", async (route) => {
    const body = await (await route.fetch()).json();
    body.arms = body.arms.map((a: any) => (a.key === placebo.key ? { ...a, arm_role: "UNCLASSIFIED" } : a));
    await route.fulfill({ json: body });
  });
  await page.goto("/benefit-risk?nct=NCT04184622");
  const row = page.getByTestId("efficacy-chart").locator(`[data-testid="chart-row"][data-row-key="${placebo.key}"]`);
  await expect(row).toContainText("Arm role not classified");
  await expect(row).not.toContainText("Placebo Placebo");
});

/* ------------------------------------------------------------------ WEB-02: one result set per estimand chart */

test("WEB-02: the estimand chart plots one source's result set at a time, never label doses as extra arms", async ({ page, request }) => {
  const data = await json(request, "/estimands?study=NCT05869903"); // ATTAIN-1: registry, publication, label, review and press release
  const entries = data.rows
    .filter((r: any) => r.category === "WEIGHT_PCT_CHANGE")
    .flatMap((r: any) => ["TREATMENT_REGIMEN", "EFFICACY"].flatMap((e) => (r.by_estimand[e] ?? []).map((f: any) => ({ r, e, f }))))
    .filter((x: any) => x.f.value != null && isPercentChange(x.f.unit));
  const setKey = (f: any) => [f.source?.id, f.tier, f.timepoint_weeks, f.baseline_definition ?? "", f.unit === "%" || /percent change/i.test(f.unit) ? "%" : f.unit].join("|");
  const sets = new Map<string, any[]>();
  for (const x of entries) sets.set(setKey(x.f), [...(sets.get(setKey(x.f)) ?? []), x]);
  expect(sets.size, "several sources report this trial").toBeGreaterThan(2);

  await page.goto("/estimands?study=NCT05869903");
  const chart = page.getByTestId("estimand-chart");
  await expect(chart).not.toContainText("Each arm appears once per estimand");
  const select = chart.getByLabel(/Result set/);
  await expect(select.locator("option")).toHaveCount(sets.size);
  for (let i = 0; i < sets.size; i += 1) {
    await select.selectOption({ index: i });
    const rows = chart.getByTestId("chart-row");
    const texts = await rows.allInnerTexts();
    // Exactly one stored set is drawn: every row comes from the same source, and that set's rows are all there.
    const match = [...sets.values()].find((set) => set.length === texts.length && set.every((x: any) => texts.some((t) => t.includes(`Source: ${x.f.source.publisher ?? x.f.source.title}`) && t.includes(`${num(x.f.value)}%`))));
    expect(match, `option ${i + 1} draws exactly one stored result set`).toBeTruthy();
    const sources = new Set(texts.map((t) => /Source: ([^\n]+)/.exec(t)?.[1]));
    expect(sources.size, "one source per chart").toBe(1);
    // An arm appears at most once per estimand.
    const keys = match!.map((x: any) => `${x.f.arm_group_key ?? x.r.arm}|${x.e}`);
    expect(new Set(keys).size).toBe(keys.length);
    // The caption is generated from the set: it names the source and says whether both estimands are drawn.
    const caption = chart.getByTestId("estimand-caption");
    await expect(caption).toContainText(match![0].f.source.tier_label);
    const estimands = new Set(match!.map((x: any) => x.e));
    await expect(caption).toContainText(estimands.size > 1 ? "reports both estimands" : "reports only the");
  }
});

test("WEB-02: where one source reports both estimands, each arm's two rows come from that source", async ({ page, request }) => {
  const data = await json(request, "/estimands?study=NCT05929066"); // TRIUMPH-1
  const bySource = new Map<string, Set<string>>();
  for (const r of data.rows.filter((x: any) => x.category === "WEIGHT_PCT_CHANGE")) for (const e of ["TREATMENT_REGIMEN", "EFFICACY"]) for (const f of r.by_estimand[e] ?? []) if (isPercentChange(f.unit)) bySource.set(`${f.source?.id}|${f.timepoint_weeks}`, (bySource.get(`${f.source?.id}|${f.timepoint_weeks}`) ?? new Set()).add(e));
  test.skip(![...bySource.values()].some((s) => s.size === 2), "no source in this dataset reports both estimands for TRIUMPH-1");
  await page.goto("/estimands?study=NCT05929066");
  const chart = page.getByTestId("estimand-chart");
  // The default set is one where both estimands are reported.
  await expect(chart.getByTestId("estimand-caption")).toContainText("reports both estimands");
  const texts = await chart.getByTestId("chart-row").allInnerTexts();
  expect(texts.some((t) => t.includes("Treatment-regimen estimand"))).toBeTruthy();
  expect(texts.some((t) => t.includes("Efficacy estimand"))).toBeTruthy();
  expect(new Set(texts.map((t) => /Source: ([^\n]+)/.exec(t)?.[1])).size).toBe(1);
});

/* ------------------------------------------------------------------ WEB-03: split endpoint records */

test("WEB-03: a control arm stored as its own endpoint record stays on the chart of its active arms", async ({ page, request }) => {
  const study = await json(request, "/studies/NCT05869903");
  const isLabelRow = (r: any) => r.tier === "REGULATORY_LABEL" && r.estimand_label === "TREATMENT_REGIMEN" && isPercentChange(r.unit) && r.value != null;
  const curated = (body: any) => body.endpoint_groups.filter((g: any) => g.category === "WEIGHT_PCT_CHANGE" && g.origin === "CURATED");
  if (new Set(curated(study).flatMap((g: any) => g.results).filter(isLabelRow).map((r: any) => r.endpoint_id)).size < 2) {
    // The data repair (DR-1) stores the label's table as one endpoint record. The split this test is about (placebo in
    // one record, the active arms in another that differs only in multiplicity control) is recreated in the browser
    // only, exactly as the seed held it before the repair.
    const split = (body: any) => {
      const source = curated(body).find((g: any) => g.results.some((r: any) => isLabelRow(r) && /placebo/i.test(r.arm_label)));
      const control = source.results.filter((r: any) => isLabelRow(r) && /placebo/i.test(r.arm_label));
      const copyId = 990000 + source.endpoint_id;
      source.results = source.results.filter((r: any) => !control.includes(r));
      const other = source.multiplicity_controlled === "YES" ? "NOT_STATED" : "YES";
      body.endpoint_groups.push({ ...source, endpoint_id: copyId, multiplicity_controlled: other, analyses: [], results: control.map((r: any) => ({ ...r, endpoint_id: copyId, multiplicity_controlled: other })) });
      return body;
    };
    split(study);
    await page.route("**/api/studies/NCT05869903", async (route) => route.fulfill({ json: split(await (await route.fetch()).json()) }));
  }
  const groups = curated(study);
  // The FDA label's table is stored as two records: placebo in one, the three active arms in the other.
  const label = groups.flatMap((g: any) => g.results).filter(isLabelRow);
  expect(new Set(label.map((r: any) => r.endpoint_id)).size, "the label's rows are stored as more than one endpoint record").toBeGreaterThan(1);
  await page.goto("/trials/NCT05869903");
  const hero = page.getByTestId("hero-chart");
  const select = hero.getByLabel(/Result set/);
  const option = select.locator("option", { hasText: "FDA prescribing information" });
  await expect(option).toHaveCount(1); // one set, not one per record
  await select.selectOption({ label: (await option.textContent()) ?? "" });
  await expect(hero.getByTestId("chart-row")).toHaveCount(label.length);
  await expect(hero.getByTestId("hero-caption")).toContainText(`${label.length} arm-level values`);
  for (const r of label) await expect(hero).toContainText(`${num(r.value)}%`);

  // The endpoint list shows no two sections that look the same, and says how the merged records differ.
  const headers = await page.getByTestId("endpoint-group").locator("> button").allInnerTexts();
  expect(new Set(headers).size, "no two endpoint sections have the same header").toBe(headers.length);
  await expect(page.getByTestId("endpoint-multiplicity").filter({ hasText: "differ in multiplicity control" }).first()).toBeVisible();

  // Benefit–risk: EloraTZP's press-release table is one panel, so its scores are comparable.
  await page.goto("/benefit-risk?nct=NCT06603571");
  const chart = page.getByTestId("efficacy-chart");
  await expect(chart.getByTestId("chart-row").first()).toBeVisible();
  await expect(chart.getByTestId("efficacy-uniform")).toBeVisible();
  await expect(page.getByTestId("utility-mixed")).toHaveCount(0);
});

/* ------------------------------------------------------------------ WEB-04: every safety term is reachable */

test("WEB-04: the safety term selector offers every term of the trial, can be searched, and says n of total", async ({ page, request }) => {
  const all = await json(request, "/safety/terms?nct=NCT04255433&limit=5000"); // SURPASS-CVOT: over a thousand terms
  const kind = "PARTICIPANTS_WITH_EVENT";
  const terms = Array.from(new Set<string>(all.rows.filter((t: any) => t.measure_kind === kind).map((t: any) => t.term)));
  expect(terms.length, "more terms than the API's default page").toBeGreaterThan(300);
  await page.goto("/safety?drug=tirzepatide");
  const events = page.getByTestId("trial-events");
  await events.getByLabel("Trial", { exact: true }).selectOption("NCT04255433");
  const select = events.getByLabel("Event / term", { exact: true });
  await expect(select.locator("option")).toHaveCount(terms.length);
  await expect(events.getByTestId("term-count")).toContainText(`All ${count(terms.length)} terms of this kind are listed`);

  // A rare serious event beyond the first page can be found and charted.
  const rare = terms.filter((t) => /pancreatitis/i.test(t));
  expect(rare.length).toBeGreaterThan(1);
  await events.getByLabel("Find an event term").fill("pancreatitis");
  await expect(events.getByTestId("term-count")).toContainText(`${count(rare.length)} of ${count(terms.length)} terms of this kind match`);
  const last = rare[rare.length - 1];
  await select.selectOption(last);
  await expect(events.getByRole("heading", { name: new RegExp(`^${last.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")} —`) })).toBeVisible();
});

/* ------------------------------------------------------------------ WEB-05: dose-escalation scenario */

test("WEB-05: the dose scenario offers only the study drug's doses and defaults to the highest", async ({ page, request }) => {
  const study = (await json(request, "/studies")).rows.find((s: any) => s.nct_id === "NCT06045221"); // ACHIEVE-3: orforglipron vs oral semaglutide
  const comparator = study.drugs.find((d: any) => d.role === "COMPARATOR");
  const profile = await json(request, "/benefit-risk/NCT06045221");
  const doses = profile.arms.filter((a: any) => a.drug_slug === study.drugs.find((d: any) => d.role === "INVESTIGATIONAL").slug && a.efficacy.weight_pct);
  expect(doses.length).toBeGreaterThan(1);
  await page.goto("/benefit-risk?nct=NCT06045221");
  const inputs = page.getByTestId("scenario-inputs");
  await expect(inputs.getByRole("spinbutton")).toHaveCount(doses.length); // no placebo arm here, so no "stop treatment" field
  for (const a of profile.arms.filter((x: any) => x.drug_slug === comparator.slug)) await expect(inputs).not.toContainText(a.label);
  const highest = [...doses].sort((x: any, y: any) => y.dose_amount - x.dose_amount)[0];
  await expect(inputs.getByLabel(new RegExp(`^${highest.label}`))).toHaveValue("100");
  await expect(page.getByTestId("scenario-no-placebo")).toContainText("cannot be represented");
  await page.getByRole("button", { name: "Run scenario" }).click();
  await expect(page.getByTestId("scenario-result")).toContainText("HYPOTHETICAL SCENARIO");

  // A trial with one dose of the study drug and an active comparator has no dose scenario at all.
  await page.goto("/benefit-risk?nct=NCT05822830"); // SURMOUNT-5
  await expect(page.getByTestId("scenario-unavailable")).toBeVisible();
  await expect(page.getByRole("button", { name: "Run scenario" })).toHaveCount(0);

  // With a placebo arm on the same basis, the stop-treatment share is offered and explained.
  await page.goto("/benefit-risk?nct=NCT06603571"); // EloraTZP: eloralintide doses, combinations and a tirzepatide arm
  const elora = page.getByTestId("scenario-inputs");
  await expect(elora).toContainText("Doses of the study drug Eloralintide only");
  await expect(elora).not.toContainText("tirzepatide 15 mg (");
  await expect(elora.getByLabel(/^Stop treatment \(assigned the placebo arm's value/)).toBeVisible();
});

/* ------------------------------------------------------------------ WEB-07: reflow (see also foundation.spec.ts) */

test("WEB-07: the design tabs and the safety page do not scroll sideways at 320 and 390 px once everything has loaded", async ({ page }) => {
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/safety?drug=tirzepatide", { waitUntil: "networkidle" });
    await expect(page.getByTestId("risk-contrast").first()).toBeVisible();
    expect(await overflow(page), `/safety at ${width}px`).toBeLessThanOrEqual(1);
    await page.goto("/design", { waitUntil: "networkidle" });
    for (const tab of ["Sample size and power", "Synthetic simulation and sensitivity", "Population coverage", "Eligibility and amendments"]) {
      await page.getByRole("tab", { name: tab }).click();
      await page.waitForLoadState("networkidle");
      await expect(page.locator('[role="status"][aria-label^="Loading"], .skeleton')).toHaveCount(0);
      expect(await overflow(page), `/design "${tab}" at ${width}px`).toBeLessThanOrEqual(1);
    }
  }
});

/* ------------------------------------------------------------------ WEB-08 / WEB-17: units and reported text */

test("WEB-08: every value in the estimand table carries its unit", async ({ page, request }) => {
  const data = await json(request, "/estimands?study=NCT06045221"); // ACHIEVE-3: kg, % HbA1c and % weight change
  await page.goto("/estimands?study=NCT06045221");
  const values = page.getByTestId("estimand-value");
  await expect(values.first()).toBeVisible();
  const texts = await values.allInnerTexts();
  const findings = data.rows.flatMap((r: any) => Object.values(r.by_estimand).flat()) as any[];
  expect(texts.length).toBe(findings.length);
  for (const t of texts) expect(t, "no bare number").toMatch(/(%|kg|% HbA1c|unit not recorded|[a-zA-Z])\s*$/);
  const kg = findings.find((f) => /kilogram|kg/i.test(f.unit ?? ""));
  expect(kg).toBeTruthy();
  expect(texts).toContain(`${num(kg.value)} kg`);
  const hba1c = findings.find((f) => /hba1c/i.test(f.unit ?? ""));
  expect(texts).toContain(`${num(hba1c.value)}% HbA1c`);
});

test("WEB-17: a value is printed with the source's own text when the API gives it", async ({ page, request }) => {
  const study = await json(request, "/studies/NCT04184622");
  const target = study.endpoint_groups.flatMap((g: any) => g.results).find((r: any) => r.value != null && Number.isInteger(r.value) && r.value !== 0 && isPercentChange(r.unit));
  expect(target, "a stored whole-number percent change").toBeTruthy();
  const text = `${target.value.toFixed(1)}`; // e.g. "-25.0" as a source would print it
  // value_text is added in the browser only, for one row, exactly as the API will send it.
  await page.route("**/api/studies/NCT04184622", async (route) => {
    const body = await (await route.fetch()).json();
    for (const g of body.endpoint_groups) for (const r of g.results) if (r.result_id === target.result_id) r.value_text = text;
    await route.fulfill({ json: body });
  });
  await page.goto("/trials/NCT04184622");
  const group = page.locator(`[data-endpoint-ids~="${target.endpoint_id}"]`);
  await openGroup(group);
  await expect(group.locator(`[data-result-id="${target.result_id}"]`).getByTestId("result-value")).toContainText(`${text.replace("-", "−")}%`);
});

/* ------------------------------------------------------------------ WEB-09: failed requests */

test("WEB-09: a failed request never reads as an empty result or loads for ever", async ({ page }) => {
  // Comparison picker: the result list fails.
  await page.route(/\/api\/findings\?study=/, (route) => route.fulfill({ status: 500, json: { detail: "Simulated failure" } }));
  await page.goto("/compare?a=4");
  await expect(page.getByTestId("picker-a-error")).toContainText("could not be loaded");
  await expect(page.getByText("No arm-level results for this endpoint in this trial")).toHaveCount(0);
  await page.unroute(/\/api\/findings\?study=/);

  // Worked examples fail.
  await page.route("**/api/compare/presets", (route) => route.fulfill({ status: 500, json: { detail: "Simulated failure" } }));
  await page.goto("/compare");
  await expect(page.getByRole("alert").filter({ hasText: "The worked examples could not be loaded" })).toBeVisible();
  await page.unroute("**/api/compare/presets");

  // Safety terms fail: no endless "Loading terms…".
  await page.route(/\/api\/safety\/terms/, (route) => route.fulfill({ status: 500, json: { detail: "Simulated failure" } }));
  await page.goto("/safety?drug=tirzepatide");
  await expect(page.getByRole("alert").filter({ hasText: "The event terms of this trial could not be loaded" })).toBeVisible();
  await expect(page.getByText("Loading terms…")).toHaveCount(0);
  await expect(page.getByTestId("trial-events").getByText("Not shown: the request above failed")).toBeVisible();
  await page.unroute(/\/api\/safety\/terms/);

  // Service status fails: the assistant does not assert that no model is used.
  await page.route("**/api/meta", (route) => route.fulfill({ status: 500, json: { detail: "Simulated failure" } }));
  await page.goto("/assistant");
  await expect(page.getByRole("alert").filter({ hasText: "Whether a language model is configured could not be determined" })).toBeVisible();
  await expect(page.getByText("No language model is configured")).toHaveCount(0);
  await page.unroute("**/api/meta");

  // Benefit–risk profile fails: an error with a retry, not a skeleton for ever.
  await page.route(/\/api\/benefit-risk\/NCT/, (route) => route.fulfill({ status: 500, json: { detail: "Simulated failure" } }));
  await page.goto("/benefit-risk?nct=NCT04184622");
  const alert = page.getByRole("alert").filter({ hasText: "could not be loaded" });
  await expect(alert).toBeVisible();
  await page.unroute(/\/api\/benefit-risk\/NCT/);
  await alert.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("efficacy-chart")).toBeVisible();
});

/* ------------------------------------------------------------------ WEB-11 / FE-1: compare wording */

test("WEB-11: a reversed contrast says 'not re-signed' once", async ({ page }) => {
  await page.goto("/compare?a=1&b=4");
  const orientation = page.getByTestId("pair-orientation").first();
  await expect(orientation).toBeVisible();
  const text = await orientation.innerText();
  expect(text.match(/not re-signed/g)?.length ?? 0).toBe(1);
  await expect(orientation).toContainText("Opposite orientation to A − B"); // said in words, not by colour alone
  await expect(page.getByTestId("direction-basis").first()).toContainText(/Registry-stated|Arm-confirmed|Not established|Stated by the curated source/);
});

/* ------------------------------------------------------------------ WEB-12: panel titles are headings */

test("WEB-12: panel titles are headings on compare, assistant, benefit–risk and safety", async ({ page }) => {
  await page.goto("/compare?a=1&b=4");
  await expect(page.getByTestId("comparison-result")).toBeVisible();
  for (const name of ["Finding A", "Finding B", "Methodological comparison", "What may be said", "What may not be said"]) await expect(page.getByRole("heading", { name, level: 2 }).first()).toBeVisible();
  for (const name of ["Reported between-arm analysis", "Descriptive difference (A − B)"]) await expect(page.getByRole("heading", { name, level: 3 })).toBeVisible();

  await page.goto("/benefit-risk?nct=NCT04184622");
  await expect(page.getByRole("heading", { name: "Percent change in body weight, arm by arm", level: 2 })).toBeVisible();

  await page.goto("/safety?drug=tirzepatide");
  await expect(page.getByRole("heading", { name: "Disproportionality screen", level: 3 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Risk contrast between two arms of this trial", level: 3 }).first()).toBeVisible();

  await page.goto("/assistant");
  await page.getByRole("button", { name: "What evidence differentiates retatrutide from tirzepatide?" }).click();
  await expect(page.getByRole("heading", { name: "Answer", level: 2 })).toBeVisible();
  await expect(page.getByRole("heading", { name: /Sources cited/, level: 2 })).toBeVisible();
  expect(await page.getByTestId("assistant-answer").getByRole("heading", { level: 3 }).count()).toBeGreaterThan(1);
});

/* ------------------------------------------------------------------ WEB-13: focus rings */

test("WEB-13: a focused tab's ring is not clipped by the tab list's scroll box", async ({ page }) => {
  for (const path of ["/sources", "/design"]) {
    await page.goto(path);
    await page.getByRole("tab").first().focus();
    await page.keyboard.press("End");
    await page.keyboard.press("Home"); // keyboard focus on the first tab, at the left edge of the scroll box
    await expect(page.getByRole("tab").first()).toBeFocused();
    const { ring, box, width } = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement;
      const style = getComputedStyle(el);
      const reach = Number.parseFloat(style.outlineWidth) + Number.parseFloat(style.outlineOffset);
      const r = el.getBoundingClientRect();
      const scroller = el.closest('[data-testid="tab-scroll"]') as HTMLElement;
      const b = scroller.getBoundingClientRect();
      return { width: Number.parseFloat(style.outlineWidth), ring: { top: r.top - reach, left: r.left - reach, right: r.right + reach, bottom: r.bottom + reach }, box: { top: b.top, left: b.left, right: b.right, bottom: b.bottom } };
    });
    expect(width, `${path}: the focus ring is drawn`).toBeGreaterThanOrEqual(2);
    expect(ring.top, `${path}: ring top`).toBeGreaterThanOrEqual(box.top - 0.5);
    expect(ring.left, `${path}: ring left`).toBeGreaterThanOrEqual(box.left - 0.5);
    expect(ring.bottom, `${path}: ring bottom`).toBeLessThanOrEqual(box.bottom + 0.5);
  }
});

/* ------------------------------------------------------------------ WEB-16: selects show their value */

test("WEB-16: selects are wide enough to show their selected value", async ({ page }) => {
  const clipped = () =>
    page.evaluate(() => {
      const canvas = document.createElement("canvas").getContext("2d")!;
      return Array.from(document.querySelectorAll("main select")).flatMap((el) => {
        const select = el as HTMLSelectElement;
        if (!select.offsetParent) return [];
        const style = getComputedStyle(select);
        canvas.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
        const text = select.options[select.selectedIndex]?.text ?? "";
        const room = select.clientWidth - Number.parseFloat(style.paddingLeft) - Number.parseFloat(style.paddingRight);
        return canvas.measureText(text).width > room + 1 ? [`${select.labels?.[0]?.textContent ?? select.name}: "${text}"`] : [];
      });
    });
  for (const width of [1440, 834]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/benefit-risk?nct=NCT04184622", { waitUntil: "networkidle" });
    await page.getByLabel("Efficacy estimand").selectOption("");
    expect(await clipped(), `/benefit-risk at ${width}px`).toEqual([]);
    await page.goto("/safety?drug=tirzepatide", { waitUntil: "networkidle" });
    await expect(page.getByTestId("term-count")).toBeVisible();
    expect(await clipped(), `/safety at ${width}px`).toEqual([]);
  }
});

/* ------------------------------------------------------------------ SEC-08: the reviewer token */

test("SEC-08: the reviewer token never reaches the DOM and is not offered to password managers", async ({ page }) => {
  await page.goto("/sources?tab=changes");
  // Wait until the service has said whether it accepts decisions.
  await expect(page.getByText(/Recording a decision requires the reviewer token|This deployment is read-only/)).toBeVisible();
  const field = page.getByLabel("Reviewer token");
  test.skip((await field.count()) === 0, "this deployment accepts no review decisions");
  const secret = "probe-token-not-a-real-one";
  await field.fill(secret);
  await expect(field).toHaveValue(secret); // the field holds it (as a property) …
  expect(await field.getAttribute("value")).toBeNull(); // … but never as an attribute
  expect(await page.locator(`[value*="${secret}"]`).count()).toBe(0);
  expect(await page.content()).not.toContain(secret);
  await expect(field).toHaveAttribute("type", "password");
  await expect(field).toHaveAttribute("autocomplete", "off");
  await expect(field).toHaveAttribute("data-1p-ignore", "");
  await expect(field).toHaveAttribute("data-lpignore", "true");
  expect(await field.evaluate((el) => (el as HTMLInputElement).form)).toBeNull(); // not part of a form with the name field
  expect(await field.getAttribute("name")).toBeNull();
  // Kept in memory: after switching tabs and back the field still holds it, still without an attribute.
  await page.getByRole("tab", { name: "Sources", exact: true }).click();
  await page.getByRole("tab", { name: "Changes and review queue" }).click();
  await expect(page.getByLabel("Reviewer token")).toHaveValue(secret);
  expect(await page.getByLabel("Reviewer token").getAttribute("value")).toBeNull();
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }) + document.cookie)).not.toContain(secret);
});
