"use client";

import Link from "next/link";
import { Async, Callout, type Column, DataTable, EmptyState, Loading, PageHeader, Section, Select, VerificationBadge } from "@/components/ui";
import { fmt, useApi, withQuery } from "@/lib/api";
import { formatCount } from "@/lib/format";
import { useQueryParams } from "@/lib/hooks";
import { type BreakdownId, EVIDENCE_TYPE_LABEL, isBreakdown, isDimension, type QualityDimensionId, type QualityMetric, type QualityOverview, type WidthGroup } from "@/lib/quality";
import { AssessmentPanel } from "./Assessment";
import { BreakdownTable } from "./BreakdownTable";
import { type BarRow, CountBars, MetricTable } from "./parts";

/*
 * Evidence Quality. Six dimensions, each in its own section with its own rule and counts, and never combined: there
 * is no overall score on this page and none may be derived from it. Every count links to exactly the records it
 * counts. Rejected and held rows appear only in the review-status section.
 */

const STATUS_COLUMNS = ["MACHINE_EXTRACTED", "QUOTE_ANCHORED", "PENDING_HUMAN_REVIEW", "HUMAN_VERIFIED", "REJECTED", "SOURCE_CHANGED_RECHECK_REQUIRED"] as const;

const SECTION_METRICS: Record<QualityDimensionId, string[]> = {
  review_status: ["review_rows", "status_machine_extracted", "status_quote_anchored", "status_pending_human_review", "status_human_verified", "status_rejected", "status_source_changed_recheck_required", "status_other", "review_hold", "open_requests", "orphaned_decisions", "change_events_open", "queue_open", "queue_in_review"],
  completeness: ["result_no_ci", "result_no_n", "result_no_unit", "result_estimand_not_stated", "result_estimand_inferred", "result_estimand_stated", "analysis_no_method", "analysis_multiplicity_not_stated", "analysis_no_method_or_multiplicity", "analysis_estimand_not_stated", "ae_no_denominator", "drug_no_trial_ae_denominators", "drug_no_label_safety", "drug_incomplete_safety"],
  authority: ["authority_rows", "authority_sources", "press_release_only"],
  reliability: ["risk_high", "risk_medium", "risk_low", "risk_none", "source_conflicts", "source_conflicts_insufficient", "source_conflicts_explainable", "source_conflicts_consistent"],
  precision: ["result_with_interval", "analysis_with_interval"],
  freshness: ["sources_changed", "sources_refresh_due"],
};

function pick(metrics: QualityMetric[], ids: string[]): QualityMetric[] {
  const byId = new Map(metrics.map((m) => [m.id, m]));
  return ids.map((id) => byId.get(id)).filter((m): m is QualityMetric => !!m);
}

function StatusByType({ metrics }: { metrics: QualityMetric[] }) {
  const byId = new Map(metrics.map((m) => [m.id, m]));
  const total = byId.get("review_rows");
  const held = byId.get("review_hold");
  if (!total?.by_type) return null;
  const types = Object.keys(total.by_type);
  const cell = (metricId: string, type: string) => {
    const m = byId.get(metricId);
    const n = m?.by_type?.[type]?.[0] ?? 0;
    const page = m?.links.page?.href;
    return page ? <Link href={withQuery(page, { etype: type })}>{formatCount(n)}</Link> : formatCount(n);
  };
  return (
    <div className="mt-4 overflow-hidden rounded-panel border border-hairline bg-surface">
      {/* oxlint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <div role="region" aria-label="Stored records by evidence type and verification status" tabIndex={0} className="overflow-x-auto focus-visible:outline-offset-[-2px]">
        <table className="tbl" style={{ minWidth: 860 }}>
          <caption className="border-b border-hairline px-3 py-2 text-data text-ink-2">Stored records by evidence type and verification status (rejected and held rows included). Each count opens the same records in Evidence Review.</caption>
          <thead>
            <tr>
              <th scope="col">Evidence type</th>
              {STATUS_COLUMNS.map((s) => (
                <th key={s} scope="col" className="r">
                  <VerificationBadge status={s} compact explain={false} />
                </th>
              ))}
              <th scope="col" className="r">
                On review hold
              </th>
              <th scope="col" className="r">
                All
              </th>
            </tr>
          </thead>
          <tbody>
            {types.map((t) => (
              <tr key={t}>
                <th scope="row">{EVIDENCE_TYPE_LABEL[t] ?? t}</th>
                {STATUS_COLUMNS.map((s) => (
                  <td key={s} className="r num">
                    {cell(`status_${s.toLowerCase()}`, t)}
                  </td>
                ))}
                <td className="r num">{formatCount(held?.by_type?.[t]?.[0] ?? 0)}</td>
                <td className="r num font-semibold">{cell("review_rows", t)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function bars(rows: { value: string | null; label: string; count: number; links: { href: string } | null }[]): BarRow[] {
  return rows.map((r) => ({ key: r.value ?? "none", label: r.label, count: r.count, href: r.links?.href ?? null }));
}

const WIDTH_COLUMNS: Column<WidthGroup>[] = [
  {
    key: "group",
    header: "Comparable group",
    rowHeader: true,
    className: "min-w-[220px] max-w-md",
    cell: (g) => (
      <>
        <span className="block font-medium text-ink">{g.endpoint_label ?? "No endpoint category"}</span>
        <span className="block text-caption text-ink-3">
          {EVIDENCE_TYPE_LABEL[g.evidence_type] ?? g.evidence_type}; {g.param_type ?? "parameter not stated"}; {g.unit ?? "no unit"}; {g.ci_level == null ? "level not stated" : `${fmt(g.ci_level)}% interval`}
        </span>
      </>
    ),
  },
  { key: "n", header: "Intervals", align: "right", cell: (g) => <Link href={g.links.href}>{formatCount(g.n)}</Link> },
  { key: "min", header: "Narrowest", align: "right", cell: (g) => fmt(g.min, 2) },
  { key: "p25", header: "First quartile", align: "right", cell: (g) => fmt(g.p25, 2) },
  { key: "median", header: "Median", align: "right", cell: (g) => fmt(g.median, 2) },
  { key: "p75", header: "Third quartile", align: "right", cell: (g) => fmt(g.p75, 2) },
  { key: "max", header: "Widest", align: "right", cell: (g) => fmt(g.max, 2) },
];

function Dashboard({ data, by, onBy, qdim, onQdim }: { data: QualityOverview; by: BreakdownId; onBy: (v: BreakdownId) => void; qdim: QualityDimensionId; onQdim: (v: QualityDimensionId) => void }) {
  const dims = Object.fromEntries(data.dimensions.map((d) => [d.id, d]));
  const section = (id: QualityDimensionId) => ({ title: dims[id]?.label ?? id, hint: <>{dims[id]?.question} {dims[id]?.caveat}</> });
  const metrics = data.metrics;
  const flags = data.distributions.flag_codes.filter((f) => f.count > 0);
  const zeroFlags = data.distributions.flag_codes.length - flags.length;
  const widths = data.interval_widths;
  return (
    <>
      <Callout tone="info" title="How to read this page" level={2} className="mb-6">
        <ul className="mt-1 list-disc space-y-0.5 pl-4">
          {data.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
          <li>
            Counted at <span className="num">{data.as_of.slice(0, 16).replace("T", " ")}</span> UTC. Every count is a link to the records behind it.
          </li>
        </ul>
      </Callout>

      <nav aria-label="Sections of this page" className="mb-6 flex flex-wrap gap-x-4 gap-y-1 text-data">
        {[...data.dimensions.map((d) => ({ id: d.id.replace("_", "-"), label: d.label })), { id: "breakdown", label: "Breakdown" }, { id: "assessment", label: "Assessment of one molecule or trial" }].map((d) => (
          <a key={d.id} href={`#${d.id}`} className="font-medium">
            {d.label}
          </a>
        ))}
      </nav>

      <Section id="review-status" {...section("review_status")}>
        <MetricTable label="Review status metrics" metrics={pick(metrics, SECTION_METRICS.review_status)} />
        <StatusByType metrics={metrics} />
      </Section>

      <Section id="completeness" {...section("completeness")}>
        <MetricTable label="Completeness metrics" metrics={pick(metrics, SECTION_METRICS.completeness)} />
        <p className="mt-2 max-w-[72ch] text-caption text-ink-3">
          Molecule safety rules: a trial adverse-event table counts when an adverse-event row of kind {data.safety_rule.measure_kind.toLowerCase().replaceAll("_", " ")} on an arm of the molecule carries its number at risk; label safety sections are {data.safety_rule.label_sections.map((s) => s.replaceAll("_", " ")).join(", ")}.
        </p>
      </Section>

      <Section id="authority" {...section("authority")}>
        <MetricTable label="Source authority metrics" metrics={pick(metrics, SECTION_METRICS.authority)} />
        <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
          <CountBars title="Records in analytic use by evidence tier" subtitle="The tier of each record's source. A category of source, not a grade of the record." rows={bars(data.distributions.records_by_tier)} noun="records" />
          <CountBars title="Records in analytic use by source type" rows={bars(data.distributions.records_by_source_type)} noun="records" />
          <CountBars title="Evidence sources by type" rows={bars(data.distributions.sources_by_source_type)} noun="sources" />
          <CountBars title="Evidence sources by tier" rows={bars(data.distributions.sources_by_tier)} noun="sources" />
        </div>
      </Section>

      <Section id="reliability" {...section("reliability")}>
        <MetricTable label="Reliability signal metrics" metrics={pick(metrics, SECTION_METRICS.reliability)} />
        <div className="mt-4">
          {flags.length ? (
            <CountBars
              title="Records carrying each automated check code"
              subtitle="A record with several codes is counted once under each. Codes are explained in the record list."
              rows={flags.map((f) => ({ key: f.value, label: f.title, sub: `${f.value}, severity ${f.severity?.toLowerCase() ?? "not stated"}`, count: f.count, href: f.links.href }))}
              noun="records"
              footnote={`${formatCount(zeroFlags)} other check codes flag no record in analytic use.`}
            />
          ) : (
            <EmptyState title="No automated check flags a record in analytic use">This does not show that the records are right.</EmptyState>
          )}
        </div>
      </Section>

      <Section id="precision" {...section("precision")}>
        <MetricTable label="Precision metrics" metrics={pick(metrics, SECTION_METRICS.precision)} />
        <div className="mt-4">
          <DataTable
            label="Width of reported intervals, by comparable group"
            caption={<>Width of reported intervals (upper minus lower limit), by comparable group. {widths.note}</>}
            columns={WIDTH_COLUMNS}
            rows={widths.groups}
            rowKey={(g) => g.key}
            maxHeight="min(60vh, 560px)"
            minWidth={760}
            pinFirstColumn
            empty={<p className="px-3 py-5 text-ink-3">No group holds {widths.min_group_size} or more comparable intervals.</p>}
            footer={
              <p className="num text-data text-ink-2">
                {formatCount(widths.groups.length)} groups shown; {formatCount(widths.groups_too_small)} smaller groups ({formatCount(widths.intervals_in_small_groups)} intervals) are not summarised.
              </p>
            }
          />
        </div>
      </Section>

      <Section id="freshness" {...section("freshness")}>
        <MetricTable label="Freshness metrics" metrics={pick(metrics, SECTION_METRICS.freshness)} />
        <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
          <CountBars title="Sources by age of the last retrieval" subtitle="Days between the last retrieval and the time of the count." rows={data.distributions.retrieval_age.map((a) => ({ key: a.value, label: a.label, count: a.count, href: a.links.href }))} noun="sources" />
          <div className="rounded-panel border border-hairline bg-surface p-4">
            <h3 className="text-body font-semibold text-ink">Refresh rule</h3>
            <p className="mt-0.5 text-data text-ink-3">
              {data.refresh_rule.rule} Set by <span className="mono" translate="no">{data.refresh_rule.setting}</span>; any other source type: {data.refresh_rule.fallback_days} days.
            </p>
            <table className="tbl mt-3">
              <caption className="sr-only">Refresh rule by source type</caption>
              <thead>
                <tr>
                  <th scope="col">Source type</th>
                  <th scope="col" className="r">
                    Refresh after (days)
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.refresh_rule.days.map((d) => (
                  <tr key={d.source_type}>
                    <th scope="row">{d.label}</th>
                    <td className="r num">
                      {formatCount(d.days)}
                      {d.days !== d.default ? <span className="block text-caption text-ink-3">default {formatCount(d.default)}</span> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </Section>

      <Section id="breakdown" title="Breakdown" hint="One quality dimension at a time, counted within each value of the breakdown you choose. Each cell opens exactly the records it counts. Columns are never added together.">
        <div className="mb-3 flex flex-wrap items-end gap-3">
          <Select label="Break down by" value={by} onChange={(v) => isBreakdown(v) && onBy(v)} options={data.breakdown_dimensions.map((d) => ({ value: d.id, label: d.label }))} className="w-full sm:w-auto" />
          <Select label="Quality dimension" value={qdim} onChange={(v) => isDimension(v) && onQdim(v)} options={data.dimensions.map((d) => ({ value: d.id, label: d.label }))} className="w-full sm:w-auto" />
        </div>
        <BreakdownTable breakdown={data.breakdown} metrics={pick(metrics, SECTION_METRICS[qdim])} dimensionLabel={dims[qdim]?.label ?? qdim} />
      </Section>
    </>
  );
}

export function QualityDashboard() {
  const [param, setParams] = useQueryParams();
  const byParam = param("by", "drug");
  const by: BreakdownId = isBreakdown(byParam) ? byParam : "drug";
  const qdimParam = param("dimension", "completeness");
  const qdim: QualityDimensionId = isDimension(qdimParam) ? qdimParam : "completeness";
  const overview = useApi<QualityOverview>(withQuery("/quality", { by }), { keepPrevious: true });
  return (
    <>
      <PageHeader
        eyebrow="Review and provenance"
        title="Evidence Quality"
        lede="How far the stored evidence has been checked, what its sources leave out, where it comes from, what the automated checks flag, how precise and how fresh it is. Each dimension stands on its own; there is no overall score."
      />
      <Async state={overview} errorTitle="The quality counts could not be loaded" loading={<Loading rows={10} label="Counting the stored evidence…" />}>
        {(data) => <Dashboard data={data} by={by} onBy={(v) => setParams({ by: v === "drug" ? null : v })} qdim={qdim} onQdim={(v) => setParams({ dimension: v === "completeness" ? null : v })} />}
      </Async>
      <AssessmentPanel />
    </>
  );
}
