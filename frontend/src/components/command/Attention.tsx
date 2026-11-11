"use client";

import clsx from "clsx";
import Link from "next/link";
import type { ReactNode } from "react";
import { Badge, ErrorNote, Loading, NextLink, Skeleton, Term } from "@/components/ui";
import type { ConflictListExtra } from "@/components/conflicts/types";
import type { ApiState } from "@/lib/api";
import { formatCount } from "@/lib/format";
import type { ReviewSummary } from "@/lib/review";
import type { ValidationReport } from "@/lib/types";

/*
 * Unresolved work, each count with the page that lists exactly what it counts. The counts are the API's; a count
 * that has not arrived is never shown (a placeholder is), and a zero is shown as zero, not dropped.
 */

type Item = { key: string; label: ReactNode; note?: ReactNode; count: number | null; href: string; linkName: string; state: "loading" | "error" | "ready"; tone?: "warn" | "critical" };

function Row({ item }: { item: Item }) {
  const zero = item.count === 0;
  return (
    <li className="grid grid-cols-[4.25rem_minmax(0,1fr)] items-baseline gap-x-3 py-2.5" data-testid={`attention-${item.key}`}>
      <span className="num text-right text-heading font-semibold leading-none">
        {item.state === "loading" ? (
          <Skeleton className="ml-auto h-5 w-10" />
        ) : item.state === "error" || item.count === null ? (
          <span className="text-caption font-normal text-ink-3">Not loaded</span>
        ) : zero ? (
          <span className="text-ink-3">0</span>
        ) : (
          <Link href={item.href} aria-label={`${formatCount(item.count)} ${item.linkName}: open the list`} className={clsx("decoration-accent hover:text-accent", item.tone === "critical" ? "text-critical" : item.tone === "warn" ? "text-warn" : "text-ink")}>
            {formatCount(item.count)}
          </Link>
        )}
      </span>
      <span className="min-w-0">
        <span className="block text-data font-medium text-ink">{item.label}</span>
        {item.note ? <span className="block text-caption text-ink-3">{item.note}</span> : null}
      </span>
    </li>
  );
}

const stateOf = (s: ApiState<unknown>): Item["state"] => (s.status === "error" ? "error" : s.data ? "ready" : "loading");

export function Attention({
  summary,
  conflicts,
  queueItems,
  validation,
}: {
  summary: ApiState<ReviewSummary>;
  conflicts: ApiState<ConflictListExtra>;
  /** Open items of the manual review queue (meta.counts.open_review_items). */
  queueItems: number;
  validation: ApiState<ValidationReport>;
}) {
  const by = summary.data?.by_status ?? {};
  const s = stateOf(summary);
  const c = stateOf(conflicts);
  const counts = conflicts.data?.counts;
  const items: Item[] = [
    {
      key: "pending",
      label: <Term k="pending-human-review">Pending human review</Term>,
      note: "Evidence records with no automatic support, waiting for a reviewer",
      count: summary.data ? (by.PENDING_HUMAN_REVIEW ?? 0) : null,
      href: "/review?status=PENDING_HUMAN_REVIEW",
      linkName: "evidence records pending human review",
      state: s,
      tone: "warn",
    },
    {
      key: "recheck",
      label: "Changed since review, recheck required",
      note: "A decision exists, but the record or its source changed afterwards",
      count: summary.data ? (by.SOURCE_CHANGED_RECHECK_REQUIRED ?? 0) : null,
      href: "/review?status=SOURCE_CHANGED_RECHECK_REQUIRED",
      linkName: "records changed since review",
      state: s,
      tone: "warn",
    },
    {
      key: "conflicts",
      label: <Term k="source-conflict">True source conflicts</Term>,
      note: "Groups of sources reporting the same quantity with different values",
      count: counts ? (counts.TRUE_CONFLICT ?? 0) : null,
      href: "/conflicts?class=TRUE_CONFLICT",
      linkName: "true source conflicts",
      state: c,
      tone: "critical",
    },
    {
      key: "insufficient",
      label: "Differences that cannot be reconciled",
      note: "A detail needed to tell whether the sources report the same quantity is not stored",
      count: counts ? (counts.INSUFFICIENT_INFORMATION ?? 0) : null,
      href: "/conflicts?class=INSUFFICIENT_INFORMATION",
      linkName: "source differences with insufficient information",
      state: c,
    },
    {
      key: "held",
      label: <Term k="review-hold">On review hold</Term>,
      note: "Excluded from analysis until a new version is decided",
      count: summary.data ? (summary.data.held_total ?? 0) : null,
      href: "/quality/records?metric=review_hold",
      linkName: "records on review hold",
      state: s,
      tone: "warn",
    },
    {
      key: "orphaned",
      label: <Term k="orphaned-decision">Orphaned decisions</Term>,
      note: summary.data?.orphaned_rejections_total ? `${formatCount(summary.data.orphaned_rejections_total)} of them rejections: the excluded value may be in use again` : "Decisions whose evidence record no longer exists",
      count: summary.data ? (summary.data.orphaned_decisions_total ?? 0) : null,
      href: "/quality/records?metric=orphaned_decisions",
      linkName: "orphaned review decisions",
      state: s,
      tone: summary.data?.orphaned_rejections_total ? "critical" : undefined,
    },
    {
      key: "requests",
      label: "Open review requests",
      note: "Requests for information that no decision has closed yet",
      count: summary.data ? (summary.data.open_requests_total ?? 0) : null,
      href: "/review?changed=request",
      linkName: "open review requests",
      state: s,
    },
    {
      key: "queue",
      label: "Manual review-queue items",
      note: "Source changes, ingestion problems and statements awaiting a decision",
      count: queueItems,
      href: "/sources?tab=changes",
      linkName: "open items in the manual review queue",
      state: "ready",
    },
  ];

  return (
    <>
      {summary.status === "error" ? <ErrorNote message={`The review counts could not be loaded. ${summary.error ?? ""}`} onRetry={summary.reload} /> : null}
      {conflicts.status === "error" ? (
        <div className="mt-2">
          <ErrorNote message={`The source-conflict counts could not be loaded. ${conflicts.error ?? ""}`} onRetry={conflicts.reload} />
        </div>
      ) : null}
      <ul className="divide-y divide-hairline" aria-label="Unresolved review work">
        {items.map((item) => (
          <Row key={item.key} item={item} />
        ))}
      </ul>
      <p className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        <NextLink href="/review">Open Evidence Review</NextLink>
        <NextLink href="/conflicts">All source groups</NextLink>
      </p>
      <Validation state={validation} />
    </>
  );
}

/** The scientific validation rules: how many pass, and any that fail, by name. Nothing is shown for a count not received. */
function Validation({ state }: { state: ApiState<ValidationReport> }) {
  return (
    <section aria-labelledby="validation-heading" className="mt-5 border-t border-hairline pt-4">
      <h3 id="validation-heading" className="text-body font-semibold text-ink">
        Scientific validation rules
      </h3>
      {state.status === "error" ? (
        <div className="mt-2">
          <ErrorNote message={`The validation rules could not be loaded. ${state.error ?? ""}`} onRetry={state.reload} />
        </div>
      ) : !state.data ? (
        <Loading rows={2} label="Running validation rules…" className="mt-2" />
      ) : (
        <>
          <p className="mt-1 text-title font-semibold text-ink">
            <span className="num">
              {state.data.passed} / {state.data.total}
            </span>{" "}
            <span className="text-data font-medium text-ink-3">passing</span>
          </p>
          {state.data.rules.some((r) => !r.passed) ? (
            <ul className="mt-2 space-y-1.5 text-data text-ink-2">
              {state.data.rules
                .filter((r) => !r.passed)
                .map((r) => (
                  <li key={r.rule_id} className="flex items-start gap-2">
                    <Badge tone="critical">Fail</Badge>
                    <span className="min-w-0">
                      <span className="mono text-ink-3" translate="no">
                        {r.rule_id}
                      </span>{" "}
                      {r.title}
                    </span>
                  </li>
                ))}
            </ul>
          ) : (
            <p className="text-caption text-ink-3">Every rule passes on the stored evidence.</p>
          )}
          <p className="mt-2">
            <NextLink href="/sources?tab=validation">All {state.data.total} rules and what they check</NextLink>
          </p>
        </>
      )}
    </section>
  );
}
