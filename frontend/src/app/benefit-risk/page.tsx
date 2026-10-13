"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import { type ChartRow, ChartFrame, ChartTable, IntervalChart } from "@/components/charts";
import { Button, Callout, ErrorNote, Hypothetical, Loading, NumberField, PageHeader, Panel, Section, Select, SourceLine } from "@/components/ui";
import { apiPost, diabetesLabel, fmt, phaseLabel, titleCase, useApi } from "@/lib/api";
import { estimandLabel } from "@/lib/rows";

const MEASURES: [string, string][] = [
  ["treatment_discontinuation_ae", "Discontinued treatment because of an adverse event"], ["study_withdrawal_ae", "Did not complete study — reason: adverse event (registry)"],
  ["nausea", "Nausea — participants with ≥1 event"], ["vomiting", "Vomiting — participants with ≥1 event"], ["diarrhoea", "Diarrhoea — participants with ≥1 event"],
  ["constipation", "Constipation — participants with ≥1 event"], ["serious_ae_any", "Any serious adverse event"],
];
const group = (a: any): ChartRow["group"] => (a.is_placebo ? "control" : ["semaglutide", "insulin-glargine"].includes(a.drug_slug) ? "comparator" : "active");

function Workbench() {
  const params = useSearchParams();
  const router = useRouter();
  const nct = params.get("nct") ?? "NCT06603571";
  const [estimand, setEstimand] = useState("");
  const [measure, setMeasure] = useState("treatment_discontinuation_ae");
  const { data: studies } = useApi<any[]>("/studies");
  const { data, error, loading } = useApi(`/benefit-risk/${nct}${estimand ? `?estimand=${estimand}` : ""}`);
  const arms: any[] = data?.arms ?? [];
  const eff: ChartRow[] = arms.filter((a) => a.efficacy.weight_pct).map((a) => {
    const e = a.efficacy.weight_pct; const lo = e.ci_low ?? (e.se != null ? e.value - 1.959964 * e.se : null); const hi = e.ci_high ?? (e.se != null ? e.value + 1.959964 * e.se : null);
    return { key: a.key, label: a.label, value: e.value, lo, hi, intervalKind: e.ci_low != null ? "reported" : e.se != null ? "derived" : undefined, group: group(a), href: `/compare?a=${e.result_id}`,
      tip: [`${estimandLabel(e.estimand_label, e.estimand_basis)} · week ${fmt(e.timepoint_weeks, 0)}`, e.n_analyzed != null ? `n = ${e.n_analyzed}` : "n not reported", e.source?.tier_label ?? e.tier] };
  });
  const tol: ChartRow[] = arms.filter((a) => a.tolerability[measure]).map((a) => {
    const t = a.tolerability[measure];
    return { key: a.key, label: a.label, value: t.percent, group: group(a), tip: [t.measure_kind_label, t.n_affected != null && t.n_at_risk ? `${t.n_affected} of ${t.n_at_risk} participants (${t.percent_basis})` : `As reported (${t.percent_basis ?? "percent"})`, t.period ?? "", `${titleCase(t.tier)}`] };
  });
  const available = MEASURES.filter(([k]) => arms.some((a) => a.tolerability[k]));
  useEffect(() => { if (available.length && !available.some(([k]) => k === measure)) setMeasure(available[0][0]); }, [available, measure]);
  const firstEff = arms.find((a) => a.efficacy.weight_pct)?.efficacy.weight_pct;
  const firstTol = arms.find((a) => a.tolerability[measure])?.tolerability[measure];

  // utility
  const [w, setW] = useState({ weight_pct: 60, tol: 40 });
  const [anchors, setAnchors] = useState({ bestW: -25, worstW: 0, bestT: 0, worstT: 30 });
  const [utility, setUtility] = useState<any>(null);
  const [uErr, setUErr] = useState<string | null>(null);
  const runUtility = () => {
    setUErr(null);
    apiPost("/benefit-risk/utility", { nct, estimand: estimand || null, criteria: { weight_pct: { weight: w.weight_pct, best: anchors.bestW, worst: anchors.worstW }, [measure]: { weight: w.tol, best: anchors.bestT, worst: anchors.worstT } } }).then(setUtility).catch((e) => { setUtility(null); setUErr(e.message); });
  };
  // dose scenario
  const doseArms = useMemo(() => arms.filter((a) => !a.is_placebo && a.efficacy.weight_pct), [arms]);
  const [shares, setShares] = useState<Record<string, number>>({});
  const [np, setNp] = useState(0);
  const [scenario, setScenario] = useState<any>(null);
  const [sErr, setSErr] = useState<string | null>(null);
  useEffect(() => { setShares(Object.fromEntries(doseArms.map((a, i) => [a.key, i === doseArms.length - 1 ? 100 : 0]))); setScenario(null); setUtility(null); setNp(0); }, [doseArms]);
  const total = Object.values(shares).reduce((s, v) => s + (Number.isFinite(v) ? v : 0), 0) + np;
  const runScenario = () => {
    setSErr(null);
    apiPost("/benefit-risk/dose-scenario", { nct, estimand: estimand || null, shares: Object.fromEntries(Object.entries(shares).filter(([, v]) => v > 0).map(([k, v]) => [k, v / 100])), non_persistent_share: np / 100 }).then(setScenario).catch((e) => { setScenario(null); setSErr(e.message); });
  };
  if (error) return <ErrorNote message={error} />;
  return (
    <>
      <PageHeader eyebrow="Benefit–risk workbench" title="Efficacy and tolerability, arm by arm" lede="Arm-level results from one trial at a time. Tolerability measures of different kinds are separate views and are never combined. Scores and scenarios are labelled as preference-dependent or hypothetical." />
      <div className="mb-5 flex flex-wrap items-end gap-3">
        <Select label="Trial" value={nct} onChange={(v) => router.replace(`/benefit-risk?nct=${v}`)} options={(studies ?? []).filter((s) => s.n_results > 0).map((s) => ({ value: s.nct_id, label: s.display_name }))} className="w-72" />
        <Select label="Efficacy estimand" value={estimand} onChange={setEstimand} options={[{ value: "", label: "Best available (stated first)" }, { value: "TREATMENT_REGIMEN", label: "Treatment-regimen" }, { value: "EFFICACY", label: "Efficacy" }]} className="w-56" />
      </div>
      {loading || !data ? <Loading rows={6} /> : (
        <>
          <Callout tone="info" title={`${data.study.display_name}: ${diabetesLabel(data.study.diabetes_status)} · ${phaseLabel(data.study.phase)}${data.timepoint_weeks ? ` · week ${data.timepoint_weeks}` : ""}`}>{data.notes[0]} {data.notes[2]}</Callout>
          <div className="mt-5 grid gap-5 xl:grid-cols-2">
            <ChartFrame title="Percent change in body weight" subtitle={firstEff ? `${estimandLabel(firstEff.estimand_label, firstEff.estimand_basis)} · ${firstEff.source?.tier_label}` : "No weight result on record"} table={<ChartTable rows={eff} unit="%" valueHeader="Change, %" />} footnote={firstEff ? <>Source: <SourceLine source={firstEff.source} /></> : undefined}>
              {eff.length ? <IntervalChart rows={eff} unit="%" axisLabel="Percent change in body weight" /> : <p className="py-6 text-[13px] text-ink-3">No arm-level percent-weight-change results are stored for this trial under the selected estimand.</p>}
            </ChartFrame>
            <div>
              <ChartFrame title={MEASURES.find(([k]) => k === measure)?.[1] ?? "Tolerability"} subtitle={firstTol ? `${firstTol.measure_kind_label}. One measure per chart — discontinuation is not adverse-event incidence.` : undefined} table={<ChartTable rows={tol} unit="%" valueHeader="% of participants" />} footnote={firstTol ? <>Source: <SourceLine source={firstTol.source} /></> : undefined}>
                <div className="mb-2"><Select label="Tolerability measure" value={measure} onChange={setMeasure} options={(available.length ? available : MEASURES).map(([k, l]) => ({ value: k, label: l }))} /></div>
                {tol.length ? <IntervalChart rows={tol} unit="%" axisLabel="Percent of participants" /> : <p className="py-6 text-[13px] text-ink-3">Not reported per arm for this trial.</p>}
              </ChartFrame>
              {nct === "NCT06603571" ? <div className="mt-3"><Callout tone="warn" title="Only a range was disclosed for the combination arms">Treatment discontinuation due to adverse events was reported as 10.8% to 27.0% across the EloraTZP arms and 0% to 10.8% across the eloralintide arms. The release does not say which arm had which value, so no bar is drawn for those arms.</Callout></div> : null}
            </div>
          </div>

          <Section id="utility" title="Exploratory utility summary" hint="A weighted sum of the efficacy value and the tolerability measure selected above. The ranking it produces is a consequence of the weights and anchors you choose.">
            <Panel>
              <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
                <NumberField label="Weight: weight change" value={w.weight_pct} onChange={(v) => setW({ ...w, weight_pct: v })} min={0} max={100} />
                <NumberField label="Weight: tolerability" value={w.tol} onChange={(v) => setW({ ...w, tol: v })} min={0} max={100} />
                <NumberField label="Best weight change" value={anchors.bestW} onChange={(v) => setAnchors({ ...anchors, bestW: v })} suffix="%" />
                <NumberField label="Worst weight change" value={anchors.worstW} onChange={(v) => setAnchors({ ...anchors, worstW: v })} suffix="%" />
                <NumberField label="Best tolerability" value={anchors.bestT} onChange={(v) => setAnchors({ ...anchors, bestT: v })} suffix="%" />
                <NumberField label="Worst tolerability" value={anchors.worstT} onChange={(v) => setAnchors({ ...anchors, worstT: v })} suffix="%" />
              </div>
              <div className="mt-3"><Button onClick={runUtility}>Compute summary</Button></div>
              {uErr ? <div className="mt-3"><ErrorNote message={uErr} /></div> : null}
              {utility ? (
                <div className="mt-4" data-testid="utility-result">
                  <Hypothetical><span className="font-semibold text-ink">{utility.label}.</span> {utility.caveats[0]} {utility.caveats[2]}</Hypothetical>
                  <table className="tbl mt-3"><thead><tr><th>Arm</th><th className="r">Score (0–1)</th><th>Components</th></tr></thead>
                    <tbody>{utility.rows.map((r: any) => (<tr key={r.key}><td className="font-medium">{r.arm}</td><td className="r num font-semibold">{r.score != null ? fmt(r.score, 3) : "—"}</td><td className="text-ink-2">{r.score != null ? r.components.map((c: any) => `${titleCase(c.criterion)} ${fmt(c.value)} → ${fmt(c.partial_value, 2)} × ${fmt(c.normalized_weight, 2)}`).join("  +  ") : r.note}</td></tr>))}</tbody>
                  </table>
                </div>
              ) : null}
            </Panel>
          </Section>

          <Section id="scenario" title="Dose-escalation scenario laboratory" hint="What the average would be if stated shares of people ended at each studied dose — assuming each behaves like the arm randomized to that dose.">
            <Panel>
              {doseArms.length ? (
                <>
                  <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                    {doseArms.map((a) => <NumberField key={a.key} label={`${a.label} (${fmt(a.efficacy.weight_pct.value)}%)`} value={shares[a.key] ?? 0} onChange={(v) => setShares({ ...shares, [a.key]: v })} min={0} max={100} suffix="%" />)}
                    <NumberField label="Not persisting (placebo-arm value assumed)" value={np} onChange={setNp} min={0} max={100} suffix="%" />
                  </div>
                  <div className="mt-3 flex flex-wrap items-center gap-3"><Button onClick={runScenario} disabled={Math.abs(total - 100) > 0.01}>Run scenario</Button><span className="num text-[12.5px] text-ink-3">Shares sum to {fmt(total, 1)}% {Math.abs(total - 100) > 0.01 ? "— must equal 100%" : ""}</span></div>
                </>
              ) : <p className="text-[13px] text-ink-3">No active-arm weight results are available for a scenario.</p>}
              {sErr ? <div className="mt-3"><ErrorNote message={sErr} /></div> : null}
              {scenario ? (
                <div className="mt-4" data-testid="scenario-result">
                  <Hypothetical>
                    <span className="font-semibold text-ink">{scenario.label}</span>
                    <p className="num mt-1 text-[20px] font-semibold text-ink">{fmt(scenario.scenario_mean)}% {scenario.scenario_interval_95 ? <span className="text-[13px] font-normal text-ink-2">(95% {fmt(scenario.scenario_interval_95[0])} to {fmt(scenario.scenario_interval_95[1])})</span> : null}</p>
                    <p className="mt-0.5 text-[12.5px] text-ink-3">{scenario.interval_note}</p>
                  </Hypothetical>
                  <div className="mt-3 grid gap-4 md:grid-cols-2 text-[13px] text-ink-2">
                    <div><p className="font-semibold text-ink">Assumptions</p><ul className="mt-1 list-disc space-y-1 pl-5">{scenario.assumptions.map((s: string) => <li key={s}>{s}</li>)}</ul><p className="mt-2 font-semibold text-ink">Not supported by aggregate data</p><ul className="mt-1 list-disc space-y-1 pl-5">{scenario.not_supported.map((s: string) => <li key={s}>{s}</li>)}</ul></div>
                    <div><p className="font-semibold text-ink">Individual participant data that would be required</p><ul className="mt-1 list-disc space-y-1 pl-5">{scenario.individual_data_required.map((s: string) => <li key={s}>{s}</li>)}</ul></div>
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
