import { type APIRequestContext, type Locator, expect, test } from "@playwright/test";
import { fmt } from "../src/lib/api";

/**
 * Scientific correctness of charts and of how results are presented.
 *
 * Expectations are computed from the API payload inside each test (ids and counts are not stable across seeds),
 * with formatting written out independently of the application code under test.
 */

// Every scenario must run without script errors or React warnings in the browser console.
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
/** A stored number exactly as the page must print it: every digit, a true minus sign, nothing rounded. */
const num = (v: number): string => String(v).replace("-", "−");
/** A number calculated by the application, at two decimals. */
const calc2 = (v: number): string => String(Number(v.toFixed(2))).replace("-", "−");
const ESTIMAND: Record<string, string> = { EFFICACY: "Efficacy estimand", TREATMENT_REGIMEN: "Treatment-regimen estimand", NOT_STATED: "Estimand not stated" };
const estimandText = (f: any): string => `${ESTIMAND[f.estimand_label]}${String(f.estimand_basis ?? "").includes("INFERRED") ? " (inferred)" : ""}`;
const weekText = (f: any): string => (f.timepoint_weeks != null ? `week ${num(f.timepoint_weeks)}` : "time point not stated");
/** Estimand · evidence tier · time point (· baseline) (· sub-measure of the endpoint): what a caption and every row under it must say. */
const basisText = (f: any): string => [estimandText(f), f.source?.tier_label, weekText(f), f.baseline_definition ? `from ${f.baseline_definition}` : null, f.class_title || null].filter(Boolean).join(" · ");
/**
 * The measure a row reports: category, endpoint title as text and responder threshold. Not the stored endpoint record:
 * one source table can be stored as several records (the control arm apart from the active arms), and its rows belong together.
 */
const measure = (f: any): string => [f.category, String(f.endpoint_title ?? "").trim().replace(/\s+/g, " ").toLowerCase(), f.threshold ?? ""].join("~");
const basisKey = (f: any): string => [measure(f), f.class_title ?? "", f.estimand_label, f.estimand_basis, f.tier, f.source?.id, f.timepoint_weeks, f.baseline_definition ?? "", f.unit].join("|");
/** A percent change, as opposed to a share of participants or any other unit. Written out here, independently of the page's code. */
const isPercentChange = (unit: string | null | undefined): boolean => /^(%|percent(age)? change)$/i.test((unit ?? "").trim());
const partition = (findings: any[]): any[][] => {
  const sets = new Map<string, any[]>();
  for (const f of findings) sets.set(basisKey(f), [...(sets.get(basisKey(f)) ?? []), f]);
  return [...sets.values()];
};
const weightSets = (study: any): any[][] =>
  // Arm-level values of the headline endpoint. Evidence rejected in review is never charted.
  // A row filed under this endpoint in another unit (a share of participants) is never plotted as weight change.
  partition(study.endpoint_groups.filter((g: any) => g.category === "WEIGHT_PCT_CHANGE").flatMap((g: any) => g.results).filter((r: any) => r.scope === "ARM" && r.value != null && !r.is_pooled && r.verification_status !== "REJECTED" && isPercentChange(r.unit)));
/**
 * The select or input whose label starts with this text. Labels are tied to their control by id (the shared form fields
 * no longer wrap the control, so a label's text is its own and not its options'), and the control is found through its
 * accessible name.
 */
const field = (scope: Locator, label: string): Locator =>
  scope.getByLabel(new RegExp(`^${label.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&")}`)).first();
const openGroup = async (group: Locator) => {
  const toggle = group.locator("> button");
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
};

/* ------------------------------------------------------------------ WEB-09 */

test("WEB-09: a reported value is printed as reported, never as 0, and whole numbers keep their zeros", async ({ request }) => {
  expect(fmt(0.0046)).toBe("0.0046");
  expect(fmt(7.973)).toBe("7.973");
  expect(fmt(0.0102)).toBe("0.0102");
  expect(fmt(-12.35)).toBe("−12.35");
  expect(fmt(80)).toBe("80");
  expect(fmt(100)).toBe("100");
  expect(fmt(20.0)).toBe("20");
  expect(fmt(0.1 + 0.2)).toBe("0.3"); // binary noise is not a reported digit
  expect(fmt(0.00000046)).toBe("0.00000046");
  expect(fmt(-0)).toBe("0");
  expect(fmt(null)).toBe("—");
  // With an explicit number of decimals (values calculated here) rounding applies, but never down to zero.
  expect(fmt(12.345, 1)).toBe("12.3");
  expect(fmt(80, 0)).toBe("80");
  expect(fmt(0, 2)).toBe("0");
  expect(fmt(0.0046, 2)).toBe("0.0046");
  expect(fmt(-0.004, 1)).toBe("−0.004");

  // Every stored result, interval limit and dispersion value survives formatting unchanged.
  const all = await json(request, "/findings?limit=2000&arm_level_only=false");
  expect(all.rows.length).toBeGreaterThan(1500);
  let checked = 0;
  for (const f of all.rows) {
    for (const v of [f.value, f.value_low, f.value_high, f.ci_low, f.ci_high, f.dispersion_value]) {
      if (v == null) continue;
      const text = fmt(v);
      expect(Number(text.replace("−", "-")), `${f.study_name} · ${f.arm_label}: ${v} shown as ${text}`).toBe(v);
      if (v !== 0) expect(text).not.toBe("0");
      checked += 1;
    }
  }
  expect(checked).toBeGreaterThan(2500);
});

test("WEB-09: the smallest reported value in the dataset appears in full on its trial page", async ({ page, request }) => {
  const all = await json(request, "/findings?limit=2000&arm_level_only=false");
  const tiny = all.rows.find((f: any) => f.value != null && f.value !== 0 && Math.abs(f.value) < 0.005);
  expect(tiny, "a stored non-zero value below 0.005").toBeTruthy();
  await page.goto(`/trials/${tiny.nct_id}`);
  const group = page.locator(`[data-testid="endpoint-group"][data-endpoint-ids~="${tiny.endpoint_id}"]`);
  await openGroup(group);
  const cell = group.locator(`[data-result-id="${tiny.result_id}"]`).getByTestId("result-value");
  await expect(cell).toContainText(num(tiny.value));
  expect((await cell.innerText()).trim().startsWith("0 ")).toBeFalsy();
  await expect(cell).toContainText(tiny.unit); // a unit with no common symbol is printed as the source wrote it
});

/* ------------------------------------------------------------------ WEB-02 */

test("WEB-02: every reported value carries its unit and every contrast says what is subtracted from what", async ({ page, request }) => {
  const study = await json(request, "/studies/NCT04184622");
  await page.goto("/trials/NCT04184622");

  // A weight endpoint in kilograms: the unit is beside the value and the standard error.
  const kg = study.endpoint_groups.find((g: any) => g.results.some((r: any) => r.value != null && /^kilograms?( \(kg\))?$|^kg$/i.test(r.unit ?? "")));
  expect(kg, "an endpoint reported in kilograms").toBeTruthy();
  const kgGroup = page.locator(`[data-endpoint-ids~="${kg.endpoint_id}"]`);
  await openGroup(kgGroup);
  for (const r of kg.results.filter((x: any) => x.value != null)) {
    const row = kgGroup.locator(`[data-result-id="${r.result_id}"]`);
    await expect(row.getByTestId("result-value")).toContainText(`${num(r.value)} kg`);
    if (r.dispersion_value != null && /standard error/i.test(r.dispersion_type)) await expect(row).toContainText(`${r.dispersion_type} ${num(r.dispersion_value)} kg`);
  }

  // Every value in every other unit of this trial: no bare numbers.
  for (const [pattern, symbol] of [[/mmHg/, "mmHg"], [/centimeters/, "cm"], [/mg\/dL/, "mg/dL"], [/^Percentage of Participants$/, "% of participants"], [/^percent change$/, "%"]] as [RegExp, string][]) {
    const g = study.endpoint_groups.find((x: any) => x.results.some((r: any) => r.value != null && pattern.test(r.unit ?? "")));
    expect(g, `an endpoint in ${symbol}`).toBeTruthy();
    const group = page.locator(`[data-endpoint-ids~="${g.endpoint_id}"]`);
    await openGroup(group);
    const r = g.results.find((x: any) => x.value != null);
    await expect(group.locator(`[data-result-id="${r.result_id}"]`).getByTestId("result-value")).toContainText(symbol.startsWith("%") ? `${num(r.value)}${symbol}` : `${num(r.value)} ${symbol}`);
  }

  // Between-arm analyses: a difference names minuend and subtrahend and has a unit; a ratio names numerator and denominator and has none.
  const differences = study.endpoint_groups.find((g: any) => g.origin === "REGISTRY" && g.analyses.some((a: any) => /difference/i.test(a.param_type) && a.unit === "percent change" && a.ci_low != null));
  const ratios = study.endpoint_groups.find((g: any) => g.analyses.some((a: any) => /odds ratio/i.test(a.param_type) && a.ci_low != null));
  expect(differences && ratios, "analyses reported as a difference and as a ratio").toBeTruthy();
  for (const g of [differences, ratios]) {
    const group = page.locator(`[data-endpoint-ids~="${g.endpoint_id}"]`);
    await openGroup(group);
    for (const a of g.analyses) {
      const row = group.locator(`[data-testid="analysis-row"][data-analysis-id="${a.id}"]`);
      // Difference or ratio exactly as the API's effect scale says.
      if (a.effect_scale.scale === "NOT_ESTABLISHED") continue; // covered by the FE-1 test below
      const ratio = a.effect_scale.scale === "RATIO";
      // The operands are written as a subtraction or division only when the API says the direction is established
      // (stated by the source or confirmed by the stored arm values); otherwise the two groups are named without one.
      const established = a.direction?.established !== false;
      await expect(row.getByTestId("contrast-direction")).toContainText(
        established ? `${ratio ? "Ratio" : "Difference"}: ${a.treatment_label} ${ratio ? "÷" : "−"} ${a.comparator_label}` : `${ratio ? "Ratio" : "Difference"}: ${a.treatment_label} and ${a.comparator_label} (which group is the reference is not established)`,
      );
      if (!established) await expect(row.getByTestId("contrast-direction")).not.toContainText(` ${ratio ? "÷" : "−"} ${a.comparator_label}`);
      // How the direction is known is printed exactly as the API states it.
      await expect(group.getByTestId("contrast-notes")).toContainText(a.direction.basis);
      const estimate = row.getByTestId("analysis-estimate");
      await expect(estimate).toContainText(ratio ? `${num(a.estimate)} (ratio, no unit)` : `${num(a.estimate)} percentage points`);
      if (a.ci_low != null) await expect(estimate).toContainText(`${num(a.ci_level)}% CI ${num(a.ci_low)} to ${num(a.ci_high)}${ratio ? "" : " percentage points"}`);
      // The estimate and its confidence level are separate words: "−10.79" followed by "95% CI" must not read "−10.795%".
      expect(await estimate.innerText()).not.toMatch(/\d95% CI/);
      expect(await estimate.evaluate((el) => el.textContent ?? "")).not.toMatch(/\d95% CI/);
    }
    // How the direction is known is stated once per table.
    await expect(group.getByTestId("contrast-notes").locator("li").first()).toBeVisible();
  }
  // FE-1: no sentence claims the registry left the direction unstated when the API says it was confirmed or stated.
  await expect(page.locator(`[data-endpoint-ids~="${differences.endpoint_id}"]`).getByTestId("contrast-notes")).not.toContainText("the registry posts the estimate without stating the direction");
});

test("WEB-02: a posted confidence level that is not a level is named as invalid, not printed as one", async ({ page, request }) => {
  const analyses = (await json(request, "/analyses?limit=1500")).rows;
  const odd = analyses.find((a: any) => a.ci_low != null && a.ci_level != null && (a.ci_level <= 0 || a.ci_level >= 100));
  if (!odd) {
    test.info().annotations.push({ type: "note", description: "No stored analysis has a confidence level outside 0–100 in this dataset." });
    return;
  }
  await page.goto(`/trials/${odd.nct_id}`);
  const group = page.locator(`[data-endpoint-ids~="${odd.endpoint_id}"]`);
  await openGroup(group);
  const estimate = group.locator(`[data-testid="analysis-row"][data-analysis-id="${odd.id}"]`).getByTestId("analysis-estimate");
  await expect(estimate).toContainText(`level posted by the source as ${num(odd.ci_level)}%, which is not a valid confidence level`);
  await expect(estimate).toContainText(`${num(odd.ci_low)} to ${num(odd.ci_high)}`); // the limits themselves are still shown as posted
  expect(await estimate.innerText()).not.toContain(`${num(odd.ci_level)}% CI`);
});

test("FE-1: an estimate the API does not establish as a difference or a ratio is written as neither", async ({ page, request }) => {
  const analyses = (await json(request, "/analyses?limit=1500")).rows;
  // For example a parameter named "Estimated treatment difference" with its unit recorded as "ratio" (bioequivalence).
  let odd = analyses.find((a: any) => a.effect_scale?.scale === "NOT_ESTABLISHED" && a.unit === "ratio" && a.estimate != null && a.comparator_label);
  if (!odd) {
    // The data repair (DR-8) gave the seed's ratios of geometric means their own parameter type, so the API now
    // establishes every stored scale. The page must still follow the API: one ratio analysis is answered, in the
    // browser only, as an estimate whose scale the API does not establish.
    const ratio = analyses.find((a: any) => a.unit === "ratio" && a.estimate != null && a.comparator_label);
    expect(ratio, "an analysis with a ratio unit to present as not established").toBeTruthy();
    odd = { ...ratio, effect_scale: { ...ratio.effect_scale, scale: "NOT_ESTABLISHED", basis: "Injected by the test: the API does not establish this estimate as a difference or a ratio." } };
    await page.route(`**/api/studies/${odd.nct_id}`, async (route) => {
      const body = await (await route.fetch()).json();
      for (const g of body.endpoint_groups) g.analyses = g.analyses.map((a: any) => (a.id === odd.id ? { ...a, effect_scale: odd.effect_scale } : a));
      await route.fulfill({ json: body });
    });
  }
  expect(odd, "an analysis whose scale the API calls not established").toBeTruthy();
  await page.goto(`/trials/${odd.nct_id}`);
  const group = page.locator(`[data-endpoint-ids~="${odd.endpoint_id}"]`);
  await openGroup(group);
  const row = group.locator(`[data-testid="analysis-row"][data-analysis-id="${odd.id}"]`);
  const direction = row.getByTestId("contrast-direction");
  await expect(direction).toContainText("Contrast (difference or ratio not established)");
  await expect(direction).toContainText(`${odd.treatment_label} vs ${odd.comparator_label}`);
  await expect(direction).not.toContainText("÷");
  await expect(direction).not.toContainText(` − ${odd.comparator_label}`);
  await expect(direction).not.toContainText("Ratio:");
  await expect(row.getByTestId("analysis-estimate")).not.toContainText("ratio, no unit");
  // The API's own reason is printed in the notes.
  await expect(group.getByTestId("contrast-notes")).toContainText(odd.effect_scale.basis);
});

test("FE-1: the direction basis is printed as the API states it — registry-stated, arm-confirmed or not established", async ({ page, request }) => {
  const study = await json(request, "/studies/NCT05616013"); // BELIEVE: the registry names its contrasts and the arm values confirm their sign
  const analyses = study.endpoint_groups.flatMap((g: any) => g.analyses.map((a: any) => ({ ...a, group: g })));
  const both = analyses.find((a: any) => a.direction_basis === "REGISTRY_GROUP_DESCRIPTION" && a.direction?.status === "VERIFIED_AGAINST_ARM_VALUES");
  expect(both, "a registry-named contrast confirmed by arm values").toBeTruthy();
  await page.goto("/trials/NCT05616013");
  const group = page.locator(`[data-endpoint-ids~="${both.endpoint_id}"]`);
  await openGroup(group);
  const row = group.locator(`[data-testid="analysis-row"][data-analysis-id="${both.id}"]`);
  await expect(row.getByTestId("contrast-direction")).toContainText("direction: registry-stated and arm-confirmed");
  const notes = group.getByTestId("contrast-notes");
  await expect(notes).toContainText(both.direction.basis);
  await expect(notes).not.toContainText("without stating the direction");

  // On the comparison page the same analysis prints the same basis, once.
  const arms = study.endpoint_groups.find((g: any) => g.endpoint_id === both.endpoint_id).results;
  const a = arms.find((r: any) => r.arm_id === both.treatment_arm_id && r.estimand_label === both.estimand_label);
  const b = arms.find((r: any) => r.arm_id === both.comparator_arm_id && r.estimand_label === both.estimand_label);
  if (a && b) {
    await page.goto(`/compare?a=${a.result_id}&b=${b.result_id}`);
    const reported = page.getByTestId("reported-analysis").filter({ hasText: both.direction.basis });
    await expect(reported.first().getByTestId("direction-basis")).toContainText("Registry-stated and arm-confirmed");
  }
});

/* ------------------------------------------------------------------ WEB-01 */

/** Asserts that the headline chart shows exactly one stored result set and says so; returns that set. */
async function expectOneCoherentSet(hero: Locator, sets: any[][]): Promise<any[]> {
  let shown: any[] = [];
  // Retried as a whole, so a caption and its rows are always read from the same render.
  await expect(async () => {
    const rowTexts = await hero.getByTestId("chart-row").allInnerTexts();
    expect(rowTexts.length).toBeGreaterThan(0);
    const caption = await hero.getByTestId("hero-caption").innerText();
    const source = await hero.getByTestId("hero-source").innerText();
    // The plotted set is identified from what the page itself claims, then checked against the stored results.
    const matches = sets.filter(
      (set) => caption.includes(basisText(set[0])) && source.includes(set[0].source.title) && set.length === rowTexts.length && set.every((f) => rowTexts.some((t) => t.includes(f.arm_label) && t.includes(`${num(f.value)}%`))),
    );
    expect(matches.length, `caption "${caption}" names a stored result set whose values are exactly the plotted rows`).toBeGreaterThanOrEqual(1);
    shown = matches[0];
    const basis = basisText(shown[0]);
    expect(caption).toContain(`${shown.length} arm-level`);
    for (const text of rowTexts) {
      expect(text, "each row names its estimand, tier and time point").toContain(basis);
      expect(text, "each row names its source").toContain(`Source: ${shown[0].source.publisher ?? shown[0].source.title}`);
      // No row belongs to a set with a different estimand, tier, time point or baseline.
      for (const other of sets) {
        const otherBasis = basisText(other[0]);
        if (!basis.includes(otherBasis)) expect(text).not.toContain(otherBasis);
      }
    }
  }).toPass({ timeout: 10_000 });
  return shown;
}

test("WEB-01: the trial chart plots one coherent result set and its caption says exactly which", async ({ page, request }) => {
  const study = await json(request, "/studies/NCT05869903"); // ATTAIN-1: four sources and two estimands for one endpoint
  const sets = weightSets(study);
  expect(sets.length).toBeGreaterThan(3);
  expect(new Set(sets.map((s) => s[0].estimand_label)).size).toBeGreaterThan(1);
  await page.goto("/trials/NCT05869903");
  const hero = page.getByTestId("hero-chart");
  const select = hero.getByLabel(/Result set/);
  await expect(select.locator("option")).toHaveCount(sets.length);

  // Default: the primary publication.
  const primary = study.publications.find((p: any) => p.relation === "PRIMARY_RESULTS");
  const first = await expectOneCoherentSet(hero, sets);
  expect(first[0].source.id).toBe(primary.source_id);
  expect(first[0].tier).toBe("PEER_REVIEWED");

  // Every other set can be chosen, and each is plotted alone under its own caption and source.
  const seen = new Set<string>();
  for (let i = 0; i < sets.length; i += 1) {
    await select.selectOption({ index: i });
    const shown = await expectOneCoherentSet(hero, sets);
    seen.add(basisKey(shown[0]));
  }
  expect(seen.size).toBe(sets.length);
});

test("WEB-01: no trial page mixes estimands, tiers, sources or time points in its headline chart", async ({ page, request }) => {
  const studies = (await json(request, "/studies")).rows; // list endpoints answer {total, limit, offset, rows}
  let mixed = 0;
  let subMeasures = 0;
  let storedSubMeasures = 0;
  for (const s of studies.filter((x: any) => x.n_results > 0)) {
    const sets = weightSets(await json(request, `/studies/${s.nct_id}`));
    if (sets.length < 2) continue;
    storedSubMeasures += sets.filter((set) => set[0].class_title).length;
    mixed += 1;
    await page.goto(`/trials/${s.nct_id}`);
    const hero = page.getByTestId("hero-chart");
    const select = hero.getByLabel(/Result set/);
    await expect(select.locator("option")).toHaveCount(sets.length);
    // Every selectable set — not only the default — is plotted alone, including the sub-measures of one endpoint
    // (for example the ≥5%, ≥10% and ≥15% responder thresholds, which must never share a caption).
    const seen = new Set<string>();
    for (let i = 0; i < sets.length; i += 1) {
      await select.selectOption({ index: i });
      const shown = await expectOneCoherentSet(hero, sets);
      seen.add(basisKey(shown[0]));
      if (shown[0].class_title) subMeasures += 1;
      expect(new Set(shown.map((f) => f.arm_label)).size, `${s.display_name}: each arm appears once in a set`).toBe(shown.length);
    }
    expect(seen.size, `${s.display_name}: every stored set can be shown`).toBe(sets.length);
  }
  expect(mixed, "trials that report percent weight change on more than one basis").toBeGreaterThanOrEqual(8);
  // Every stored sub-measure set (a percent change reported for a subgroup or period of one endpoint) was shown alone.
  expect(subMeasures, "sets that are one sub-measure of an endpoint").toBe(storedSubMeasures);
});

test("WEB-01: the benefit–risk chart gives each estimand, tier, source and time point its own panel", async ({ page, request }) => {
  for (const nct of ["NCT05929066", "NCT05616013", "NCT05869903", "NCT06603571"]) {
    const profile = await json(request, `/benefit-risk/${nct}`);
    // Only percent changes are drawn as weight change; a share of participants (a responder proportion) never is.
    const arms = profile.arms.filter((a: any) => a.efficacy.weight_pct && isPercentChange(a.efficacy.weight_pct.unit));
    const otherUnit = profile.arms.filter((a: any) => a.efficacy.weight_pct && !isPercentChange(a.efficacy.weight_pct.unit));
    const basis = (a: any) => { const e = a.efficacy.weight_pct; return [e.estimand_label, e.estimand_basis, e.tier, e.source?.id, e.timepoint_weeks, e.unit].join("|"); };
    await page.goto(`/benefit-risk?nct=${nct}`);
    await page.waitForLoadState("networkidle");
    const chart = page.getByTestId("efficacy-chart");
    await expect(chart.getByTestId("chart-row")).toHaveCount(arms.length);
    if (otherUnit.length) await expect(page.getByTestId("efficacy-other-unit")).toContainText(`${otherUnit.length} arm`);
    else await expect(page.getByTestId("efficacy-other-unit")).toHaveCount(0);
    if (!arms.length) continue;
    const facets = chart.getByTestId("chart-facet");
    const count = await facets.count();
    expect(count, `${nct}: at least one panel per reported basis`).toBeGreaterThanOrEqual(new Set(arms.map(basis)).size);
    const placed = new Set<string>();
    for (let i = 0; i < count; i += 1) {
      const facet = facets.nth(i);
      const keys = await facet.getByTestId("chart-row").evaluateAll((els) => els.map((el) => el.getAttribute("data-row-key") ?? ""));
      const members = keys.map((k) => arms.find((a: any) => a.key === k));
      expect(members.every(Boolean)).toBeTruthy();
      expect(new Set(members.map(basis)).size, `${nct} panel ${i + 1}: one estimand, tier, source, time point and unit`).toBe(1);
      const e = members[0].efficacy.weight_pct;
      const label = `${estimandText(e)} · ${e.source.tier_label} · ${weekText(e)}`;
      await expect(facet.getByTestId("facet-caption")).toContainText(label);
      for (const text of await facet.getByTestId("chart-row").allInnerTexts()) expect(text).toContain(label);
      keys.forEach((k) => placed.add(k));
    }
    expect(placed.size).toBe(arms.length);
    if (count > 1) await expect(chart.getByTestId("efficacy-mixed")).toContainText(`${count} different combinations`);
    else await expect(chart.getByTestId("efficacy-uniform")).toBeVisible();
  }
});

/* ------------------------------------------------------------------ WEB-03 */

test("WEB-03: a comparison is charted only when permitted, and each permitted kind looks different without colour", async ({ page, request }) => {
  const presets = (await json(request, "/compare/presets")).filter((p: any) => p.a !== p.b);
  const preset = (classification: string) => presets.find((p: any) => p.classification === classification);
  const signatures: Record<string, string> = {};

  const direct = preset("DIRECT RANDOMIZED COMPARISON");
  expect(direct).toBeTruthy();
  await page.goto(`/compare?a=${direct.a}&b=${direct.b}`);
  let chart = page.getByTestId("comparison-chart");
  await expect(chart).toHaveAttribute("data-kind", "direct");
  await expect(chart.getByTestId("chart-facet")).toHaveCount(1); // randomized arms of one trial share one panel
  await expect(chart.getByTestId("chart-facet").getByTestId("chart-row")).toHaveCount(2);
  await expect(chart.getByTestId("facet-divider")).toHaveCount(0);
  let bar = chart.locator('[data-testid="chart-row"] svg path').first();
  expect(await bar.getAttribute("fill")).toMatch(/^var\(--series/); // solid bar
  signatures.direct = `${await chart.getAttribute("data-panels")}|${await chart.getAttribute("data-fill")}`;

  const descriptive = preset("DESCRIPTIVE CROSS-STUDY COMPARISON");
  expect(descriptive).toBeTruthy();
  await page.goto(`/compare?a=${descriptive.a}&b=${descriptive.b}`);
  chart = page.getByTestId("comparison-chart");
  await expect(chart).toHaveAttribute("data-kind", "descriptive");
  await expect(chart.getByTestId("chart-facet")).toHaveCount(2); // different trials never share a panel
  for (const i of [0, 1]) await expect(chart.getByTestId("chart-facet").nth(i).getByTestId("chart-row")).toHaveCount(1);
  await expect(chart.getByTestId("facet-divider")).toContainText("no randomization between these arms");
  bar = chart.locator('[data-testid="chart-row"] svg path').first();
  expect(await bar.getAttribute("fill")).toBe("var(--surface)"); // unfilled bar
  expect(await bar.getAttribute("stroke")).toMatch(/^var\(--series/);
  await expect(chart.getByTestId("comparison-chart-how")).toContainText("unfilled bars");
  signatures.descriptive = `${await chart.getAttribute("data-panels")}|${await chart.getAttribute("data-fill")}`;

  const partial = preset("PARTIALLY COMPARABLE");
  if (partial) {
    await page.goto(`/compare?a=${partial.a}&b=${partial.b}`);
    chart = page.getByTestId("comparison-chart");
    await expect(chart).toHaveAttribute("data-kind", "partial");
    await expect(chart.getByTestId("chart-facet")).toHaveCount(2);
    bar = chart.locator('[data-testid="chart-row"] svg path').first();
    expect(await bar.getAttribute("fill")).toMatch(/^url\(#/); // hatched bar
    await expect(chart.locator('[data-testid="chart-row"] svg pattern').first()).toBeAttached();
    signatures.partial = `${await chart.getAttribute("data-panels")}|${await chart.getAttribute("data-fill")}`;
  } else {
    test.info().annotations.push({ type: "note", description: "No worked example classified PARTIALLY COMPARABLE in this dataset; the hatched style was not exercised." });
  }
  // Structure and texture, not colour, tell the kinds apart.
  expect(new Set(Object.values(signatures)).size).toBe(Object.keys(signatures).length);

  // Not suitable: no chart at all, and the engine's refusal is shown.
  const unsuitable = preset("NOT SUITABLE FOR EFFICACY COMPARISON");
  expect(unsuitable).toBeTruthy();
  await page.goto(`/compare?a=${unsuitable.a}&b=${unsuitable.b}`);
  await expect(page.getByTestId("classification")).toHaveText("Not suitable for efficacy comparison");
  await expect(page.getByTestId("no-comparison-chart")).toContainText("No comparison chart is drawn");
  await expect(page.getByTestId("comparison-chart")).toHaveCount(0);
  await expect(page.getByTestId("comparison-result").getByTestId("chart-row")).toHaveCount(0);
});

test("WEB-03: values in different units are never put on one axis or given each other's unit", async ({ page, request }) => {
  const percent = (await json(request, "/findings?study=NCT04184622&category=WEIGHT_PCT_CHANGE")).rows.find((f: any) => f.value != null);
  const kilograms = (await json(request, "/findings?study=NCT04184622&category=WEIGHT_ABS_CHANGE&arm_level_only=false")).rows.find((f: any) => f.value != null && /kilogram|kg/i.test(f.unit ?? ""));
  expect(percent && kilograms).toBeTruthy();
  await page.goto(`/compare?a=${percent.result_id}&b=${kilograms.result_id}`);
  await expect(page.getByTestId("comparison-result")).toBeVisible();
  await expect(page.getByTestId("comparison-chart")).toHaveCount(0);
  await expect(page.getByTestId("no-comparison-chart")).toBeVisible();
  await expect(page.getByTestId("finding-a-value")).toContainText(`${num(percent.value)}%`);
  await expect(page.getByTestId("finding-b-value")).toContainText(`${num(kilograms.value)} kg`);
  await expect(page.getByTestId("comparison-result")).not.toContainText(`${num(kilograms.value)}%`);
});

test("WEB-03 / WEB-02: a permitted difference states its direction, its operands and its unit", async ({ page, request }) => {
  const presets = await json(request, "/compare/presets");
  const direct = presets.find((p: any) => p.classification === "DIRECT RANDOMIZED COMPARISON" && p.a !== p.b);
  const result = await (await request.post("/api/compare", { data: { a: direct.a, b: direct.b } })).json();
  await page.goto(`/compare?a=${direct.a}&b=${direct.b}`);
  const difference = page.getByTestId("descriptive-difference");
  await expect(difference).toContainText(`${num(result.comparison.descriptive_difference.value)} percentage points`);
  await expect(difference).toContainText(`A minus B: ${result.a.study_name} · ${result.a.arm_label} (${num(result.a.value)}%) − ${result.b.study_name} · ${result.b.arm_label} (${num(result.b.value)}%)`);
  const analysis = page.getByTestId("reported-analysis").first();
  const reported = result.comparison.reported_analyses[0];
  await expect(analysis).toContainText(`${num(reported.estimate)} percentage points`);
  await expect(analysis.getByTestId("contrast-direction")).toContainText(`Difference: ${reported.treatment_label} − ${reported.comparator_label}`);
});

/* ------------------------------------------------------------------ WEB-04 */

test("WEB-04: chart labels are complete at desktop and phone widths", async ({ page, request }) => {
  const study = await json(request, "/studies/NCT05869903");
  const primary = study.publications.find((p: any) => p.relation === "PRIMARY_RESULTS");
  const shown = weightSets(study).find((s) => s[0].source.id === primary.source_id)!;
  expect(shown.some((f) => f.arm_label.length > 34), "a label longer than the old 34-character cut").toBeTruthy();
  for (const width of [1440, 560, 375]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/trials/NCT05869903");
    const hero = page.getByTestId("hero-chart");
    const rows = hero.getByTestId("chart-row");
    await expect(rows).toHaveCount(shown.length);
    const labels = await rows.evaluateAll((els) =>
      els.map((el) => {
        const label = el.querySelector('[data-testid="chart-row-label"]') as HTMLElement;
        const box = label.getBoundingClientRect();
        const cut = [label, ...Array.from(label.querySelectorAll<HTMLElement>("*"))].some((node) => {
          const style = getComputedStyle(node);
          return style.textOverflow === "ellipsis" || style.webkitLineClamp !== "none" || (node.clientWidth > 0 && node.scrollWidth > node.clientWidth + 1 && style.overflowX !== "visible");
        });
        return { text: label.innerText, left: box.left, right: box.right, cut, viewport: document.documentElement.clientWidth };
      }),
    );
    for (const f of shown) {
      const label = labels.find((l) => l.text.includes(f.arm_label));
      expect(label, `${width}px: full arm name "${f.arm_label}"`).toBeTruthy();
      expect(label!.text).toContain(basisText(f)); // estimand, tier and time point at every width
      expect(label!.text).toContain(`Source: ${f.source.publisher}`);
      expect(label!.cut, `${width}px: no clipped or ellipsised text`).toBeFalsy();
      expect(label!.left).toBeGreaterThanOrEqual(0);
      expect(label!.right).toBeLessThanOrEqual(label!.viewport);
    }
    expect(await hero.locator("svg text").filter({ hasText: "…" }).count()).toBe(0);
    // The chart never makes its card wider than the page.
    const card = await hero.evaluate((el) => ({ right: el.getBoundingClientRect().right, viewport: document.documentElement.clientWidth }));
    expect(card.right).toBeLessThanOrEqual(card.viewport);
  }

  // The estimand inspector uses the same chart: at phone width every row still says which estimand and source it is.
  await page.setViewportSize({ width: 375, height: 900 });
  await page.goto("/estimands?study=NCT05869903");
  const inspectorRows = page.getByTestId("chart-row");
  await expect(inspectorRows.first()).toBeVisible();
  for (const text of await inspectorRows.allInnerTexts()) {
    expect(text).toMatch(/(Efficacy|Treatment-regimen) estimand/);
    expect(text).toMatch(/week \d+/);
    expect(text).toContain("Source: ");
  }
});

/* ------------------------------------------------------------------ WEB-05 */

test("WEB-05: arm roles come from data — semaglutide arms are active comparators, never placebo — and calculated percentages say so", async ({ page, request }) => {
  const table = await json(request, "/safety/trial-ae?nct=NCT05616013&measure_kind=PARTICIPANTS_WITH_EVENT&term=Nausea");
  const arms = table.rows.filter((r: any) => r.term === "Nausea" && r.scope === "ARM");
  expect(arms.filter((r: any) => /placebo/i.test(r.arm_label)).length, "arms whose label contains the word placebo").toBeGreaterThanOrEqual(3);
  expect(arms.filter((r: any) => r.arm_role === "PLACEBO")).toHaveLength(0);

  await page.goto("/safety?drug=bimagrumab");
  const events = page.getByTestId("trial-events");
  await field(events, "Trial").selectOption("NCT05616013");
  await field(events, "Event / term").selectOption("Nausea");
  await expect(events.getByTestId("chart-row")).toHaveCount(arms.length);
  const roleName: Record<string, string> = { PLACEBO: "Placebo", ACTIVE_COMPARATOR: "Active comparator", STUDY_DRUG: "Study drug", SEQUENCE: "Treatment sequence", UNCLASSIFIED: "Arm role not classified" };
  const groupName: Record<string, string> = { PLACEBO: "control", ACTIVE_COMPARATOR: "comparator", STUDY_DRUG: "active", SEQUENCE: "sequence", UNCLASSIFIED: "other" };
  for (const r of arms) {
    const row = events.locator(`[data-testid="chart-row"][data-row-key="${r.id}"]`);
    await expect(row).toContainText(r.arm_label);
    await expect(row.getByTestId("chart-row-role")).toHaveText(roleName[r.arm_role]);
    await expect(row).toHaveAttribute("data-group", groupName[r.arm_role]);
    // Registry rows hold counts; the percentage is a calculation and is marked as one, with the counts beside it.
    expect(r.percent_reported).toBeNull();
    await expect(row).toHaveAttribute("data-calculated", "true");
    await expect(row.getByTestId("chart-row-value")).toContainText(`${num(r.percent_calculated)}% †`);
    await expect(row.getByTestId("chart-row-value")).toContainText(`calculated from ${r.n_affected} of ${r.n_at_risk} participants`);
  }
  for (const label of ["Placebo + 1.0 mg Semaglutide", "Placebo + 2.4 mg Semaglutide"]) {
    const row = events.getByTestId("chart-row").filter({ hasText: label });
    await expect(row.getByTestId("chart-row-role")).toHaveText("Active comparator");
  }
  await expect(events.getByTestId("chart-row-role").filter({ hasText: /^Placebo$/ })).toHaveCount(0);
  const legend = events.getByTestId("chart-legend");
  await expect(legend).toContainText("Active comparator");
  await expect(legend).toContainText("† Calculated here from the reported counts; not a percentage reported by the source");
  await expect(legend).not.toContainText("Placebo");
  await expect(events.getByTestId("ae-caption")).toContainText("Every percentage shown here was calculated from the reported counts");
  // A switch arm registered as placebo is explained, not coloured as placebo. Since the data repair it is stored as a
  // treatment sequence (arm role SEQUENCE): placebo in one period, the study drug in another.
  const switchArm = events.getByTestId("chart-row").filter({ hasText: "Placebo/30 mg/kg Bimagrumab" });
  await expect(switchArm).toContainText("it is neither a placebo arm nor an arm of that drug");
  await expect(switchArm).toHaveAttribute("data-group", "sequence");

  // With no pure placebo arm there is no default reference arm, and no contrast is offered until the reader picks both arms.
  const contrast = events.getByTestId("risk-contrast");
  await expect(contrast).toContainText("No arm in this table is recorded as a placebo arm");
  await expect(field(contrast, "Arm B (reference)")).toHaveValue("");
  await expect(contrast.getByRole("button", { name: "Calculate the risk contrast" })).toBeDisabled();

  // A trial with one placebo arm: it is the default reference, and the result names both operands.
  const surmount = (await json(request, "/safety/trial-ae?nct=NCT04184622&measure_kind=PARTICIPANTS_WITH_EVENT&term=Nausea")).rows.filter((r: any) => r.term === "Nausea" && r.scope === "ARM" && !r.is_serious);
  const placebo = surmount.filter((r: any) => r.arm_role === "PLACEBO");
  expect(placebo).toHaveLength(1);
  await page.goto("/safety?drug=tirzepatide");
  await field(events, "Trial").selectOption("NCT04184622");
  await field(events, "Event / term").selectOption("Nausea");
  await expect(events.getByTestId("chart-row").filter({ hasText: placebo[0].arm_label }).getByTestId("chart-row-role")).toHaveText("Placebo");
  await expect(field(contrast, "Arm B (reference)")).toHaveValue(String(placebo[0].id));
  const armA = surmount.filter((r: any) => r.arm_role === "STUDY_DRUG").sort((x: any, y: any) => (x.arm_dose_amount ?? Infinity) - (y.arm_dose_amount ?? Infinity)).pop(); // highest studied dose
  await expect(field(contrast, "Arm A")).toHaveValue(String(armA.id));
  await contrast.getByRole("button", { name: "Calculate the risk contrast" }).click();
  const outcome = events.getByTestId("risk-contrast-result");
  await expect(outcome).toContainText(`Risk difference, ${armA.arm_label} − ${placebo[0].arm_label}:`);
  await expect(outcome).toContainText("percentage points");
  await expect(outcome).toContainText(`Risk ratio, ${armA.arm_label} ÷ ${placebo[0].arm_label}:`);
  await expect(outcome).toContainText("a ratio has no unit");
});

/* ------------------------------------------------------------------ WEB-08 */

test("WEB-08: mandatory qualifiers are all visible on every result row", async ({ page, request }) => {
  const elsewhere = ["ESTIMAND_EFFICACY", "ESTIMAND_TREATMENT_REGIMEN", "CI_NOT_REPORTED", "MACHINE_EXTRACTED", "QUOTE_ANCHORED", "PENDING_REVIEW"];
  const study = await json(request, "/studies/NCT04881760"); // a Phase 2 trial: rows carry four or five qualifiers
  const groups = study.endpoint_groups.filter((g: any) => g.results.some((f: any) => f.labels.filter((l: any) => !elsewhere.includes(l.code)).length > 3)).slice(0, 3);
  expect(groups.length, "groups with rows that carry more than three qualifiers").toBeGreaterThan(0);
  await page.goto("/trials/NCT04881760");
  for (const g of groups) {
    const group = page.locator(`[data-endpoint-ids~="${g.endpoint_id}"]`);
    await openGroup(group);
    for (const f of g.results) {
      const row = group.locator(`[data-result-id="${f.result_id}"]`);
      const cell = row.getByTestId("result-qualifiers");
      for (const label of f.labels.filter((l: any) => !elsewhere.includes(l.code))) await expect(cell.getByText(label.text, { exact: true }).first(), `${f.arm_label}: ${label.text}`).toBeVisible();
      expect(await cell.innerText()).not.toMatch(/\+\d/); // nothing is collapsed behind a "+N" chip
      // The qualifiers left out of that cell are printed in their own columns of the same row.
      const codes = f.labels.map((l: any) => l.code);
      if (codes.includes("CI_NOT_REPORTED")) await expect(row).toContainText("CI not reported");
      if (codes.includes("ESTIMAND_EFFICACY")) await expect(row).toContainText("Efficacy estimand");
      if (codes.includes("ESTIMAND_TREATMENT_REGIMEN")) await expect(row).toContainText("Treatment-regimen estimand");
      if (codes.includes("MACHINE_EXTRACTED")) await expect(cell).toContainText("Machine-extracted");
      if (codes.includes("QUOTE_ANCHORED")) await expect(cell).toContainText("Quote-anchored");
    }
  }
});

/* ------------------------------------------------------------------ WEB-10 */

test("WEB-10: range disclosures and derived intervals on the workbench come from the API with their source", async ({ page, request }) => {
  // Ranges across arms: read from the adverse-event table, with the source's own sentence.
  const table = await json(request, "/safety/trial-ae?nct=NCT06603571&measure_kind=TREATMENT_DISCONTINUATION_DUE_TO_AE");
  const ranges = table.rows.filter((r: any) => r.scope !== "ARM" && r.percent_low != null);
  expect(ranges.length).toBeGreaterThan(0);
  await page.goto("/benefit-risk?nct=NCT06603571");
  const disclosure = page.getByTestId("range-disclosure");
  await expect(disclosure).toContainText(`Only a range across arms was disclosed for ${ranges.length} groups of arms`);
  for (const r of ranges) {
    await expect(disclosure).toContainText(`${r.arm_label}: ${num(r.percent_low)}% to ${num(r.percent_high)}%`);
    const band = page.getByTestId("tolerability-chart").locator(`[data-row-key="range-${r.id}"]`);
    await expect(band.getByTestId("chart-row-value")).toContainText(`${num(r.percent_low)} to ${num(r.percent_high)}%`);
    await expect(band.getByTestId("chart-row-value")).toContainText("not attributable to any single arm");
    await expect(band.getByTestId("chart-row-role")).toHaveCount(0); // a range belongs to no arm
  }
  await expect(disclosure).toContainText(ranges[0].locator_quote); // the source's wording, digits included
  await expect(disclosure).toContainText(ranges[0].source.title);

  // Derived intervals: the statistics engine's numbers, not a browser calculation.
  const profile = await json(request, "/benefit-risk/NCT04184622?estimand=EFFICACY");
  const arm = profile.arms.find((a: any) => a.efficacy.weight_pct && a.efficacy.weight_pct.ci_low == null && a.efficacy.weight_pct.se != null);
  expect(arm, "an arm whose source gives a standard error but no interval").toBeTruthy();
  const reading = await json(request, `/findings/${arm.efficacy.weight_pct.result_id}`);
  const derived = reading.interpretation.derived_interval;
  await page.goto("/benefit-risk?nct=NCT04184622");
  await page.getByLabel("Efficacy estimand").selectOption("EFFICACY");
  const row = page.getByTestId("efficacy-chart").locator(`[data-row-key="${arm.key}"]`);
  await expect(row.getByTestId("chart-row-value")).toContainText(`derived 95% interval ${calc2(derived.ci_low)} to ${calc2(derived.ci_high)}%`);
  await expect(row.locator('[data-interval="derived"]')).toBeAttached();
});

test("WEB-10: default selections come from the API, not from identifiers written into the pages", async ({ page, request }) => {
  const studies = (await json(request, "/studies")).rows; // list endpoints answer {total, limit, offset, rows}
  const drugs = (await json(request, "/drugs")).rows;
  const firstWithResults = studies.find((s: any) => s.n_results > 0);
  const firstTracked = drugs.find((d: any) => d.tier_group !== "COMPARATOR");

  await page.goto("/benefit-risk");
  await expect(page.getByLabel("Trial")).toHaveValue(firstWithResults.nct_id);
  await expect(page.getByText(`${firstWithResults.display_name}: `).first()).toBeVisible();

  await page.goto("/safety");
  await expect(page.getByLabel("Molecule")).toHaveValue(firstTracked.slug);

  await page.goto("/molecules");
  await expect(page.getByRole("heading", { level: 2, name: drugs[0].name })).toBeVisible();

  // Formulation names and the starting dose of the harmonizer are read from the documented pairs.
  const drug = await json(request, "/drugs/orforglipron");
  const capsule = drug.formulations.find((f: any) => f.key === drug.equivalences[0].a.formulation);
  const tablet = drug.formulations.find((f: any) => f.key === drug.equivalences[0].b.formulation);
  await page.goto("/molecules?drug=orforglipron");
  const header = page.getByTestId("equivalence-table").locator("thead");
  await expect(header).toContainText(capsule.dosage_form);
  await expect(header).toContainText(tablet.dosage_form);
  await expect(header).toContainText("calculated here");
  const highest = Math.max(...drug.equivalences.map((e: any) => e.a.amount));
  await expect(page.getByLabel("Dose")).toHaveValue(String(highest));

  await page.goto("/design");
  await page.getByRole("tab", { name: "Population coverage" }).click();
  await expect(page.getByLabel("Molecule")).toHaveValue(firstTracked.slug);
  const changes = (await json(request, "/changes")).rows; // list endpoints answer {total, limit, offset, rows}
  const changed = /NCT\d{8}/.exec(changes[0].source.key)![0];
  await page.getByRole("tab", { name: "Eligibility and amendments" }).click();
  await expect(page.getByLabel("Trial A")).toHaveValue(changed);
});

/* ------------------------------------------------------------------ WEB-11 */

test("WEB-11: the safety page opens on a trial and measure that have data", async ({ page, request }) => {
  const summary = await json(request, "/safety/trial-ae?drug=tirzepatide&limit=1");
  const incidence = (c: any) => c.by_measure_kind.PARTICIPANTS_WITH_EVENT?.arm_level_rows ?? 0;
  const best = summary.coverage.reduce((top: any, c: any) => (incidence(c) > incidence(top) || (incidence(c) === incidence(top) && c.rows > top.rows) ? c : top));
  expect(incidence(best)).toBeGreaterThan(0);
  expect(summary.coverage.some((c: any) => incidence(c) === 0), "a tirzepatide trial without an incidence table").toBeTruthy();
  await page.goto("/safety?drug=tirzepatide");
  const events = page.getByTestId("trial-events");
  await expect(field(events, "Trial")).toHaveValue(best.nct_id);
  await expect(field(events, "What is being counted")).toHaveValue("PARTICIPANTS_WITH_EVENT");
  await expect(field(events, "Event / term")).not.toHaveValue("");
  await expect(events.getByTestId("chart-row").first()).toBeVisible();
  await expect(events).not.toContainText("No terms of this kind");
  await expect(events).not.toContainText("Not reported for this trial.");

  // A trial that holds only a discontinuation disclosure offers that measure, not an empty incidence chart.
  const sparse = summary.coverage.find((c: any) => incidence(c) === 0);
  await field(events, "Trial").selectOption(sparse.nct_id);
  await expect(field(events, "What is being counted")).toHaveValue(Object.keys(sparse.by_measure_kind)[0]);
  await expect(field(events, "What is being counted").locator("option")).toHaveCount(Object.keys(sparse.by_measure_kind).length);
  await expect(events.getByTestId("chart-row").first()).toBeVisible();
});

test("WEB-11: the benefit–risk trial select is never blank and never shows a different trial from the page", async ({ page, request }) => {
  const studies = (await json(request, "/studies")).rows; // list endpoints answer {total, limit, offset, rows}
  const selected = (label: string) => page.getByLabel(label).locator("option:checked");
  await page.route("**/api/studies", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.continue();
  });
  await page.goto("/benefit-risk?nct=NCT04184622");
  await expect(selected("Trial")).toHaveText("Loading trials…");
  await expect(selected("Trial")).toHaveText("SURMOUNT-1");
  await page.unroute("**/api/studies");

  // A trial in the URL that is not among the trials with stored results is named in the select, not replaced by the first option.
  const empty = studies.find((s: any) => s.n_results === 0);
  expect(empty).toBeTruthy();
  await page.goto(`/benefit-risk?nct=${empty.nct_id}`);
  await expect(selected("Trial")).toHaveText(`${empty.display_name} (no stored results)`);
  await expect(page.getByText(`${empty.display_name}: `).first()).toBeVisible();
});

/* ------------------------------------------------------------------ WEB-16 */

test("WEB-16: chart rows are real controls — details open on focus, hover and click and close with Escape", async ({ page }) => {
  await page.goto("/trials/NCT04184622");
  const hero = page.getByTestId("hero-chart");
  const chart = hero.getByRole("list", { name: /Chart of \d+ rows; a table view is available/ });
  await expect(chart).toBeVisible();
  await expect(chart.getByRole("listitem")).toHaveCount(await hero.getByTestId("chart-row").count());
  // Nothing focusable is hidden inside an image role, and no row is a focusable element without a role.
  const inert = await page.evaluate(() => ({
    insideImage: Array.from(document.querySelectorAll('[role="img"]')).filter((el) => el.querySelector('button, a[href], input, select, [tabindex]:not([tabindex="-1"])')).length,
    bareTabStops: Array.from(document.querySelectorAll('[data-testid="hero-chart"] [tabindex]')).filter((el) => !["BUTTON", "A", "INPUT", "SELECT"].includes(el.tagName) && !el.getAttribute("role")).length,
  }));
  expect(inert).toEqual({ insideImage: 0, bareTabStops: 0 });

  const first = hero.getByTestId("chart-row").first().getByRole("button");
  const details = hero.getByTestId("chart-row-details");
  await expect(details).toHaveCount(0);
  await first.focus();
  await expect(first).toHaveAttribute("aria-expanded", "true");
  await expect(details).toBeVisible();
  await expect(details).toContainText(/n = \d+|n not reported/);
  await page.keyboard.press("Escape");
  await expect(details).toHaveCount(0);
  await expect(first).toBeFocused(); // dismissing does not move focus
  await expect(first).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Enter"); // the same control reopens it from the keyboard
  await expect(details).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(details).toHaveCount(0);

  // Keyboard order reaches every row.
  await first.focus();
  await page.keyboard.press("Tab");
  await expect(hero.getByTestId("chart-row").nth(1).getByRole("button")).toBeFocused();
  await expect(details).toBeVisible();
  await page.keyboard.press("Escape");

  // Pointer: the details can be hovered without vanishing, and Escape closes them without a focused row.
  await page.locator("h1").click();
  await hero.getByTestId("chart-row").nth(2).hover();
  await expect(details).toBeVisible();
  await details.hover();
  await expect(details).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(details).toHaveCount(0);

  // The table view carries the same content for every row.
  await hero.getByRole("button", { name: "View as table" }).click();
  await expect(hero.getByRole("table")).toContainText("Interval reported by the source.");
});

/* ------------------------------------------------------------------ WEB-21 */

test("WEB-21: the two sensitivity tables are separate, titled cards that fit the page", async ({ page }) => {
  await page.goto("/design");
  await page.getByRole("tab", { name: "Synthetic simulation and sensitivity" }).click();
  await page.getByRole("button", { name: "Run synthetic simulation" }).click();
  const attrition = page.getByTestId("sensitivity-attrition");
  const missing = page.getByTestId("sensitivity-missing-data");
  await expect(attrition).toContainText("Sensitivity to attrition");
  await expect(missing).toContainText("Delta adjustment for missing data");
  await expect(attrition.getByRole("table")).toHaveCount(1);
  await expect(missing.getByRole("table")).toHaveCount(1);
  await expect(attrition.getByRole("columnheader")).toHaveText(["Attrition", "Evaluable participants per arm", "Power"]);
  await expect(missing.getByRole("columnheader").last()).toHaveText("Difference (treatment − control)");
  // No row of one table lines up with a row of the other: the cards do not sit side by side.
  const a = (await attrition.boundingBox())!;
  const b = (await missing.boundingBox())!;
  expect(a.y + a.height).toBeLessThanOrEqual(b.y);
  // Neither card nor the page scrolls sideways at the suite's desktop width.
  const overflow = await page.evaluate(() => ({
    page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    tables: Array.from(document.querySelectorAll('[data-testid="sensitivity"] table')).map((t) => t.getBoundingClientRect().right - t.closest('[data-testid^="sensitivity-"]')!.getBoundingClientRect().right),
  }));
  expect(overflow.page).toBeLessThanOrEqual(0);
  for (const excess of overflow.tables) expect(excess).toBeLessThanOrEqual(1);
  // The default scenario is informative: power is neither ~0% nor ~100% in every row.
  const powers = (await attrition.getByRole("row").allInnerTexts()).slice(1).map((r) => r.trim().split(/\s+/).pop()!);
  expect(new Set(powers).size).toBeGreaterThan(1);
});

/* ------------------------------------------------------------------ WEB-22 */

test("WEB-22: boxed warnings are shown in full", async ({ page, request }) => {
  const squash = (text: string) => text.replace(/\s+/g, " ").trim();
  for (const slug of ["tirzepatide", "orforglipron"]) {
    const { labels } = await json(request, `/safety/labels/${slug}`);
    const boxed = labels.filter((l: any) => l.boxed_warning);
    expect(boxed.length).toBeGreaterThan(0);
    expect(boxed.some((l: any) => l.boxed_warning.length > 420), "a warning longer than the old 420-character cut").toBeTruthy();
    await page.goto(`/safety?drug=${slug}`);
    const boxes = page.getByTestId("boxed-warning");
    await expect(boxes).toHaveCount(boxed.length);
    for (const [i, label] of boxed.entries()) {
      const shown = squash(await boxes.nth(i).innerText());
      expect(shown).toContain(squash(label.boxed_warning)); // every character of the stored text
      expect(shown.endsWith("…")).toBeFalsy();
      // Readable, not just present: nothing is clipped inside the box.
      const clipped = await boxes.nth(i).evaluate((el) => el.scrollHeight > el.clientHeight + 1 || getComputedStyle(el).overflow !== "visible");
      expect(clipped).toBeFalsy();
    }
  }
});

/* ------------------------------------------------------------------ WEB-23 */

test("WEB-23: derived intervals differ from reported ones in shape, not colour, and the legend says so", async ({ page }) => {
  await page.goto("/trials/NCT05869903");
  const hero = page.getByTestId("hero-chart");
  const shape = (kind: string) =>
    hero.locator(`[data-interval="${kind}"]`).first().evaluate((g) => ({
      stroke: getComputedStyle(g).stroke,
      dashed: Array.from(g.querySelectorAll("line")).some((line) => (line.getAttribute("stroke-dasharray") ?? "") !== ""),
      roundCaps: g.querySelectorAll("circle").length,
      barCaps: g.querySelectorAll("line").length - 1,
    }));

  // Default set: intervals reported by the publication.
  await expect(hero.locator('[data-interval="reported"]').first()).toBeAttached();
  await expect(hero.locator('[data-interval="derived"]')).toHaveCount(0);
  const reported = await shape("reported");
  expect(reported).toMatchObject({ dashed: false, roundCaps: 0, barCaps: 2 });
  await expect(hero.getByTestId("chart-legend")).toContainText("Solid line, bar caps: interval reported by the source");
  await expect(hero.getByTestId("chart-row-value").first()).toContainText(/95% CI −[\d.]+ to −[\d.]+%/); // the confidence level is printed

  // Registry set: the source gives standard errors only, so the intervals are derived.
  const select = hero.getByLabel(/Result set/);
  const registry = await select.locator("option").filter({ hasText: "Registry-posted results" }).first().getAttribute("value");
  await select.selectOption(registry!);
  await expect(hero.locator('[data-interval="derived"]').first()).toBeAttached();
  await expect(hero.locator('[data-interval="reported"]')).toHaveCount(0);
  const derived = await shape("derived");
  expect(derived).toMatchObject({ dashed: true, roundCaps: 2, barCaps: 0 });
  expect(derived.stroke).toBe(reported.stroke); // same ink: the difference does not depend on colour
  await expect(hero.getByTestId("chart-legend")).toContainText("Dashed line, open round caps: interval derived here from the reported SE (an inference, not reported by the source)");
  await expect(hero.getByTestId("chart-row-value").first()).toContainText(/derived 95% interval/);
  // A row without any interval says so on the chart.
  await select.selectOption({ index: (await select.locator("option").allInnerTexts()).findIndex((t) => t.includes("FDA prescribing information")) });
  await expect(hero.getByTestId("chart-row-value").first()).toContainText("CI not reported");
});
