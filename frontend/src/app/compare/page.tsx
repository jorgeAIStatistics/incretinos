"use client";

import clsx from "clsx";
import { Ban, Check, Download, HelpCircle, Minus, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { ChartFrame, ChartTable, IntervalChart } from "@/components/charts";
import { Badge, Callout, ClassificationBadge, ErrorNote, Loading, PageHeader, Panel, Qualifiers, Section, Select, SourceLine, VerificationBadge } from "@/components/ui";
import { apiPost, fmt, phaseLabel, titleCase, useApi, diabetesLabel } from "@/lib/api";
import { estimandLabel, findingRow } from "@/lib/rows";

const CATEGORIES = [
  ["WEIGHT_PCT_CHANGE", "Percent change in body weight"], ["WEIGHT_ABS_CHANGE", "Change in body weight (kg)"], ["HBA1C_CHANGE", "Change in HbA1c"],
  ["WEIGHT_RESPONDER", "Weight-reduction responders"], ["BODY_COMPOSITION", "Body composition"], ["MACE", "Major adverse cardiovascular events"],
];

const ASSESS: Record<string, { tone: string; Icon: any; label: string }> = {
  MATCH: { tone: "text-[var(--ok)]", Icon: Check, label: "Match" },
  MINOR_DIFFERENCE: { tone: "text-[var(--warn)]", Icon: Minus, label: "Minor difference" },
  MAJOR_DIFFERENCE: { tone: "text-[var(--serious)]", Icon: TriangleAlert, label: "Major difference" },
  BLOCKING: { tone: "text-[var(--critical)]", Icon: Ban, label: "Blocking" },
  UNKNOWN: { tone: "text-ink-3", Icon: HelpCircle, label: "Not assessable" },
  NOT_APPLICABLE: { tone: "text-ink-3", Icon: Minus, label: "Not applicable" },
};

function FindingPicker({ side, value, onPick, studies }: { side: "A" | "B"; value: number | null; onPick: (id: number) => void; studies: any[] }) {
  const { data: current } = useApi(value ? `/findings/${value}` : null);
  const [study, setStudy] = useState("");
  const [category, setCategory] = useState("WEIGHT_PCT_CHANGE");
  useEffect(() => {
    if (current?.finding && !study) {
      setStudy(current.finding.nct_id);
      setCategory(current.finding.category);
    }
  }, [current, study]);
  const { data: list } = useApi(study ? `/findings?study=${study}&category=${category}&limit=200` : null);
  const options = (list?.rows ?? []).map((f: any) => ({
    value: String(f.result_id),
    label: `${f.arm_label} · ${fmt(f.value)}${/percent|%/i.test(f.unit ?? "") ? "%" : ""} · wk ${fmt(f.timepoint_weeks, 0)} · ${estimandLabel(f.estimand_label, f.estimand_basis)} · ${f.source?.tier_label ?? f.tier}`,
  }));
  return (
    <Panel>
      <p className="mb-2 text-[11.5px] font-semibold uppercase tracking-wide text-ink-3">Finding {side}</p>
      <div className="grid gap-2.5 sm:grid-cols-2">
        <Select label="Trial" value={study} onChange={setStudy} options={[{ value: "", label: "Select a trial…" }, ...studies.filter((s) => s.n_results > 0).map((s) => ({ value: s.nct_id, label: s.display_name }))]} />
        <Select label="Endpoint" value={category} onChange={setCategory} options={CATEGORIES.map(([v, l]) => ({ value: v, label: l }))} />
      </div>
      <Select className="mt-2.5" label={`Reported result (${options.length} available)`} value={value ? String(value) : ""} onChange={(v) => v && onPick(Number(v))} options={[{ value: "", label: options.length ? "Select a result…" : "No arm-level results for this endpoint" }, ...options]} />
    </Panel>
  );
}

function FindingCard({ side, f, reading }: { side: string; f: any; reading: any }) {
  return (
    <Panel className="min-w-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11.5px] font-semibold uppercase tracking-wide text-ink-3">Finding {side}</p>
        <VerificationBadge status={f.verification_status} />
      </div>
      <p className="mt-1 text-[15px] font-semibold leading-snug text-ink"><Link href={`/trials/${f.nct_id}`}>{f.study_name}</Link> — {f.arm_label}</p>
      <p className="mt-0.5 text-[12.5px] text-ink-3">{phaseLabel(f.phase)} · {diabetesLabel(f.diabetes_status)} · {f.endpoint_title}</p>
      <p className="num mt-3 text-[28px] font-semibold leading-none text-ink">{fmt(f.value)}{/percent|%/i.test(f.unit ?? "") ? "%" : ""}<span className="ml-2 text-[13px] font-normal text-ink-3">{f.timepoint_weeks != null ? `week ${fmt(f.timepoint_weeks, 0)}` : ""}{f.baseline_definition ? ` · from ${f.baseline_definition}` : ""}</span></p>
      <p className="num mt-1 text-[12.5px] text-ink-2">{f.ci_low != null ? `${fmt(f.ci_level, 1)}% CI ${fmt(f.ci_low)} to ${fmt(f.ci_high)}` : "Confidence interval not reported"} · {f.n_analyzed != null ? `n = ${f.n_analyzed}` : "n not reported"}{f.dispersion_value != null ? ` · ${f.dispersion_type} ${fmt(f.dispersion_value)}` : ""}</p>
      {reading.derived_interval ? <p className="mt-1 text-[12px] text-ink-3"><Badge tone="warn" icon={false}>Inference</Badge> {reading.derived_interval.display}</p> : null}
      <div className="mt-3"><Qualifiers labels={f.labels} /></div>
      <p className="mt-3 border-t border-hairline pt-2.5 text-[13px] leading-relaxed text-ink-2">{f.statement}</p>
      <details className="mt-2 text-[13px]">
        <summary className="cursor-pointer font-medium text-accent">What this result does and does not establish</summary>
        <ul className="mt-1.5 list-disc space-y-1 pl-5 text-ink-2">
          {reading.establishes.map((s: string) => <li key={s}><span className="font-medium text-ink">Establishes: </span>{s}</li>)}
          {reading.does_not_establish.map((s: string) => <li key={s}><span className="font-medium text-ink">Does not establish: </span>{s}</li>)}
          {reading.missing.map((s: string) => <li key={s}><span className="font-medium text-ink">Not available: </span>{s}</li>)}
        </ul>
      </details>
      <p className="mt-2.5"><SourceLine source={f.source} quote={f.locator_quote} location={f.origin === "REGISTRY" ? f.source_location : null} /></p>
    </Panel>
  );
}

function Lab() {
  const params = useSearchParams();
  const router = useRouter();
  const a = Number(params.get("a")) || null;
  const b = Number(params.get("b")) || null;
  const { data: studies } = useApi<any[]>("/studies");
  const { data: presets } = useApi<any[]>("/compare/presets");
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (key: "a" | "b", id: number) => {
    const next = new URLSearchParams(params.toString());
    next.set(key, String(id));
    router.replace(`/compare?${next.toString()}`);
  };
  useEffect(() => {
    if (!a || !b) { setResult(null); return; }
    setBusy(true); setError(null);
    apiPost("/compare", { a, b }).then(setResult).catch((e) => { setError(e.message); setResult(null); }).finally(() => setBusy(false));
  }, [a, b]);
  const c = result?.comparison;
  const rows = result ? [findingRow(result.a, { studyInLabel: true }), findingRow(result.b, { studyInLabel: true })] : [];
  return (
    <>
      <PageHeader eyebrow="Trial comparison laboratory" title="Can these two results be compared?" lede="Pick any two reported results. The engine classifies the comparison, shows every methodological difference behind that classification, and states what may and may not be concluded.">
        {a && b && c ? <a href={`/api/reports/comparison?a=${a}&b=${b}`} className="inline-flex h-9 items-center gap-1.5 rounded-md bg-accent px-3.5 text-[13.5px] font-medium text-white hover:bg-[#184f9f] hover:no-underline"><Download aria-hidden size={14} /> Export cited report</a> : null}
      </PageHeader>

      <div className="grid gap-4 md:grid-cols-2">
        <FindingPicker key={`A${a ?? ""}`} side="A" value={a} onPick={(id) => set("a", id)} studies={studies ?? []} />
        <FindingPicker key={`B${b ?? ""}`} side="B" value={b} onPick={(id) => set("b", id)} studies={studies ?? []} />
      </div>

      {!c && !busy ? (
        <Section title="Worked examples" hint="One for each kind of answer the engine gives.">
          <div className="grid gap-3 md:grid-cols-2">
            {(presets ?? []).map((p) => (
              <Link key={p.title} href={`/compare?a=${p.a}&b=${p.b}`} className="block rounded-lg border border-hairline bg-surface p-3.5 transition-colors hover:border-accent hover:no-underline">
                <span className="block text-[13.5px] font-semibold text-ink">{p.title}</span>
                <span className="mt-0.5 block text-[12.5px] text-ink-3">{p.question}</span>
                <span className="mt-2 block"><ClassificationBadge value={p.classification} /></span>
              </Link>
            ))}
          </div>
        </Section>
      ) : null}
      {error ? <div className="mt-5"><ErrorNote message={error} /></div> : null}
      {busy ? <div className="mt-6"><Loading rows={5} /></div> : null}

      {c ? (
        <div className="fade-in mt-6" data-testid="comparison-result">
          <div className={clsx("mb-6 rounded-lg border-l-4 bg-surface px-5 py-4 shadow-[0_1px_0_rgba(16,24,32,0.04)]", c.classification.startsWith("DIRECT") ? "border-[var(--ok)]" : c.classification.startsWith("NOT") ? "border-[var(--critical)]" : c.classification.startsWith("DESCRIPTIVE") ? "border-[var(--warn)]" : c.classification.startsWith("PARTIALLY") ? "border-accent" : "border-ink-3")}>
            <p className="text-[11.5px] font-semibold uppercase tracking-wide text-ink-3">Classification</p>
            <h2 className="mt-0.5 text-[22px] font-semibold text-ink" data-testid="classification">{titleCase(c.classification)}</h2>
            <p className="mt-0.5 text-[14px] text-ink-2">{c.headline}.</p>
            <p className="num mt-2 text-[12.5px] text-ink-3">{c.counts.blocking} blocking · {c.counts.major} major · {c.counts.minor} minor · {c.counts.unknown} not assessable · {c.counts.match} matching</p>
            <ul className="mt-3 list-disc space-y-1 pl-5 text-[13.5px] text-ink-2">{c.reasons.map((r: string) => <li key={r}>{r}</li>)}</ul>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <FindingCard side="A" f={result.a} reading={result.interpretation_a} />
            <FindingCard side="B" f={result.b} reading={result.interpretation_b} />
          </div>

          <div className="mt-5">
            <ChartFrame
              title="The two reported values, side by side"
              subtitle={c.same_study ? "Arms of one randomized trial." : "Arms of different trials: the gap between them is not an estimate of comparative efficacy."}
              table={<ChartTable rows={rows} unit={/percent|%/i.test(result.a.unit ?? "") ? "%" : ""} />}
              footnote={<>Sources: <SourceLine source={result.a.source} compact /> · <SourceLine source={result.b.source} compact /></>}
            >
              <IntervalChart rows={rows} unit={/percent|%/i.test(result.a.unit ?? "") ? "%" : ""} axisLabel={result.a.endpoint_title} />
            </ChartFrame>
          </div>

          <div className="mt-5 grid gap-4 lg:grid-cols-2">
            <Panel>
              <p className="text-[13.5px] font-semibold text-ink">Reported between-arm analysis</p>
              {c.reported_analyses.length ? c.reported_analyses.map((an: any, i: number) => (
                <div key={an.id} className="mt-2 text-[13px] text-ink-2">
                  <p className="num text-[15px] font-semibold text-ink">{an.param_type}: {fmt(an.estimate)} {an.ci_low != null ? <span className="text-[12.5px] font-normal text-ink-2">({fmt(an.ci_level, 1)}% CI {fmt(an.ci_low)} to {fmt(an.ci_high)})</span> : <span className="text-[12.5px] font-normal text-ink-3">(CI not reported)</span>}</p>
                  <p className="mt-0.5">{an.treatment_label} vs {an.comparator_label} · p-value: {an.p_value_text ?? "not reported in this source"}</p>
                  <div className="mt-1.5"><Qualifiers labels={an.labels} /></div>
                  <ul className="mt-2 list-disc space-y-1 pl-5">{[...result.analysis_interpretations[i].establishes.map((s: string) => `Establishes: ${s}`), ...result.analysis_interpretations[i].does_not_establish.slice(0, 3).map((s: string) => `Does not establish: ${s}`)].map((s) => <li key={s}>{s}</li>)}</ul>
                  <p className="mt-2"><SourceLine source={an.source} compact location={an.origin === "REGISTRY" ? an.source_location : null} /></p>
                </div>
              )) : <p className="mt-1.5 text-[13px] text-ink-2">{c.same_study ? "No between-arm analysis for this pair is stored. Any difference between the two arm-level values is descriptive." : "None: the results come from different trials, so no randomized between-arm analysis exists."}</p>}
            </Panel>
            <Panel>
              <p className="text-[13.5px] font-semibold text-ink">Descriptive difference (A − B)</p>
              {c.descriptive_difference ? (
                <>
                  <p className="num mt-1 text-[22px] font-semibold text-ink">{fmt(c.descriptive_difference.value)} <span className="text-[13px] font-normal text-ink-3">{/percent|%/i.test(c.descriptive_difference.unit ?? "") ? "percentage points" : c.descriptive_difference.unit}</span></p>
                  <p className="mt-1 text-[12.5px] text-ink-3">Confidence interval: not available.</p>
                  <p className="mt-1.5 text-[13px] text-ink-2">{c.descriptive_difference.note}</p>
                </>
              ) : <Callout tone="critical" title="Not computed">The classification does not permit a difference between these two values.</Callout>}
            </Panel>
          </div>

          <Section id="factors" title="Methodological comparison" hint="Every dimension the classification rests on.">
            <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
              <table className="tbl">
                <thead><tr><th>Dimension</th><th>Finding A</th><th>Finding B</th><th>Assessment</th><th>Why it matters here</th></tr></thead>
                <tbody>
                  {c.factors.map((f: any) => { const s = ASSESS[f.assessment] ?? ASSESS.UNKNOWN; return (
                    <tr key={f.key}>
                      <td className="font-medium">{f.label}</td><td className="max-w-[230px] break-words text-ink-2">{f.a}</td><td className="max-w-[230px] break-words text-ink-2">{f.b}</td>
                      <td className="whitespace-nowrap"><span className={clsx("inline-flex items-center gap-1 text-[12.5px] font-medium", s.tone)}><s.Icon aria-hidden size={13} strokeWidth={2.4} /> {s.label}</span></td>
                      <td className="min-w-[260px] text-ink-2">{f.explanation}</td>
                    </tr>); })}
                </tbody>
              </table>
            </div>
          </Section>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel><p className="mb-1.5 flex items-center gap-1.5 text-[13.5px] font-semibold text-ink"><Check aria-hidden size={15} className="text-[var(--ok)]" /> What may be said</p><ul className="list-disc space-y-1.5 pl-5 text-[13px] text-ink-2">{c.permitted_statements.map((s: string) => <li key={s}>{s}</li>)}</ul></Panel>
            <Panel><p className="mb-1.5 flex items-center gap-1.5 text-[13.5px] font-semibold text-ink"><Ban aria-hidden size={15} className="text-[var(--critical)]" /> What may not be said</p><ul className="list-disc space-y-1.5 pl-5 text-[13px] text-ink-2">{c.prohibited_statements.map((s: string) => <li key={s}>{s}</li>)}</ul></Panel>
          </div>
          {c.requirements.length ? <div className="mt-4"><Panel><p className="mb-1.5 text-[13.5px] font-semibold text-ink">What a rigorous comparison would require</p><ul className="list-disc space-y-1.5 pl-5 text-[13px] text-ink-2">{c.requirements.map((s: string) => <li key={s}>{s}</li>)}</ul></Panel></div> : null}
        </div>
      ) : null}
    </>
  );
}

export default function Page() {
  return <Suspense fallback={<Loading />}><Lab /></Suspense>;
}
