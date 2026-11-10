"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { ChartFrame } from "@/components/charts";
import { formatCount } from "@/lib/format";
import { type MetricLinks, type QualityMetric, shareText } from "@/lib/quality";

/*
 * Pieces shared by the Evidence Quality pages. A count is always printed as a number and is a link to the records
 * it counts; a bar only repeats that number on a zero-based scale (it is hidden from assistive technology, and never
 * the only carrier of a value). Bars use one series hue: they encode a count, never a status.
 */

/** Zero-based bar for count / denominator, with the share printed beside it. */
export function ShareBar({ count, denominator }: { count: number; denominator: number }) {
  const pct = denominator > 0 ? (100 * count) / denominator : 0;
  return (
    <span className="flex min-w-[7rem] items-center gap-2">
      <span aria-hidden className="relative h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-grid">
        <span className="absolute inset-y-0 left-0 rounded-full bg-series-1" style={{ width: `${Math.min(100, pct)}%` }} />
      </span>
      <span className="num w-12 shrink-0 text-right text-caption text-ink-3">{shareText(count, denominator)}</span>
    </span>
  );
}

/** The count of a metric as a link to its records (or to the page that shows exactly the same records). */
export function CountLink({ count, links, label }: { count: number; links: MetricLinks | null; label: string }) {
  if (!links) return <span className="num font-semibold text-ink">{formatCount(count)}</span>;
  return (
    <Link href={links.href} className="num font-semibold" aria-label={`${formatCount(count)} ${label}: open these records`} data-testid="metric-count">
      {formatCount(count)}
    </Link>
  );
}

/** Where the records of a metric can be opened, said in words. */
export function DrillNote({ links }: { links: MetricLinks }) {
  return (
    <span className="block text-caption text-ink-3">
      {links.page ? (
        <>
          The count opens {pageName(links.page.href)}; <Link href={links.records.href}>list them here</Link>.
        </>
      ) : null}
      {links.context ? (
        <>
          {" "}
          Related: <Link href={links.context.href}>{links.context.label}</Link>.
        </>
      ) : null}
    </span>
  );
}

export function pageName(href: string): string {
  if (href.startsWith("/review")) return "Evidence Review";
  if (href.startsWith("/conflicts")) return "Source Conflicts";
  if (href.startsWith("/gaps")) return "the Evidence Gap Tracker";
  if (href.startsWith("/sources")) return "Sources & Audit";
  return "another page";
}

/**
 * Metrics of one dimension as a table: what is counted (with its rule), the count (a link to the records), the
 * denominator, and the share. Shares are of the metric's own denominator; nothing is summed across metrics.
 */
export function MetricTable({ metrics, label, caption }: { metrics: QualityMetric[]; label: string; caption?: ReactNode }) {
  return (
    <div className="overflow-hidden rounded-panel border border-hairline bg-surface">
      {/* oxlint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <div role="region" aria-label={label} tabIndex={0} className="overflow-x-auto focus-visible:outline-offset-[-2px]">
        <table className="tbl" style={{ minWidth: 560 }}>
          <caption className={caption ? "border-b border-hairline px-3 py-2 text-data text-ink-2" : "sr-only"}>{caption ?? label}</caption>
          <thead>
            <tr>
              <th scope="col">What is counted, and the rule</th>
              <th scope="col" className="r">
                Count
              </th>
              <th scope="col" className="r">
                Of
              </th>
              <th scope="col">Share</th>
            </tr>
          </thead>
          <tbody>
            {metrics.map((m) => (
              <tr key={m.id} data-metric={m.id}>
                <th scope="row" className="min-w-[16rem] max-w-xl">
                  <span className="block font-medium text-ink">{m.label}</span>
                  <span className="block text-caption text-ink-3">{m.rule}</span>
                  <DrillNote links={m.links} />
                </th>
                <td className="r">
                  <CountLink count={m.count} links={m.links} label={m.label.toLowerCase()} />
                </td>
                <td className="r num whitespace-nowrap text-ink-2">
                  {formatCount(m.denominator)} <span className="block text-caption text-ink-3">{m.denominator_label}</span>
                </td>
                <td className="w-40">
                  <ShareBar count={m.count} denominator={m.denominator} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export type BarRow = { key: string; label: string; sub?: string; count: number; href: string | null };

/**
 * Horizontal count bars on one zero-based scale, with the count printed on every row, in a chart frame whose table
 * view lists the same rows. Labels are text, so they wrap instead of being cut.
 */
export function CountBars({ title, subtitle, rows, noun, footnote, level = 3 }: { title: string; subtitle?: ReactNode; rows: BarRow[]; noun: string; footnote?: ReactNode; level?: 2 | 3 | 4 }) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  const total = rows.reduce((sum, r) => sum + r.count, 0);
  const table = (
    <table className="tbl">
      <caption className="sr-only">{title}</caption>
      <thead>
        <tr>
          <th scope="col">Category</th>
          <th scope="col" className="r">
            {noun.charAt(0).toUpperCase() + noun.slice(1)}
          </th>
          <th scope="col" className="r">
            Share of {formatCount(total)}
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key}>
            <th scope="row">
              {r.label}
              {r.sub ? <span className="block text-caption text-ink-3">{r.sub}</span> : null}
            </th>
            <td className="r num">{r.href ? <Link href={r.href}>{formatCount(r.count)}</Link> : formatCount(r.count)}</td>
            <td className="r num">{shareText(r.count, total)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
  return (
    <ChartFrame title={title} subtitle={subtitle} footnote={footnote} table={table} level={level}>
      <ul className="space-y-2 py-1" aria-label={`${title}: ${rows.length} categories; use the table view for the same counts`}>
        {rows.map((r) => (
          <li key={r.key} className="grid grid-cols-1 gap-x-3 gap-y-0.5 sm:grid-cols-[minmax(0,14rem)_minmax(0,1fr)]">
            <span className="min-w-0 text-data text-ink-2">
              {r.label}
              {r.sub ? <span className="block text-caption text-ink-3">{r.sub}</span> : null}
            </span>
            <span className="flex min-w-0 items-center gap-2">
              <span aria-hidden className="relative h-3 min-w-0 flex-1 overflow-hidden rounded-chip bg-grid">
                <span className="absolute inset-y-0 left-0 rounded-chip bg-series-1" style={{ width: `${(100 * r.count) / max}%` }} />
              </span>
              <span className="num w-16 shrink-0 text-right text-data font-semibold">{r.href ? <Link href={r.href}>{formatCount(r.count)}</Link> : formatCount(r.count)}</span>
            </span>
          </li>
        ))}
      </ul>
    </ChartFrame>
  );
}
