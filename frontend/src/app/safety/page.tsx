"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { type ChartRow, type Facet, ChartFrame, ChartTable, FacetedChart, ReportsTrend } from "@/components/charts";
import { Badge, Button, Callout, ErrorNote, Loading, PageHeader, Panel, Section, Select, SourceLine } from "@/components/ui";
import { apiPost, fmt, titleCase, useApi } from "@/lib/api";
import { useRows } from "@/lib/paging";
import { armGroup, byRoleThenDose } from "@/lib/rows";

// Incidence within the trial is the natural first view; the other measure kinds follow in the order the API lists them.
const PREFERRED_KIND = "PARTICIPANTS_WITH_EVENT";

/** Risk difference and ratio between two arms the reader chooses. No reference arm is assumed unless the data name exactly one placebo arm. */
function RiskContrast({ rows, table }: { rows: any[]; table: string | null }) {
  const eligible = rows.filter((r) => r.n_affected != null && r.n_at_risk);
  const placebo = eligible.filter((r) => r.arm_role === "PLACEBO");
  const studyDrug = byRoleThenDose(eligible.filter((r) => r.arm_role === "STUDY_DRUG").map((r) => ({ r, group: "active" as const })), (x) => x.r.arm_dose_amount).map((x) => x.r);
  const defaultB = placebo.length === 1 ? String(placebo[0].id) : "";
  const defaultA = defaultB && studyDrug.length ? String(studyDrug[studyDrug.length - 1].id) : "";
  const [pickedA, setA] = useState<string | null>(null);
  const [pickedB, setB] = useState<string | null>(null);
  const [contrast, setContrast] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const a = pickedA ?? defaultA;
  const b = pickedB ?? defaultB;
  const armA = eligible.find((r) => String(r.id) === a);
  const armB = eligible.find((r) => String(r.id) === b);
  if (eligible.length < 2) return null;
  const options = eligible.map((r) => ({ value: String(r.id), label: `${r.arm_label} — ${r.arm_role_label ?? "role not classified"} (${r.n_affected} of ${r.n_at_risk})` }));
  const run = () => {
    setError(null);
    apiPost("/stats/risk-contrast", { ae_a: Number(a), ae_b: Number(b) }).then(setContrast).catch((e) => { setContrast(null); setError(e.message); });
  };
  const pick = (set: (v: string) => void) => (v: string) => { set(v); setContrast(null); setError(null); };
  const level = (c: any) => `${fmt((c.ci_level ?? 0.95) * 100)}% CI`;
  return (
    <Panel className="mt-3 min-w-0">
      <div data-testid="risk-contrast">
        <p className="text-[13.5px] font-semibold text-ink">Risk contrast between two arms of this trial</p>
        {table ? <p className="mt-0.5 text-[12.5px] text-ink-2">Arms of one table only: {table}</p> : null}
        <p className="mt-0.5 text-[12.5px] text-ink-3">
          Arm B is the reference: the difference is A − B and the ratio is A ÷ B.{" "}
          {placebo.length === 1 ? "The placebo arm is preselected as the reference." : placebo.length === 0 ? "No arm in this table is recorded as a placebo arm, so no reference arm is preselected." : "More than one placebo arm is recorded, so no reference arm is preselected."}
        </p>
        <div className="mt-2 grid grid-cols-1 gap-3 md:grid-cols-2">
          <Select label="Arm A" value={a} onChange={pick(setA)} options={[{ value: "", label: "Select arm A…" }, ...options]} />
          <Select label="Arm B (reference)" value={b} onChange={pick(setB)} options={[{ value: "", label: "Select the reference arm…" }, ...options]} />
        </div>
        <div className="mt-3"><Button variant="ghost" onClick={run} disabled={!armA || !armB || a === b}>Calculate risk difference (A − B) and risk ratio (A ÷ B)</Button></div>
        {error ? <div className="mt-2"><ErrorNote message={error} /></div> : null}
        {contrast && armA && armB ? (
          <div className="mt-3 border-t border-hairline pt-3" data-testid="risk-contrast-result">
            <p className="text-[12px]"><Badge tone="warn" icon={false}>Inference</Badge> <span className="text-ink-3">Calculated here from reported counts; unadjusted and post hoc.</span></p>
            <p className="num mt-1.5 text-[14px] text-ink">
              Risk difference, {armA.arm_label} − {armB.arm_label}: <b>{fmt(contrast.risk_difference.estimate * 100, 1)} percentage points</b> ({level(contrast.risk_difference)} {fmt(contrast.risk_difference.ci_low * 100, 1)} to {fmt(contrast.risk_difference.ci_high * 100, 1)} percentage points)
            </p>
            <p className="num mt-1 text-[14px] text-ink">
              Risk ratio, {armA.arm_label} ÷ {armB.arm_label}: <b>{contrast.risk_ratio.estimate != null ? fmt(contrast.risk_ratio.estimate, 2) : "not estimable"}</b>
              {contrast.risk_ratio.estimate != null ? ` (${level(contrast.risk_ratio)} ${fmt(contrast.risk_ratio.ci_low, 2)} to ${fmt(contrast.risk_ratio.ci_high, 2)}; a ratio has no unit)` : ""}
            </p>
            <p className="mt-1 text-[12.5px] text-ink-3">{contrast.risk_difference.method}; {contrast.risk_ratio.method}. {contrast.risk_difference.interpretation}</p>
          </div>
        ) : null}
      </div>
    </Panel>
  );
}

function TrialEvents({ drug }: { drug: string }) {
  // One light request tells which trials hold which kinds of adverse-event rows, so the page opens on data rather than on an empty chart.
  const { data: summary, error: summaryError } = useApi(`/safety/trial-ae?drug=${drug}&limit=1`);
  const coverage: any[] = summary?.coverage ?? [];
  const incidence = (c: any) => c.by_measure_kind?.[PREFERRED_KIND]?.arm_level_rows ?? 0;
  const [pickedNct, setNct] = useState<string | null>(null);
  const [pickedKind, setKind] = useState<string | null>(null);
  const [pickedTerm, setTerm] = useState<string | null>(null);
  const best = coverage.reduce<any>((top, c) => (!top || incidence(c) > incidence(top) || (incidence(c) === incidence(top) && c.rows > top.rows) ? c : top), null);
  const trial = coverage.find((c) => c.nct_id === pickedNct) ?? best;
  const nct: string | null = trial?.nct_id ?? null;
  const kinds: [string, any][] = trial ? Object.entries<any>(trial.by_measure_kind).sort(([x], [y]) => Number(y === PREFERRED_KIND) - Number(x === PREFERRED_KIND)) : [];
  const kind = kinds.some(([k]) => k === pickedKind) ? pickedKind : (kinds[0]?.[0] ?? null);
  const { data: terms } = useApi(nct ? `/safety/terms?nct=${nct}` : null);
  // A term can be listed twice (once in the serious-event table, once in the other-event table): it is offered once and drawn as two panels.
  const termOptions: any[] = terms?.study?.nct_id === nct ? terms.terms.filter((t: any, i: number, all: any[]) => t.measure_kind === kind && all.findIndex((x) => x.term === t.term && x.measure_kind === kind) === i) : [];
  const term: string | null = termOptions.some((t) => t.term === pickedTerm) ? pickedTerm : (termOptions[0]?.term ?? null);
  const { data, error, loading } = useApi(nct && kind && term ? `/safety/trial-ae?nct=${nct}&measure_kind=${kind}&term=${encodeURIComponent(term)}` : null);
  const mine = (data?.rows ?? []).filter((r: any) => r.term === term && r.nct_id === nct && r.measure_kind === kind);

  if (summaryError) return <ErrorNote message={summaryError} />;
  if (!summary) return <Loading rows={4} />;
  if (!coverage.length) return <Callout tone="muted" title="No trial adverse-event tables are on record for this agent.">Registry-posted results or curated publications are needed for within-trial incidence.</Callout>;

  const armRow = (r: any): ChartRow & { dose: number | null } => {
    const calculated = r.percent_basis ? r.percent_basis === "CALCULATED_FROM_COUNTS" : r.percent_reported == null && r.percent_calculated != null;
    const counts = r.n_affected != null && r.n_at_risk ? `${r.n_affected} of ${r.n_at_risk} participants` : null;
    return {
      key: String(r.id),
      label: r.arm_label,
      meta: [r.arm_role === "UNCLASSIFIED" && r.arm_role_basis ? r.arm_role_basis : null].filter(Boolean) as string[],
      value: r.percent_reported ?? r.percent_calculated,
      unit: "%",
      group: armGroup(r),
      calculated,
      valueNote: calculated ? `calculated from ${counts ?? "reported counts"}` : `percentage as reported${counts ? `; ${counts}` : "; counts not given"}`,
      tip: [r.measure_kind_label, r.arm_role_basis, `${r.source?.tier_label ?? titleCase(r.tier)} · ${titleCase(r.verification_status)}`, r.notes].filter(Boolean),
      dose: r.arm_dose_amount ?? null,
    };
  };
  const rangeRow = (r: any): ChartRow => ({
    key: String(r.id),
    label: r.arm_label,
    value: null,
    unit: "%",
    range: [r.percent_low, r.percent_high],
    group: "other",
    valueNote: "range across arms; not attributable to any single arm",
    tip: [r.measure_kind_label, `${r.source?.tier_label ?? titleCase(r.tier)} · ${titleCase(r.verification_status)}`, r.notes].filter(Boolean),
  });
  // Rows from different sources, observation periods or event tables (serious / other) are not drawn against each other: one panel each.
  const panels = new Map<string, { first: any; rows: any[] }>();
  for (const r of mine) {
    const key = `${r.source?.id ?? "none"}|${r.period ?? ""}|${r.is_serious ? "serious" : "other"}`;
    const panel = panels.get(key);
    if (panel) panel.rows.push(r);
    else panels.set(key, { first: r, rows: [r] });
  }
  const panelLabel = (panel: { first: any; rows: any[] }): string =>
    `${kind === PREFERRED_KIND ? `${panel.first.is_serious ? "Reported as a serious adverse event" : "Not reported as serious (other adverse-event table)"} · ` : ""}${panel.first.source?.tier_label ?? titleCase(panel.first.tier)} · observation period: ${panel.first.period ?? "not stated"}`;
  const facets: Facet[] = [...panels.entries()].map(([key, panel]) => {
    const arms = byRoleThenDose(panel.rows.filter((r) => r.scope === "ARM").map(armRow), (row) => row.dose).map(({ dose: _dose, ...row }) => row as ChartRow);
    return {
      key,
      unit: "%",
      axisLabel: "Percent of participants in the arm",
      rows: [...arms, ...panel.rows.filter((r) => r.scope !== "ARM").map(rangeRow)],
      caption: <>{panelLabel(panel)} · {panel.rows.length} {panel.rows.length === 1 ? "row" : "rows"}</>,
      footnote: <span className="[overflow-wrap:anywhere]">Source: <SourceLine source={panel.first.source} quote={panel.first.locator_quote} location={panel.first.origin === "REGISTRY" ? panel.first.source_location : null} /></span>,
    };
  });
  const allRows = facets.flatMap((f) => f.rows);
  const kindLabel = kinds.find(([k]) => k === kind)?.[1]?.label ?? (kind ? titleCase(kind) : "");
  const calculatedCount = allRows.filter((r) => r.calculated).length;
  return (
    <div data-testid="trial-events">
      <div className="mb-3 grid grid-cols-1 gap-3 md:grid-cols-3">
        <Select
          label="Trial"
          value={nct ?? ""}
          onChange={(v) => { setNct(v); setKind(null); setTerm(null); }}
          options={coverage.map((c) => ({ value: c.nct_id, label: `${c.display_name} — ${incidence(c) ? `${c.by_measure_kind[PREFERRED_KIND].arm_level_terms} event terms` : "no incidence table"}` }))}
        />
        <Select label="What is being counted" value={kind ?? ""} onChange={(v) => { setKind(v); setTerm(null); }} options={kinds.map(([value, entry]) => ({ value, label: entry.label }))} />
        <Select label="Event / term" value={term ?? ""} onChange={setTerm} options={termOptions.length ? termOptions.map((t) => ({ value: t.term, label: t.term })) : [{ value: "", label: terms ? "No terms of this kind" : "Loading terms…" }]} />
      </div>
      {error ? <ErrorNote message={error} /> : null}
      <ChartFrame
        title={term ? `${term} — ${kindLabel}` : kindLabel}
        subtitle={
          <span data-testid="ae-caption">
            Denominator: participants at risk in each randomized arm of this trial. One measure kind per chart.{" "}
            {calculatedCount ? `${calculatedCount === allRows.length ? "Every percentage" : `${calculatedCount} of ${allRows.length} percentages`} shown here was calculated from the reported counts (marked †); the source reports counts, not percentages.` : allRows.length ? "Percentages are shown as the source reports them." : ""}
          </span>
        }
        table={<ChartTable rows={allRows} unit="%" valueHeader="% of participants" />}
      >
        {allRows.length ? <FacetedChart facets={facets} note={`Shown as ${facets.length} separate panels: the rows come from different sources, observation periods or event tables (serious and other) and are not added together or compared with one another.`} /> : <p className="py-5 text-[13px] text-ink-3">{loading || (term === null && !terms) ? "Loading…" : "Not reported for this trial."}</p>}
      </ChartFrame>
      {/* A contrast is offered within one table only: arms of different sources, periods or event tables are never contrasted. */}
      {[...panels.entries()].map(([key, panel]) => (
        <RiskContrast key={`${nct}|${kind}|${term}|${key}`} rows={panel.rows.filter((r) => r.scope === "ARM")} table={panels.size > 1 ? panelLabel(panel) : null} />
      ))}
    </div>
  );
}

/** Label text in full. It is split into paragraphs at its own headings; no character is dropped and nothing is cut. */
function LabelText({ title, text }: { title: string; text: string }) {
  return (
    <>
      <span className="font-semibold">{title}. </span>
      {text.split(/(?=WARNING:)/).map((part, i) => (
        <span key={i} className={i ? "mt-1.5 block" : undefined}>{part}</span>
      ))}
    </>
  );
}

function Safety() {
  const params = useSearchParams();
  const router = useRouter();
  const { data: drugs, error: drugsError } = useRows<any>("/drugs");
  const tracked = (drugs ?? []).filter((d) => d.tier_group !== "COMPARATOR");
  const drug = params.get("drug") ?? tracked[0]?.slug ?? null;
  const record = (drugs ?? []).find((d) => d.slug === drug);
  const spontaneous = useApi(drug ? `/safety/spontaneous/${drug}` : null);
  const labelRequest = useApi(drug ? `/safety/labels/${drug}` : null);
  // A response for the previously selected molecule is never shown under the new one.
  const sp = spontaneous.data?.drug === drug ? spontaneous.data : null;
  const labels = labelRequest.data?.drug === drug ? labelRequest.data : null;
  const options = tracked.map((d) => ({ value: d.slug, label: d.name }));
  const unavailableNote = sp?.disproportionality_screen?.find((r: any) => r.drug_reports == null)?.note;
  return (
    <>
      <PageHeader eyebrow="Safety intelligence" title="Trial incidence and spontaneous reports are different kinds of evidence" lede="Adverse events in a randomized arm have a denominator. Spontaneous reports do not. The two are shown in separate sections and never on one scale.">
        <Select
          label="Molecule"
          value={drug ?? ""}
          onChange={(v) => router.replace(`/safety?drug=${v}`)}
          options={!drugs ? [{ value: drug ?? "", label: "Loading molecules…" }] : drug && !options.some((o) => o.value === drug) ? [{ value: drug, label: record ? `${record.name} (comparator)` : `${drug} (not on record)` }, ...options] : options}
          className="w-60"
        />
      </PageHeader>
      {drugsError ? <ErrorNote message={drugsError} /> : null}
      {spontaneous.error ? <ErrorNote message={spontaneous.error} /> : null}
      {!drug ? <Loading rows={6} /> : (
        <>
          <Section title="Regulatory warnings and label changes" hint="From FDA-approved prescribing information; changes are detected by comparing stored label versions.">
            {labelRequest.error ? <ErrorNote message={labelRequest.error} /> : !labels ? <Loading rows={3} /> : labels.labels.length ? (
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                {labels.labels.map((l: any) => (
                  <Panel key={l.brand} className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2"><span className="text-[14.5px] font-semibold">{l.brand}</span><span className="text-[12px] text-ink-3">label version {l.label_version} · effective {l.effective_time}</span></div>
                    {l.boxed_warning ? (
                      <div className="mt-2 rounded border border-ink bg-surface px-3 py-2 text-[12.5px] leading-snug text-ink" data-testid="boxed-warning"><LabelText title="Boxed warning" text={l.boxed_warning} /></div>
                    ) : <p className="mt-2 text-[12.5px] text-ink-3">No boxed warning in this label.</p>}
                    {l.recent_major_changes ? (
                      <div className="mt-2 text-[12.5px] text-ink-2" data-testid="recent-major-changes">
                        <p className="font-semibold text-ink">Recent major changes</p>
                        <ul className="mt-0.5 list-disc space-y-0.5 pl-5">{String(l.recent_major_changes).split(/(?<=\b\d{1,2}\/\d{4})\s+/).map((item, i) => <li key={i}>{item}</li>)}</ul>
                      </div>
                    ) : null}
                    {l.contraindications ? <details className="mt-2 text-[12.5px] text-ink-2"><summary className="cursor-pointer font-medium text-accent">Contraindications (full label text)</summary><p className="mt-1 leading-snug">{l.contraindications}</p></details> : null}
                    {l.warnings_and_cautions ? <details className="mt-1 text-[12.5px] text-ink-2"><summary className="cursor-pointer font-medium text-accent">Warnings and precautions (full label text)</summary><p className="mt-1 leading-snug">{l.warnings_and_cautions}</p></details> : null}
                    <p className="mt-2"><SourceLine source={l.source} compact /></p>
                  </Panel>
                ))}
              </div>
            ) : (
              <Callout tone="muted" title="No FDA labeling is on record for this agent.">
                {record?.regulatory_status === "INVESTIGATIONAL" ? "It is recorded as investigational; there are no label warnings to show." : `No label has been ingested for it${record?.regulatory_status ? ` (regulatory status on record: ${titleCase(record.regulatory_status)})` : ""}.`} That is not a statement about its safety.
              </Callout>
            )}
          </Section>

          <Section title="Adverse events in randomized trials" hint="Events over participants at risk, by arm, within one trial.">
            <TrialEvents key={drug} drug={drug} />
          </Section>

          <Section id="spontaneous" title="Spontaneous reports (FAERS)" hint="Counts of voluntary reports that mention the drug.">
            {!sp ? (spontaneous.error ? null : <Loading rows={4} />) : (
              <>
                <Callout tone="warn" title="Why these numbers are not incidence — and not evidence of causation">
                  <ul className="mt-1 list-disc space-y-1 pl-5">{(sp.caveats ?? []).map((c: string) => <li key={c}>{c}</li>)}</ul>
                </Callout>
                {!sp.available ? <div className="mt-3"><Callout tone="muted" title="No spontaneous-report series is stored for this agent.">{sp.explanation}</Callout></div> : null}
                {sp.available ? (
                  <div className="mt-4 grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
                    <div className="min-w-0">
                      <ChartFrame title="Reports received per quarter" subtitle={`${sp.total_reports.toLocaleString()} reports in total · FAERS data through ${sp.database_last_updated}. The axis counts reports; it rises with use, publicity and time on market.`} footnote={<>Unit: {sp.unit}. Source: <SourceLine source={sp.source} compact /></>}
                        table={<table className="tbl"><thead><tr><th>Quarter</th><th className="r">Reports received</th></tr></thead><tbody>{sp.reports_by_quarter.map((q: any) => <tr key={q.period}><td>{q.period}</td><td className="r num">{q.count.toLocaleString()}</td></tr>)}</tbody></table>}>
                        <ReportsTrend data={sp.reports_by_quarter} />
                      </ChartFrame>
                      <Panel className="mt-4" pad={false}>
                        <p className="px-4 pt-3 text-[13.5px] font-semibold">Most frequently reported terms ({sp.top_reactions.length} listed)</p>
                        <div className="max-h-[300px] overflow-auto" tabIndex={0} role="region" aria-label="Most frequently reported terms"><table className="tbl"><thead><tr><th>MedDRA preferred term</th><th className="r">Reports mentioning term</th></tr></thead><tbody>{sp.top_reactions.map((r: any) => <tr key={r.term}><td>{titleCase(r.term)}</td><td className="r num">{r.count.toLocaleString()}</td></tr>)}</tbody></table></div>
                      </Panel>
                    </div>
                    <Panel pad={false} className="min-w-0">
                      <div className="px-4 pt-3"><p className="text-[13.5px] font-semibold">Disproportionality screen</p><p className="mt-0.5 text-[12.5px] text-ink-3">{sp.disproportionality_caveats[0]}</p></div>
                      <div className="mt-2 max-h-[560px] overflow-auto" tabIndex={0} role="region" aria-label="Disproportionality screen"><table className="tbl"><thead><tr><th>Term</th><th className="r">Reports (drug)</th><th className="r">PRR (ratio)</th><th className="r">ROR (ratio)</th></tr></thead>
                        <tbody>{sp.disproportionality_screen.map((r: any) => (
                          <tr key={r.term}>
                            <td>{titleCase(r.term)}</td>
                            <td className="r num">{r.drug_reports != null ? r.drug_reports.toLocaleString() : <span className="text-ink-3">unavailable</span>}</td>
                            {/* Estimate and interval sit on two lines that never break inside a number. */}
                            <td className="r num">{r.prr != null ? <><span className="block whitespace-nowrap">{fmt(r.prr, 2)}</span><span className="block whitespace-nowrap text-[11.5px] text-ink-3">95% CI {fmt(r.prr_ci[0], 2)} to {fmt(r.prr_ci[1], 2)}</span></> : <span className="text-ink-3">not calculated</span>}</td>
                            <td className="r num">{r.ror != null ? <><span className="block whitespace-nowrap">{fmt(r.ror, 2)}</span><span className="block whitespace-nowrap text-[11.5px] text-ink-3">95% CI {fmt(r.ror_ci[0], 2)} to {fmt(r.ror_ci[1], 2)}</span></> : <span className="text-ink-3">not calculated</span>}</td>
                          </tr>
                        ))}</tbody></table></div>
                      <p className="border-t border-hairline px-4 py-2.5 text-[12px] text-ink-3">{sp.disproportionality_caveats[2]}{unavailableNote ? ` “Unavailable”: ${unavailableNote}` : ""}</p>
                    </Panel>
                  </div>
                ) : null}
              </>
            )}
          </Section>
        </>
      )}
    </>
  );
}

export default function Page() {
  return <Suspense fallback={<Loading />}><Safety /></Suspense>;
}
