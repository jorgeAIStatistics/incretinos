"use client";

import clsx from "clsx";
import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { ChartFrame, ChartTable, IntervalChart } from "@/components/charts";
import { Async, Callout, Drawer, EmptyState, EpistemicBadge, ErrorState, Loading, Missing, PageHeader, Panel, Qualifiers, Section, Select, SourceLine, TableCard, TableSkeleton, Term, TierBadge, VerificationBadge } from "@/components/ui";
import { fmt, titleCase, useApi } from "@/lib/api";
import { enumLabel } from "@/lib/format";
import { useQueryParams } from "@/lib/hooks";
import { useWholeList } from "@/lib/paging";
import { byRoleThenDose, estimandLabel, findingRow, isWeightPercentChange, reportedText, sourceShort, unitLabel, weekLabel, withUnit } from "@/lib/rows";
import type { EstimandDefinition, EstimandInspector as EstimandData, EstimandRow, Finding, FindingDetail, StudyListItem } from "@/lib/types";

/** Estimand columns in reading order. Any other label found in the data gets its own column after these. */
const KNOWN_COLUMNS = ["TREATMENT_REGIMEN", "EFFICACY", "NOT_STATED"];

/** The two named estimands, in the order they are drawn for each arm. */
const CHARTED = ["TREATMENT_REGIMEN", "EFFICACY"];

type Entry = { arm: string; estimand: string; f: Finding };
type EstimandSet = { key: string; first: Finding; entries: Entry[]; estimands: string[]; arms: number; extra: number };

/**
 * The chart plots one result set at a time: the values ONE source reports for the arms of this trial, at one time
 * point, from one baseline, in percent change in body weight. Within a set each arm appears at most once per
 * estimand, so when a source reports both estimands, the gap between an arm's two rows reflects only how treatment
 * discontinuation was handled. Values from different sources (a label's tablet doses, a press release, the registry)
 * are never drawn on one axis as if they were further arms; the reader picks the set.
 */
function estimandSets(rows: EstimandRow[]): EstimandSet[] {
  const sets = new Map<string, EstimandSet>();
  for (const r of rows) {
    for (const estimand of CHARTED) {
      for (const f of r.by_estimand[estimand] ?? []) {
        if (!isWeightPercentChange({ category: r.category, unit: f.unit }) || f.value == null) continue;
        const key = [f.source?.id ?? "no-source", f.tier, f.timepoint_weeks ?? "no-time", f.baseline_definition ?? "", unitLabel(f.unit), f.threshold ?? "", f.class_title ?? ""].join("¦");
        const set = sets.get(key) ?? { key, first: f, entries: [], estimands: [], arms: 0, extra: 0 };
        sets.set(key, set);
        const arm = f.arm_group_key ?? r.arm;
        // One value per arm and estimand in a set; a second one from the same source stays in the table below.
        if (set.entries.some((x) => (x.f.arm_group_key ?? x.arm) === arm && x.estimand === estimand)) {
          set.extra += 1;
          continue;
        }
        set.entries.push({ arm: r.arm, estimand, f });
      }
    }
  }
  return [...sets.values()]
    .map((set) => ({ ...set, estimands: CHARTED.filter((e) => set.entries.some((x) => x.estimand === e)), arms: new Set(set.entries.map((x) => x.f.arm_group_key ?? x.arm)).size }))
    .sort((a, b) => b.estimands.length - a.estimands.length || b.arms - a.arms);
}

/** Short enough to show in full in the select; the caption above the chart describes the chosen set completely. */
function setOptions(sets: EstimandSet[]): { value: string; label: string }[] {
  const short = (set: EstimandSet) => `${set.first.source?.tier_label ?? titleCase(set.first.tier)} · ${weekLabel(set.first.timepoint_weeks)} · ${set.estimands.map((e) => estimandLabel(e).replace(/ estimand$/, "")).join(" and ")} (${set.arms} ${set.arms === 1 ? "arm" : "arms"})`;
  return sets.map((set) => {
    const repeated = sets.filter((other) => short(other) === short(set)).length > 1;
    return { value: set.key, label: repeated ? `${short(set)} · ${sourceShort(set.first.source)}` : short(set) };
  });
}

function EstimandChart({ data }: { data: EstimandData }) {
  const sets = estimandSets(data.rows);
  const [picked, setPicked] = useState<string | null>(null);
  const current = sets.find((s) => s.key === picked) ?? sets[0];
  if (!current) {
    return (
      <Callout tone="muted" title="No percent change in body weight under a named estimand is stored for this trial.">
        Values in other units, and values whose estimand is not stated, are listed in the table below. Other endpoints are on the trial&apos;s page in the trial atlas.
      </Callout>
    );
  }
  const first = current.first;
  // Ordered by arm role, then dose; the treatment-regimen row of an arm comes before its efficacy row.
  const ordered = byRoleThenDose(
    current.entries.map((x, index) => {
      const row = { ...findingRow(x.f, { drugs: data.study.drugs }), key: `${x.f.result_id}`, label: x.arm };
      return { row, group: row.group, dose: x.f.dose_amount ?? null, index, armOrder: current.entries.findIndex((y) => (y.f.arm_group_key ?? y.arm) === (x.f.arm_group_key ?? x.arm)), estimandOrder: CHARTED.indexOf(x.estimand) };
    }).sort((a, b) => a.armOrder - b.armOrder || a.estimandOrder - b.estimandOrder),
    (x) => x.dose,
  );
  const rows = ordered.map((x) => x.row);
  const both = current.estimands.length > 1;
  const missingOther = both ? current.arms * 2 - current.entries.length : 0;
  return (
    <ChartFrame
      level={2}
      title={`Percent change in body weight at ${weekLabel(first.timepoint_weeks)}, by estimand`}
      subtitle={
        <span data-testid="estimand-caption">
          {current.arms} {current.arms === 1 ? "arm" : "arms"} from one result set: <span className="font-medium text-ink-2">{first.source?.tier_label ?? titleCase(first.tier)} — {sourceShort(first.source)} · {weekLabel(first.timepoint_weeks)}{first.baseline_definition ? ` · from ${first.baseline_definition}` : " · baseline definition not recorded for this source"} · values in %</span>.{" "}
          {both
            ? `This source reports both estimands for ${missingOther ? "some of " : ""}these arms, so the gap between the two rows of one arm reflects how treatment discontinuation was handled — it is not a treatment effect.`
            : `This source reports only the ${estimandLabel(current.estimands[0]).toLowerCase()} for these arms. Values under the other estimand come from other sources and are a different result set: they are not drawn beside these rows.`}
        </span>
      }
      controls={sets.length > 1 ? <Select label={`Result set (${sets.length} on record — one is plotted at a time, never mixed)`} value={current.key} onChange={setPicked} options={setOptions(sets)} /> : undefined}
      table={<ChartTable rows={rows} unit="%" valueHeader="Change, %" />}
      footnote={
        <>
          <span className="block" data-testid="estimand-source">Source of every plotted value: <SourceLine source={first.source} /></span>
          {current.extra ? <span className="mt-1 block">{current.extra} further {current.extra === 1 ? "value" : "values"} from this source for an arm and estimand already plotted {current.extra === 1 ? "is" : "are"} listed in the table below, not plotted.</span> : null}
          <span className="mt-1 block">{data.rule}</span>
        </>
      }
    >
      <IntervalChart rows={rows} unit="%" mark="dot" axisLabel="Percent change in body weight" />
    </ChartFrame>
  );
}

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
  // The curator's research note names the estimand, but no checked quote from this source does: it is not inferred
  // from an analysis description, and the card's own text says so (the chip stays short enough for a phone).
  if (basis === "INFERRED_BY_CURATOR") return <EpistemicBadge kind="INFERENCE">Curator&apos;s attribution</EpistemicBadge>;
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
      {estimand.basis === "INFERRED_BY_CURATOR" ? (
        <p className="mt-1.5 text-data text-ink-3">The curator&apos;s research note names this estimand, but no checked quote from this source does.</p>
      ) : null}
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
          <span className="num min-w-16 shrink-0 whitespace-nowrap text-right font-semibold text-ink" data-testid="estimand-value">
            {f.value == null ? "not reported" : withUnit(reportedText(f.value, f.value_text), f.unit)}
            {f.value != null && !unitLabel(f.unit) ? <span className="font-normal text-ink-3"> (unit not recorded)</span> : null}
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

      <div className="mb-8" data-testid="estimand-chart">
        <EstimandChart key={data.study.nct_id} data={data} />
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
