"use client";

import Link from "next/link";
import { Badge, ClassificationBadge, EmptyState, Hypothetical, NextLink, Term, Timeline, TimelineItem, TierBadge } from "@/components/ui";
import { day, withQuery } from "@/lib/api";
import { enumLabel, formatCount } from "@/lib/format";
import type { ChangeEvent, ComparePreset, Gap } from "@/lib/types";

/** The head of a gap-nature label from the API ("Public reporting gap"); the full label is printed beside it. */
const natureHead = (label: string | null | undefined, code: string): string => (label ? label.split(" — ")[0] : enumLabel(code));

/**
 * Open evidence questions: how many of each nature (each count opens the gap tracker filtered to that nature) and
 * the highest-priority questions with the reason each is open. The nature labels are the API's own words.
 */
export function Gaps({ total, byNature, top, natureLabels }: { total: number; byNature: Record<string, number>; top: Gap[]; /** From GET /api/gaps; until it arrives, the labels of the listed gaps are used and any other nature shows its code in words. */ natureLabels?: Record<string, string> }) {
  const labels = new Map<string, string | null>([...top.map((g) => [g.gap_nature, g.gap_nature_label] as const), ...Object.entries(natureLabels ?? {})]);
  const natures = Object.entries(byNature).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return (
    <>
      <p className="mb-3 max-w-[68ch] text-data text-ink-3">
        A gap in the public record is not confirmation that the sponsor has no data. Each <Term k="evidence-gap">evidence gap</Term> states why it is open.
      </p>
      <table className="w-full border-collapse text-data" data-testid="gaps-by-nature">
        <caption className="sr-only">Open evidence questions by nature</caption>
        <thead className="sr-only">
          <tr>
            <th scope="col">Nature of the gap</th>
            <th scope="col">Open questions</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-hairline">
          {natures.map(([code, n]) => (
            <tr key={code}>
              <th scope="row" className="py-1.5 pr-3 text-left font-normal text-ink-2">
                {natureHead(labels.get(code), code)}
              </th>
              <td className="num w-14 py-1.5 text-right font-semibold">
                <Link href={withQuery("/gaps", { nature: code })} aria-label={`${formatCount(n)} open questions: ${natureHead(labels.get(code), code)}. Open in the gap tracker`}>
                  {formatCount(n)}
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3 className="mt-5 text-body font-semibold text-ink">Highest priority</h3>
      {top.length === 0 ? (
        <EmptyState title="No open question is recorded" className="mt-2" />
      ) : (
        <ol className="mt-2 space-y-3">
          {top.map((g) => (
            <li key={g.id} className="text-data">
              <p className="font-medium leading-snug text-ink">{g.question}</p>
              <p className="mt-0.5 text-caption text-ink-3">
                {g.gap_nature_label ?? enumLabel(g.gap_nature)}
                {g.drug ? (
                  <>
                    {" "}
                    <Link href={withQuery("/gaps", { drug: g.drug.slug })}>All gaps for {g.drug.name}</Link>
                  </>
                ) : null}
              </p>
            </li>
          ))}
        </ol>
      )}
      <p className="mt-3">
        <NextLink href="/gaps">All {formatCount(total)} open questions, ranked</NextLink>
      </p>
    </>
  );
}

/**
 * Recently detected source changes, newest first, on a timeline. A synthetic demonstration change keeps the
 * hatched "not evidence" container and the API's own notice; it is never shown as a real registry change.
 */
export function Changes({ changes }: { changes: ChangeEvent[] }) {
  if (changes.length === 0) return <p className="text-data text-ink-3">No tracked source has changed since the last ingestion.</p>;
  return (
    <Timeline label="Recently detected source changes">
      {changes.map((c) => (
        <TimelineItem key={c.id} date={day(c.detected_at)} dateNote="detected" testId="source-change">
          <div className="flex flex-wrap items-center gap-1.5">
            <TierBadge tier={c.source.tier} label={c.source.tier_label} />
            <Badge tone={c.significance === "HIGH" ? "warn" : "muted"}>{enumLabel(c.significance)} significance</Badge>
            {c.review_status === "OPEN" ? <span className="text-caption font-medium text-ink-2">Awaiting review</span> : <span className="text-caption text-ink-3">{enumLabel(c.review_status)}</span>}
          </div>
          <p className="mt-1">
            <Link href={`/sources?id=${c.source.id}`} className="font-medium">
              {c.source.title}
            </Link>
          </p>
          {c.is_synthetic ? (
            <div className="mt-1.5">
              <Hypothetical label="Synthetic demonstration change">
                {c.summary} {c.synthetic_notice ?? "The “before” version was generated from the real record to demonstrate change review; it is not a real registry change."}
              </Hypothetical>
            </div>
          ) : (
            <p className="mt-0.5 text-ink-2">{c.summary}</p>
          )}
        </TimelineItem>
      ))}
    </Timeline>
  );
}

/** Worked comparisons: each opens in the comparison laboratory with its classification and the reasons for it. */
export function Comparisons({ presets }: { presets: ComparePreset[] }) {
  if (presets.length === 0) return <EmptyState title="No worked comparison is available">Build one in the comparison laboratory by choosing two reported results.</EmptyState>;
  return (
    <ul className="grid grid-cols-1 gap-x-8 md:grid-cols-2" data-testid="worked-comparisons">
      {presets.map((p) => (
        <li key={`${p.title}:${p.a}:${p.b}`} className="border-t border-hairline py-3">
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
  );
}
