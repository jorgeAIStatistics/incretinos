"use client";

import { ArrowRight } from "lucide-react";
import Link from "next/link";
import {
  Async,
  Badge,
  Callout,
  ClassificationBadge,
  type Column,
  DataTable,
  EmptyState,
  ErrorNote,
  Hypothetical,
  LinkButton,
  Loading,
  Missing,
  PageHeader,
  Panel,
  RegulatoryBadge,
  Section,
  Skeleton,
  StatTile,
  Term,
  TierBadge,
  VerificationBadge,
} from "@/components/ui";
import { day, phaseLabel, useApi } from "@/lib/api";
import { countByStatus, VERIFICATION_ORDER } from "@/lib/evidence";
import { formatCount, share, trialStatusLabel } from "@/lib/format";
import type { DrugSummary, Meta, Overview, StudyBrief, ValidationReport } from "@/lib/types";

/**
 * How the stored results were checked. Every status the API reports is counted, including ones this interface
 * does not know. Quote-anchored and machine-extracted results are listed as what they are: mechanical checks.
 * They are never added together into a "verified" total; only a recorded human decision is verification.
 */
function VerificationBreakdown({ meta }: { meta: Meta }) {
  const { known, unknown, total } = countByStatus(meta.result_verification);
  const rows = [...VERIFICATION_ORDER.map((status) => ({ status: status as string, n: known[status], recognised: true })), ...unknown.map((u) => ({ status: u.status, n: u.n, recognised: false }))];
  return (
    <div className="mt-5 border-t border-hairline pt-4">
      <h3 className="text-body font-semibold text-ink">How the stored results were checked</h3>
      <p className="mt-0.5 max-w-[70ch] text-data text-ink-3">
        A result counts as verified only after a reviewer&rsquo;s recorded decision. <Term k="machine-extracted">Machine-extracted</Term> and <Term k="quote-anchored">quote-anchored</Term> are automatic checks, not verification.
      </p>
      <table className="mt-3 w-full border-collapse text-data">
        <caption className="sr-only">Stored results by verification status</caption>
        <thead className="sr-only">
          <tr>
            <th scope="col">Verification status</th>
            <th scope="col">Results</th>
            <th scope="col">Share of all results</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const pct = share(row.n, total);
            return (
              <tr key={row.status} className="align-middle">
                <th scope="row" className="py-1 pr-3 text-left font-normal">
                  <VerificationBadge status={row.status} compact />
                </th>
                <td className="num w-16 py-1 pr-3 text-right font-semibold text-ink">{formatCount(row.n)}</td>
                <td className="w-[42%] py-1">
                  <span className="flex items-center gap-2">
                    {/* Bars share one scale that starts at zero. They repeat the counts; the numbers are the content. */}
                    <span aria-hidden className="relative h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-grid">
                      <span className="absolute inset-y-0 left-0 rounded-full bg-series-1" style={{ width: `${pct ?? 0}%` }} />
                    </span>
                    <span className="num w-11 shrink-0 text-right text-caption text-ink-3">{pct === null ? "—" : `${pct < 0.05 && row.n > 0 ? "<0.1" : pct.toFixed(1)}%`}</span>
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ValidationPanel() {
  const validation = useApi<ValidationReport>("/validation");
  return (
    <Panel as="section" title="Scientific validation rules" level={2} className="!p-5">
      <Async state={validation} errorTitle="The validation rules could not be loaded" loading={<Loading rows={5} label="Running validation rules…" />}>
        {(report) => {
          const failing = report.rules.filter((r) => !r.passed);
          // Failures always come first; passing rules only fill the remaining places.
          const listed = [...failing, ...report.rules.filter((r) => r.passed)].slice(0, Math.max(5, failing.length));
          return (
            <>
              <p className="text-hero font-semibold text-ink">
                {report.passed} / {report.total} <span className="text-body font-medium text-ink-3">passing</span>
              </p>
              <ul className="mt-3 space-y-2 text-data text-ink-2">
                {listed.map((r) => (
                  <li key={r.rule_id} className="flex items-start gap-2">
                    <Badge tone={r.passed ? "ok" : "critical"}>{r.passed ? "Pass" : "Fail"}</Badge>
                    <span className="min-w-0 leading-snug">
                      <span className="mono text-ink-3">{r.rule_id}</span> {r.title}
                    </span>
                  </li>
                ))}
              </ul>
              <p className="mt-3 text-data">
                <Link href="/sources?tab=validation" className="font-medium">
                  All {report.total} rules and what they check <ArrowRight aria-hidden size={13} className="inline align-[-2px]" />
                </Link>
              </p>
            </>
          );
        }}
      </Async>
    </Panel>
  );
}

const PORTFOLIO: Column<DrugSummary>[] = [
  {
    key: "molecule",
    header: "Molecule",
    rowHeader: true,
    className: "min-w-[150px] sm:min-w-[220px]",
    cell: (d) => {
      const code = d.code_name ?? d.brand_names?.join(" / ");
      return (
        <>
          <Link href={`/molecules?drug=${d.slug}`} className="font-semibold">
            {d.name}
          </Link>
          {code ? <span className="mono ml-2 text-ink-3" translate="no">{code}</span> : null}
          {d.tier_group === "ADJACENT" ? <span className="block text-caption text-ink-3">Adjacent pipeline</span> : null}
        </>
      );
    },
  },
  { key: "class", header: "Class (derived)", className: "min-w-[220px]", cell: (d) => d.classification.class_label },
  { key: "status", header: "Regulatory status", cell: (d) => <RegulatoryBadge status={d.regulatory_status} /> },
  {
    key: "source",
    header: "Status source",
    className: "min-w-[150px]",
    cell: (d) =>
      d.regulatory_source ? (
        <>
          <TierBadge tier={d.regulatory_source.tier} label={d.regulatory_source.tier_label} />
          <span className="num mt-0.5 block whitespace-nowrap text-caption text-ink-3">as of {d.regulatory_as_of ?? "date not recorded"}</span>
        </>
      ) : (
        <Missing>Source not recorded</Missing>
      ),
  },
  { key: "trials", header: "Trials tracked", align: "right", cell: (d) => formatCount(d.n_studies) },
];

const READOUTS: Column<StudyBrief>[] = [
  {
    key: "trial",
    header: "Trial",
    rowHeader: true,
    className: "min-w-[150px]",
    cell: (s) => (
      <Link href={`/trials/${s.nct_id}`} className="font-medium">
        {s.display_name}
      </Link>
    ),
  },
  { key: "phase", header: "Phase", className: "whitespace-nowrap", cell: (s) => phaseLabel(s.phase) },
  { key: "status", header: "Status", cell: (s) => trialStatusLabel(s.overall_status) },
  {
    key: "completion",
    header: "Primary completion",
    className: "whitespace-nowrap",
    cell: (s) =>
      s.primary_completion_date ? (
        <>
          <span className="num">{s.primary_completion_date}</span>
          {s.primary_completion_type ? <span className="text-ink-3"> ({s.primary_completion_type.toLowerCase()})</span> : null}
        </>
      ) : (
        <Missing />
      ),
  },
];

function Dashboard({ data }: { data: Overview }) {
  const m = data.meta;
  return (
    <>
      <div className="mb-8 grid grid-cols-1 items-start gap-5 lg:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
        <Panel as="section" title="Evidence on record" level={2} className="!p-5">
          <div className="grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-4">
            <StatTile label="Trials in depth" value={formatCount(m.counts.studies)} note={`${formatCount(m.counts.discovered_trials)} in the registry index`} />
            <StatTile label="Reported results" value={formatCount(m.counts.results)} note={`${formatCount(m.counts.analyses)} between-arm analyses`} />
            <StatTile label="Sources" value={formatCount(m.counts.sources)} note="Hashed and versioned" />
            <StatTile label="Open evidence gaps" value={formatCount(m.counts.gaps)} note={`${formatCount(m.counts.open_review_items)} items awaiting review`} />
          </div>
          <VerificationBreakdown meta={m} />
        </Panel>
        <ValidationPanel />
      </div>

      <Section title="Portfolio" hint="Pharmacologic class is derived from sourced receptor actions. Regulatory status reads “approved” only when FDA labeling is on record.">
        <DataTable
          label="Portfolio molecules"
          columns={PORTFOLIO}
          rows={data.drugs}
          rowKey={(d) => d.slug}
          maxHeight="none"
          pinFirstColumn
          minWidth={760}
          empty={<p className="px-3 py-5 text-ink-3">No molecule is on record.</p>}
          footer={<p className="num text-data text-ink-2">{formatCount(data.drugs.length)} of {formatCount(data.drugs.length)} tracked molecules</p>}
        />
      </Section>

      <div className="grid grid-cols-1 gap-x-8 lg:grid-cols-2">
        <Section title="Worked comparisons" hint="Each opens with its classification and the reasons for it.">
          {data.presets.length === 0 ? (
            <EmptyState title="No worked comparison is available">Build one in the comparison laboratory by choosing two reported results.</EmptyState>
          ) : (
            <ul className="divide-y divide-hairline rounded-panel border border-hairline bg-surface">
              {data.presets.map((p) => (
                <li key={`${p.title}:${p.a}:${p.b}`} className="px-4 py-3">
                  {p.a === p.b ? (
                    <p className="text-body font-medium text-ink">{p.title}</p>
                  ) : (
                    <Link href={`/compare?a=${p.a}&b=${p.b}`} className="text-body font-medium">
                      {p.title}
                    </Link>
                  )}
                  <p className="mt-0.5 text-data text-ink-3">{p.question}</p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-2">
                    <ClassificationBadge value={p.classification} />
                    {p.a === p.b ? <span className="text-caption text-warn">Both sides resolve to the same stored result, so this comparison cannot be opened.</span> : null}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Section>
        <div>
          <Section title="Highest-priority open questions" action={<Link href="/gaps" className="font-medium">All gaps <ArrowRight aria-hidden size={13} className="inline align-[-2px]" /></Link>}>
            {data.top_gaps.length === 0 ? (
              <EmptyState title="No open question is recorded" />
            ) : (
              <ul className="divide-y divide-hairline rounded-panel border border-hairline bg-surface">
                {data.top_gaps.map((g) => (
                  <li key={g.id} className="px-4 py-3">
                    <p className="text-body font-medium leading-snug text-ink">{g.question}</p>
                    {g.gap_nature_label ? <p className="mt-1 text-caption text-ink-3">{g.gap_nature_label}</p> : null}
                  </li>
                ))}
              </ul>
            )}
          </Section>
          <Section title="Source changes awaiting review" action={<Link href="/sources?tab=changes" className="font-medium">Review queue <ArrowRight aria-hidden size={13} className="inline align-[-2px]" /></Link>}>
            {data.changes.length === 0 ? <p className="text-data text-ink-3">No tracked source has changed since the last ingestion.</p> : null}
            <div className="space-y-2">
              {data.changes.map((c) =>
                c.is_synthetic ? (
                  <Hypothetical key={c.id} label="Synthetic demonstration change">
                    {c.summary} The “before” version was generated from the real record to demonstrate change review; it is not a real registry change.
                  </Hypothetical>
                ) : (
                  <Callout key={c.id} tone="info" title={`Detected ${day(c.detected_at)}`}>
                    {c.summary}
                  </Callout>
                ),
              )}
            </div>
          </Section>
          <Section title="Next expected primary completions" hint="Registry dates for trials without posted results.">
            <DataTable label="Next expected primary completions" columns={READOUTS} rows={data.upcoming_readouts} rowKey={(s) => s.nct_id} maxHeight="none" minWidth={460} empty={<p className="px-3 py-5 text-ink-3">No ongoing trial without posted results has a primary completion date on record.</p>} />
          </Section>
        </div>
      </div>
    </>
  );
}

function DashboardSkeleton() {
  return (
    <div role="status" aria-label="Loading the evidence overview…">
      <div className="mb-8 grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
        <Skeleton className="h-72" />
        <Skeleton className="h-72" />
      </div>
      <Skeleton className="mb-3 h-6 w-40" />
      <Skeleton className="h-80" />
      <span className="sr-only">Loading the evidence overview…</span>
    </div>
  );
}

export function CommandCenter() {
  const overview = useApi<Overview>("/overview");
  return (
    <>
      <PageHeader
        eyebrow="Research command center"
        title="What the public evidence supports — and what it does not"
        lede="Lilly's incretin and amylin portfolio, read from registries, publications, FDA labeling and sponsor disclosures. Every number carries its source, estimand and verification status."
      >
        <LinkButton href="/compare">
          Open the comparison laboratory <ArrowRight aria-hidden size={15} />
        </LinkButton>
      </PageHeader>
      {overview.status === "error" ? (
        <ErrorNote message={`The evidence overview could not be loaded. ${overview.error ?? ""}`} onRetry={overview.reload} />
      ) : (
        <Async state={overview} loading={<DashboardSkeleton />}>
          {(data) => <Dashboard data={data} />}
        </Async>
      )}
    </>
  );
}
