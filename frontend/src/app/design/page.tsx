"use client";

import * as Tabs from "@radix-ui/react-tabs";
import { Check } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Badge, Button, Callout, ErrorNote, Hypothetical, KV, Loading, NumberField, PageHeader, Panel, Section, Select } from "@/components/ui";
import { apiPost, fmt, phaseLabel, titleCase, useApi } from "@/lib/api";

const DESIGNS: [string, string][] = [
  ["two_means", "Two arms · continuous endpoint · superiority"], ["two_proportions", "Two arms · binary endpoint · superiority"], ["noninferiority_means", "Two arms · continuous endpoint · non-inferiority"],
  ["logrank_events", "Time-to-event · superiority (events required)"], ["noninferiority_hr_events", "Time-to-event · non-inferiority on the hazard ratio (events required)"],
];

function PowerCalc() {
  const [design, setDesign] = useState("two_means");
  const [p, setP] = useState({ delta: 5, sd: 10, p1: 0.6, p2: 0.5, margin: 1.8, true_difference: 0, hazard_ratio: 0.8, true_hazard_ratio: 1, alpha: 0.05, power: 0.9, ratio: 1, attrition: 0.15 });
  const [out, setOut] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const f = (k: keyof typeof p, label: string, step = 0.1, suffix?: string) => <NumberField key={k} label={label} value={p[k]} step={step} suffix={suffix} onChange={(v) => setP({ ...p, [k]: v })} />;
  const run = () => {
    setErr(null);
    const body: any = { design, alpha: p.alpha, power: p.power, ratio: p.ratio, attrition: p.attrition };
    if (design === "two_means") Object.assign(body, { delta: p.delta, sd: p.sd });
    if (design === "two_proportions") Object.assign(body, { p1: p.p1, p2: p.p2 });
    if (design === "noninferiority_means") Object.assign(body, { margin: p.margin, sd: p.sd, true_difference: p.true_difference });
    if (design === "logrank_events") Object.assign(body, { hazard_ratio: p.hazard_ratio });
    if (design === "noninferiority_hr_events") Object.assign(body, { margin: p.margin, true_hazard_ratio: p.true_hazard_ratio });
    apiPost("/design/power", body).then(setOut).catch((e) => { setOut(null); setErr(e.message); });
  };
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <Panel>
        <Select label="Design" value={design} onChange={(v) => { setDesign(v); setOut(null); }} options={DESIGNS.map(([v, l]) => ({ value: v, label: l }))} />
        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3">
          {design === "two_means" ? [f("delta", "Difference to detect (δ)"), f("sd", "Standard deviation (σ)")] : null}
          {design === "two_proportions" ? [f("p1", "Proportion, arm 1", 0.01), f("p2", "Proportion, arm 2", 0.01)] : null}
          {design === "noninferiority_means" ? [f("margin", "Non-inferiority margin"), f("sd", "Standard deviation (σ)"), f("true_difference", "Assumed true difference")] : null}
          {design === "logrank_events" ? [f("hazard_ratio", "Hazard ratio to detect", 0.01)] : null}
          {design === "noninferiority_hr_events" ? [f("margin", "Hazard-ratio margin", 0.05), f("true_hazard_ratio", "Assumed true hazard ratio", 0.01)] : null}
          {f("alpha", "Two-sided α", 0.005)}{f("power", "Power", 0.01)}{f("ratio", "Allocation ratio (arm 2 : arm 1)", 0.5)}
          {design.includes("events") ? null : f("attrition", "Expected attrition", 0.01)}
        </div>
        <div className="mt-3"><Button onClick={run}>Calculate</Button></div>
        {err ? <div className="mt-3"><ErrorNote message={err} /></div> : null}
      </Panel>
      <Panel>
        {out ? (
          <div data-testid="power-result">
            <p className="text-[12px] font-semibold uppercase tracking-wide text-ink-3">{out.design}</p>
            {out.events_required != null ? <p className="num mt-1 text-[30px] font-semibold leading-none">{out.events_required.toLocaleString()} <span className="text-[14px] font-medium text-ink-3">events required</span></p>
              : <p className="num mt-1 text-[30px] font-semibold leading-none">{out.n_group_1.toLocaleString()} + {out.n_group_2.toLocaleString()} <span className="text-[14px] font-medium text-ink-3">evaluable participants ({out.n_total.toLocaleString()} total)</span></p>}
            {out.with_attrition ? <p className="num mt-2 text-[13.5px] text-ink-2">Allowing for {fmt(out.with_attrition.attrition * 100, 0)}% attrition: randomize <b>{out.with_attrition.n_to_randomize.toLocaleString()}</b>.</p> : null}
            <div className="mt-3 border-t border-hairline pt-3"><KV rows={[["Method", out.method], ...Object.entries(out.assumptions).map(([k, v]) => [titleCase(k), String(v)] as [string, string])]} /></div>
            <p className="mt-3 text-[12.5px] text-ink-3">{out.disclaimer}</p>
          </div>
        ) : <p className="text-[13px] text-ink-3">Results appear here with the formula and every assumption used. Calculations are validated against statsmodels and published worked examples in the test-suite.</p>}
      </Panel>
    </div>
  );
}

function Simulation() {
  const [p, setP] = useState({ n_per_arm: 150, mean_treatment: -15, mean_control: -3, sd: 9, dropout_treatment: 0.15, dropout_control: 0.2, n_sims: 2000, seed: 20261013 });
  const [handling, setHandling] = useState("COMPLETERS");
  const [out, setOut] = useState<any>(null);
  const [sens, setSens] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const f = (k: keyof typeof p, label: string, step = 1) => <NumberField key={k} label={label} value={p[k]} step={step} onChange={(v) => setP({ ...p, [k]: v })} />;
  const run = () => {
    setErr(null);
    apiPost("/design/simulate", { ...p, missing_handling: handling }).then(setOut).catch((e) => { setOut(null); setErr(e.message); });
    apiPost("/design/sensitivity", { delta: p.mean_treatment - p.mean_control, sd: p.sd, n_randomized_per_group: p.n_per_arm, mean_treatment: p.mean_treatment, mean_control: p.mean_control, missing_treatment: p.dropout_treatment, missing_control: p.dropout_control }).then(setSens).catch(() => setSens(null));
  };
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Panel>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {f("n_per_arm", "Participants per arm")}{f("mean_treatment", "Assumed mean, treatment", 0.5)}{f("mean_control", "Assumed mean, control", 0.5)}{f("sd", "Standard deviation", 0.5)}
          {f("dropout_treatment", "Dropout, treatment", 0.01)}{f("dropout_control", "Dropout, control", 0.01)}{f("n_sims", "Simulated trials", 500)}{f("seed", "Random seed")}
        </div>
        <Select className="mt-3" label="Handling of dropouts" value={handling} onChange={setHandling} options={[{ value: "COMPLETERS", label: "Completers only (hypothetical-like)" }, { value: "JUMP_TO_REFERENCE", label: "Jump to reference (treatment-policy-like)" }]} />
        <div className="mt-3"><Button onClick={run}>Run synthetic simulation</Button></div>
        {err ? <div className="mt-3"><ErrorNote message={err} /></div> : null}
      </Panel>
      <div>
        {out ? (
          <div data-testid="simulation-result">
            <Hypothetical>
              <span className="font-semibold text-ink">{out.label}</span> — seed {out.seed}, {out.n_sims.toLocaleString()} simulated trials.
              <p className="num mt-1.5 text-[22px] font-semibold text-ink">{fmt(out.empirical_power * 100, 1)}% <span className="text-[13px] font-normal text-ink-2">of simulated trials rejected the null (Monte Carlo SE {fmt(out.monte_carlo_se * 100, 2)} points)</span></p>
              <p className="num mt-1 text-[13px]">Mean estimated difference {fmt(out.mean_estimated_difference)} (SD {fmt(out.sd_of_estimated_difference)}); 2.5th–97.5th percentile {fmt(out.percentiles_of_estimated_difference["2.5"])} to {fmt(out.percentiles_of_estimated_difference["97.5"])}.</p>
              <p className="mt-1.5 text-[12.5px] text-ink-3">{out.caveat}</p>
            </Hypothetical>
            {sens ? (
              <Panel className="mt-3" pad={false}>
                <p className="px-4 pt-3 text-[13.5px] font-semibold">Sensitivity scenarios</p>
                <div className="grid gap-0 md:grid-cols-2">
                  <table className="tbl"><thead><tr><th>Attrition</th><th className="r">Evaluable / arm</th><th className="r">Power</th></tr></thead><tbody>{sens.attrition.map((r: any) => <tr key={r.attrition}><td className="num">{fmt(r.attrition * 100, 0)}%</td><td className="r num">{r.n_evaluable_per_group}</td><td className="r num">{r.power != null ? `${fmt(r.power * 100, 1)}%` : "—"}</td></tr>)}</tbody></table>
                  {sens.missing_data ? <table className="tbl"><thead><tr><th>δ for missing (treatment)</th><th className="r">Marginal mean</th><th className="r">Difference</th></tr></thead><tbody>{sens.missing_data.map((r: any) => <tr key={r.delta}><td className="num">+{fmt(r.delta)}</td><td className="r num">{fmt(r.marginal_treatment_mean)}</td><td className="r num">{fmt(r.difference)}</td></tr>)}</tbody></table> : null}
                </div>
                <p className="border-t border-hairline px-4 py-2 text-[12px] text-ink-3">{sens.attrition_note} {sens.missing_data_note}</p>
              </Panel>
            ) : null}
          </div>
        ) : <Callout tone="info" title="Synthetic by construction">Outcomes are drawn from the distributions you specify, with the seed recorded so every run can be reproduced. Nothing here is, or is derived from, trial data — and the rejection rate is an operating characteristic of this simplified design, not a forecast that any trial will succeed.</Callout>}
      </div>
    </div>
  );
}

function Coverage() {
  const [drug, setDrug] = useState("eloralintide");
  const { data: drugs } = useApi<any[]>("/drugs");
  const { data } = useApi(`/design/coverage?drug=${drug}`);
  return (
    <>
      <div className="mb-3"><Select label="Molecule" value={drug} onChange={setDrug} options={(drugs ?? []).filter((d) => d.tier_group !== "COMPARATOR").map((d) => ({ value: d.slug, label: d.name }))} className="w-60" /></div>
      {!data ? <Loading /> : (
        <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
          <table className="tbl">
            <thead><tr><th>Trial</th><th>Phase</th><th>Status</th><th className="r">N</th><th>BMI</th>{data.dimensions.map(([k, l]: [string, string]) => <th key={k} className="!whitespace-normal text-center leading-tight" style={{ minWidth: 62 }}>{l}</th>)}<th className="r">Endpoints</th><th className="r">Criteria</th></tr></thead>
            <tbody>
              {data.rows.map((r: any) => (
                <tr key={r.nct_id}>
                  <td className="whitespace-nowrap"><Link href={`/trials/${r.nct_id}`} className="font-medium">{r.display_name}</Link></td><td className="whitespace-nowrap">{phaseLabel(r.phase)}</td><td>{titleCase(r.overall_status)}</td><td className="r num">{r.enrollment ?? "—"}</td><td className="num">{r.bmi_inclusion ?? "—"}</td>
                  {data.dimensions.map(([k]: [string]) => <td key={k} className="text-center">{r.covers[k] ? <Check aria-label="covered" size={15} className="inline text-teal" strokeWidth={2.6} /> : <span aria-label="not indicated" className="text-hairline-strong">·</span>}</td>)}
                  <td className="r num">{r.complexity.primary_endpoints + r.complexity.secondary_endpoints}</td><td className="r num">{r.complexity.inclusion_criteria + r.complexity.exclusion_criteria}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="border-t border-hairline px-3 py-2 text-[12px] text-ink-3">{data.note}</p>
        </div>
      )}
    </>
  );
}

function Eligibility() {
  const { data: studies } = useApi<any[]>("/studies");
  const [a, setA] = useState("NCT07321886");
  const [b, setB] = useState("NCT07392190");
  const { data, error } = useApi(`/design/eligibility-compare?a=${a}&b=${b}`);
  const { data: hist } = useApi(`/design/amendments/${a}`);
  const opts = (studies ?? []).map((s) => ({ value: s.nct_id, label: s.display_name }));
  return (
    <>
      <div className="mb-3 flex flex-wrap gap-3"><Select label="Trial A" value={a} onChange={setA} options={opts} className="w-64" /><Select label="Trial B" value={b} onChange={setB} options={opts} className="w-64" /></div>
      {error ? <ErrorNote message={error} /> : null}
      {data ? (
        <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
          <table className="tbl">
            <thead><tr><th>Topic</th><th>{data.a.study.display_name} <span className="font-normal normal-case text-ink-3">({data.counts.a_inclusion} inclusion / {data.counts.a_exclusion} exclusion)</span></th><th>{data.b.study.display_name} <span className="font-normal normal-case text-ink-3">({data.counts.b_inclusion} / {data.counts.b_exclusion})</span></th></tr></thead>
            <tbody>
              <tr><td className="font-medium">Age / sex</td><td>{data.a.min_age ?? "—"} to {data.a.max_age ?? "no upper limit"} · {titleCase(data.a.sex)}</td><td>{data.b.min_age ?? "—"} to {data.b.max_age ?? "no upper limit"} · {titleCase(data.b.sex)}</td></tr>
              {data.by_topic.map((t: any) => (
                <tr key={t.tag}>
                  <td className="whitespace-nowrap font-medium">{titleCase(t.tag)}{t.status !== "BOTH" ? <span className="block"><Badge tone="warn" icon={false}>{t.status === "ONLY_A" ? "Only in A" : "Only in B"}</Badge></span> : null}</td>
                  {(["a", "b"] as const).map((s) => <td key={s} className="max-w-md align-top text-[12.5px]">{t[`${s}_inclusion`].map((x: string) => <p key={x} className="mb-1"><span className="font-semibold text-teal">Include: </span>{x}</p>)}{t[`${s}_exclusion`].map((x: string) => <p key={x} className="mb-1"><span className="font-semibold text-[var(--serious)]">Exclude: </span>{x}</p>)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
          <p className="border-t border-hairline px-3 py-2 text-[12px] text-ink-3">{data.note}</p>
        </div>
      ) : <Loading />}
      {hist?.changes?.length ? (
        <Section title={`Protocol amendment impact — ${hist.study.display_name}`} hint={hist.note}>
          {hist.changes.map((c: any) => (
            <Panel key={c.id} pad={false} className="mb-3">
              <div className="px-4 pt-3">{c.is_synthetic ? <Badge tone="warn">Synthetic demonstration — not a real registry change</Badge> : <Badge tone="info">Detected change</Badge>} <span className="ml-2 text-[13px] text-ink-2">{c.summary}</span></div>
              <table className="tbl mt-2"><thead><tr><th>Field</th><th>Before</th><th>After</th><th>Significance</th><th>Why it matters</th></tr></thead>
                <tbody>{c.changes.map((x: any, i: number) => <tr key={i}><td className="font-medium">{x.label}</td><td className="max-w-xs text-ink-2">{Array.isArray(x.before) ? x.before.join("; ") : String(x.before ?? "—")}</td><td className="max-w-xs text-ink-2">{Array.isArray(x.after) ? x.after.join("; ") : String(x.after ?? "—")}</td><td>{titleCase(x.significance)}</td><td className="text-ink-3">{x.note}</td></tr>)}</tbody>
              </table>
            </Panel>
          ))}
        </Section>
      ) : null}
    </>
  );
}

export default function Design() {
  return (
    <>
      <PageHeader eyebrow="Protocol design workbench" title="Design calculations, coverage and eligibility — with every assumption shown" lede="Standard sample-size formulas, reproducible synthetic simulations, and side-by-side reading of registered protocols. Nothing here predicts whether a trial will succeed." />
      <Tabs.Root defaultValue="power">
        <Tabs.List className="mb-5 flex gap-5 overflow-x-auto border-b border-hairline" aria-label="Design tools">
          {[["power", "Sample size and power"], ["simulation", "Synthetic simulation and sensitivity"], ["coverage", "Population coverage"], ["eligibility", "Eligibility and amendments"]].map(([v, l]) => (
            <Tabs.Trigger key={v} value={v} className="-mb-px shrink-0 border-b-2 border-transparent pb-2 text-[13.5px] font-medium text-ink-3 data-[state=active]:border-accent data-[state=active]:text-ink">{l}</Tabs.Trigger>
          ))}
        </Tabs.List>
        <Tabs.Content value="power"><PowerCalc /></Tabs.Content>
        <Tabs.Content value="simulation"><Simulation /></Tabs.Content>
        <Tabs.Content value="coverage"><Coverage /></Tabs.Content>
        <Tabs.Content value="eligibility"><Eligibility /></Tabs.Content>
      </Tabs.Root>
    </>
  );
}
