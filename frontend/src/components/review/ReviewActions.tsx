"use client";

import { Ban, Check, Flag, MessageSquareMore, PencilLine, RefreshCcw } from "lucide-react";
import { type ReactNode } from "react";
import { Button, Callout, Term, VerificationBadge } from "@/components/ui";
import { DECISION_ORDER, DECISIONS, type DecisionKind, type DecisionResponse, type EvidenceDetail, decisionLabel, stamp } from "@/lib/review";
import { Kbd } from "./ShortcutHelp";
import { ReviewerAccess, useReviewer } from "./ReviewerSession";

/*
 * RIGHT panel, top: the record's status exactly as the API reports it, and the five review actions. An action opens
 * the decision dialog; nothing is recorded by a single click.
 */

const ICON: Record<DecisionKind, typeof Check> = { APPROVE: Check, REJECT: Ban, REQUEST_INFO: MessageSquareMore, FLAG_WRONG_ATTRIBUTION: Flag, PROPOSE_CORRECTION: PencilLine };

export function StatusSummary({ detail, lastRecorded }: { detail: EvidenceDetail; lastRecorded: DecisionResponse | null }) {
  const s = detail.status;
  const base = s.base_verification_status && s.base_verification_status !== s.verification_status ? s.base_verification_status : null;
  const openRequests = s.open_requests ?? [];
  return (
    <div className="flex flex-col gap-2" data-testid="status-summary">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-caption text-ink-3">Verification status</span>
        <span data-testid="record-status" data-status={s.verification_status}>
          <VerificationBadge status={s.verification_status} />
        </span>
      </div>
      {base ? (
        <p className="flex flex-wrap items-center gap-1.5 text-caption text-ink-2">
          Without the decision it would be <VerificationBadge status={base} compact />
        </p>
      ) : null}
      {s.reason_text ? <p className="text-data text-ink-2" data-testid="status-reason">{s.reason_text}</p> : null}
      {s.decided_by ? (
        <p className="text-caption text-ink-2">
          {decisionLabel(s.decided_by.decision)} by <span className="font-medium text-ink">{s.decided_by.reviewer ?? "a reviewer"}</span> on {stamp(s.decided_by.decided_at)}
        </p>
      ) : null}
      {s.usable === false ? (
        <p className="flex items-start gap-1.5 text-data font-medium text-critical" data-testid="excluded-note">
          <Ban aria-hidden size={14} className="mt-[3px] shrink-0" />
          Excluded from every comparison, summary, calculation and report. Kept here for audit.
        </p>
      ) : null}
      {s.exclusion_hold ? <p className="text-data font-medium text-warn">On exclusion hold: a reviewer rejected an earlier version of this record.</p> : null}
      {s.verification_status === "QUOTE_ANCHORED" || s.base_verification_status === "QUOTE_ANCHORED" ? (
        <p className="text-caption text-ink-2">
          <Term k="quote-anchored">Quote-anchored</Term> means the quoted words were found in the source. It is not a review: nobody has checked that the number belongs to this study, arm, endpoint, estimand or population.
        </p>
      ) : null}
      {openRequests.length ? (
        <p className="text-caption text-ink-2">
          {openRequests.length} open {openRequests.length === 1 ? "request" : "requests"}: {openRequests.map((r) => `${decisionLabel(r.decision)} by ${r.reviewer}`).join("; ")}
        </p>
      ) : null}
      {lastRecorded ? (
        <p role="status" className="rounded-control border border-control px-2 py-1.5 text-caption text-ink-2" data-testid="last-recorded">
          Recorded just now: {decisionLabel(lastRecorded.decision)}
          {lastRecorded.reviewer ? ` as ${lastRecorded.reviewer}` : ""}. Status {lastRecorded.status_label ?? lastRecorded.status_after}
          {lastRecorded.reason_text ? `. ${lastRecorded.reason_text}` : "."}
        </p>
      ) : null}
    </div>
  );
}

export function ReviewActions({ detail, onAction, stale, onReload }: { detail: EvidenceDetail; onAction: (decision: DecisionKind) => void; stale: boolean; onReload: () => void }) {
  const { enabled, canDecide, token } = useReviewer();
  const reconciled = detail.reconciled !== false && !!detail.fingerprint && !!detail.evidence_key;
  const disabled = !canDecide || !reconciled || stale;
  let why: ReactNode = null;
  if (stale) why = "This record changed after you opened it. Reload the current version before deciding.";
  else if (!reconciled) why = "This record has not been keyed by the review lifecycle yet, so decisions are refused. Run the reconcile step, then reload.";
  else if (enabled === false) why = "Read-only public mode: this deployment accepts no decisions.";
  else if (!token) why = "Read-only public mode: enter the reviewer token below to record a decision.";
  else if (!canDecide) why = "Enter your name below to record a decision.";

  return (
    <div className="@container flex flex-col gap-3" data-testid="review-actions">
      {stale ? (
        <Callout tone="warn" title="This evidence changed after you opened it">
          Your decision would apply to a version you have not seen.
          <div className="mt-2">
            <Button size="sm" variant="secondary" Icon={RefreshCcw} onClick={onReload}>
              Reload the current version
            </Button>
          </div>
        </Callout>
      ) : null}
      <div className="grid grid-cols-1 gap-2 @[30rem]:grid-cols-2">
        {DECISION_ORDER.map((d) => {
          const info = DECISIONS[d];
          return (
            <Button key={d} variant={d === "APPROVE" ? "primary" : d === "REJECT" || d === "FLAG_WRONG_ATTRIBUTION" ? "danger" : "secondary"} size="sm" Icon={ICON[d]} disabled={disabled} onClick={() => onAction(d)} className="justify-start" aria-describedby={why ? "review-actions-why" : undefined}>
              {info.label}
              {info.key ? <Kbd className="ml-auto">{info.key}</Kbd> : null}
            </Button>
          );
        })}
      </div>
      {why ? (
        <p id="review-actions-why" className="text-caption text-ink-2" data-testid="actions-disabled-reason">
          {why}
        </p>
      ) : null}
      <ReviewerAccess />
    </div>
  );
}
