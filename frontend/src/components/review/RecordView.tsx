"use client";

import { ArrowLeft, ChevronLeft, ChevronRight, Link2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { Button, ErrorState, Loading, Tabs, TabPanel, useToast } from "@/components/ui";
import { apiGet, useApi, withQuery } from "@/lib/api";
import { useMediaQuery } from "@/lib/hooks";
import { type DecisionKind, type DecisionResponse, type EvidenceDetail, type EvidenceListResponse, evidenceTypeLabel, type QueueFilters, queuePath, recordHref, shortHash } from "@/lib/review";
import { AssurancePanel } from "./AssurancePanel";
import { DecisionDialog } from "./DecisionDialog";
import { EvidencePanel } from "./EvidencePanel";
import { ReviewActions, StatusSummary } from "./ReviewActions";
import { useReviewer } from "./ReviewerSession";
import { Kbd, useShortcuts } from "./ShortcutHelp";
import { SourcePanel } from "./SourcePanel";

/*
 * One record, three panels: the original source on the left, the structured evidence in the middle, checks,
 * history and the review actions on the right. Below 1280 px the panels become tabs, with the status and actions
 * kept above them so a decision is always one step away.
 *
 * `pos` is the record's index in the filtered queue it was opened from. Next and previous ask the service for the
 * neighbouring row under the same filters, so they work across pages and stay correct after a decision moves the
 * current record out of the filtered queue.
 */

type Props = {
  type: string;
  evidenceKey: string | null;
  rowId: string | null;
  filters: QueueFilters;
  pos: number | null;
  /** Address of another record under the same filters. */
  hrefFor: (type: string, key: string, pos: number | null) => string;
  navigate: (href: string, mode?: "push" | "replace") => void;
  backHref: string;
  onDecided: () => void;
};

const PANEL_TABS = [
  { value: "source", label: "Source" },
  { value: "evidence", label: "Extracted evidence" },
  { value: "checks", label: "Checks and history" },
];

export function RecordView({ type, evidenceKey, rowId, filters, pos, hrefFor, navigate, backHref, onDecided }: Props) {
  const path = evidenceKey ? `/review/evidence/by-key/${encodeURIComponent(type)}/${encodeURIComponent(evidenceKey)}` : rowId ? `/review/evidence/${encodeURIComponent(type)}/${encodeURIComponent(rowId)}` : null;
  const detail = useApi<EvidenceDetail>(path);
  const position = useApi<EvidenceListResponse>(pos !== null ? withQuery(queuePath(filters), { limit: 1, offset: pos }) : null);
  const wide = useMediaQuery("(min-width: 1280px)");
  const [tab, setTab] = useState("source");
  const [dialog, setDialog] = useState<DecisionKind | null>(null);
  const [lastRecorded, setLastRecorded] = useState<{ key: string; result: DecisionResponse } | null>(null);
  const [staleFor, setStaleFor] = useState<string | null>(null);
  const [moving, setMoving] = useState(false);
  const { canDecide, enabled, token } = useReviewer();
  const { toast } = useToast();

  const d = detail.data;
  const currentKey = d?.evidence_key ?? evidenceKey;
  const stale = !!d?.fingerprint && staleFor === d.fingerprint;

  // Opened by row id (an older link): once the lasting key is known, the address carries the key instead.
  useEffect(() => {
    if (!evidenceKey && d?.evidence_key) navigate(hrefFor(d.evidence_type, d.evidence_key, pos), "replace");
  }, [evidenceKey, d, hrefFor, navigate, pos]);

  const total = position.data?.total ?? null;
  const atPos = position.data?.rows?.[0] ?? null;
  const inQueue = !!atPos && atPos.evidence_key === currentKey && atPos.evidence_type === type;

  const go = async (direction: 1 | -1) => {
    if (pos === null || moving) return;
    // After a decision the record may have left the filtered queue; the record now at its position is then "next".
    const target = direction === 1 ? (inQueue || !position.data ? pos + 1 : pos) : pos - 1;
    if (target < 0 || (total !== null && target >= total)) return;
    setMoving(true);
    try {
      const page = await apiGet<EvidenceListResponse>(withQuery(queuePath(filters), { limit: 1, offset: target }));
      const row = page.rows?.[0];
      if (row) navigate(hrefFor(row.evidence_type, row.evidence_key, target));
      else toast({ tone: "info", title: direction === 1 ? "This is the last record in the queue" : "This is the first record in the queue" });
    } catch (e) {
      toast({ tone: "critical", title: "The next record could not be loaded", description: e instanceof Error ? e.message : String(e) });
    } finally {
      setMoving(false);
    }
  };

  const canGoBack = pos !== null && pos > 0;
  const canGoNext = pos !== null && total !== null && (inQueue ? pos + 1 < total : pos < total);
  const actionable = !!d && d.reconciled !== false && !!d.fingerprint && !stale;

  const open = (decision: DecisionKind) => {
    if (!d) return;
    if (!actionable) return;
    if (!canDecide) {
      toast({
        tone: "info",
        title: "Read-only public mode",
        description: enabled === false ? "This deployment accepts no decisions." : !token ? "Enter the reviewer token in the review actions to record a decision." : "Enter your name to record a decision.",
      });
      return;
    }
    setDialog(decision);
  };

  useShortcuts({
    j: canGoNext ? () => void go(1) : undefined,
    k: canGoBack ? () => void go(-1) : undefined,
    a: () => open("APPROVE"),
    r: () => open("REJECT"),
    i: () => open("REQUEST_INFO"),
    f: () => open("FLAG_WRONG_ATTRIBUTION"),
    c: () => open("PROPOSE_CORRECTION"),
    Escape: () => navigate(backHref),
  });

  const recorded = (result: DecisionResponse) => {
    setDialog(null);
    setLastRecorded({ key: result.evidence_key, result });
    detail.reload();
    position.reload();
    onDecided();
  };

  const reload = () => {
    setStaleFor(null);
    detail.reload();
  };

  const header = (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-3 border-b border-hairline pb-3">
      <div className="flex flex-wrap items-center gap-2">
        <Link href={backHref} className="inline-flex items-center gap-1 text-data font-medium text-accent hover:underline">
          <ArrowLeft aria-hidden size={14} /> Back to the queue
        </Link>
        <span className="text-caption text-ink-3">
          <Kbd>Esc</Kbd>
        </span>
      </div>
      <nav aria-label="Records in this queue" className="flex flex-wrap items-center gap-2 text-data text-ink-2">
        <p role="status" className="num" data-testid="record-position">
          {pos === null
            ? "Opened by link"
            : total === null
              ? "Finding the record’s place in the queue…"
              : inQueue
                ? `Record ${pos + 1} of ${total} in this queue`
                : `No longer in this filtered queue (its status changed); ${total} remain`}
        </p>
        <Button variant="secondary" size="sm" Icon={ChevronLeft} disabled={!canGoBack || moving} onClick={() => void go(-1)} aria-label="Previous record (k)">
          Previous
        </Button>
        <Button variant="secondary" size="sm" disabled={!canGoNext || moving} onClick={() => void go(1)} aria-label="Next record (j)">
          Next
          <ChevronRight aria-hidden size={13} />
        </Button>
      </nav>
    </div>
  );

  if (detail.status === "error") {
    return (
      <>
        {header}
        <ErrorState title="This record could not be loaded" message={detail.error ?? ""} onRetry={detail.reload} />
      </>
    );
  }
  if (!d || wide === null) {
    return (
      <>
        {header}
        <Loading rows={8} label="Loading the record…" />
      </>
    );
  }

  const statusAndActions = (
    <div className="flex flex-col gap-4">
      <StatusSummary detail={d} lastRecorded={lastRecorded && lastRecorded.key === d.evidence_key ? lastRecorded.result : null} />
      <ReviewActions detail={d} onAction={open} stale={stale} onReload={reload} />
    </div>
  );

  const panelTitle = (text: string) => <h2 className={wide ? "mb-3 text-body font-semibold text-ink" : "sr-only"}>{text}</h2>;

  return (
    <article aria-labelledby="record-title" data-testid="record-view" data-evidence-key={d.evidence_key ?? ""} aria-busy={detail.refreshing || undefined}>
      {header}
      <div className="mb-4">
        <p className="text-caption text-ink-3">
          {evidenceTypeLabel(d.evidence_type)}
          {d.origin ? ` · ${d.origin === "CURATED" ? "Curated" : d.origin === "REGISTRY" ? "Registry" : d.origin}` : ""} · key{" "}
          <span className="mono" title={d.evidence_key ?? ""}>
            {shortHash(d.evidence_key, 12)}
          </span>{" "}
          · version{" "}
          <span className="mono" title={d.fingerprint ?? ""}>
            {shortHash(d.fingerprint, 12)}
          </span>
        </p>
        <h2 id="record-title" className="mt-0.5 text-title font-semibold leading-snug text-ink">
          {d.label ?? "Unlabelled record"}
        </h2>
        {d.evidence_key ? (
          <Link href={recordHref(d.evidence_type, d.evidence_key)} className="mt-1 inline-flex items-center gap-1 text-caption text-accent hover:underline">
            <Link2 aria-hidden size={12} /> Permanent link to this record
          </Link>
        ) : null}
      </div>

      {wide ? (
        <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)] items-start gap-4">
          <section aria-label="Original source" className="min-w-0 rounded-panel border border-hairline bg-surface p-4">
            {panelTitle("Original source")}
            <SourcePanel detail={d} />
          </section>
          <section aria-label="Extracted evidence" className="min-w-0 rounded-panel border border-hairline bg-surface p-4">
            {panelTitle("Extracted evidence")}
            <EvidencePanel detail={d} />
          </section>
          <section aria-label="Checks, history and decision" className="min-w-0 rounded-panel border border-hairline bg-surface p-4">
            {panelTitle("Decision, checks and history")}
            {statusAndActions}
            <div className="mt-4 border-t border-hairline pt-4">
              <AssurancePanel detail={d} />
            </div>
          </section>
        </div>
      ) : (
        <>
          <section aria-label="Decision" className="mb-4 rounded-panel border border-hairline bg-surface p-4">
            {panelTitle("Decision")}
            {statusAndActions}
          </section>
          <Tabs value={tab} onValueChange={setTab} items={PANEL_TABS} label="Parts of this record">
            <TabPanel value="source">
              <section aria-label="Original source" className="rounded-panel border border-hairline bg-surface p-4">
                {panelTitle("Original source")}
                <SourcePanel detail={d} />
              </section>
            </TabPanel>
            <TabPanel value="evidence">
              <section aria-label="Extracted evidence" className="rounded-panel border border-hairline bg-surface p-4">
                {panelTitle("Extracted evidence")}
                <EvidencePanel detail={d} />
              </section>
            </TabPanel>
            <TabPanel value="checks">
              <section aria-label="Checks and history" className="rounded-panel border border-hairline bg-surface p-4">
                {panelTitle("Checks and history")}
                <AssurancePanel detail={d} />
              </section>
            </TabPanel>
          </Tabs>
        </>
      )}

      {dialog ? (
        <DecisionDialog
          key={`${dialog}-${d.fingerprint}`}
          detail={d}
          initial={dialog}
          onClose={() => setDialog(null)}
          onRecorded={recorded}
          onStale={reload}
          onConflict={() => setStaleFor(d.fingerprint)}
        />
      ) : null}
    </article>
  );
}
