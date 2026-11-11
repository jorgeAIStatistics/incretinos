"use client";

import { Check, X } from "lucide-react";
import Link from "next/link";
import { type FormEvent, useState } from "react";
import { Badge, Button, Callout, type Column, DataTable, EmptyState, ErrorState, Hypothetical, Loading, Modal, Pagination, Panel, SecretField, SegmentedControl, TableCard, TableSkeleton, TextArea, TextField, useToast } from "@/components/ui";
import { apiPost, day, errorMessage, useApi } from "@/lib/api";
import { enumLabel, formatCount } from "@/lib/format";
import { rowsOf, usePagedList } from "@/lib/paging";
import { legacyQueueReviewHref } from "@/lib/review";
import type { ChangeEvent, ChangeField, Meta, ReviewQueueExtra, ReviewQueueItem } from "@/lib/types";

export type Reviewer = { name: string; token: string };
type Decision = "APPROVE" | "REJECT" | "NEEDS_INFO";

const DECISIONS: { value: Decision; label: string }[] = [
  { value: "APPROVE", label: "Approve" },
  { value: "REJECT", label: "Reject" },
  { value: "NEEDS_INFO", label: "Needs information" },
];

const show = (value: unknown): string => (value === null || value === undefined || value === "" ? "Not reported" : Array.isArray(value) ? value.join("; ") : String(value));

function ChangeCard({ change }: { change: ChangeEvent }) {
  return (
    <Panel as="article" pad={false} className="mb-5">
      <div className="space-y-2 px-4 pt-4">
        {change.is_synthetic ? <Hypothetical label="Synthetic demonstration">{change.synthetic_notice}</Hypothetical> : null}
        <h2 className="text-body font-semibold text-ink">{change.source.title}</h2>
        <p className="text-data text-ink-3">
          Detected {day(change.detected_at)}. Significance {enumLabel(change.significance).toLowerCase()}. Review status {enumLabel(change.review_status).toLowerCase()}.
        </p>
        <p className="text-data text-ink-2">{change.summary}</p>
      </div>
      <TableCard label={`Field changes in ${change.source.title}`} maxHeight="420px" className="mt-3 rounded-none border-x-0 border-b-0">
        <table className="tbl" style={{ minWidth: 760 }}>
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">Before</th>
              <th scope="col">After</th>
              <th scope="col">Significance</th>
              <th scope="col">Why it matters</th>
            </tr>
          </thead>
          <tbody>
            {change.changes.map((field: ChangeField, i) => (
              <tr key={`${field.label}:${i}`}>
                <td className="min-w-[150px] font-medium">{field.label}</td>
                <td className="max-w-xs break-words text-ink-2">{show(field.before)}</td>
                <td className="max-w-xs break-words text-ink-2">{show(field.after)}</td>
                <td>
                  <Badge tone={field.significance === "HIGH" ? "critical" : field.significance === "MEDIUM" ? "warn" : "muted"}>{enumLabel(field.significance)}</Badge>
                </td>
                <td className="min-w-[200px] text-ink-3">{field.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableCard>
    </Panel>
  );
}

/**
 * Confirmation step for a review decision. A decision is written to the audit log under the reviewer's name and
 * cannot be undone from here, so it is never recorded by a single click: the reviewer sees the item again,
 * chooses the decision and writes the rationale.
 */
function DecisionDialog({ item, initial, reviewer, serverNamesReviewer, onClose, onRecorded }: { item: ReviewQueueItem; initial: Decision; reviewer: Reviewer; serverNamesReviewer: boolean; onClose: () => void; onRecorded: () => void }) {
  const [decision, setDecision] = useState<Decision>(initial);
  const [rationale, setRationale] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { toast } = useToast();
  const tooShort = rationale.trim().length < 3;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (tooShort) {
      setError("Write a rationale of at least 3 characters: it is stored with the decision.");
      // Move focus to the field that needs attention.
      const field = (e.currentTarget as HTMLFormElement).elements.namedItem("rationale");
      if (field instanceof HTMLElement) field.focus();
      return;
    }
    setBusy(true);
    setError(null);
    // With personal reviewer tokens the service records the token's own name; a typed name is not sent.
    const body = serverNamesReviewer ? { decision, rationale: rationale.trim() } : { decision, reviewer: reviewer.name.trim(), rationale: rationale.trim() };
    apiPost(`/review-queue/${item.id}/decision`, body, "POST", { "X-Reviewer-Token": reviewer.token })
      .then(() => {
        toast({ tone: "ok", title: "Decision recorded", description: `${DECISIONS.find((d) => d.value === decision)?.label} was written to the audit log${serverNamesReviewer ? " under the name bound to your token" : ` under ${reviewer.name.trim()}`}.` });
        onRecorded();
        onClose();
      })
      .catch((cause: unknown) => setError(`The decision was not recorded. ${errorMessage(cause)}`))
      .finally(() => setBusy(false));
  };

  return (
    <Modal
      open
      onOpenChange={(open) => !open && !busy && onClose()}
      title="Record a review decision"
      description="The decision is stored with your name and rationale in the audit log. Check the item against its source before you confirm."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form="review-decision" variant={decision === "REJECT" ? "danger" : "primary"} loading={busy}>
            {busy ? "Recording…" : `Record: ${DECISIONS.find((d) => d.value === decision)?.label}`}
          </Button>
        </>
      }
    >
      <form id="review-decision" onSubmit={submit} className="space-y-4">
        <div className="rounded-control border border-hairline bg-sunken px-3 py-2.5 text-data">
          <p className="font-medium text-ink">{item.title}</p>
          {item.payload?.quote ? <p className="mt-1 text-ink-2">“{item.payload.quote}”</p> : null}
          <p className="mt-1 text-ink-3">{item.reason}</p>
        </div>
        <SegmentedControl label="Decision" value={decision} onChange={setDecision} options={DECISIONS} />
        <TextArea label="Rationale" name="rationale" value={rationale} onChange={setRationale} required rows={3} hint="What you checked and why you decided this. It is stored with the decision." error={error} />
        <p className="text-caption text-ink-3">
          {serverNamesReviewer ? (
            "Recording under the name bound to your reviewer token."
          ) : (
            <>
              Recording as <span className="font-medium text-ink-2">{reviewer.name.trim()}</span>.
            </>
          )}
        </p>
      </form>
    </Modal>
  );
}

export function ChangesTab({ reviewer, onReviewer }: { reviewer: Reviewer; onReviewer: (next: Reviewer) => void }) {
  const changes = useApi<unknown>("/changes");
  const queue = usePagedList<ReviewQueueItem, ReviewQueueExtra>("/review-queue", { pageSize: 25, legacyLimit: 1000 });
  const meta = useApi<Meta>("/meta");
  const [pending, setPending] = useState<{ item: ReviewQueueItem; decision: Decision } | null>(null);

  // Whether decisions can be recorded is a fact reported by the service. Until it is known, nothing is claimed either way.
  const reviewEnabled = queue.data?.extra.review_enabled ?? meta.data?.review_enabled ?? null;
  // With personal reviewer tokens (reviewer_identity PERSONAL_TOKENS) the service names the reviewer from the token.
  const serverNamesReviewer = meta.data?.reviewer_identity === "PERSONAL_TOKENS";
  const ready = reviewer.token.length > 0 && (serverNamesReviewer || reviewer.name.trim().length >= 2);
  const events = rowsOf<ChangeEvent>(changes.data);

  const columns: Column<ReviewQueueItem>[] = [
    { key: "type", header: "Type", className: "whitespace-nowrap", cell: (r) => enumLabel(r.item_type) },
    {
      key: "item",
      header: "Item",
      rowHeader: true,
      className: "min-w-[260px] max-w-md",
      cell: (r) => (
        <>
          {r.title}
          {r.payload?.quote ? <span className="block text-caption text-ink-3">“{r.payload.quote}”</span> : null}
          {(() => {
            const href = legacyQueueReviewHref(r);
            return href ? (
              <Link href={href} className="block text-caption text-accent hover:underline" data-testid="legacy-open-in-review">
                Open in Evidence Review
              </Link>
            ) : null;
          })()}
        </>
      ),
    },
    { key: "reason", header: "Reason", className: "min-w-[220px] max-w-sm text-ink-2", cell: (r) => r.reason },
    { key: "priority", header: "Priority", className: "whitespace-nowrap", cell: (r) => enumLabel(r.priority) },
    ...(reviewEnabled
      ? [
          {
            key: "decision",
            header: "Decision",
            className: "whitespace-nowrap",
            cell: (r: ReviewQueueItem) => (
              <span className="flex gap-1.5">
                <Button variant="secondary" size="sm" Icon={Check} disabled={!ready} onClick={() => setPending({ item: r, decision: "APPROVE" })} aria-label={`Approve: ${r.title}`}>
                  Approve
                </Button>
                <Button variant="danger" size="sm" Icon={X} disabled={!ready} onClick={() => setPending({ item: r, decision: "REJECT" })} aria-label={`Reject: ${r.title}`}>
                  Reject
                </Button>
              </span>
            ),
          } satisfies Column<ReviewQueueItem>,
        ]
      : []),
  ];

  return (
    <>
      {changes.status === "error" ? (
        <ErrorState title="Source changes could not be loaded" message={changes.error ?? ""} onRetry={changes.reload} className="mb-5" />
      ) : changes.status === "loading" ? (
        <Loading rows={4} label="Loading source changes…" className="mb-5" />
      ) : events.length === 0 ? (
        <Callout tone="muted" title="No source has changed since the last ingestion." level={2} className="mb-5">
          Run <span className="mono" translate="no">incretinos check-updates --live</span> to re-fetch tracked records.
        </Callout>
      ) : (
        events.map((c) => <ChangeCard key={c.id} change={c} />)
      )}

      <Panel as="section" title="Manual review queue" level={2} className="mb-4">
        {reviewEnabled === null ? (
          queue.status === "error" ? null : <p role="status" className="text-data text-ink-3">Checking whether this deployment accepts review decisions…</p>
        ) : reviewEnabled ? (
          <>
            <p className="max-w-[80ch] text-data text-ink-2">
              {serverNamesReviewer
                ? "Recording a decision requires your personal reviewer token; the service records the name bound to it. Each decision asks for a rationale and is written to the audit log."
                : "Recording a decision requires the reviewer token and your name. Each decision asks for a rationale and is written to the audit log."}
            </p>
            {/* Not a <form>: a name field beside a password field in a form is what password managers offer to save. */}
            <div role="group" aria-label="Reviewer identity" className="mt-3 flex flex-wrap items-start gap-3">
              {serverNamesReviewer ? null : <TextField label="Reviewer name" name="reviewer" autoComplete="name" value={reviewer.name} onChange={(name) => onReviewer({ ...reviewer, name })} hint="Stored with each decision." className="w-full sm:w-60" />}
              <SecretField label="Reviewer token" initial={reviewer.token} onChange={(token) => onReviewer({ ...reviewer, token })} hint="Kept in memory only; cleared on reload." className="w-full sm:w-60" />
            </div>
            {!ready ? <p className="mt-2 text-caption text-ink-3">{serverNamesReviewer ? "Enter your reviewer token to enable the decision buttons." : "Enter your name and the reviewer token to enable the decision buttons."}</p> : null}
          </>
        ) : (
          <p className="max-w-[80ch] text-data text-ink-2">This deployment is read-only: no reviewer token is configured, so decisions cannot be recorded.</p>
        )}
      </Panel>

      {queue.status === "error" ? (
        <ErrorState title="The review queue could not be loaded" message={queue.error ?? ""} onRetry={queue.reload} />
      ) : !queue.data ? (
        <TableSkeleton rows={8} columns={4} label="Loading the review queue…" />
      ) : (
        <DataTable
          label="Open review items"
          columns={columns}
          rows={queue.data.rows}
          rowKey={(r) => r.id}
          refreshing={queue.refreshing}
          minWidth={860}
          toolbar={<p><span className="num font-semibold text-ink">{formatCount(queue.data.total)}</span> open {queue.data.total === 1 ? "item" : "items"}, highest priority first.</p>}
          empty={<EmptyState title="The review queue is empty" className="m-3 border-0">Nothing is waiting for a decision.</EmptyState>}
          footer={<Pagination page={queue.page} pageSize={queue.pageSize} total={queue.data.total} reachable={queue.data.reachable} truncated={queue.data.truncated} onPage={queue.setPage} onPageSize={queue.setPageSize} noun="review items" />}
        />
      )}

      {pending ? <DecisionDialog item={pending.item} initial={pending.decision} reviewer={reviewer} serverNamesReviewer={serverNamesReviewer} onClose={() => setPending(null)} onRecorded={queue.reload} /> : null}
    </>
  );
}
