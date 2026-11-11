"use client";

import Link from "next/link";
import { useMemo } from "react";
import { DataTable, type Column, EmptyState } from "@/components/ui";
import { formatCount } from "@/lib/format";
import { type Breakdown, type BreakdownGroup, type QualityMetric, recordsHref, shareText } from "@/lib/quality";

/**
 * One quality dimension, counted within each value of one breakdown. A cell is "count of denominator" for its own
 * metric and opens exactly those records. Metrics that cannot be split this way (a molecule has no evidence tier,
 * an orphaned decision has no trial) are named under the table instead of being shown as zero.
 */
export function BreakdownTable({ breakdown, metrics, dimensionLabel }: { breakdown: Breakdown; metrics: QualityMetric[]; dimensionLabel: string }) {
  const applicable = metrics.filter((m) => breakdown.metrics.includes(m.id));
  const missing = metrics.filter((m) => !breakdown.metrics.includes(m.id));
  const columns = useMemo<Column<BreakdownGroup>[]>(
    () => [
      {
        key: "group",
        header: breakdown.label,
        rowHeader: true,
        className: "min-w-[180px] max-w-xs",
        cell: (g) => (
          <>
            <span className="block font-medium text-ink">{g.label}</span>
            {g.label !== g.value ? <span className="mono block text-caption text-ink-3" translate="no">{g.value}</span> : null}
          </>
        ),
      },
      ...applicable.map<Column<BreakdownGroup>>((m) => ({
        key: m.id,
        header: <span className="inline-block max-w-[11rem] whitespace-normal">{m.label}</span>,
        align: "right",
        cell: (g) => {
          const [count, denominator] = g.counts[m.id] ?? [0, 0];
          if (denominator === 0) return <span className="text-caption text-ink-3">none in scope</span>;
          return (
            <>
              <Link href={recordsHref(m.id, { [breakdown.by]: g.value })} aria-label={`${formatCount(count)} of ${formatCount(denominator)}: ${m.label.toLowerCase()}, ${g.label}`} className="font-semibold">
                {formatCount(count)}
              </Link>
              <span className="block text-caption text-ink-3">
                of {formatCount(denominator)} ({shareText(count, denominator)})
              </span>
            </>
          );
        },
      })),
    ],
    [applicable, breakdown.by, breakdown.label],
  );
  if (!applicable.length) {
    return (
      <EmptyState title={`${dimensionLabel} cannot be broken down by ${breakdown.label.toLowerCase()}`}>
        {missing.map((m) => m.label).join("; ")}: what these count has no {breakdown.label.toLowerCase()}.
      </EmptyState>
    );
  }
  return (
    <>
      <DataTable
        label={`${dimensionLabel} by ${breakdown.label.toLowerCase()}`}
        columns={columns}
        rows={breakdown.groups}
        rowKey={(g) => g.value}
        density="compact"
        pinFirstColumn
        minWidth={220 + applicable.length * 130}
        maxHeight="min(70vh, 720px)"
        empty={<p className="px-3 py-5 text-ink-3">No {breakdown.label.toLowerCase()} has a record in scope.</p>}
        footer={
          <p className="num text-data text-ink-2">
            {formatCount(breakdown.groups.length)} values of {breakdown.label.toLowerCase()}. A record belongs to every value it carries (a trial's records count under each of its molecules), so rows are not added together.
          </p>
        }
      />
      {missing.length ? (
        <p className="mt-2 text-caption text-ink-3">
          Not split by {breakdown.label.toLowerCase()}: {missing.map((m) => m.label).join("; ")}. What these count has no {breakdown.label.toLowerCase()}.
        </p>
      ) : null}
    </>
  );
}
