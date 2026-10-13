"use client";

import clsx from "clsx";
import { Ban, Check, Download, HelpCircle, Minus, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { type BarFill, ChartFrame, ChartTable, FacetedChart, Reported } from "@/components/charts";
import { Badge, Callout, ClassificationBadge, ErrorNote, Loading, PageHeader, Panel, Qualifiers, Section, Select, SourceLine, VerificationBadge } from "@/components/ui";
import { apiPost, fmt, phaseLabel, titleCase, useApi, diabetesLabel } from "@/lib/api";
import { ciLabel, contrastDirection, contrastUnit, differenceUnit, estimandLabel, findingRow, isRatio, reportedText, tierLabel, unitLabel, weekLabel, withUnit } from "@/lib/rows";

// Vocabulary requested once: the API returns the label of every endpoint category with any findings response.
const VOCABULARY = "/findings?limit=1";
const RESULT_LIMIT = 2000;

const ASSESS: Record<string, { tone: string; Icon: any; label: string }> = {
  MATCH: { tone: "text-[var(--ok)]", Icon: Check, label: "Match" },
  MINOR_DIFFERENCE: { tone: "text-[var(--warn)]", Icon: Minus, label: "Minor difference" },
  MAJOR_DIFFERENCE: { tone: "text-[var(--serious)]", Icon: TriangleAlert, label: "Major difference" },
  BLOCKING: { tone: "text-[var(--critical)]", Icon: Ban, label: "Blocking" },
  UNKNOWN: { tone: "text-ink-3", Icon: HelpCircle, label: "Not assessable" },
  NOT_APPLICABLE: { tone: "text-ink-3", Icon: Minus, label: "Not applicable" },
};

/**
 * How each kind of comparison is drawn. The kinds differ in structure and bar texture, not only in colour:
 * one shared panel with solid bars for randomized arms of one trial; separate panels with hatched bars for a
 * partial comparison; separate panels with unfilled bars for a description. Any other classification gets no chart.
 */
const CHART_KIND: Record<string, { kind: string; fill: BarFill; title: string; how: string }> = {
  "DIRECT RANDOMIZED COMPARISON": { kind: "direct", fill: "solid", title: "Randomized arms of one trial, on one axis", how: "One panel, solid bars: both arms were randomized within the same trial." },
  "PARTIALLY COMPARABLE": { kind: "partial", fill: "hatched", title: "Two results with similar designs, in separate panels", how: "Separate panels, hatched bars: the arms were not randomized against each other; the designs are similar, so the comparison is partial." },
  "DESCRIPTIVE CROSS-STUDY COMPARISON": { kind: "descriptive", fill: "outline", title: "Two results side by side, for description only", how: "Separate panels, unfilled bars: the arms were not randomized against each other and the designs differ; this is a description, not a comparison of efficacy." },
};

const sameScale = (a: any, b: any) => unitLabel(a.unit) === unitLabel(b.unit) && a.category === b.category && (a.threshold ?? null) === (b.threshold ?? null);

function FindingPicker({ side, value, onPick, studies, categories }: { side: "A" | "B"; value: number | null; onPick: (id: number) => void; studies: any[]; categories: Record<string, string> | null }) {
  const { data: current } = useApi(value ? `/findings/${value}` : null);
  const [study, setStudy] = useState("");
  const [category, setCategory] = useState("WEIGHT_PCT_CHANGE");
  useEffect(() => {
    if (current?.finding && !study) {
      setStudy(current.finding.nct_id);
      setCategory(current.finding.category);
    }
  }, [current, study]);
  const { data: list, loading } = useApi(study ? `/findings?study=${study}&category=${category}&limit=${RESULT_LIMIT}` : null);
  const options = (list?.rows ?? []).map((f: any) => ({
    value: String(f.result_id),
    label: `${f.arm_label} · ${withUnit(reportedText(f.value, f.value_text), f.unit)} · ${weekLabel(f.timepoint_weeks)} · ${estimandLabel(f.estimand_label, f.estimand_basis)} · ${tierLabel(f)}`,
  }));
  const categoryOptions = categories ? Object.entries(categories).map(([v, l]) => ({ value: v, label: `${l.charAt(0).toUpperCase()}${l.slice(1)}` })) : [{ value: category, label: titleCase(category) }];
  const placeholder = !study ? "Select a trial first" : loading && !list ? "Loading results…" : options.length ? "Select a result…" : "No arm-level results for this endpoint in this trial";
  return (
    <Panel className="min-w-0">
      <p className="mb-2 text-[11.5px] font-semibold uppercase tracking-wide text-ink-3">Finding {side}</p>
      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
        <Select label="Trial" value={study} onChange={setStudy} options={[{ value: "", label: "Select a trial…" }, ...studies.filter((s) => s.n_results > 0).map((s) => ({ value: s.nct_id, label: s.display_name }))]} />
        <Select label="Endpoint" value={category} onChange={setCategory} options={categoryOptions} />
      </div>
      <Select className="mt-2.5" label={study && list ? `Reported result (${options.length} available${options.length >= RESULT_LIMIT ? "; list truncated" : ""})` : "Reported result"} value={value ? String(value) : ""} onChange={(v) => v && onPick(Number(v))} options={[{ value: "", label: placeholder }, ...options]} />
    </Panel>
  );
}

function FindingCard({ side, f, reading }: { side: string; f: any; reading: any }) {
  const sameUnit = /standard (error|deviation)/i.test(f.dispersion_type ?? "");
  return (
    <Panel className="min-w-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11.5px] font-semibold uppercase tracking-wide text-ink-3">Finding {side}</p>
        <VerificationBadge status={f.verification_status} />
      </div>
      <p className="mt-1 text-[15px] font-semibold leading-snug text-ink"><Link href={`/trials/${f.nct_id}`}>{f.study_name}</Link> — {f.arm_label}</p>
      <p className="mt-0.5 text-[12.5px] text-ink-3">{phaseLabel(f.phase)} · {diabetesLabel(f.diabetes_status)} · {f.endpoint_title}</p>
      <p className="num mt-3 text-[28px] font-semibold leading-none text-ink" data-testid={`finding-${side.toLowerCase()}-value`}>
        {f.value != null ? withUnit(reportedText(f.value, f.value_text), f.unit) : f.value_low != null ? `${fmt(f.value_low)} to ${withUnit(fmt(f.value_high), f.unit)}` : "not reported"}
        {unitLabel(f.unit) ? null : <span className="ml-2 text-[13px] font-normal text-ink-3">(unit not recorded)</span>}
        <span className="ml-2 text-[13px] font-normal text-ink-3">{weekLabel(f.timepoint_weeks)}{f.baseline_definition ? ` · from ${f.baseline_definition}` : ""}</span>
      </p>
      <p className="num mt-1 text-[12.5px] text-ink-2">
        {f.ci_low != null && f.ci_high != null ? `${ciLabel(f.ci_level)} ${fmt(f.ci_low)} to ${withUnit(fmt(f.ci_high), f.unit)}` : "Confidence interval not reported"} · {f.n_analyzed != null ? `n = ${f.n_analyzed}` : "n not reported"}
        {f.dispersion_value != null ? ` · ${f.dispersion_type} ${sameUnit ? withUnit(fmt(f.dispersion_value), f.unit) : fmt(f.dispersion_value)}` : ""}
      </p>
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
      {/* Registry locators are long unbroken paths: they wrap instead of widening the page. */}
      <p className="mt-2.5 [overflow-wrap:anywhere]"><SourceLine source={f.source} quote={f.locator_quote} location={f.origin === "REGISTRY" ? f.source_location : null} /></p>
    </Panel>
  );
}

/** The two values as a chart — only when the classification permits one and both values share unit and endpoint. */
function ComparisonChart({ result, studies }: { result: any; studies: any[] }) {
  const c = result.comparison;
  const { a, b } = result;
  const kind = CHART_KIND[c.classification];
  if (!kind) {
    return (
      <div data-testid="no-comparison-chart">
        <Callout tone="muted" title="No comparison chart is drawn">
          The classification is “{titleCase(c.classification)}”, so the two values are not placed on a common axis. Each finding is shown above on its own, with its unit and context.
        </Callout>
      </div>
    );
  }
  if (!sameScale(a, b)) {
    return (
      <div data-testid="no-comparison-chart">
        <Callout tone="warn" title="No comparison chart is drawn: the two values are not on a common scale">
          Finding A is in {unitLabel(a.unit) || "a unit the source does not record"} ({a.endpoint_title}); finding B is in {unitLabel(b.unit) || "a unit the source does not record"} ({b.endpoint_title}). Values with different units or endpoints are never drawn on one axis.
        </Callout>
      </div>
    );
  }
  const drugsOf = (nct: string) => studies.find((s) => s.nct_id === nct)?.drugs ?? null;
  // A derived interval is taken from the API's reading of each finding (calculated by the statistics engine), never recomputed here.
  const rowA = findingRow({ ...a, derived_interval: result.interpretation_a?.derived_interval ?? null }, { studyInLabel: true, drugs: drugsOf(a.nct_id), keyPrefix: "a:" });
  const rowB = findingRow({ ...b, derived_interval: result.interpretation_b?.derived_interval ?? null }, { studyInLabel: true, drugs: drugsOf(b.nct_id), keyPrefix: "b:" });
  const unit = unitLabel(a.unit);
  const scale = `${a.category}|${a.threshold ?? ""}|${unit}`;
  const facets =
    kind.kind === "direct"
      ? [{ key: "ab", caption: <>One randomized trial ({a.study_name}): findings A and B are arms allocated by randomization.</>, rows: [rowA, rowB], unit, axisLabel: a.endpoint_title, scale }]
      : [
          { key: "a", caption: <>Finding A — {a.study_name}</>, rows: [rowA], unit, axisLabel: a.endpoint_title, scale },
          { key: "b", caption: <>Finding B — {b.study_name}</>, rows: [rowB], unit, axisLabel: b.endpoint_title, scale },
        ];
  return (
    <div data-testid="comparison-chart" data-kind={kind.kind} data-fill={kind.fill} data-panels={facets.length}>
      <ChartFrame
        title={kind.title}
        subtitle={<><ClassificationBadge value={c.classification} /> <span data-testid="comparison-chart-how">{kind.how}</span></>}
        table={<ChartTable rows={[rowA, rowB]} unit={unit} />}
        footnote={<>Sources: A — <SourceLine source={a.source} compact /> · B — <SourceLine source={b.source} compact /><span className="mt-1 block">Chart styles used in this laboratory: {Object.values(CHART_KIND).map((k) => k.how).join(" ")}</span></>}
      >
        <FacetedChart
          facets={facets}
          fill={kind.fill}
          note={c.same_study ? "The two values come from one trial but are not a like-for-like randomized contrast, so each has its own panel." : "The two values come from different trials, so each has its own panel on the same scale."}
          divider={
            <p className="my-2 flex items-center gap-2 text-[12px] font-medium text-ink-2">
              <span aria-hidden className="h-px flex-1 border-t border-dashed border-ink-3" />
              {c.same_study ? "Not a like-for-like randomized contrast" : "Different trials — no randomization between these arms; the gap is not an estimate of comparative efficacy"}
              <span aria-hidden className="h-px flex-1 border-t border-dashed border-ink-3" />
            </p>
          }
        />
      </ChartFrame>
    </div>
  );
}

function Lab() {
  const params = useSearchParams();
  const router = useRouter();
  const a = Number(params.get("a")) || null;
  const b = Number(params.get("b")) || null;
  const { data: studies } = useApi<any[]>("/studies");
  const { data: presets } = useApi<any[]>("/compare/presets");
  const { data: vocabulary } = useApi(VOCABULARY);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const same = a !== null && a === b;
  const set = (key: "a" | "b", id: number) => {
    const next = new URLSearchParams(params.toString());
    next.set(key, String(id));
    router.replace(`/compare?${next.toString()}`);
  };
  useEffect(() => {
    if (!a || !b || a === b) { setResult(null); setBusy(false); return; }
    let live = true; // a slower, older response must not replace a newer pair
    setBusy(true); setError(null);
    apiPost("/compare", { a, b })
      .then((r) => { if (live) setResult(r); })
      .catch((e) => { if (live) { setError(e.message); setResult(null); } })
      .finally(() => { if (live) setBusy(false); });
    return () => { live = false; };
  }, [a, b]);
  const c = result?.comparison;
  const difference = c?.descriptive_difference;
  return (
    <>
      <PageHeader eyebrow="Trial comparison laboratory" title="Can these two results be compared?" lede="Pick any two reported results. The engine classifies the comparison, shows every methodological difference behind that classification, and states what may and may not be concluded.">
        {a && b && c ? <a href={`/api/reports/comparison?a=${a}&b=${b}`} className="inline-flex h-9 items-center gap-1.5 rounded-md bg-accent px-3.5 text-[13.5px] font-medium text-white hover:bg-[#184f9f] hover:no-underline"><Download aria-hidden size={14} /> Export cited report</a> : null}
      </PageHeader>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <FindingPicker key={`A${a ?? ""}`} side="A" value={a} onPick={(id) => set("a", id)} studies={studies ?? []} categories={vocabulary?.category_labels ?? null} />
        <FindingPicker key={`B${b ?? ""}`} side="B" value={b} onPick={(id) => set("b", id)} studies={studies ?? []} categories={vocabulary?.category_labels ?? null} />
      </div>

      {same ? <div className="mt-5" data-testid="same-result"><Callout tone="warn" title="Finding A and finding B are the same stored result">A result cannot be compared with itself. Choose a different result for one side.</Callout></div> : null}
      {!c && !busy && !same ? (
        <Section title="Worked examples" hint="One for each kind of answer the engine gives.">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
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

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <FindingCard side="A" f={result.a} reading={result.interpretation_a} />
            <FindingCard side="B" f={result.b} reading={result.interpretation_b} />
          </div>

          <div className="mt-5"><ComparisonChart result={result} studies={studies ?? []} /></div>

          <div className="mb-8 mt-5 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Panel className="min-w-0">
              <p className="text-[13.5px] font-semibold text-ink">Reported between-arm analysis</p>
              {c.reported_analyses.length ? c.reported_analyses.map((an: any, i: number) => {
                const direction = contrastDirection(an);
                return (
                  <div key={an.id} className="mt-2 text-[13px] text-ink-2" data-testid="reported-analysis">
                    <p className="text-[15px] text-ink">
                      <span className="font-semibold">{an.param_type ?? "Estimate"}:</span>{" "}
                      {an.estimate == null ? <span className="text-ink-3">not reported</span> : <Reported text={reportedText(an.estimate, an.estimate_text)} unit={contrastUnit(an)} lo={an.ci_low} hi={an.ci_high} level={an.ci_level} missingUnit={isRatio(an.param_type) ? "ratio, no unit" : "unit not recorded"} />}
                      {an.ci_low == null || an.ci_high == null ? <span className="text-[12.5px] text-ink-3"> (CI not reported)</span> : null}
                    </p>
                    <p className="mt-0.5" data-testid="contrast-direction">{isRatio(an.param_type) ? "Ratio" : "Difference"}: {direction.expression}. <span className="text-ink-3">{direction.basis}</span></p>
                    <p className="mt-0.5">p-value: {an.p_value_text ?? "not reported in this source"}{an.unit ? ` · outcome unit as stored: ${an.unit}` : ""}</p>
                    <div className="mt-1.5"><Qualifiers labels={an.labels} /></div>
                    <ul className="mt-2 list-disc space-y-1 pl-5">{[...result.analysis_interpretations[i].establishes.map((s: string) => `Establishes: ${s}`), ...result.analysis_interpretations[i].does_not_establish.slice(0, 3).map((s: string) => `Does not establish: ${s}`)].map((s) => <li key={s}>{s}</li>)}</ul>
                    <p className="mt-2 [overflow-wrap:anywhere]"><SourceLine source={an.source} compact location={an.origin === "REGISTRY" ? an.source_location : null} /></p>
                  </div>
                );
              }) : <p className="mt-1.5 text-[13px] text-ink-2">{c.same_study ? "No between-arm analysis for this pair is stored. Any difference between the two arm-level values is descriptive." : "None: the results come from different trials, so no randomized between-arm analysis exists."}</p>}
            </Panel>
            <Panel className="min-w-0">
              <p className="text-[13.5px] font-semibold text-ink">Descriptive difference (A − B)</p>
              {difference ? (
                <div data-testid="descriptive-difference">
                  <p className="num mt-1 text-[22px] font-semibold text-ink">{fmt(difference.value)} <span className="text-[13px] font-normal text-ink-3">{differenceUnit(difference.unit) || "unit not recorded"}</span></p>
                  <p className="num mt-1 text-[12.5px] text-ink-2">
                    A minus B: {result.a.study_name} · {result.a.arm_label} ({withUnit(reportedText(result.a.value, result.a.value_text), result.a.unit)}) − {result.b.study_name} · {result.b.arm_label} ({withUnit(reportedText(result.b.value, result.b.value_text), result.b.unit)})
                  </p>
                  <p className="mt-1 text-[12.5px] text-ink-3">Confidence interval: not available.</p>
                  <p className="mt-1.5 text-[13px] text-ink-2">{difference.note}</p>
                </div>
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

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Panel className="min-w-0"><p className="mb-1.5 flex items-center gap-1.5 text-[13.5px] font-semibold text-ink"><Check aria-hidden size={15} className="text-[var(--ok)]" /> What may be said</p><ul className="list-disc space-y-1.5 pl-5 text-[13px] text-ink-2">{c.permitted_statements.map((s: string) => <li key={s}>{s}</li>)}</ul></Panel>
            <Panel className="min-w-0"><p className="mb-1.5 flex items-center gap-1.5 text-[13.5px] font-semibold text-ink"><Ban aria-hidden size={15} className="text-[var(--critical)]" /> What may not be said</p><ul className="list-disc space-y-1.5 pl-5 text-[13px] text-ink-2">{c.prohibited_statements.map((s: string) => <li key={s}>{s}</li>)}</ul></Panel>
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
