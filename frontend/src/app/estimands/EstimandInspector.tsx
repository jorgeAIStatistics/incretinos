"use client";

import clsx from "clsx";
import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { ChartFrame, ChartTable, IntervalChart } from "@/components/charts";
import { Async, Callout, Drawer, EmptyState, EpistemicBadge, ErrorState, Loading, Missing, PageHeader, Panel, Qualifiers, Section, Select, SourceLine, TableCard, TableSkeleton, Term, TierBadge, VerificationBadge } from "@/components/ui";
import { fmt, useApi } from "@/lib/api";
import { enumLabel } from "@/lib/format";
import { useQueryParams } from "@/lib/hooks";
import { useWholeList } from "@/lib/paging";
import { estimandLabel, findingRow } from "@/lib/rows";
import type { EstimandDefinition, EstimandInspector as EstimandData, EstimandRow, Finding, FindingDetail, StudyListItem } from "@/lib/types";

/** Estimand columns in reading order. Any other label found in the data gets its own column after these. */
const KNOWN_COLUMNS = ["TREATMENT_REGIMEN", "EFFICACY", "NOT_STATED"];

const isPercent = (unit: string | null | undefined) => /percent|%/i.test(unit ?? "");

/**
 * The trial shown when the address names none. It is chosen from the trial list, not written into the code:
 * the first trial with results from both a peer-reviewed publication and the registry (where estimands most
 * often differ between sources), otherwise the first trial with any result.
 */
function defaultStudy(studies: StudyListItem[]): string | null {
  const withResults = studies.filter((s) => s.n_results > 0);
  const both = withResults.find((s) => s.evidence_tiers.includes("PEER_REVIEWED") && s.evidence_tiers.includes("REGISTRY_RESULTS"));
  return (both ?? withResults[0])?.nct_id ?? null;
}

function BasisBadge({ basis }: { basis: string }) {
  if (basis === "STATED_BY_SOURCE") return <EpistemicBadge kind="DOCUMENTED">Stated by the source</EpistemicBadge>;
  if (basis.includes("INFERRED")) return <EpistemicBadge kind="INFERENCE">Inferred from the analysis description</EpistemicBadge>;
  return <Missing>{enumLabel(basis)}</Missing>;
}

function EstimandCard({ estimand }: { estimand: EstimandDefinition }) {
  return (
    <Panel as="article">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-body font-semibold text-ink">{estimandLabel(estimand.label)}</h3>
        <BasisBadge basis={estimand.basis} />
      </div>
      {estimand.explainer ? <p className="mt-1.5 text-data text-ink-2">{estimand.explainer}</p> : null}
      {estimand.strategies.length ? (
        <ul className="mt-1.5 space-y-0.5 text-data text-ink-3">
          {estimand.strategies.map((s) => (
            <li key={s.event}>
              {enumLabel(s.event)}: {enumLabel(s.strategy).toLowerCase()} strategy
            </li>
          ))}
        </ul>
      ) : null}
      {estimand.quote ? <p className="mt-1.5 text-data text-ink-3">Source text: “…{estimand.quote}…”</p> : null}
      {estimand.source ? (
        <p className="mt-2">
          <TierBadge tier={estimand.source.tier} label={estimand.source.tier_label} />
        </p>
      ) : null}
    </Panel>
  );
}

function ValueButtons({ items, onPick, picked }: { items: Finding[] | undefined; onPick: (id: number) => void; picked: number | null }) {
  if (!items?.length) return <Missing />;
  return (
    <span className="flex flex-col items-start gap-1">
      {items.map((f) => (
        <button
          key={f.result_id}
          type="button"
          onClick={() => onPick(f.result_id)}
          aria-haspopup="dialog"
          aria-expanded={picked === f.result_id}
          className={clsx(
            "flex items-center gap-2 rounded-chip px-1.5 py-0.5 text-left transition-colors duration-(--dur-fast) hover:bg-accent-soft",
            picked === f.result_id && "bg-accent-soft shadow-[inset_0_0_0_1px_var(--accent)]",
          )}
        >
          <span className="num w-16 shrink-0 text-right font-semibold text-ink">
            {fmt(f.value)}
            {isPercent(f.unit) ? "%" : ""}
          </span>
          <span className="text-caption text-ink-3">
            {f.source?.tier_label ?? "Source not recorded"}
            {f.estimand_basis?.includes("INFERRED") ? ", inferred" : ""}
          </span>
        </button>
      ))}
    </span>
  );
}

function FindingInspector({ id }: { id: number }) {
  const state = useApi<FindingDetail>(`/findings/${id}`);
  return (
    <Async state={state} errorTitle="This result could not be loaded" loading={<Loading rows={5} label="Loading the result…" />}>
      {({ finding, interpretation }) => (
        <div className="space-y-4 text-data">
          <div className="flex flex-wrap items-center gap-1.5">
            <VerificationBadge status={finding.verification_status} />
            <Qualifiers labels={finding.labels} omitVerification />
          </div>
          <p className="text-body leading-relaxed text-ink">{finding.statement}</p>
          <div>
            <h3 className="font-semibold text-ink">Analysis</h3>
            <ul className="mt-1 list-disc space-y-1 pl-5 text-ink-2">
              {interpretation.analysis.map((s) => (
                <li key={s}>{s}</li>
              ))}
            </ul>
          </div>
          <div>
            <h3 className="font-semibold text-ink">Does not establish</h3>
            <ul className="mt-1 list-disc space-y-1 pl-5 text-ink-2">
              {interpretation.does_not_establish.map((s) => (
                <li key={s}>{s}</li>
              ))}
            </ul>
          </div>
          {interpretation.missing.length ? (
            <div>
              <h3 className="font-semibold text-ink">Not available in this source</h3>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-ink-2">
                {interpretation.missing.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </div>
          ) : null}
          {interpretation.derived_interval ? (
            <p className="text-ink-2">
              <EpistemicBadge kind="INFERENCE" /> {interpretation.derived_interval.display}
            </p>
          ) : null}
          <p className="border-t border-hairline pt-3">
            <SourceLine source={finding.source} quote={finding.locator_quote} location={finding.origin === "REGISTRY" ? finding.source_location : null} />
          </p>
          <p>
            <Link href={`/compare?a=${finding.result_id}`} className="font-medium">
              Compare this result with another <ArrowRight aria-hidden size={13} className="inline align-[-2px]" />
            </Link>
          </p>
        </div>
      )}
    </Async>
  );
}

function Results({ data, picked, onPick }: { data: EstimandData; picked: number | null; onPick: (id: number) => void }) {
  const weight = data.rows.filter((r) => r.category === "WEIGHT_PCT_CHANGE");
  // The chart shows the time point at which the most estimands are reported.
  const mainWeeks = weight.length ? weight.reduce((best, r) => (Object.keys(r.by_estimand).length > Object.keys(best.by_estimand).length ? r : best), weight[0]).timepoint_weeks : null;
  const chartRows = weight
    .filter((r) => r.timepoint_weeks === mainWeeks)
    .flatMap((r) =>
      ["TREATMENT_REGIMEN", "EFFICACY"]
        .filter((e) => r.by_estimand[e]?.length)
        .map((e) => {
          const f = r.by_estimand[e].find((x) => x.tier === "PEER_REVIEWED") ?? r.by_estimand[e][0];
          return { ...findingRow(f), key: `${f.result_id}`, label: r.arm, sub: `${estimandLabel(e, f.estimand_basis)} · ${f.source?.tier_label ?? "source not recorded"}` };
        }),
    );
  const extraColumns = Array.from(new Set(data.rows.flatMap((r) => Object.keys(r.by_estimand)))).filter((label) => !KNOWN_COLUMNS.includes(label));
  const columns = [...KNOWN_COLUMNS, ...extraColumns];

  return (
    <>
      <Section
        title="Estimands defined for this trial"
        hint={
          <>
            As stated by each source, or inferred from its analysis description where the source does not name the <Term k="estimand">estimand</Term>. Each card lists how <Term k="intercurrent-event">intercurrent events</Term> such as stopping treatment were handled.
          </>
        }
      >
        {data.estimands.length ? (
          <div className="grid grid-cols-1 items-start gap-3 md:grid-cols-2">
            {data.estimands.map((e, i) => (
              <EstimandCard key={`${e.label}:${e.source?.id ?? i}:${i}`} estimand={e} />
            ))}
          </div>
        ) : (
          <EmptyState title="No estimand definition is stored for this trial">The sources on record do not describe how events such as stopping treatment were handled.</EmptyState>
        )}
      </Section>

      <div className="mb-8">
        {chartRows.length ? (
          <ChartFrame
            title={`Percent change in body weight at week ${fmt(mainWeeks, 0)}, by estimand`}
            subtitle="Each arm appears once per estimand. The gap between the two rows of one arm reflects the handling of treatment discontinuation — it is not a treatment effect."
            table={<ChartTable rows={chartRows} unit="%" valueHeader="Change, %" />}
            footnote={data.rule}
          >
            <IntervalChart rows={chartRows} unit="%" mark="dot" axisLabel="Percent change in body weight" />
          </ChartFrame>
        ) : (
          <Callout tone="muted" title="No weight-change results under a named estimand are stored for this trial.">
            Select another trial, or see its full results in the trial atlas.
          </Callout>
        )}
      </div>

      <Section title="Reported values by estimand" hint="Select any value to inspect its source, analysis and limits. Several values in one cell come from different sources and are kept separate.">
        {data.rows.length ? (
          <TableCard label="Reported values by estimand" footer={<p className="num text-data text-ink-2">{data.rows.length} of {data.rows.length} arm and time-point rows</p>}>
            <table className="tbl" style={{ minWidth: 820 }}>
              <thead>
                <tr>
                  <th scope="col">Endpoint</th>
                  <th scope="col">Arm</th>
                  <th scope="col">Time point</th>
                  {columns.map((c) => (
                    <th key={c} scope="col">
                      {estimandLabel(c)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.rows.map((r: EstimandRow, i) => (
                  <tr key={`${r.category}:${r.arm}:${r.timepoint_weeks}:${i}`}>
                    <td className="min-w-[150px]">{r.category_label ?? enumLabel(r.category)}</td>
                    <td className="min-w-[130px] font-medium">{r.arm}</td>
                    <td className="whitespace-nowrap">
                      {r.timepoint_weeks === null || r.timepoint_weeks === undefined ? <Missing /> : `wk ${fmt(r.timepoint_weeks, 0)}`}
                      {r.baseline_definition ? <span className="block max-w-[180px] whitespace-normal text-caption text-ink-3">from {r.baseline_definition}</span> : null}
                    </td>
                    {columns.map((c) => (
                      <td key={c}>
                        <ValueButtons items={r.by_estimand[c]} onPick={onPick} picked={picked} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </TableCard>
        ) : (
          <EmptyState title="No arm-level result is stored for body weight or HbA1c in this trial">Other endpoints of this trial are listed on its page in the trial atlas.</EmptyState>
        )}
      </Section>
    </>
  );
}

export function EstimandInspector() {
  const [param, setParams] = useQueryParams();
  const studies = useWholeList<StudyListItem>("/studies");
  const requested = param("study");
  const study = requested || (studies.data ? defaultStudy(studies.data.rows) : null);
  const data = useApi<EstimandData>(study ? `/estimands?study=${encodeURIComponent(study)}` : null);
  // The open result is remembered together with its trial, so changing trial closes the inspector.
  const [selection, setSelection] = useState<{ study: string | null; id: number } | null>(null);
  const picked = selection && selection.study === study ? selection.id : null;

  const options = (studies.data?.rows ?? []).filter((s) => s.n_results > 0).map((s) => ({ value: s.nct_id, label: s.display_name }));
  if (study && !options.some((o) => o.value === study)) options.unshift({ value: study, label: study });

  return (
    <>
      <PageHeader
        eyebrow="Evidence and estimand inspector"
        title="One arm, two estimands, two different numbers"
        lede={
          <>
            The <Term k="efficacy-estimand">efficacy estimand</Term> asks what would happen if everyone stayed on treatment; the <Term k="treatment-regimen-estimand">treatment-regimen estimand</Term> asks what happened regardless. They are shown in separate columns and never merged.
          </>
        }
      />
      <form onSubmit={(e) => e.preventDefault()} className="mb-6 flex flex-wrap items-end gap-3">
        <Select
          label="Trial"
          name="study"
          value={study ?? ""}
          onChange={(v) => setParams({ study: v })}
          options={options.length ? options : [{ value: "", label: studies.status === "error" ? "Trial list unavailable" : "Loading trials…" }]}
          disabled={options.length === 0}
          hint={studies.status === "error" ? "The trial list could not be loaded. Reload the page to try again." : undefined}
          className="w-full sm:w-80"
        />
      </form>

      {studies.status === "error" && !study ? (
        <ErrorState title="The trial list could not be loaded" message={studies.error ?? ""} onRetry={studies.reload} />
      ) : !study ? (
        studies.data ? (
          <EmptyState title="No trial has results on record">Estimands can be inspected once a trial with posted or published results has been ingested.</EmptyState>
        ) : (
          <TableSkeleton rows={8} columns={5} label="Loading trials…" />
        )
      ) : (
        <Async state={data} errorTitle="The estimands for this trial could not be loaded" loading={<TableSkeleton rows={8} columns={5} label="Loading estimands…" />}>
          {(d) => <Results data={d} picked={picked} onPick={(id) => setSelection({ study, id })} />}
        </Async>
      )}

      <Drawer open={picked !== null} onOpenChange={(open) => !open && setSelection(null)} title="Evidence inspector" width="34rem">
        <div className="px-4 py-4">{picked !== null ? <FindingInspector key={picked} id={picked} /> : null}</div>
      </Drawer>
    </>
  );
}
