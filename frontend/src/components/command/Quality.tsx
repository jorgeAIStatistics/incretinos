"use client";

import Link from "next/link";
import { Async, Loading, NextLink, Term, VerificationBadge } from "@/components/ui";
import type { ApiState } from "@/lib/api";
import { countByStatus, VERIFICATION_ORDER } from "@/lib/evidence";
import { formatCount, share } from "@/lib/format";
import { type QualityDimensionId, type QualityMetric, type QualityOverview, shareText } from "@/lib/quality";
import type { Meta } from "@/lib/types";

/**
 * How the stored results were checked. Every status the API reports is counted, including ones this interface
 * does not know. Quote-anchored and machine-extracted results are listed as what they are: mechanical checks.
 * They are never added together into a "verified" total; only a recorded human decision is verification.
 */
export function VerificationBreakdown({ meta }: { meta: Meta }) {
  const { known, unknown, total } = countByStatus(meta.result_verification);
  const rows = [...VERIFICATION_ORDER.map((status) => ({ status: status as string, n: known[status] })), ...unknown.map((u) => ({ status: u.status, n: u.n }))];
  return (
    <div>
      <h3 className="text-body font-semibold text-ink">How the stored results were checked</h3>
      <p className="mt-0.5 max-w-[60ch] text-data text-ink-3">
        A result counts as verified only after a reviewer&rsquo;s recorded decision. <Term k="machine-extracted">Machine-extracted</Term> and <Term k="quote-anchored">quote-anchored</Term> are
        automatic checks, not verification.
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
                <td className="w-[40%] py-1">
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
      <p className="mt-2 text-caption text-ink-3">
        <span className="num">{formatCount(total)}</span> stored results, including any not in analytic use (the ledger above counts results in analytic use). Every reviewable record type is counted on the{" "}
        <Link href="/review">Evidence Review</Link> page.
      </p>
    </div>
  );
}

/**
 * The counts shown for each quality dimension, by the metric ids of GET /api/quality. A metric the API does not
 * return is left out; nothing is computed here.
 */
const PICKS: Record<QualityDimensionId, string[]> = {
  review_status: ["status_human_verified", "status_pending_human_review", "status_rejected"],
  completeness: ["result_no_ci", "result_estimand_not_stated", "analysis_multiplicity_not_stated"],
  authority: ["press_release_only", "authority_sources"],
  reliability: ["risk_high", "source_conflicts", "source_conflicts_insufficient"],
  precision: ["result_with_interval", "analysis_with_interval"],
  freshness: ["sources_changed", "sources_refresh_due"],
};

function Figure({ m }: { m: QualityMetric }) {
  const href = m.links.records?.href ?? null;
  return (
    <div className="min-w-0" data-testid={`quality-metric-${m.id}`}>
      <p className="text-data text-ink-2">{m.label}</p>
      <p className="mt-0.5 text-data text-ink-3">
        <span className="num text-title font-semibold text-ink">
          {href && m.count > 0 ? (
            <Link href={href} className="text-ink decoration-accent hover:text-accent" aria-label={`${formatCount(m.count)} ${m.label.toLowerCase()}: open the records`}>
              {formatCount(m.count)}
            </Link>
          ) : (
            formatCount(m.count)
          )}
        </span>{" "}
        of <span className="num">{formatCount(m.denominator)}</span> {m.denominator_label}
        {m.denominator > 0 ? <span className="num"> ({shareText(m.count, m.denominator)})</span> : null}
      </p>
    </div>
  );
}

function Dimensions({ data }: { data: QualityOverview }) {
  const byId = new Map(data.metrics.map((m) => [m.id, m]));
  return (
    <dl className="divide-y divide-hairline border-y border-hairline" aria-label="Evidence quality by dimension">
      {data.dimensions.map((d) => {
        const metrics = (PICKS[d.id] ?? []).map((id) => byId.get(id)).filter((m): m is QualityMetric => !!m);
        return (
          <div key={d.id} className="grid grid-cols-1 gap-x-6 gap-y-2 py-3.5 md:grid-cols-[minmax(0,13rem)_minmax(0,1fr)]" data-testid={`quality-dimension-${d.id}`}>
            <dt className="min-w-0">
              <span className="block text-body font-semibold text-ink">{d.label}</span>
              <span className="block text-caption text-ink-3">{d.question}</span>
            </dt>
            <dd className="grid min-w-0 grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-2 xl:grid-cols-3">
              {metrics.length ? metrics.map((m) => <Figure key={m.id} m={m} />) : <span className="text-data text-ink-3">See the Evidence Quality page.</span>}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

export function Quality({ meta, quality }: { meta: Meta; quality: ApiState<QualityOverview> }) {
  return (
    <div className="grid grid-cols-1 gap-x-10 gap-y-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.75fr)]">
      <VerificationBreakdown meta={meta} />
      <div className="min-w-0">
        <h3 className="text-body font-semibold text-ink">Six separate dimensions</h3>
        <p className="mb-3 mt-0.5 max-w-[72ch] text-data text-ink-3">
          Each count opens exactly the records it counts. There is no overall score, and a complete record does not mean a drug works or is safe.
        </p>
        {quality.status === "idle" ? (
          // Waiting for the requests before it (the command center loads its heavy reads in sequence).
          <Loading rows={6} label="Counting the stored evidence…" />
        ) : (
          <Async state={quality} errorTitle="The quality counts could not be loaded" loading={<Loading rows={6} label="Counting the stored evidence…" />}>
            {(data) => <Dimensions data={data} />}
          </Async>
        )}
        <p className="mt-3">
          <NextLink href="/quality">Breakdowns by molecule, trial, tier and source</NextLink>
        </p>
      </div>
    </div>
  );
}
