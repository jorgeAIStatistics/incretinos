"use client";

import { Download } from "lucide-react";
import Link from "next/link";
import { use, useState } from "react";
import { ChartFrame, ChartTable, IntervalChart, Reported } from "@/components/charts";
import { Badge, Callout, ErrorNote, KV, Loading, PageHeader, Panel, Qualifiers, Section, Select, SourceLine, VerificationBadge } from "@/components/ui";
import { fmt, phaseLabel, titleCase, useApi, diabetesLabel } from "@/lib/api";
import { byRoleThenDose, contrastDirection, contrastKindLabel, contrastUnit, estimandLabel, findingRow, findingSets, isRatio, reportedText, setLabel, weekLabel, withUnit } from "@/lib/rows";

// Qualifier codes whose content this table already prints in its own column of the same row (estimand, interval, verification).
const SHOWN_IN_OTHER_COLUMNS = ["ESTIMAND_EFFICACY", "ESTIMAND_TREATMENT_REGIMEN", "CI_NOT_REPORTED", "MACHINE_EXTRACTED", "QUOTE_ANCHORED", "PENDING_REVIEW"];

function ResultValue({ f }: { f: any }) {
  if (f.value == null && f.value_low != null) {
    return (
      <span className="num">
        <span className="whitespace-nowrap font-semibold text-ink">{fmt(f.value_low)} to {withUnit(fmt(f.value_high), f.unit)}</span>
        <span className="block text-[11.5px] text-ink-3">range across arms; no arm-level value disclosed</span>
      </span>
    );
  }
  if (f.value == null) return <span className="text-ink-3">not reported</span>;
  return (
    <>
      <Reported text={reportedText(f.value, f.value_text)} unit={f.unit} lo={f.ci_low} hi={f.ci_high} level={f.ci_level} />
      {f.ci_low == null || f.ci_high == null ? <span className="block text-[11.5px] text-ink-3">CI not reported</span> : null}
    </>
  );
}

function Dispersion({ f }: { f: any }) {
  if (f.dispersion_value == null) return <>—</>;
  // A standard error or standard deviation is in the unit of the value; other dispersion measures are printed as posted.
  const sameUnit = /standard (error|deviation)/i.test(f.dispersion_type ?? "");
  return <>{f.dispersion_type} {sameUnit ? withUnit(fmt(f.dispersion_value), f.unit) : fmt(f.dispersion_value)}</>;
}

function AnalysisEstimate({ a }: { a: any }) {
  const ratio = isRatio(a);
  return (
    <>
      <span className="text-[12px] text-ink-3">{a.param_type ?? "Parameter not stated"}</span>
      <br />
      {a.estimate == null ? (
        <span className="text-ink-3">estimate not reported</span>
      ) : (
        <Reported text={reportedText(a.estimate, a.estimate_text)} unit={contrastUnit(a)} lo={a.ci_low} hi={a.ci_high} level={a.ci_level} missingUnit={ratio ? "ratio, no unit" : "unit not recorded"} />
      )}
      {a.ci_low == null || a.ci_high == null ? <span className="block text-[11.5px] text-ink-3">CI not reported</span> : null}
      {a.unit ? <span className="block text-[11.5px] text-ink-3">Outcome unit as stored: {a.unit}</span> : null}
    </>
  );
}

function EndpointGroup({ g, open }: { g: any; open: boolean }) {
  const [expanded, setExpanded] = useState(open);
  const directions = g.analyses.map((a: any) => contrastDirection(a));
  const notes = Array.from(new Map<string, string>(directions.map((d: any) => [d.mark, d.basis])).entries()).sort(([x], [y]) => x.localeCompare(y));
  return (
    <div className="border-b border-hairline last:border-b-0" data-testid="endpoint-group" data-endpoint-id={g.endpoint_id}>
      <button type="button" onClick={() => setExpanded((e) => !e)} aria-expanded={expanded} className="flex w-full items-start justify-between gap-3 px-4 py-3 text-left hover:bg-sunken">
        <span className="min-w-0">
          <span className="block text-[13.5px] font-medium text-ink">{g.title}</span>
          <span className="mt-1 flex flex-wrap items-center gap-1.5 text-[12px] text-ink-3">
            <Badge tone={g.role === "PRIMARY" ? "info" : g.role === "EXPLORATORY" || g.role === "POST_HOC" ? "warn" : "muted"} icon={false}>{titleCase(g.role)}</Badge>
            <span>{g.origin === "REGISTRY" ? "Registry-posted" : "From publication, label or release"}</span>
            {g.timeframe ? <span>· {g.timeframe}</span> : null}
            <span>· {g.results.length} values · {g.analyses.length} between-arm analyses</span>
          </span>
        </span>
        <span aria-hidden className="mt-0.5 text-ink-3">{expanded ? "−" : "+"}</span>
      </button>
      {expanded ? (
        <div className="overflow-x-auto px-2 pb-3" data-testid="endpoint-results">
          {g.results.length ? (
            <table className="tbl">
              <thead>
                <tr><th>Arm</th><th className="r">Reported value</th><th>Dispersion</th><th className="r">n</th><th>Estimand and time point</th><th>Qualifiers</th><th>Source</th></tr>
              </thead>
              <tbody>
                {g.results.map((f: any) => (
                  <tr key={f.result_id} data-testid="result-row" data-result-id={f.result_id}>
                    <td className="font-medium">{f.arm_label}{f.class_title ? <span className="block text-[11.5px] font-normal text-ink-3">{f.class_title}</span> : null}</td>
                    <td className="r" data-testid="result-value"><ResultValue f={f} /></td>
                    <td className="num text-ink-3"><Dispersion f={f} /></td>
                    <td className="r num">{f.n_analyzed ?? <span className="text-ink-3">n/r</span>}</td>
                    <td>
                      <span className="whitespace-nowrap">{estimandLabel(f.estimand_label, f.estimand_basis)}</span>
                      <span className="block text-[11.5px] text-ink-3">{weekLabel(f.timepoint_weeks)}{f.baseline_definition ? ` · from ${f.baseline_definition}` : ""}</span>
                    </td>
                    {/* Every mandatory qualifier is printed: none is collapsed behind a counter. */}
                    <td className="min-w-[220px]" data-testid="result-qualifiers">
                      <Qualifiers labels={f.labels.filter((l: any) => !SHOWN_IN_OTHER_COLUMNS.includes(l.code))} />
                      <span className="mt-1 block"><VerificationBadge status={f.verification_status} /></span>
                    </td>
                    <td className="min-w-[180px]"><SourceLine source={f.source} compact /> <Link href={`/compare?a=${f.result_id}`} className="block text-[12px]">Compare →</Link></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
          {g.analyses.length ? (
            <>
              <table className="tbl mt-2">
                <thead>
                  <tr><th>Contrast and its direction</th><th>Estimate</th><th>p-value</th><th>Qualifiers</th><th>Source</th></tr>
                </thead>
                <tbody>
                  {g.analyses.map((a: any, i: number) => (
                    <tr key={a.id} data-testid="analysis-row">
                      <td className="font-medium">
                        {a.treatment_label} <span className="font-normal text-ink-3">vs</span> {a.comparator_label ?? "comparator not stated"}
                        <span className="block text-[12px] font-normal text-ink-2" data-testid="contrast-direction">
                          {contrastKindLabel(a)}: {directions[i].expression} <sup>{directions[i].mark}</sup>
                        </span>
                        <span className="block text-[11.5px] font-normal text-ink-3">{estimandLabel(a.estimand_label, a.estimand_basis)} · {weekLabel(a.timepoint_weeks)}</span>
                      </td>
                      <td data-testid="analysis-estimate"><AnalysisEstimate a={a} /></td>
                      <td className="num">{a.p_value_text ?? <span className="text-ink-3">not reported</span>}</td>
                      <td className="min-w-[220px]"><Qualifiers labels={a.labels} /></td>
                      <td className="min-w-[180px]"><SourceLine source={a.source} compact quote={a.locator_quote} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <ul className="mt-1.5 space-y-0.5 px-2.5 text-[11.5px] leading-snug text-ink-3" data-testid="contrast-notes">
                {notes.map(([mark, basis]) => (
                  <li key={mark}><sup>{mark}</sup> {basis}</li>
                ))}
              </ul>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * The headline chart. A trial usually reports percent weight change several times — under different estimands, from
 * different sources, at different time points. Those are different numbers answering different questions, so exactly one
 * coherent set is plotted, the caption is generated from that set, and the reader chooses which set to see.
 */
function WeightChart({ s }: { s: any }) {
  const [picked, setPicked] = useState<string | null>(null);
  const groups = s.endpoint_groups.filter((g: any) => g.category === "WEIGHT_PCT_CHANGE");
  const all = groups.flatMap((g: any) => g.results);
  const armLevel = all.filter((r: any) => r.scope === "ARM" && r.value != null && !r.is_pooled);
  const sets = findingSets(armLevel);
  if (!sets.length) {
    return (
      <Callout tone="muted" title="No arm-level weight results are on record for this trial">
        {s.has_results ? "The registry record posts other outcomes; see the endpoint list below." : "The registry has no posted results and no publication or release results have been curated. This is a public reporting gap, not evidence that no data exist."}
      </Callout>
    );
  }
  const primaryPub = s.publications.find((p: any) => p.relation === "PRIMARY_RESULTS");
  const fallback = sets.find((set) => primaryPub && set.first.source?.id === primaryPub.source_id) ?? sets[0];
  const current = sets.find((set) => set.key === picked) ?? fallback;
  const first = current.first;
  const group = groups.find((g: any) => g.endpoint_id === first.endpoint_id);
  const labels = sets.map((set) => setLabel(set));
  const options = sets.map((set, i) => {
    const repeated = labels.filter((l) => l === labels[i]).length > 1;
    return { value: set.key, label: `${labels[i]}${repeated ? ` — ${set.first.endpoint_title}` : ""} (${set.findings.length} ${set.findings.length === 1 ? "arm" : "arms"})` };
  });
  const rows = byRoleThenDose(
    current.findings.map((f: any) => {
      const row = findingRow(f, { drugs: s.drugs, arms: s.arms });
      return { row, group: row.group, dose: f.dose_amount as number | null };
    }),
    (x) => x.dose,
  ).map((x) => x.row);
  const notPlotted = all.length - armLevel.length;
  const axis = group?.category_label ? `${group.category_label.charAt(0).toUpperCase()}${group.category_label.slice(1)}` : first.endpoint_title;
  return (
    <div data-testid="hero-chart" className="min-w-0">
      <ChartFrame
        title={first.endpoint_title}
        subtitle={
          <>
            <span data-testid="hero-caption">
              {rows.length} arm-level {rows.length === 1 ? "value" : "values"} from one result set: <span className="font-medium text-ink-2">{setLabel(current)}</span>
              {first.baseline_definition ? "" : " · baseline definition not recorded for this source"} · values in {current.unit || "a unit the source does not record"}.
            </span>{" "}
            The treatment effect is the randomized contrast with the control arm, not the gap between two bars.
          </>
        }
        controls={sets.length > 1 ? <Select label={`Result set (${sets.length} on record — one is plotted at a time, never mixed)`} value={current.key} onChange={setPicked} options={options} /> : undefined}
        table={<ChartTable rows={rows} unit={current.unit} valueHeader={`Change${current.unit ? ` (${current.unit})` : ""}`} />}
        footnote={
          <span data-testid="hero-source">
            Source of every plotted value: <SourceLine source={first.source} /> · endpoint role: {titleCase(first.role)}
            {notPlotted > 0 ? <span className="block">{notPlotted} further {notPlotted === 1 ? "row" : "rows"} for this endpoint (pooled arms, ranges across arms or rows without a value) {notPlotted === 1 ? "is" : "are"} listed in the results below, not plotted.</span> : null}
          </span>
        }
      >
        <IntervalChart rows={rows} unit={current.unit} axisLabel={axis} />
      </ChartFrame>
    </div>
  );
}

export default function TrialPage({ params }: { params: Promise<{ nct: string }> }) {
  const { nct } = use(params);
  const { data: s, error, loading } = useApi(`/studies/${nct}`);
  if (error) return <ErrorNote message={error} />;
  if (loading || !s) return <Loading rows={9} />;
  const primaryPub = s.publications.find((p: any) => p.relation === "PRIMARY_RESULTS");
  return (
    <>
      <PageHeader eyebrow={`Clinical trial · ${s.nct_id}`} title={s.display_name} lede={s.brief_title}>
        <a href={`/api/reports/study/${s.nct_id}`} className="inline-flex h-9 items-center gap-1.5 rounded-md border border-hairline-strong bg-surface px-3 text-[13.5px] font-medium text-ink hover:bg-sunken hover:no-underline"><Download aria-hidden size={14} /> Evidence report (.md)</a>
      </PageHeader>
      <div className="mb-7 grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)]">
        <Panel className="min-w-0">
          <KV rows={[
            ["Phase / status", `${phaseLabel(s.phase)} · ${titleCase(s.overall_status)}`],
            ["Design", `${titleCase(s.allocation)} · ${titleCase(s.masking)} masking · ${titleCase(s.intervention_model)}`],
            ["Enrollment", `${s.enrollment?.toLocaleString() ?? "—"} (${(s.enrollment_type ?? "").toLowerCase()})`],
            ["Population", <span key="p">{diabetesLabel(s.population?.diabetes_status)}<span className="block text-[12px] text-ink-3">{s.population?.diabetes_basis}</span></span>],
            ["Primary completion", `${s.primary_completion_date ?? "—"} (${(s.primary_completion_type ?? "").toLowerCase()})`],
            ["Registry results", s.has_results ? `Posted ${s.results_first_posted ?? ""}` : "Not posted"],
            ["Primary publication", primaryPub ? <span key="pub">{primaryPub.journal} {primaryPub.year} · <a href={`https://pubmed.ncbi.nlm.nih.gov/${primaryPub.pmid}/`} target="_blank" rel="noopener noreferrer">PMID {primaryPub.pmid}</a><span className="block text-[12px] text-ink-3">Linked by: {primaryPub.link_bases.map((b: string) => titleCase(b)).join(", ")} · confidence {primaryPub.link_confidence.toLowerCase()}</span></span> : "None linked"],
            ["Registry record", <SourceLine key="src" source={s.source} compact />],
          ]} />
        </Panel>
        <WeightChart s={s} />
      </div>

      {s.estimands.length ? (
        <Section title="Estimands on record" hint="How each analysis handled participants who stopped treatment.">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {s.estimands.map((e: any) => (
              <Panel key={e.id} className="min-w-0">
                <div className="flex flex-wrap items-center gap-2"><span className="text-[14px] font-semibold">{estimandLabel(e.label)}</span><Badge tone={e.basis === "STATED_BY_SOURCE" ? "ok" : e.basis.includes("INFERRED") ? "warn" : "muted"}>{titleCase(e.basis)}</Badge></div>
                <p className="mt-1.5 text-[13px] text-ink-2">{e.explainer}</p>
                {e.quote ? <p className="mt-1.5 text-[12.5px] text-ink-3">Basis in source: “…{e.quote}…”</p> : null}
                <p className="mt-1.5"><SourceLine source={e.source} compact /></p>
              </Panel>
            ))}
          </div>
        </Section>
      ) : null}

      <Section title="Reported results" hint="Grouped by endpoint. Registry-posted and publication-reported values are kept as separate rows even when they describe the same arm. Every value is shown with its unit, as reported.">
        <div className="rounded-lg border border-hairline bg-surface">
          {s.endpoint_groups.length ? s.endpoint_groups.map((g: any, i: number) => <EndpointGroup key={g.endpoint_id} g={g} open={i === 0} />) : <p className="px-4 py-3 text-[13px] text-ink-3">No results stored.</p>}
        </div>
      </Section>

      <div className="grid grid-cols-1 gap-x-8 lg:grid-cols-2">
        <Section title="Arms as registered">
          <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
            <table className="tbl"><thead><tr><th>Arm</th><th>Type</th><th className="r">Started</th></tr></thead>
              <tbody>{s.arms.filter((a: any) => a.origin === "REGISTRY" && a.arm_type !== "RESULTS_GROUP").map((a: any) => (<tr key={a.id}><td className="font-medium">{a.label}{a.formulation_key ? <span className="block text-[11.5px] font-normal text-ink-3">Formulation: {a.formulation_key}</span> : null}</td><td>{titleCase(a.arm_type)}</td><td className="r num">{a.n_randomized ?? "—"}</td></tr>))}</tbody>
            </table>
          </div>
        </Section>
        <Section title={`Linked publications (${s.publications.length})`} hint="Each link lists the independent identifiers that support it.">
          <ul className="max-h-[360px] divide-y divide-hairline overflow-auto rounded-lg border border-hairline bg-surface">
            {s.publications.map((p: any) => (
              <li key={p.pmid} className="px-4 py-2.5 text-[13px]">
                {p.relation === "PRIMARY_RESULTS" ? <Badge tone="info" icon={false}>Primary results</Badge> : null} <a href={`https://pubmed.ncbi.nlm.nih.gov/${p.pmid}/`} target="_blank" rel="noopener noreferrer" className="font-medium">{p.title}</a>
                <span className="block text-[12px] text-ink-3">{p.journal} {p.year} · PMID {p.pmid} · {p.link_bases.map((b: string) => titleCase(b)).join(" + ")}</span>
              </li>
            ))}
            {s.publications.length === 0 ? <li className="px-4 py-3 text-[13px] text-ink-3">No publication is linked to this registration in the registry or in PubMed.</li> : null}
          </ul>
        </Section>
      </div>
      <Section title="Registered protocol endpoints">
        <div className="max-h-[320px] overflow-auto rounded-lg border border-hairline bg-surface" tabIndex={0} role="region" aria-label="Registered protocol endpoints">
          <table className="tbl"><thead><tr><th>Role</th><th>Outcome</th><th>Time frame</th></tr></thead>
            <tbody>{s.protocol_endpoints.map((e: any, i: number) => (<tr key={i}><td>{titleCase(e.role)}</td><td>{e.title}</td><td className="text-ink-3">{e.timeframe}</td></tr>))}</tbody>
          </table>
        </div>
      </Section>
    </>
  );
}
