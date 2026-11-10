"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { Badge, Callout, type Column, DataTable, ErrorState, LinkButton, Missing, PageHeader, Pagination, TableSkeleton, TierBadge, VerificationBadge } from "@/components/ui";
import { withQuery } from "@/lib/api";
import { formatCount } from "@/lib/format";
import { useQueryParams } from "@/lib/hooks";
import { usePagedList } from "@/lib/paging";
import { BREAKDOWN_IDS, type QualityRecord, type QualityRecordsExtra, shareText } from "@/lib/quality";
import { pageName } from "./parts";

/*
 * The records behind one quality count: exactly the rows the metric counted under the filters in the address, with
 * the true total. Each row says what it is and where to open it (the review dossier, the trial, the source).
 */

const KIND_LABEL: Record<QualityRecord["kind"], string> = {
  evidence: "Evidence record",
  source: "Source",
  drug: "Molecule",
  decision: "Review decision",
  change_event: "Source change",
  queue_item: "Review-queue item",
  conflict: "Source conflict",
};

const FILTER_NAME: Record<string, string> = { drug: "Molecule", study: "Trial", tier: "Evidence tier", source_type: "Source type", endpoint_category: "Endpoint category" };

const COLUMNS: Column<QualityRecord>[] = [
  {
    key: "record",
    header: "Record",
    rowHeader: true,
    className: "min-w-[260px] max-w-xl",
    cell: (r) => (
      <>
        <span className="block font-medium text-ink">{r.label}</span>
        <span className="block text-caption text-ink-3">{r.evidence_type_label ?? KIND_LABEL[r.kind]}</span>
        <span className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-caption">
          {/* The trial has its own column. */}
          {r.links.filter((l) => !l.href.startsWith("/trials/")).map((l) => (
            <Link key={l.href} href={l.href}>
              {l.label}
            </Link>
          ))}
        </span>
      </>
    ),
  },
  {
    key: "trial",
    header: "Trial",
    className: "min-w-[120px]",
    cell: (r) => (r.study_nct ? <Link href={`/trials/${r.study_nct}`}>{r.study_name ?? r.study_nct}</Link> : <Missing>No trial</Missing>),
  },
  {
    key: "status",
    header: "Verification",
    cell: (r) =>
      r.kind === "evidence" ? (
        <span className="flex flex-col items-start gap-1">
          <VerificationBadge status={r.verification_status} compact />
          {r.review_hold ? <Badge tone="warn">On review hold</Badge> : null}
        </span>
      ) : (
        <span className="text-caption text-ink-3">Not an evidence record</span>
      ),
  },
  { key: "tier", header: "Source", cell: (r) => (r.tier ? <TierBadge tier={r.tier} /> : <Missing>Not recorded</Missing>) },
  { key: "note", header: "What was counted", className: "min-w-[220px] max-w-md", cell: (r) => (r.note ? <span className="text-ink-2">{r.note}</span> : <span className="text-ink-3">The record meets the rule above.</span>) },
];

export function QualityRecords() {
  const [param] = useQueryParams();
  const metric = param("metric");
  const value = param("value");
  const filters = Object.fromEntries(BREAKDOWN_IDS.map((k) => [k, param(k)]).filter(([, v]) => v));
  const list = usePagedList<QualityRecord, QualityRecordsExtra>(metric ? withQuery("/quality/records", { metric, value, ...filters }) : null, { pageSize: 25 });
  const extra = list.data?.extra;
  const m = extra?.metric;
  return (
    <>
      <PageHeader
        eyebrow="Evidence Quality"
        title={m ? m.label : "Records behind a quality count"}
        lede={m ? m.rule : "Exactly the records one quality count includes, with the true total."}
      >
        <LinkButton href="/quality" variant="secondary" size="sm" Icon={ArrowLeft}>
          All quality dimensions
        </LinkButton>
      </PageHeader>
      {!metric ? (
        <Callout tone="warn" title="No metric was named" level={2}>
          Open this page from a count on the Evidence Quality page.
        </Callout>
      ) : list.status === "error" ? (
        <ErrorState title="These records could not be loaded" message={list.error ?? "Unknown error"} onRetry={list.reload} />
      ) : !list.data || !extra || !m ? (
        <TableSkeleton rows={8} columns={5} label="Loading the counted records…" />
      ) : (
        <>
          <dl className="mb-4 grid grid-cols-1 gap-x-6 gap-y-2 text-data sm:grid-cols-[max-content_minmax(0,1fr)]" data-testid="records-summary">
            <dt className="text-ink-3">Dimension</dt>
            <dd className="text-ink">{extra.dimension_label}</dd>
            {extra.value_label ? (
              <>
                <dt className="text-ink-3">Counting</dt>
                <dd className="text-ink">
                  {extra.value_label} <span className="mono text-caption text-ink-3" translate="no">{extra.value}</span>
                </dd>
              </>
            ) : null}
            {Object.entries(extra.filters).map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-ink-3">{FILTER_NAME[k] ?? k}</dt>
                <dd className="text-ink">
                  {extra.filter_labels[k as keyof typeof extra.filter_labels] ?? v}{" "}
                  <Link href={withQuery("/quality/records", { metric, value, ...Object.fromEntries(Object.entries(extra.filters).filter(([fk]) => fk !== k)) })} className="text-caption">
                    Remove this filter
                  </Link>
                </dd>
              </div>
            ))}
            <dt className="text-ink-3">Count</dt>
            <dd className="text-ink" data-testid="records-count">
              <span className="num font-semibold">{formatCount(m.count)}</span> of <span className="num">{formatCount(m.denominator)}</span> {m.denominator_label} ({shareText(m.count, m.denominator)})
            </dd>
            <dt className="text-ink-3">Counted among</dt>
            <dd className="text-ink-2">{m.universe_label}</dd>
          </dl>
          <Callout tone="info" className="mb-4">
            {extra.dimension_caveat}
            {m.links.page ? (
              <>
                {" "}
                <Link href={m.links.page.href} className="font-medium">
                  Open the same {formatCount(m.count)} records in {pageName(m.links.page.href)}
                </Link>
                .
              </>
            ) : null}
            {m.links.context ? (
              <>
                {" "}
                Related: <Link href={m.links.context.href}>{m.links.context.label}</Link>.
              </>
            ) : null}
          </Callout>
          <DataTable
            label={`Records counted by: ${m.label}`}
            columns={COLUMNS}
            rows={list.data.rows}
            rowKey={(r) => r.key}
            refreshing={list.refreshing}
            pinFirstColumn
            minWidth={900}
            empty={<p className="px-3 py-5 text-ink-3">No record meets this rule{Object.keys(extra.filters).length ? " under these filters" : ""}.</p>}
            footer={<Pagination page={list.page} pageSize={list.pageSize} total={list.data.total} onPage={list.setPage} onPageSize={list.setPageSize} noun="records" truncated={list.data.truncated} reachable={list.data.reachable} />}
          />
        </>
      )}
    </>
  );
}
