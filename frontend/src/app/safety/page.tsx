"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { type ChartRow, ChartFrame, ChartTable, IntervalChart, ReportsTrend } from "@/components/charts";
import { Badge, Button, Callout, ErrorNote, Loading, PageHeader, Panel, Section, Select, SourceLine } from "@/components/ui";
import { apiPost, fmt, titleCase, useApi } from "@/lib/api";

const KINDS: [string, string][] = [
  ["PARTICIPANTS_WITH_EVENT", "Participants with ≥1 event (incidence within the trial)"],
  ["TREATMENT_DISCONTINUATION_DUE_TO_AE", "Discontinued treatment because of an adverse event"],
  ["STUDY_WITHDRAWAL_DUE_TO_AE", "Did not complete study — reason: adverse event"],
  ["DEATHS", "Deaths (all causes)"],
];

function TrialEvents({ drug }: { drug: string }) {
  const { data: studies } = useApi<any[]>(`/studies?drug=${drug}`);
  const withData = (studies ?? []).filter((s) => s.n_results > 0);
  const [nct, setNct] = useState("");
  const [kind, setKind] = useState("PARTICIPANTS_WITH_EVENT");
  const [term, setTerm] = useState("Nausea");
  useEffect(() => { if (withData.length && !withData.some((s) => s.nct_id === nct)) setNct(withData[0].nct_id); }, [withData, nct]);
  const { data: terms } = useApi(nct ? `/safety/terms?nct=${nct}` : null);
  const termOptions = (terms?.terms ?? []).filter((t: any) => t.measure_kind === kind);
  useEffect(() => { if (termOptions.length && !termOptions.some((t: any) => t.term === term)) setTerm(termOptions[0].term); }, [termOptions, term]);
  const { data, error } = useApi(nct && term ? `/safety/trial-ae?nct=${nct}&measure_kind=${kind}&term=${encodeURIComponent(term)}` : null);
  const exact = (data?.rows ?? []).filter((r: any) => r.term === term && r.scope === "ARM");
  const ranges = (data?.rows ?? []).filter((r: any) => r.term === term && r.scope !== "ARM");
  const rows: ChartRow[] = [
    ...exact.map((r: any) => ({ key: String(r.id), label: r.arm_label, value: r.percent_reported ?? r.percent_calculated, group: /placebo/i.test(r.arm_label) ? ("control" as const) : ("active" as const),
      tip: [r.measure_kind_label, r.n_affected != null && r.n_at_risk ? `${r.n_affected} of ${r.n_at_risk} participants${r.percent_reported == null ? " — percentage calculated from reported counts" : ""}` : "Percentage as reported", r.period ?? "", `${r.source?.tier_label} · ${titleCase(r.verification_status)}`] })),
    ...ranges.map((r: any) => ({ key: String(r.id), label: r.arm_label, value: null, range: [r.percent_low, r.percent_high] as [number, number], group: "active" as const, tip: [r.measure_kind_label, "Range across arms; not attributable to any single arm", r.source?.tier_label] })),
  ];
  const [contrast, setContrast] = useState<any>(null);
  const [cErr, setCErr] = useState<string | null>(null);
  const control = exact.find((r: any) => /placebo/i.test(r.arm_label));
  const top = exact.filter((r: any) => !/placebo/i.test(r.arm_label)).slice(-1)[0];
  useEffect(() => { setContrast(null); setCErr(null); }, [nct, kind, term]);
  const runContrast = () => { apiPost("/stats/risk-contrast", { ae_a: top.id, ae_b: control.id }).then(setContrast).catch((e) => setCErr(e.message)); };
  if (!withData.length) return <Callout tone="muted" title="No trial adverse-event tables are on record for this agent.">Registry-posted results or curated publications are needed for within-trial incidence.</Callout>;
  return (
    <>
      <div className="mb-3 flex flex-wrap items-end gap-3">
        <Select label="Trial" value={nct} onChange={setNct} options={withData.map((s) => ({ value: s.nct_id, label: s.display_name }))} className="w-64" />
        <Select label="What is being counted" value={kind} onChange={setKind} options={KINDS.map(([value, label]) => ({ value, label }))} className="w-[340px]" />
        <Select label="Event / term" value={term} onChange={setTerm} options={termOptions.length ? termOptions.map((t: any) => ({ value: t.term, label: t.term })) : [{ value: term, label: "No terms of this kind" }]} className="w-72" />
      </div>
      {error ? <ErrorNote message={error} /> : null}
      <ChartFrame title={`${term} — ${KINDS.find(([k]) => k === kind)?.[1]}`} subtitle="Denominator: participants at risk in each randomized arm of this trial. One measure kind per chart." table={<ChartTable rows={rows} unit="%" valueHeader="% of participants" />}
        footnote={exact[0] ? <>Source: <SourceLine source={exact[0].source} location={exact[0].origin === "REGISTRY" ? exact[0].source_location : null} />{exact[0].notes ? <span className="block">{exact[0].notes}</span> : null}</> : undefined}>
        {rows.length ? <IntervalChart rows={rows} unit="%" axisLabel="Percent of participants in the arm" /> : <p className="py-5 text-[13px] text-ink-3">Not reported for this trial.</p>}
      </ChartFrame>
      {control && top && control.n_affected != null && top.n_affected != null ? (
        <div className="mt-3">
          <Button variant="ghost" onClick={runContrast}>Calculate risk difference and ratio: {top.arm_label} vs {control.arm_label}</Button>
          {cErr ? <div className="mt-2"><ErrorNote message={cErr} /></div> : null}
          {contrast ? (
            <Panel className="mt-2">
              <p className="text-[12px]"><Badge tone="warn" icon={false}>Inference</Badge> <span className="text-ink-3">Calculated here from reported counts; unadjusted and post hoc.</span></p>
              <p className="num mt-1.5 text-[14px] text-ink">Risk difference <b>{fmt(contrast.risk_difference.estimate * 100, 1)} percentage points</b> (95% CI {fmt(contrast.risk_difference.ci_low * 100, 1)} to {fmt(contrast.risk_difference.ci_high * 100, 1)}) · Risk ratio <b>{contrast.risk_ratio.estimate != null ? fmt(contrast.risk_ratio.estimate, 2) : "not estimable"}</b>{contrast.risk_ratio.estimate != null ? ` (95% CI ${fmt(contrast.risk_ratio.ci_low, 2)} to ${fmt(contrast.risk_ratio.ci_high, 2)})` : ""}</p>
              <p className="mt-1 text-[12.5px] text-ink-3">{contrast.risk_difference.method}; {contrast.risk_ratio.method}. {contrast.risk_difference.interpretation}</p>
            </Panel>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

function Safety() {
  const params = useSearchParams();
  const router = useRouter();
  const drug = params.get("drug") ?? "tirzepatide";
  const { data: drugs } = useApi<any[]>("/drugs");
  const { data: sp, error } = useApi(`/safety/spontaneous/${drug}`);
  const { data: labels } = useApi(`/safety/labels/${drug}`);
  return (
    <>
      <PageHeader eyebrow="Safety intelligence" title="Trial incidence and spontaneous reports are different kinds of evidence" lede="Adverse events in a randomized arm have a denominator. Spontaneous reports do not. The two are shown in separate sections and never on one scale.">
        <Select label="Molecule" value={drug} onChange={(v) => router.replace(`/safety?drug=${v}`)} options={(drugs ?? []).filter((d) => d.tier_group !== "COMPARATOR").map((d) => ({ value: d.slug, label: d.name }))} className="w-60" />
      </PageHeader>
      {error ? <ErrorNote message={error} /> : null}

      <Section title="Regulatory warnings and label changes" hint="From FDA-approved prescribing information; changes are detected by comparing stored label versions.">
        {labels?.labels?.length ? (
          <div className="grid gap-3 lg:grid-cols-2">
            {labels.labels.map((l: any) => (
              <Panel key={l.brand}>
                <div className="flex flex-wrap items-center gap-2"><span className="text-[14.5px] font-semibold">{l.brand}</span><span className="text-[12px] text-ink-3">label version {l.label_version} · effective {l.effective_time}</span></div>
                {l.boxed_warning ? <div className="mt-2 rounded border border-ink bg-surface px-3 py-2 text-[12.5px] leading-snug text-ink"><span className="font-semibold">Boxed warning. </span>{l.boxed_warning.slice(0, 420)}…</div> : <p className="mt-2 text-[12.5px] text-ink-3">No boxed warning in this label.</p>}
                {l.recent_major_changes ? <p className="mt-2 text-[12.5px] text-ink-2"><span className="font-semibold text-ink">Recent major changes: </span>{l.recent_major_changes}</p> : null}
                <p className="mt-2"><SourceLine source={l.source} compact /></p>
              </Panel>
            ))}
          </div>
        ) : <Callout tone="muted" title="No FDA labeling is on record for this agent.">It is investigational; there are no label warnings to show. That is not a statement about its safety.</Callout>}
      </Section>

      <Section title="Adverse events in randomized trials" hint="Events over participants at risk, by arm, within one trial.">
        <TrialEvents key={drug} drug={drug} />
      </Section>

      <Section id="spontaneous" title="Spontaneous reports (FAERS)" hint="Counts of voluntary reports that mention the drug.">
        <Callout tone="warn" title="Why these numbers are not incidence — and not evidence of causation">
          <ul className="mt-1 list-disc space-y-1 pl-5">{(sp?.caveats ?? []).map((c: string) => <li key={c}>{c}</li>)}</ul>
        </Callout>
        {sp && !sp.available ? <div className="mt-3"><Callout tone="muted" title="No spontaneous-report series is stored for this agent.">{sp.explanation}</Callout></div> : null}
        {sp?.available ? (
          <div className="mt-4 grid gap-5 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
            <div>
              <ChartFrame title="Reports received per quarter" subtitle={`${sp.total_reports.toLocaleString()} reports in total · FAERS data through ${sp.database_last_updated}. The axis counts reports; it rises with use, publicity and time on market.`} footnote={<>Unit: {sp.unit}. Source: <SourceLine source={sp.source} compact /></>}
                table={<table className="tbl"><thead><tr><th>Quarter</th><th className="r">Reports received</th></tr></thead><tbody>{sp.reports_by_quarter.map((q: any) => <tr key={q.period}><td>{q.period}</td><td className="r num">{q.count.toLocaleString()}</td></tr>)}</tbody></table>}>
                <ReportsTrend data={sp.reports_by_quarter} />
              </ChartFrame>
              <Panel className="mt-4" pad={false}>
                <p className="px-4 pt-3 text-[13.5px] font-semibold">Most frequently reported terms</p>
                <div className="max-h-[300px] overflow-auto"><table className="tbl"><thead><tr><th>MedDRA preferred term</th><th className="r">Reports mentioning term</th></tr></thead><tbody>{sp.top_reactions.slice(0, 25).map((r: any) => <tr key={r.term}><td>{titleCase(r.term)}</td><td className="r num">{r.count.toLocaleString()}</td></tr>)}</tbody></table></div>
              </Panel>
            </div>
            <Panel pad={false}>
              <div className="px-4 pt-3"><p className="text-[13.5px] font-semibold">Disproportionality screen</p><p className="mt-0.5 text-[12.5px] text-ink-3">{sp.disproportionality_caveats[0]}</p></div>
              <div className="mt-2 max-h-[560px] overflow-auto"><table className="tbl"><thead><tr><th>Term</th><th className="r">Reports (drug)</th><th className="r">PRR (95% CI)</th><th className="r">ROR (95% CI)</th></tr></thead>
                <tbody>{sp.disproportionality_screen.map((r: any) => (<tr key={r.term}><td>{titleCase(r.term)}</td><td className="r num">{r.drug_reports != null ? r.drug_reports.toLocaleString() : <span className="text-ink-3">unavailable</span>}</td><td className="r num">{r.prr != null ? `${fmt(r.prr, 2)} (${fmt(r.prr_ci[0], 2)}–${fmt(r.prr_ci[1], 2)})` : <span className="text-ink-3">not calculated</span>}</td><td className="r num">{r.ror != null ? `${fmt(r.ror, 2)} (${fmt(r.ror_ci[0], 2)}–${fmt(r.ror_ci[1], 2)})` : "—"}</td></tr>))}</tbody></table></div>
              <p className="border-t border-hairline px-4 py-2.5 text-[12px] text-ink-3">{sp.disproportionality_caveats[2]} “Unavailable” means the term is outside the 500 most-reported terms retrieved — not that there are zero reports.</p>
            </Panel>
          </div>
        ) : null}
      </Section>
    </>
  );
}

export default function Page() {
  return <Suspense fallback={<Loading />}><Safety /></Suspense>;
}
