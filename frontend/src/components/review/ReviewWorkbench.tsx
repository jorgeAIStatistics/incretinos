"use client";

import { Keyboard } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo, useRef, useState } from "react";
import { Button, PageHeader, Panel, VerificationBadge } from "@/components/ui";
import { useApi } from "@/lib/api";
import { VERIFICATION_ORDER } from "@/lib/evidence";
import { formatCount } from "@/lib/format";
import { useQueryParams } from "@/lib/hooks";
import { type EvidenceListItem, FILTER_KEYS, type QueueFilters, type ReviewSummary } from "@/lib/review";
import type { Meta } from "@/lib/types";
import { PAGE_SIZES, QueueView } from "./QueueView";
import { RecordView } from "./RecordView";
import { ModeLine, ReviewerAccess, ReviewerSessionProvider } from "./ReviewerSession";
import { ShortcutHelp, useShortcuts } from "./ShortcutHelp";

/*
 * The Evidence Review workbench. The address holds everything needed to come back to the same place: the queue
 * filters, the highlighted position (`pos`) and, when a record is open, its evidence type and lasting evidence key.
 * Opening a record adds a history entry (Back returns to the queue); moving the highlight or changing a filter
 * replaces the current one.
 */

function SummaryStrip({ summary }: { summary: ReviewSummary | null }) {
  if (!summary?.by_status) return null;
  const by = summary.by_status;
  return (
    <dl id="review-summary" className="mb-4 flex scroll-mt-4 flex-wrap gap-x-5 gap-y-2 text-data" aria-label="Reviewable records by verification status" data-testid="review-summary">
      {VERIFICATION_ORDER.map((s) => (
        <div key={s} className="flex items-center gap-1.5">
          <dt>
            <VerificationBadge status={s} compact explain={false} />
          </dt>
          <dd className="num font-semibold text-ink">{formatCount(by[s] ?? 0)}</dd>
        </div>
      ))}
      {summary.open_requests_total !== undefined ? (
        <div className="flex items-center gap-1.5">
          <dt className="text-ink-2">Open requests</dt>
          <dd className="num font-semibold text-ink">{formatCount(summary.open_requests_total)}</dd>
        </div>
      ) : null}
      {summary.held_total ? (
        <div className="flex items-center gap-1.5" data-testid="summary-held">
          <dt className="text-ink-2">On review hold</dt>
          <dd className="num font-semibold text-warn">{formatCount(summary.held_total)}</dd>
        </div>
      ) : null}
      {summary.orphaned_decisions_total ? (
        <div className="flex items-center gap-1.5" data-testid="summary-orphaned">
          <dt className="text-ink-2">Decisions whose evidence no longer exists</dt>
          <dd className="num font-semibold text-ink">
            {formatCount(summary.orphaned_decisions_total)}
            {summary.orphaned_rejections_total ? <span className="font-normal text-critical"> ({formatCount(summary.orphaned_rejections_total)} rejections: the excluded value may be in use again)</span> : null}
          </dd>
        </div>
      ) : null}
      {summary.decisions_recorded !== undefined ? (
        <div className="flex items-center gap-1.5">
          <dt className="text-ink-2">Decisions recorded</dt>
          <dd className="num font-semibold text-ink">{formatCount(summary.decisions_recorded)}</dd>
        </div>
      ) : null}
    </dl>
  );
}

export function ReviewWorkbench() {
  const meta = useApi<Meta>("/meta");
  const summary = useApi<ReviewSummary>("/review/summary");
  const enabled = meta.data ? meta.data.review_enabled === true : meta.status === "error" ? (summary.data?.review_enabled ?? null) : null;
  // Whether the service names the reviewer from the token; until known, a typed name is asked for.
  const identityMode = meta.data?.reviewer_identity ?? summary.data?.reviewer_identity ?? null;
  return (
    <ReviewerSessionProvider enabled={enabled} identityMode={identityMode}>
      <Workbench summary={summary.data} reloadSummary={summary.reload} />
    </ReviewerSessionProvider>
  );
}

function Workbench({ summary, reloadSummary }: { summary: ReviewSummary | null; reloadSummary: () => void }) {
  const router = useRouter();
  const params = useSearchParams();
  const [get, set] = useQueryParams();
  const [help, setHelp] = useState(false);
  const searchRef = useRef<HTMLDivElement>(null);

  const filters = useMemo<QueueFilters>(() => {
    const f: QueueFilters = {};
    for (const k of FILTER_KEYS) {
      const v = params.get(k);
      if (v) f[k] = v;
    }
    const sort = params.get("sort");
    if (sort) f.sort = sort;
    if (params.get("desc") === "1") f.desc = "1";
    return f;
  }, [params]);

  const size = PAGE_SIZES.includes(Number(get("size"))) ? Number(get("size")) : PAGE_SIZES[0];
  const posParam = get("pos");
  const pos = /^\d+$/.test(posParam) ? Number(posParam) : null;
  const type = get("type");
  const key = get("key");
  const id = get("id");
  const recordOpen = !!type && (!!key || !!id);

  /** An address on this page with the current filters kept and `patch` applied. */
  const href = useCallback(
    (patch: Record<string, string | number | null | undefined>) => {
      const next = new URLSearchParams(params.toString());
      for (const [k, v] of Object.entries(patch)) {
        if (v === null || v === undefined || v === "") next.delete(k);
        else next.set(k, String(v));
      }
      const q = next.toString();
      return q ? `/review?${q}` : "/review";
    },
    [params],
  );

  const recordHrefAt = useCallback((t: string, k: string, at: number | null) => href({ type: t, key: k, id: null, pos: at }), [href]);
  const navigate = useCallback((to: string, mode: "push" | "replace" = "push") => (mode === "push" ? router.push(to, { scroll: true }) : router.replace(to, { scroll: false })), [router]);
  const backHref = href({ type: null, key: null, id: null });

  const setParams = useCallback((patch: Record<string, string | null>) => set(patch), [set]);
  const openRow = useCallback((row: EvidenceListItem, at: number) => navigate(recordHrefAt(row.evidence_type, row.evidence_key, at)), [navigate, recordHrefAt]);

  useShortcuts({ "?": () => setHelp(true) });

  return (
    <>
      <PageHeader
        eyebrow="Review and provenance"
        title="Evidence Review"
        lede="Check each stored record against its original source, then record a decision on that exact version. Every decision is written to the audit log with your rationale."
      >
        <ModeLine />
        <Button variant="secondary" size="sm" Icon={Keyboard} onClick={() => setHelp(true)} aria-label="Keyboard shortcuts (?)">
          Shortcuts
        </Button>
      </PageHeader>

      <SummaryStrip summary={summary} />

      {recordOpen ? (
        <RecordView
          key={`${type}:${key || id}`}
          type={type}
          evidenceKey={key || null}
          rowId={key ? null : id || null}
          filters={filters}
          pos={pos}
          hrefFor={recordHrefAt}
          navigate={navigate}
          backHref={backHref}
          onDecided={reloadSummary}
        />
      ) : (
        <>
          <Panel as="section" title="Reviewer access" level={2} className="mb-4">
            <ReviewerAccess />
          </Panel>
          <QueueView filters={filters} pos={pos ?? 0} size={size} setParams={setParams} hrefFor={(row, at) => recordHrefAt(row.evidence_type, row.evidence_key, at)} onOpen={openRow} searchRef={searchRef} />
        </>
      )}

      <ShortcutHelp open={help} onOpenChange={setHelp} />
    </>
  );
}
