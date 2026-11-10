"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import { type ChartRow, type Facet, ChartFrame, ChartTable, FacetedChart } from "@/components/charts";
import { Button, Callout, ErrorNote, ErrorState, Hypothetical, Loading, NumberField, PageHeader, Panel, Section, Select, SourceLine } from "@/components/ui";
import { apiPost, diabetesLabel, fmt, phaseLabel, titleCase, useApi, withQuery } from "@/lib/api";
import { useRows } from "@/lib/paging";
import { type FindingSet, armGroup, byRoleThenDose, estimandLabel, findingRow, findingSets, isWeightPercentChange, reportedText, setLabel, unitLabel, weekLabel, withUnit } from "@/lib/rows";

// Display names for the tolerability measures the workbench engine returns. A measure without an entry is named from its own data.
const MEASURE_LABEL: Record<string, string> = {
  treatment_discontinuation_ae: "Discontinued treatment because of an adverse event",
  study_withdrawal_ae: "Did not complete study — reason: adverse event (registry)",
  nausea: "Nausea — participants with ≥1 event",
  vomiting: "Vomiting — participants with ≥1 event",
  diarrhoea: "Diarrhoea — participants with ≥1 event",
  constipation: "Constipation — participants with ≥1 event",
  serious_ae_any: "Any serious adverse event",
};
const EFFICACY_CATEGORY = "WEIGHT_PCT_CHANGE"; // the engine's `weight_pct` metric

// Stable empty list: a fresh [] per render would re-trigger the effects that depend on the arm list.
const NO_ARMS: any[] = [];

function Workbench() {
  const params = useSearchParams();
  const router = useRouter();
  const { data: studies, error: studiesError, reload: reloadStudies } = useRows<any>("/studies");
  const withResults = (studies ?? []).filter((s) => s.n_results > 0);
  const nct = params.get("nct") ?? withResults[0]?.nct_id ?? null;
  const study = (studies ?? []).find((s) => s.nct_id === nct);
  const [estimand, setEstimand] = useState("");
  const [pickedMeasure, setMeasure] = useState<string | null>(null);
  // The engine reports each metric at ONE time point (the planned treatment duration unless another week is asked for)
  // and never substitutes another week. The choice belongs to one trial: changing trial returns to the planned duration.
  const [pickedWeeks, setPickedWeeks] = useState<{ nct: string | null; weeks: string }>({ nct: null, weeks: "" });
  const weeks = pickedWeeks.nct === nct ? pickedWeeks.weeks : "";
  const request = useApi(nct ? withQuery(`/benefit-risk/${nct}`, { estimand, weeks }) : null);
  // A profile for the previously selected trial, estimand or time point is never shown under the new one.
  const data = request.data && request.data.study?.nct_id === nct?.toUpperCase() && (request.data.estimand_requested ?? "") === estimand && String(request.data.timepoint_requested ?? "") === weeks ? request.data : null;
  // Full findings for the same trial: they carry the baseline definition, the endpoint title and — where the source gives only a
  // standard error — the interval derived by the statistics engine. Nothing is recalculated in the browser.
  const findingsRequest = useApi(nct ? `/findings?study=${nct}&category=${EFFICACY_CATEGORY}&limit=2000` : null);
  const findings = findingsRequest.data;
  const arms: any[] = data?.arms ?? NO_ARMS;
  const drugs = study?.drugs ?? null;

  const efficacy = useMemo(() => {
    const byId = new Map<number, any>((findings?.rows ?? []).map((f: any) => [f.result_id, f]));
    const merged = arms.filter((a) => a.efficacy.weight_pct).map((a) => {
      const e = a.efficacy.weight_pct;
      const full = byId.get(e.result_id);
      // The engine's weight metric is drawn from percent-weight-change rows; the stored row says which category it is.
      // The arm's role comes from the API (`arm_role`) where it gives one; otherwise from the placebo flag and drug roles.
      return { category: EFFICACY_CATEGORY, ...e, is_placebo: a.is_placebo, drug_slug: a.drug_slug, dose_amount: a.dose_amount, ...full, arm_role: a.arm_role ?? full?.arm_role ?? e.arm_role ?? null, arm_label: a.label, arm_key: a.key };
    });
    // A value in another unit (a share of participants reaching a threshold) is not a weight change: never plotted,
    // scored or averaged as one.
    const plotted = merged.filter((f) => isWeightPercentChange(f));
    const excluded = merged.filter((f) => !isWeightPercentChange(f));
    const sets = findingSets(plotted);
    const setOf = new Map<string, FindingSet>();
    for (const set of sets) for (const f of set.findings) setOf.set(f.arm_key, set);
    return { sets, setOf, excluded };
  }, [arms, findings]);
  const efficacyFacets: Facet[] = efficacy.sets.map((set) => {
    const rows = byRoleThenDose(set.findings.map((f: any) => { const row = { ...findingRow(f, { drugs }), key: f.arm_key }; return { row, group: row.group, dose: f.dose_amount as number | null }; }), (x) => x.dose).map((x) => x.row);
    return {
      key: set.key,
      rows,
      unit: set.unit,
      axisLabel: set.first.endpoint_title ?? "Weight change",
      scale: `${set.first.endpoint_title ?? ""}|${set.unit}`,
      caption: <><span className="font-medium text-ink">{rows.length} {rows.length === 1 ? "arm" : "arms"}: {setLabel(set)}</span>{set.first.endpoint_title ? ` · ${set.first.endpoint_title}` : ""} · values in {set.unit || "a unit the source does not record"}</>,
      footnote: <>Source: <SourceLine source={set.first.source} /></>,
    };
  });
  const efficacyRows = efficacyFacets.flatMap((f) => f.rows);
  const substituted = data?.estimand_requested ? efficacy.sets.filter((set) => set.first.estimand_label !== data.estimand_requested) : [];

  // Tolerability: one measure per chart, chosen from the measures this trial actually reports.
  const measureKeys = Array.from(new Set(arms.flatMap((a) => Object.keys(a.tolerability)))).sort((x, y) => {
    const order = Object.keys(MEASURE_LABEL);
    return (order.indexOf(x) + 1 || 99) - (order.indexOf(y) + 1 || 99) || x.localeCompare(y);
  });
  const measure = pickedMeasure && measureKeys.includes(pickedMeasure) ? pickedMeasure : (measureKeys[0] ?? null);
  const entries = measure ? arms.filter((a) => a.tolerability[measure]).map((a) => ({ arm: a, t: a.tolerability[measure] })) : [];
  const measureName = (key: string) => MEASURE_LABEL[key] ?? (() => { const t = arms.find((a) => a.tolerability[key])?.tolerability[key]; return t ? `${t.term} — ${t.measure_kind_label}` : titleCase(key); })();
  const measureKind: string | null = entries[0]?.t.measure_kind ?? null;
  const measureTerms = new Set(entries.map((x) => String(x.t.term).toLowerCase()));
  // Disclosures that give only a range across arms come from the adverse-event table, with their source and quote.
  const aeRequest = useApi(nct && measureKind ? `/safety/trial-ae?nct=${nct}&measure_kind=${measureKind}` : null);
  const aeTable = aeRequest.data;
  const ranges: any[] = (aeTable?.rows ?? []).filter((r: any) => r.nct_id === nct?.toUpperCase() && r.measure_kind === measureKind && r.scope !== "ARM" && r.percent_low != null && r.percent_high != null && measureTerms.has(String(r.term).toLowerCase()));
  const tolerabilityFacets: Facet[] = (() => {
    const panels = new Map<string, { source: any; period: string | null; kindLabel: string; term: string; rows: ChartRow[] }>();
    const panel = (source: any, period: string | null, kindLabel: string, term: string) => {
      const key = `${source?.id ?? "none"}|${period ?? ""}|${term.toLowerCase()}`;
      if (!panels.has(key)) panels.set(key, { source, period, kindLabel, term, rows: [] });
      return panels.get(key)!;
    };
    const armRows = byRoleThenDose(
      entries.map(({ arm, t }) => {
        const calculated = /calculated/i.test(t.percent_basis ?? "");
        const counts = t.n_affected != null && t.n_at_risk ? `${t.n_affected} of ${t.n_at_risk} participants` : null;
        const row: ChartRow = {
          key: arm.key,
          label: arm.label,
          value: t.percent,
          unit: "%",
          group: armGroup(arm, drugs),
          calculated,
          valueNote: t.percent == null ? undefined : calculated ? `calculated from ${counts ?? "reported counts"}` : `percentage as reported${counts ? `; ${counts}` : "; counts not given"}`,
          tip: [t.measure_kind_label, `${t.source?.tier_label ?? titleCase(t.tier)} · ${titleCase(t.verification_status)}`].filter(Boolean),
        };
        return { row, group: row.group, dose: arm.dose_amount as number | null, t };
      }),
      (x) => x.dose,
    );
    for (const x of armRows) panel(x.t.source, x.t.period ?? null, x.t.measure_kind_label, x.t.term).rows.push(x.row);
    for (const r of ranges) {
      panel(r.source, r.period ?? null, r.measure_kind_label, r.term).rows.push({
        key: `range-${r.id}`,
        label: r.arm_label,
        value: null,
        unit: "%",
        range: [r.percent_low, r.percent_high],
        group: "other",
        valueNote: "range across arms; not attributable to any single arm",
        tip: [r.measure_kind_label, `${r.source?.tier_label ?? titleCase(r.tier)} · ${titleCase(r.verification_status)}`, r.notes].filter(Boolean),
      });
    }
    return [...panels.entries()].map(([key, p]) => ({
      key,
      rows: p.rows,
      unit: "%",
      axisLabel: "Percent of participants in the arm",
      caption: <><span className="font-medium text-ink">{p.kindLabel}</span> · {p.term} · {p.source?.tier_label ?? "source tier not recorded"} · period: {p.period ?? "not stated"}</>,
      footnote: <>Source: <SourceLine source={p.source} /></>,
    }));
  })();
  const tolerabilityRows = tolerabilityFacets.flatMap((f) => f.rows);
  const calculatedCount = tolerabilityRows.filter((r) => r.calculated).length;

  // utility
  const [w, setW] = useState({ weight_pct: 60, tol: 40 });
  const [anchors, setAnchors] = useState({ bestW: -25, worstW: 0, bestT: 0, worstT: 30 });
  const [utilityResult, setUtilityResult] = useState<{ key: string; value: any } | null>(null);
  const utilityKey = `${nct}|${estimand}|${weeks}`;
  const utility = utilityResult?.key === utilityKey ? utilityResult.value : null;
  const setUtility = (value: any) => setUtilityResult(value ? { key: utilityKey, value } : null);
  const [uErr, setUErr] = useState<string | null>(null);
  // The score maps each weight value onto the anchors as a percent change; it is refused when any arm's value is in another unit.
  const utilityRefused = efficacy.excluded.length > 0;
  const runUtility = () => {
    setUErr(null);
    apiPost("/benefit-risk/utility", { nct, estimand: estimand || null, weeks: weeks ? Number(weeks) : null, criteria: { weight_pct: { weight: w.weight_pct, best: anchors.bestW, worst: anchors.worstW }, ...(measure ? { [measure]: { weight: w.tol, best: anchors.bestT, worst: anchors.worstT } } : {}) } }).then(setUtility).catch((e) => { setUtility(null); setUErr(e.message); });
  };
  const criterionUnit = (armKey: string, criterion: string) => (criterion === "weight_pct" ? unitLabel(arms.find((a) => a.key === armKey)?.efficacy.weight_pct?.unit) : "%");
  // Dose scenario: only the dose arms of ONE study drug, each reported as a percent weight change. Active comparators,
  // other drugs, combinations and arms without a stated dose are never offered as "doses".
  const doseCandidates = arms.filter((a) => a.efficacy.weight_pct && isWeightPercentChange({ category: EFFICACY_CATEGORY, unit: a.efficacy.weight_pct.unit }) && armGroup(a, drugs) === "active" && a.dose_amount != null && a.drug_slug && !String(a.drug_slug).includes("+"));
  const byDrug = new Map<string, any[]>();
  for (const a of doseCandidates) byDrug.set(a.drug_slug, [...(byDrug.get(a.drug_slug) ?? []), a]);
  // A dose-escalation scenario needs at least two doses of the drug.
  const scenarioDrugs = [...byDrug.entries()].filter(([, list]) => new Set(list.map((a) => a.dose_amount)).size > 1).sort((x, y) => y[1].length - x[1].length);
  const [pickedDrug, setPickedDrug] = useState<{ nct: string | null; drug: string } | null>(null);
  const scenarioDrug = pickedDrug?.nct === nct && scenarioDrugs.some(([slug]) => slug === pickedDrug?.drug) ? pickedDrug!.drug : (scenarioDrugs[0]?.[0] ?? null);
  const drugName = (slug: string | null) => (slug ? (drugs?.find((d: any) => d.slug === slug)?.name ?? slug) : "");
  const doseArms = [...(scenarioDrugs.find(([slug]) => slug === scenarioDrug)?.[1] ?? [])].sort((x, y) => x.dose_amount - y.dose_amount);
  const excludedFromScenario = arms.filter((a) => a.efficacy.weight_pct && !doseArms.includes(a) && armGroup(a, drugs) !== "control");
  // The inputs and results belong to one trial, drug and set of doses; any change of those starts again from the default:
  // everyone at the highest studied dose of the study drug.
  const scenarioKey = `${nct}|${estimand}|${weeks}|${doseArms.map((a) => a.key).join(",")}`;
  const [input, setInput] = useState<{ key: string; shares: Record<string, number>; np: number } | null>(null);
  const shares: Record<string, number> = input?.key === scenarioKey ? input.shares : Object.fromEntries(doseArms.map((a, i) => [a.key, i === doseArms.length - 1 ? 100 : 0]));
  const np = input?.key === scenarioKey ? input.np : 0;
  const setShares = (next: Record<string, number>) => setInput({ key: scenarioKey, shares: next, np });
  const setNp = (next: number) => setInput({ key: scenarioKey, shares, np: next });
  const [scenarioResult, setScenarioResult] = useState<{ key: string; value: any } | null>(null);
  const scenario = scenarioResult?.key === scenarioKey ? scenarioResult.value : null;
  const setScenario = (value: any) => setScenarioResult(value ? { key: scenarioKey, value } : null);
  const [sErr, setSErr] = useState<string | null>(null);
  const total = Object.values(shares).reduce((s, v) => s + (Number.isFinite(v) ? v : 0), 0) + np;
  // Participants who stop treatment can only be represented by a placebo arm reported on the same basis.
  const placeboArm = arms.find((a) => a.efficacy.weight_pct && isWeightPercentChange({ category: EFFICACY_CATEGORY, unit: a.efficacy.weight_pct.unit }) && armGroup(a, drugs) === "control");
  const placeboInSet = placeboArm && doseArms.length ? efficacy.setOf.get(placeboArm.key)?.key === efficacy.setOf.get(doseArms[0].key)?.key : false;
  // A mixture is only meaningful across arms reported under one estimand, time point, source and unit.
  const scenarioSets = new Set([...doseArms.filter((a) => (shares[a.key] ?? 0) > 0).map((a) => efficacy.setOf.get(a.key)?.key), ...(np > 0 && placeboArm ? [efficacy.setOf.get(placeboArm.key)?.key] : [])].filter(Boolean));
  const scenarioMixed = scenarioSets.size > 1;
  const scenarioUnit = scenarioSets.size === 1 ? (efficacy.sets.find((s) => s.key === [...scenarioSets][0])?.unit ?? "") : "";
  const runScenario = () => {
    setSErr(null);
    apiPost("/benefit-risk/dose-scenario", { nct, estimand: estimand || null, weeks: weeks ? Number(weeks) : null, shares: Object.fromEntries(Object.entries(shares).filter(([k, v]) => v > 0 && doseArms.some((a) => a.key === k)).map(([k, v]) => [k, v / 100])), non_persistent_share: placeboInSet ? np / 100 : 0 }).then(setScenario).catch((e) => { setScenario(null); setSErr(e.message); });
  };
  const mixedNote = efficacy.sets.length > 1 ? `The arms of this trial are reported under ${efficacy.sets.length} different combinations of estimand, time point, evidence tier, source or unit.` : null;
  const trialOptions = withResults.map((s) => ({ value: s.nct_id, label: s.display_name }));
  // What the engine used for the weight metric, and what it left out (fields of the benefit-risk response).
  const selection: any = data?.selection?.weight_pct ?? null;
  const storedWeeks: string[] = Object.keys(selection?.arms_by_week ?? {}).sort((x, y) => Number(x) - Number(y));
  const weekOptions = [
    { value: "", label: data?.timepoint_weeks && !weeks ? `Planned treatment duration (week ${fmt(data.timepoint_weeks)})` : "Planned treatment duration" },
    ...Array.from(new Set([...storedWeeks, ...(weeks ? [weeks] : [])])).sort((x, y) => Number(x) - Number(y)).map((wk) => ({ value: wk, label: `Week ${fmt(Number(wk))}${selection?.arms_by_week?.[wk] ? ` (${selection.arms_by_week[wk]} ${selection.arms_by_week[wk] === 1 ? "arm" : "arms"})` : ""}` })),
  ];
  const messages: string[] = data?.messages ?? [];
  const notAvailable: any[] = (data?.not_available ?? []).filter((x: any) => x.metric === "weight_pct");
  const otherSources = arms.some((a) => (a.efficacy.weight_pct?.alternatives ?? 0) > 0);
  return (
    <>
      <PageHeader eyebrow="Benefit–risk workbench" title="Efficacy and tolerability, arm by arm" lede="Arm-level results from one trial at a time. Tolerability measures of different kinds are separate views and are never combined. Scores and scenarios are labelled as preference-dependent or hypothetical." />
      <div className="mb-5 flex flex-wrap items-end gap-3">
        <Select
          label="Trial"
          value={nct ?? ""}
          onChange={(v) => router.replace(`/benefit-risk?nct=${v}`)}
          options={!studies ? [{ value: nct ?? "", label: "Loading trials…" }] : nct && !trialOptions.some((o) => o.value === nct) ? [{ value: nct, label: `${study?.display_name ?? nct} (no stored results)` }, ...trialOptions] : trialOptions}
          className="w-full sm:w-auto sm:min-w-72"
        />
        <Select label="Efficacy estimand" value={estimand} onChange={setEstimand} options={[{ value: "", label: "Reported for the most arms" }, { value: "TREATMENT_REGIMEN", label: "Treatment-regimen" }, { value: "EFFICACY", label: "Efficacy" }]} className="w-full sm:w-auto" />
        <Select label="Time point" value={weeks} onChange={(v) => setPickedWeeks({ nct, weeks: v })} options={weekOptions} className="w-full sm:w-auto" />
      </div>
      {studiesError ? <div className="mb-3"><ErrorNote message={`The trial list could not be loaded, so ${nct ? "arm roles are shown as not classified" : "no trial can be selected"}. ${studiesError}`} onRetry={reloadStudies} /></div> : null}
      {/* Arm roles come from the trial's recorded drug roles (the trial list), so the charts wait for it rather than show every arm as unclassified. */}
      {!nct ? (studiesError ? null : studies ? <Callout tone="muted" title="No trial has arm-level results on record.">The workbench needs a trial with posted or published results.</Callout> : <Loading rows={6} />) : request.status === "error" ? <ErrorState title="The benefit–risk profile of this trial could not be loaded" message={request.error ?? ""} onRetry={request.reload} /> : !data || (!studies && !studiesError) ? <Loading rows={6} /> : (
        <>
          <Callout tone="info" title={`${data.study.display_name}: ${diabetesLabel(data.study.diabetes_status)} · ${phaseLabel(data.study.phase)}${data.timepoint_weeks ? ` · values at week ${fmt(data.timepoint_weeks)}${weeks ? " (chosen)" : " (planned treatment duration)"}` : ""}`}>
            {data.notes[0]} {data.notes[2]}
            {/* Notes after the standard four are about this registration (for example separately randomized sub-studies). */}
            {data.notes.slice(4).map((n: string) => <span key={n} className="mt-1 block">{n}</span>)}
          </Callout>
          {selection ? (
            <p className="mt-3 text-[12.5px] text-ink-2" data-testid="benefit-risk-selection">
              <span className="font-semibold text-ink">Weight values shown:</span>{" "}
              {selection.estimand ? estimandLabel(selection.estimand) : "no estimand has a value at this time point"}
              {selection.timepoint_weeks != null ? ` · week ${fmt(selection.timepoint_weeks)}` : ""}
              {selection.baseline_label ? ` · change from ${selection.baseline_label}` : ""}. One estimand, one time point and one baseline are used for every arm; an arm without such a value is listed as not available and is never given another estimand or week.
              {selection.estimand_basis ? <span className="block text-ink-3">Estimand: {String(selection.estimand_basis).replace(/^[A-Z_]+ \(/, "").replace(/\)$/, "")}.</span> : null}
              {Object.keys(selection.rows_not_used ?? {}).length ? (
                <span className="block text-ink-3">Stored values not used here: {Object.entries(selection.rows_not_used).map(([why, n]) => `${n} at ${why}`).join("; ")}.</span>
              ) : null}
            </p>
          ) : null}
          {messages.length ? (
            <div className="mt-3" data-testid="benefit-risk-messages">
              <Callout tone="warn" title="Some stored values are not shown at this time point">
                <ul className="list-disc pl-5">{messages.map((m) => <li key={m}>{m}</li>)}</ul>
              </Callout>
            </div>
          ) : null}
          {notAvailable.length ? (
            <div className="mt-3" data-testid="benefit-risk-not-available">
              <Callout tone="muted" title={`No weight value under this estimand and time point for ${notAvailable.length} ${notAvailable.length === 1 ? "arm" : "arms"}`}>
                <ul className="list-disc pl-5">{notAvailable.map((x) => <li key={x.arm_group_key}><span className="font-medium">{x.arm}:</span> {x.reason} {x.note}</li>)}</ul>
              </Callout>
            </div>
          ) : null}
          {findingsRequest.status === "error" ? (
            <div className="mt-3"><ErrorNote message={`The stored weight results of this trial could not be loaded, so baselines, endpoint titles and derived intervals are missing from the chart. ${findingsRequest.error ?? ""}`} onRetry={findingsRequest.reload} /></div>
          ) : null}
          {efficacy.excluded.length ? (
            <div className="mt-3" data-testid="efficacy-other-unit">
              <Callout tone="warn" title={`${efficacy.excluded.length} ${efficacy.excluded.length === 1 ? "arm's weight value is" : "arms' weight values are"} not a percent change in body weight`}>
                {Array.from(new Set(efficacy.excluded.map((f) => unitLabel(f.unit) || "unit not recorded"))).join("; ")}: {efficacy.excluded.length === 1 ? "this value is" : "these values are"} a different measure (for example the share of participants reaching a weight-loss threshold). {efficacy.excluded.length === 1 ? "It is" : "They are"} not plotted as weight change, not scored and not used in a dose scenario. See the trial&apos;s page for the values as reported.
              </Callout>
            </div>
          ) : null}
          {substituted.length ? (
            <div className="mt-3" data-testid="estimand-substituted">
              <Callout tone="warn" title={`Not every arm has a value under the requested ${estimandLabel(data.estimand_requested).toLowerCase()}`}>
                {substituted.reduce((n, set) => n + set.findings.length, 0)} {substituted.reduce((n, set) => n + set.findings.length, 0) === 1 ? "arm is" : "arms are"} shown with a value reported under a different estimand ({Array.from(new Set(substituted.map((set) => estimandLabel(set.first.estimand_label, set.first.estimand_basis)))).join("; ")}). Those values answer a different question and are kept in their own panel.
              </Callout>
            </div>
          ) : null}
          <div className="mb-8 mt-5 grid grid-cols-1 gap-5 xl:grid-cols-2">
            <div className="min-w-0" data-testid="efficacy-chart">
              <ChartFrame
                level={2}
                title="Percent change in body weight, arm by arm"
                subtitle={
                  !efficacyRows.length ? "No weight result on record" : efficacy.sets.length === 1 ? (
                    <span data-testid="efficacy-uniform">All {efficacyRows.length} {efficacyRows.length === 1 ? "arm is" : "arms are"} reported under one estimand, evidence tier, time point and source: <span className="font-medium text-ink-2">{setLabel(efficacy.sets[0])}</span>.</span>
                  ) : (
                    <span data-testid="efficacy-mixed">{mixedNote} Each combination has its own panel and caption below.</span>
                  )
                }
                table={<ChartTable rows={efficacyRows} valueHeader="Change" />}
              >
                {efficacyRows.length ? <FacetedChart facets={efficacyFacets} /> : <p className="py-6 text-[13px] text-ink-3">{efficacy.excluded.length ? "None of the weight values selected for this trial is a percent change in body weight (see the note above), so nothing is plotted." : `No arm-level percent-weight-change result is shown for this trial under the selected estimand at this time point${messages.length ? ": see the note above for the weeks at which values are stored" : ""}.`}</p>}
                {otherSources && data.display_rule ? <p className="mt-2 text-[12px] text-ink-3" data-testid="benefit-risk-display-rule">More than one stored source reports some of these arms. {data.display_rule}</p> : null}
              </ChartFrame>
            </div>
            <div className="min-w-0" data-testid="tolerability-chart">
              <ChartFrame
                level={2}
                title={measure ? measureName(measure) : "Tolerability"}
                subtitle={
                  measure ? (
                    <>
                      One measure per chart — discontinuation is not adverse-event incidence.{" "}
                      {calculatedCount ? `${calculatedCount === tolerabilityRows.length ? "Every percentage" : `${calculatedCount} of ${tolerabilityRows.length} percentages`} here was calculated from reported counts (marked †).` : ""}
                    </>
                  ) : undefined
                }
                table={<ChartTable rows={tolerabilityRows} unit="%" valueHeader="% of participants" />}
                controls={measureKeys.length ? <Select label="Tolerability measure" value={measure ?? ""} onChange={setMeasure} options={measureKeys.map((k) => ({ value: k, label: measureName(k) }))} /> : undefined}
              >
                {tolerabilityRows.length ? <FacetedChart facets={tolerabilityFacets} note={`Shown as ${tolerabilityFacets.length} separate panels: the rows come from different sources or observation periods and are not comparable with one another.`} /> : <p className="py-6 text-[13px] text-ink-3">Not reported per arm for this trial.</p>}
              </ChartFrame>
              {aeRequest.status === "error" ? <div className="mt-3"><ErrorNote message={`Range disclosures from the adverse-event table could not be loaded, so any range reported across arms is missing here. ${aeRequest.error ?? ""}`} onRetry={aeRequest.reload} /></div> : null}
              {ranges.length ? (
                <div className="mt-3" data-testid="range-disclosure">
                  <Callout tone="warn" title={`Only a range across arms was disclosed for ${ranges.length} ${ranges.length === 1 ? "group" : "groups"} of arms`}>
                    <ul className="mt-1 list-disc space-y-0.5 pl-5">
                      {ranges.map((r) => (
                        <li key={r.id}>{r.arm_label}: {fmt(r.percent_low)}% to {fmt(r.percent_high)}%</li>
                      ))}
                    </ul>
                    <p className="mt-1">The source does not say which arm had which value, so the range is drawn as a band and no bar is drawn for any single arm.</p>
                    <p className="mt-1"><SourceLine source={ranges[0].source} quote={ranges[0].locator_quote} /></p>
                  </Callout>
                </div>
              ) : null}
            </div>
          </div>

          <Section id="utility" title="Exploratory utility summary" hint="A weighted sum of the efficacy value and the tolerability measure selected above. The ranking it produces is a consequence of the weights and anchors you choose.">
            <Panel>
              {mixedNote ? <div className="mb-3" data-testid="utility-mixed"><Callout tone="warn" title="Scores are not comparable across all arms of this trial">{mixedNote} See the panels of the weight-change chart above. A score built on values from different estimands or time points does not rank those arms against each other.</Callout></div> : null}
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
                <NumberField label="Weight: weight change" value={w.weight_pct} onChange={(v) => setW({ ...w, weight_pct: v })} min={0} max={100} />
                <NumberField label="Weight: tolerability" value={w.tol} onChange={(v) => setW({ ...w, tol: v })} min={0} max={100} />
                <NumberField label="Best weight change" value={anchors.bestW} onChange={(v) => setAnchors({ ...anchors, bestW: v })} suffix="%" />
                <NumberField label="Worst weight change" value={anchors.worstW} onChange={(v) => setAnchors({ ...anchors, worstW: v })} suffix="%" />
                <NumberField label="Best tolerability" value={anchors.bestT} onChange={(v) => setAnchors({ ...anchors, bestT: v })} suffix="%" />
                <NumberField label="Worst tolerability" value={anchors.worstT} onChange={(v) => setAnchors({ ...anchors, worstT: v })} suffix="%" />
              </div>
              {utilityRefused ? <div className="mt-3" data-testid="utility-refused"><Callout tone="warn" title="Not computed for this trial">The weight {efficacy.excluded.length === 1 ? "value of 1 arm is" : `values of ${efficacy.excluded.length} arms are`} not a percent change in body weight, so they cannot be placed on the weight-change anchors.</Callout></div> : null}
              <div className="mt-3"><Button onClick={runUtility} disabled={utilityRefused}>Compute summary</Button></div>
              {uErr ? <div className="mt-3"><ErrorNote message={uErr} /></div> : null}
              {utility ? (
                <div className="mt-4" data-testid="utility-result">
                  <Hypothetical><span className="font-semibold text-ink">{utility.label}.</span> {utility.caveats[0]} {utility.caveats[2]}</Hypothetical>
                  <div className="overflow-x-auto">
                    <table className="tbl mt-3"><thead><tr><th>Arm</th><th className="r">Score (0–1, no unit)</th><th>Components: reported value → partial value × weight</th></tr></thead>
                      <tbody>{utility.rows.map((r: any) => (<tr key={r.key}><td className="font-medium">{r.arm}</td><td className="r num font-semibold">{r.score != null ? fmt(r.score, 3) : "—"}</td><td className="text-ink-2">{r.score != null ? r.components.map((c: any) => `${MEASURE_LABEL[c.criterion] ?? titleCase(c.criterion)} ${withUnit(fmt(c.value), criterionUnit(r.key, c.criterion))} → ${fmt(c.partial_value, 2)} × ${fmt(c.normalized_weight, 2)}`).join("  +  ") : r.note}</td></tr>))}</tbody>
                    </table>
                  </div>
                </div>
              ) : null}
            </Panel>
          </Section>

          <Section id="scenario" title="Dose-escalation scenario laboratory" hint="What the average would be if stated shares of people ended at each studied dose — assuming each behaves like the arm randomized to that dose.">
            <Panel>
              {doseArms.length ? (
                <div data-testid="scenario-inputs">
                  <p className="text-[13px] text-ink-2">
                    Doses of the study drug <span className="font-medium text-ink">{drugName(scenarioDrug)}</span> only. {excludedFromScenario.length ? `${excludedFromScenario.length} other ${excludedFromScenario.length === 1 ? "arm is" : "arms are"} not offered: active comparators, other drugs, combinations, arms without a stated dose and values that are not a percent weight change are not doses of this drug.` : ""}
                  </p>
                  {scenarioDrugs.length > 1 ? <Select className="mt-3 w-full sm:w-auto" label="Study drug" value={scenarioDrug ?? ""} onChange={(v) => setPickedDrug({ nct, drug: v })} options={scenarioDrugs.map(([slug, list]) => ({ value: slug, label: `${drugName(slug)} (${list.length} doses)` }))} /> : null}
                  <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                    {doseArms.map((a) => {
                      const e = a.efficacy.weight_pct;
                      return <NumberField key={a.key} label={`${a.label} (${withUnit(reportedText(e.value, e.value_text), e.unit)}${efficacy.sets.length > 1 ? `; ${estimandLabel(e.estimand_label, e.estimand_basis)}, ${weekLabel(e.timepoint_weeks)}` : ""})`} value={shares[a.key] ?? 0} onChange={(v) => setShares({ ...shares, [a.key]: v })} min={0} max={100} suffix="%" />;
                    })}
                    {placeboInSet ? (
                      <NumberField label={`Stop treatment (assigned the placebo arm's value, ${withUnit(reportedText(placeboArm.efficacy.weight_pct.value, placeboArm.efficacy.weight_pct.value_text), placeboArm.efficacy.weight_pct.unit)})`} value={np} onChange={setNp} min={0} max={100} suffix="%" hint="An assumption, not an observation: people who stop are taken to change like the placebo arm." />
                    ) : null}
                  </div>
                  {!placeboInSet ? <p className="mt-2 text-[12.5px] text-ink-3" data-testid="scenario-no-placebo">{placeboArm ? "The placebo arm is reported on a different basis (estimand, time point, source or unit) from these doses" : "This trial has no placebo arm with a percent weight change on record"}, so participants who stop treatment cannot be represented: every share is assigned to a studied dose.</p> : null}
                  {scenarioMixed ? <div className="mt-3" data-testid="scenario-mixed"><Callout tone="critical" title="These shares mix arms that are not reported on the same basis">The selected arms are reported under different estimands, time points, sources or units. A weighted average across them has no interpretation, so the scenario is not run. Put the shares on arms from one panel above.</Callout></div> : null}
                  <div className="mt-3 flex flex-wrap items-center gap-3"><Button onClick={runScenario} disabled={Math.abs(total - 100) > 0.01 || scenarioMixed}>Run scenario</Button><span className="num text-[12.5px] text-ink-3">Shares sum to {fmt(total, 1)}% {Math.abs(total - 100) > 0.01 ? "— must equal 100%" : ""}</span></div>
                </div>
              ) : <p className="text-[13px] text-ink-3" data-testid="scenario-unavailable">A dose scenario needs at least two doses of one study drug, each with a percent change in body weight on record. This trial has none: active comparators, other drugs, combinations and values in other units are never treated as doses.</p>}
              {sErr ? <div className="mt-3"><ErrorNote message={sErr} /></div> : null}
              {scenario ? (
                <div className="mt-4" data-testid="scenario-result">
                  <Hypothetical>
                    <span className="font-semibold text-ink">{scenario.label}</span>
                    <p className="num mt-1 text-[20px] font-semibold text-ink">{withUnit(fmt(scenario.scenario_mean, 2), scenarioUnit)} {scenario.scenario_interval_95 ? <span className="text-[13px] font-normal text-ink-2">(95% interval {fmt(scenario.scenario_interval_95[0], 2)} to {withUnit(fmt(scenario.scenario_interval_95[1], 2), scenarioUnit)})</span> : null}</p>
                    <p className="mt-0.5 text-[12.5px] text-ink-3">{scenario.interval_note}</p>
                  </Hypothetical>
                  <div className="mt-3 grid grid-cols-1 gap-4 text-[13px] text-ink-2 md:grid-cols-2">
                    <div><h3 className="font-semibold text-ink">Assumptions</h3><ul className="mt-1 list-disc space-y-1 pl-5">{scenario.assumptions.map((s: string) => <li key={s}>{s}</li>)}</ul><h3 className="mt-2 font-semibold text-ink">Not supported by aggregate data</h3><ul className="mt-1 list-disc space-y-1 pl-5">{scenario.not_supported.map((s: string) => <li key={s}>{s}</li>)}</ul></div>
                    <div><h3 className="font-semibold text-ink">Individual participant data that would be required</h3><ul className="mt-1 list-disc space-y-1 pl-5">{scenario.individual_data_required.map((s: string) => <li key={s}>{s}</li>)}</ul></div>
                  </div>
                </div>
              ) : null}
            </Panel>
          </Section>
        </>
      )}
    </>
  );
}

export default function Page() {
  return <Suspense fallback={<Loading />}><Workbench /></Suspense>;
}
