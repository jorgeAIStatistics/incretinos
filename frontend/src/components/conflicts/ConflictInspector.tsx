"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { Callout, type Column, DataTable, Disclosure, ErrorState, Missing, Panel, Section, TableSkeleton, TierBadge, VerificationBadge } from "@/components/ui";
import { useApi } from "@/lib/api";
import { formatCount } from "@/lib/format";
import { recordHref } from "@/lib/review";
import { CLASS_ORDER, ClassBadge, DimensionStatusBadge } from "./ClassBadge";
import type { Comparison, ConflictDetail, ConflictRow } from "./types";

/*
 * One group of rows that purport to report the same quantity: the values side by side with everything that defines
 * them, every pairwise comparison with the fields that decide it, and what a reviewer should do next. Values are the
 * text the API says the source printed; the evidence tier is shown as context and never ranks anything.
 */

const minus = (text: string): string => text.replace(/^-(?=\d)/, "−");
const num = (v: number | null | undefined): string | null => (v === null || v === undefined ? null : minus(String(v)));

const ESTIMAND: Record<string, string> = { EFFICACY: "Efficacy estimand", TREATMENT_REGIMEN: "Treatment-regimen estimand", OTHER: "Another named estimand", NOT_STATED: "Not named" };
const BASIS: Record<string, string> = {
  STATED_BY_SOURCE: "named by the source",
  INFERRED_FROM_ANALYSIS_DESCRIPTION: "inferred from the registry's analysis description",
  INFERRED_BY_CURATOR: "curator's attribution, no checked quote",
  NOT_DETERMINABLE: "not determinable",
};
const PRECISION: Record<string, string> = {
  REPORTED_TEXT: "from the value as the source printed it",
  LOCATOR_QUOTE: "from the number as printed in the source quote",
  STORED_NUMBER: "from the stored number (no printed text kept; a trailing zero may be lost)",
  COUNT: "a count of participants (exact)",
  NOT_REPORTED: "no value reported",
};

function Text({ value, missing }: { value: ReactNode; missing?: string }) {
  if (value === null || value === undefined || value === "") return <Missing className="!whitespace-normal">{missing}</Missing>;
  return <>{value}</>;
}

type Field = { key: string; label: string; cell: (row: ConflictRow) => ReactNode };

function fieldsFor(detail: ConflictDetail): Field[] {
  const ae = detail.evidence_type === "adverse_event";
  const analysis = detail.evidence_type === "analysis";
  const fields: Field[] = [
    {
      key: "value",
      label: ae ? "Reported" : analysis ? "Estimate as printed" : "Value as printed",
      cell: (r) => (
        <span className="num text-body font-semibold text-ink" data-testid="side-by-side-value">
          {minus(r.value_printed)} {r.unit ? <span className="text-data font-normal text-ink-2">{r.unit}</span> : null}
        </span>
      ),
    },
    {
      key: "precision",
      label: "Reported precision",
      cell: (r) => (
        <span className="text-ink-2">
          {r.decimals === null ? "Not determinable" : `${r.decimals} decimal ${r.decimals === 1 ? "place" : "places"}`}
          <span className="block text-caption text-ink-3">{PRECISION[r.precision_basis] ?? r.precision_basis}</span>
        </span>
      ),
    },
  ];
  if (ae) {
    fields.push(
      { key: "counts", label: "Participants with the event", cell: (r) => (r.n_affected !== null ? <span className="num">{r.n_affected} of {r.n_at_risk ?? "a denominator not reported"}</span> : <Missing>Count not reported</Missing>) },
      { key: "percent", label: "Percentage", cell: (r) => (r.percent_reported !== null ? <span className="num">{num(r.percent_reported)}% (reported)</span> : r.percent_calculated !== null ? <span className="num">{num(r.percent_calculated)}% (calculated here from the reported counts)</span> : <Missing />) },
      { key: "period", label: "Observation period", cell: (r) => <Text value={r.period} /> },
      { key: "scope", label: "Events counted", cell: (r) => (r.is_serious ? "Serious events only" : r.origin === "REGISTRY" ? "Registry table of non-serious events" : "All events of the term") },
    );
  } else {
    fields.push(
      {
        key: "ci",
        label: "Confidence interval",
        cell: (r) => (r.ci_low !== null && r.ci_high !== null ? <span className="num">{r.ci_level !== null ? `${num(r.ci_level)}% ` : ""}CI {num(r.ci_low)} to {num(r.ci_high)}</span> : <Missing>Not reported in this source</Missing>),
      },
      { key: "n", label: "Number analysed", cell: (r) => (r.n !== null ? <span className="num">{formatCount(r.n)}</span> : <Missing />) },
      {
        key: "estimand",
        label: "Estimand",
        cell: (r) => (
          <>
            {ESTIMAND[r.estimand_label ?? "NOT_STATED"] ?? r.estimand_label}
            {r.estimand_basis && r.estimand_label !== "NOT_STATED" ? <span className="block text-caption text-ink-3">{BASIS[r.estimand_basis] ?? r.estimand_basis}</span> : null}
          </>
        ),
      },
      {
        key: "analysis_set",
        label: "Analysis set",
        cell: (r) => (r.analysis_set_label ? <>{r.analysis_set_label}<span className="block text-caption text-ink-3">“{r.analysis_set_text}”</span></> : <Missing>Not named in the stored record</Missing>),
      },
      { key: "missing_data", label: "Missing-data handling", cell: (r) => (r.missing_data_handling ? r.missing_data_handling : <Missing>Not stated</Missing>) },
      { key: "baseline", label: "Baseline", cell: (r) => <>{r.baseline_label ?? <Missing />}{r.baseline_definition ? <span className="block text-caption text-ink-3">Stored as “{r.baseline_definition}”</span> : null}</> },
      { key: "time", label: "Time point", cell: (r) => (r.timepoint_weeks !== null ? `Week ${num(r.timepoint_weeks)}` : <Missing>Not stated</Missing>) },
    );
  }
  if (analysis) fields.push({ key: "measure", label: "Effect measure", cell: (r) => <Text value={r.param_type} /> }, { key: "p", label: "P value", cell: (r) => <Text value={r.p_value_text} /> });
  fields.push(
    { key: "arm", label: ae || !analysis ? "Group as the source names it" : "Contrast as the source names it", cell: (r) => <span className="text-ink-2">{r.arm_label}</span> },
    {
      key: "formulation",
      label: "Formulation named",
      cell: (r) => (r.formulation_name ? <>{r.formulation_name}{r.dose_reported ? `, ${r.dose_reported}` : ""}<span className="block text-caption text-ink-3">{r.formulation_basis}</span></> : <Missing>Not stated</Missing>),
    },
    { key: "status", label: "Verification", cell: (r) => <VerificationBadge status={r.verification_status} compact /> },
    {
      key: "review",
      label: "Review",
      cell: (r) =>
        r.evidence_key ? (
          <Link href={recordHref(r.evidence_type, r.evidence_key)} className="text-accent hover:underline" data-testid="review-link">
            Open in review
          </Link>
        ) : (
          <Missing>Not yet keyed</Missing>
        ),
    },
  );
  return fields;
}

function SideBySide({ detail }: { detail: ConflictDetail }) {
  const fields = fieldsFor(detail);
  const columns: Column<Field>[] = [
    { key: "field", header: "Field", rowHeader: true, className: "min-w-[150px] text-ink-2 font-medium", cell: (f) => f.label },
    ...detail.rows.map((r, i) => ({
      key: `row-${r.evidence_type}-${r.id}`,
      header: (
        <span className="flex flex-col items-start gap-1">
          <span className="text-caption text-ink-3">Source {i + 1}</span>
          <TierBadge tier={r.tier} />
          <span className="line-clamp-2 font-normal text-ink-2" title={r.source?.title}>
            {r.source?.title ?? r.source?.key ?? "Source not recorded"}
          </span>
        </span>
      ),
      className: "min-w-[190px] align-top",
      cell: (f: Field) => f.cell(r),
    })),
  ];
  return (
    <DataTable
      label={`The ${detail.rows.length} stored values side by side`}
      columns={columns}
      rows={fields}
      rowKey={(f) => f.key}
      density="compact"
      pinFirstColumn
      minWidth={170 + 200 * detail.rows.length}
      maxHeight="none"
    />
  );
}

function rowName(detail: ConflictDetail, ref: string): string {
  const i = detail.rows.findIndex((r) => `${r.evidence_type}:${r.id}` === ref);
  const row = detail.rows[i];
  return row ? `Source ${i + 1} (${row.tier_label.split(" (")[0]})` : ref;
}

function ComparisonItem({ detail, comparison }: { detail: ConflictDetail; comparison: Comparison }) {
  const a = rowName(detail, comparison.a);
  const b = rowName(detail, comparison.b);
  return (
    <li className="rounded-control border border-hairline px-3 py-2.5 text-data" data-testid="comparison">
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <ClassBadge value={comparison.class} label={comparison.class_label} definition={detail.class_definitions[comparison.class]} />
        <span className="num font-medium text-ink">
          {a}: {minus(comparison.values.a)} <span className="text-ink-3">and</span> {b}: {minus(comparison.values.b)}
        </span>
      </p>
      <p className="mt-1.5 text-ink-2" data-testid="comparison-explanation">
        {comparison.explanation}
      </p>
      <Disclosure summary="Field by field" className="mt-1.5">
        <div className="relative mt-2 overflow-x-auto">
          <table className="tbl tbl-compact min-w-[640px]">
            <caption className="sr-only">
              Fields of {a} and {b} compared
            </caption>
            <thead>
              <tr>
                <th scope="col">Field</th>
                <th scope="col">{a}</th>
                <th scope="col">{b}</th>
                <th scope="col">Verdict</th>
              </tr>
            </thead>
            <tbody>
              {comparison.dimensions.map((d) => (
                <tr key={d.dimension}>
                  <th scope="row" className="font-medium text-ink-2">
                    {d.label}
                  </th>
                  <td>{d.a ?? <Missing />}</td>
                  <td>{d.b ?? <Missing />}</td>
                  <td>
                    <DimensionStatusBadge status={d.status} explains={d.explains} />
                    {d.note ? <span className="mt-1 block text-caption text-ink-3">{d.note}</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Disclosure>
    </li>
  );
}

function List({ items, empty, testId }: { items: ReactNode[]; empty: string; testId: string }) {
  if (!items.length) return <p className="text-data text-ink-3">{empty}</p>;
  return (
    <ul className="flex list-disc flex-col gap-1.5 pl-5 text-data text-ink-2" data-testid={testId}>
      {items.map((item, i) => (
        <li key={i}>{item}</li>
      ))}
    </ul>
  );
}

export function ConflictInspector({ groupId, onBack }: { groupId: string; onBack: () => void }) {
  const state = useApi<ConflictDetail>(`/conflicts/${encodeURIComponent(groupId)}`);
  const back = (
    <button type="button" onClick={onBack} className="mb-4 inline-flex min-h-6 items-center gap-1 rounded-chip text-data font-medium text-accent hover:underline">
      <ArrowLeft aria-hidden size={14} /> All groups
    </button>
  );
  if (state.status === "error") {
    return (
      <>
        {back}
        <ErrorState title="This group could not be loaded" message={state.error ?? ""} onRetry={state.reload} />
      </>
    );
  }
  if (!state.data) {
    return (
      <>
        {back}
        <TableSkeleton rows={8} columns={4} label="Loading the group…" />
      </>
    );
  }
  const detail = state.data;
  const comparisons = [...detail.comparisons].sort((x, y) => CLASS_ORDER.indexOf(x.class) - CLASS_ORDER.indexOf(y.class));
  const open = detail.class === "TRUE_CONFLICT" || detail.class === "INSUFFICIENT_INFORMATION";
  return (
    <div data-testid="conflict-inspector">
      {back}
      <Section
        title={`${detail.study.display_name}: ${detail.quantity}`}
        hint={
          <>
            {detail.evidence_type_label}, {detail.arm}
            {detail.time_point ? `, ${detail.time_point}` : ""}.{" "}
            <Link href={`/trials/${detail.study.nct_id}`} className="mono" translate="no">
              {detail.study.nct_id}
            </Link>
          </>
        }
      >
        <div className="mb-4 flex flex-wrap items-start gap-x-3 gap-y-2">
          <ClassBadge value={detail.class} label={detail.class_label} definition={detail.class_definitions[detail.class]} />
          <p className="min-w-0 max-w-[72ch] flex-1 text-data text-ink-2" data-testid="group-explanation">
            {detail.explanation}
          </p>
        </div>
        <SideBySide detail={detail} />
      </Section>

      <Section title="How each pair of sources compares" hint="Every two rows from different sources, most action-requiring first. Open “Field by field” to see the stored fields that decide each verdict.">
        <ul className="flex flex-col gap-2">
          {comparisons.map((c) => (
            <ComparisonItem key={`${c.a}-${c.b}`} detail={detail} comparison={c} />
          ))}
        </ul>
      </Section>

      <div className="mb-8 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel title={open ? "Next review action" : "What to do with these values"} level={2}>
          <List items={detail.next_actions} empty="No action: the values agree at the precision the sources printed." testId="next-actions" />
        </Panel>
        <Panel title={open ? "Possible reasons" : "Why the values differ"} level={2}>
          <List items={detail.possible_reasons} empty="The values agree; there is nothing to explain." testId="possible-reasons" />
        </Panel>
        <Panel title="Missing details" level={2}>
          <List
            items={detail.missing_details.map((m) => (
              <>
                {m.detail} <span className="text-ink-3">({m.source})</span>
                {m.evidence_key ? (
                  <>
                    {" "}
                    <Link href={recordHref(detail.evidence_type, m.evidence_key)} className="text-accent hover:underline">
                      Open in review
                    </Link>
                  </>
                ) : null}
              </>
            ))}
            empty="Nothing that decides the classification is missing from the stored records."
            testId="missing-details"
          />
        </Panel>
        <Panel title="Source hierarchy, as context only" level={2}>
          <p className="mb-2 text-data text-ink-3" data-testid="hierarchy-note">
            {detail.source_hierarchy.note}
          </p>
          <ol className="flex flex-col gap-1.5 text-data">
            {detail.source_hierarchy.tiers.map((t) => (
              <li key={t.tier} className="flex flex-wrap items-center gap-2">
                <TierBadge tier={t.tier} />
                <span className="num text-ink-2">{t.values.map(minus).join(", ")}</span>
              </li>
            ))}
          </ol>
        </Panel>
      </div>
      <Callout tone="muted" title="How groups are formed and classified">
        {detail.method}
      </Callout>
    </div>
  );
}
