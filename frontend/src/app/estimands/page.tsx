"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { ChartFrame, ChartTable, IntervalChart } from "@/components/charts";
import { Badge, Callout, ErrorNote, Loading, PageHeader, Panel, Qualifiers, Section, Select, SourceLine, VerificationBadge } from "@/components/ui";
import { fmt, titleCase, useApi } from "@/lib/api";
import { estimandLabel, findingRow } from "@/lib/rows";

const COLS = ["TREATMENT_REGIMEN", "EFFICACY", "NOT_STATED"];

function Cell({ items, onPick, picked }: { items: any[] | undefined; onPick: (id: number) => void; picked: number | null }) {
  if (!items?.length) return <span className="text-ink-3">not reported</span>;
  return (
    <span className="flex flex-col gap-1">
      {items.map((f) => (
        <button key={f.result_id} type="button" onClick={() => onPick(f.result_id)} aria-pressed={picked === f.result_id}
          className={`flex items-center gap-2 rounded px-1.5 py-0.5 text-left hover:bg-accent-soft ${picked === f.result_id ? "bg-accent-soft ring-1 ring-accent" : ""}`}>
          <span className="num w-14 text-right font-semibold text-ink">{fmt(f.value)}{/percent|%/i.test(f.unit ?? "") ? "%" : ""}</span>
          <span className="text-[11.5px] text-ink-3">{f.source?.tier_label}{f.estimand_basis?.includes("INFERRED") ? " · inferred" : ""}</span>
        </button>
      ))}
    </span>
  );
}

function Inspector() {
  const params = useSearchParams();
  const router = useRouter();
  const study = params.get("study") ?? "NCT05869903";
  const { data: studies } = useApi<any[]>("/studies");
  const { data, error, loading } = useApi(`/estimands?study=${study}`);
  const [picked, setPicked] = useState<number | null>(null);
  const { data: detail } = useApi(picked ? `/findings/${picked}` : null);
  if (error) return <ErrorNote message={error} />;
  const weight = (data?.rows ?? []).filter((r: any) => r.category === "WEIGHT_PCT_CHANGE");
  const mainWeeks = weight.length ? weight.reduce((best: any, r: any) => (Object.keys(r.by_estimand).length > Object.keys(best.by_estimand).length ? r : best), weight[0]).timepoint_weeks : null;
  const chartRows = weight.filter((r: any) => r.timepoint_weeks === mainWeeks).flatMap((r: any) =>
    ["TREATMENT_REGIMEN", "EFFICACY"].filter((e) => r.by_estimand[e]?.length).map((e) => {
      const f = r.by_estimand[e].find((x: any) => x.tier === "PEER_REVIEWED") ?? r.by_estimand[e][0];
      return { ...findingRow(f), key: `${f.result_id}`, label: r.arm, sub: `${estimandLabel(e, f.estimand_basis)} · ${f.source?.tier_label}` };
    }));
  return (
    <>
      <PageHeader eyebrow="Evidence and estimand inspector" title="One arm, two estimands, two different numbers" lede="The efficacy estimand asks what would happen if everyone stayed on treatment; the treatment-regimen estimand asks what happened regardless. They are shown in separate columns and never merged." />
      <div className="mb-5 flex flex-wrap items-end gap-3">
        <Select label="Trial" value={study} onChange={(v) => { setPicked(null); router.replace(`/estimands?study=${v}`); }} options={(studies ?? []).filter((s) => s.n_results > 0).map((s) => ({ value: s.nct_id, label: s.display_name }))} className="w-72" />
      </div>
      {loading || !data ? <Loading rows={6} /> : (
        <>
          <div className="mb-6 grid gap-3 md:grid-cols-2">
            {data.estimands.map((e: any, i: number) => (
              <Panel key={i}>
                <div className="flex flex-wrap items-center gap-2"><span className="text-[14px] font-semibold">{estimandLabel(e.label)}</span><Badge tone={e.basis === "STATED_BY_SOURCE" ? "ok" : e.basis.includes("INFERRED") ? "warn" : "muted"} title={e.basis.includes("INFERRED") ? "The registry does not name the estimand; this label was inferred from its analysis-population text." : undefined}>{titleCase(e.basis)}</Badge></div>
                <p className="mt-1.5 text-[13px] text-ink-2">{e.explainer}</p>
                {e.strategies.map((s: any) => <p key={s.event} className="mt-1 text-[12.5px] text-ink-3">{titleCase(s.event)} → {titleCase(s.strategy)} strategy</p>)}
                {e.quote ? <p className="mt-1.5 text-[12.5px] text-ink-3">Source text: “…{e.quote}…”</p> : null}
              </Panel>
            ))}
          </div>

          {chartRows.length ? (
            <div className="mb-7">
              <ChartFrame title={`Percent change in body weight at week ${fmt(mainWeeks, 0)}, by estimand`} subtitle="Each arm appears once per estimand. The gap between the two rows of one arm reflects the handling of treatment discontinuation — it is not a treatment effect." table={<ChartTable rows={chartRows} unit="%" valueHeader="Change, %" />} footnote={data.rule}>
                <IntervalChart rows={chartRows} unit="%" mark="dot" axisLabel="Percent change in body weight" />
              </ChartFrame>
            </div>
          ) : <Callout tone="muted" title="No weight-change results under a named estimand are stored for this trial.">Select another trial, or see its full results in the trial atlas.</Callout>}

          <Section title="Reported values by estimand" hint="Select any value to inspect its source, analysis and limits. Several values in one cell come from different sources and are kept separate.">
            <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
              <table className="tbl">
                <thead><tr><th>Endpoint</th><th>Arm</th><th>Time point</th>{COLS.map((c) => <th key={c}>{estimandLabel(c)}</th>)}</tr></thead>
                <tbody>
                  {data.rows.map((r: any, i: number) => (
                    <tr key={i}>
                      <td>{r.category_label}</td><td className="font-medium">{r.arm}</td>
                      <td className="num whitespace-nowrap">wk {fmt(r.timepoint_weeks, 0)}{r.baseline_definition ? <span className="block text-[11.5px] text-ink-3">from {r.baseline_definition}</span> : null}</td>
                      {COLS.map((c) => <td key={c}><Cell items={r.by_estimand[c]} onPick={setPicked} picked={picked} /></td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          {detail ? (
            <Section title="Evidence inspector">
              <Panel className="fade-in" >
                <div className="flex flex-wrap items-center gap-2"><VerificationBadge status={detail.finding.verification_status} /><Qualifiers labels={detail.finding.labels} /></div>
                <p className="mt-3 text-[14px] leading-relaxed text-ink">{detail.finding.statement}</p>
                <div className="mt-3 grid gap-4 md:grid-cols-2">
                  <div><p className="text-[12px] font-semibold uppercase tracking-wide text-ink-3">Analysis</p><ul className="mt-1 list-disc space-y-1 pl-5 text-[13px] text-ink-2">{detail.interpretation.analysis.map((s: string) => <li key={s}>{s}</li>)}</ul></div>
                  <div><p className="text-[12px] font-semibold uppercase tracking-wide text-ink-3">Does not establish</p><ul className="mt-1 list-disc space-y-1 pl-5 text-[13px] text-ink-2">{detail.interpretation.does_not_establish.map((s: string) => <li key={s}>{s}</li>)}</ul>
                    {detail.interpretation.missing.length ? <><p className="mt-3 text-[12px] font-semibold uppercase tracking-wide text-ink-3">Not available in this source</p><ul className="mt-1 list-disc space-y-1 pl-5 text-[13px] text-ink-2">{detail.interpretation.missing.map((s: string) => <li key={s}>{s}</li>)}</ul></> : null}</div>
                </div>
                {detail.interpretation.derived_interval ? <p className="mt-3 text-[12.5px] text-ink-3"><Badge tone="warn" icon={false}>Inference</Badge> {detail.interpretation.derived_interval.display}</p> : null}
                <p className="mt-3 border-t border-hairline pt-2.5"><SourceLine source={detail.finding.source} quote={detail.finding.locator_quote} location={detail.finding.origin === "REGISTRY" ? detail.finding.source_location : null} /></p>
                <Link href={`/compare?a=${detail.finding.result_id}`} className="mt-2 inline-block text-[13px] font-medium">Compare this result with another →</Link>
              </Panel>
            </Section>
          ) : null}
        </>
      )}
    </>
  );
}

export default function Page() {
  return <Suspense fallback={<Loading />}><Inspector /></Suspense>;
}
