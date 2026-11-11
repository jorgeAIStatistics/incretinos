"use client";

import { Plus, RefreshCcw, Trash2 } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { Button, Callout, IconButton, Modal, Select, TextArea, TextField, useToast, VerificationBadge } from "@/components/ui";
import {
  DECISION_ORDER,
  DECISIONS,
  DecisionError,
  type DecisionKind,
  type DecisionRequest,
  type DecisionResponse,
  type EvidenceDetail,
  fieldLabel,
  flatten,
  parseProposedValue,
  plainValue,
  postDecision,
  rationaleProblem,
  shortHash,
} from "@/lib/review";
import { useReviewer } from "./ReviewerSession";

/*
 * The confirmation step of a review decision. The reviewer sees which record and which exact version (fingerprint)
 * the decision applies to, writes what they checked, and, for a decision that settles the status, attests that a
 * person read the original source. The fingerprint sent is the one shown when the dialog opened: if the stored
 * evidence changed in the meantime the service refuses the decision (409) and the dialog says so.
 */

type CorrectionRow = { id: number; field: string; value: string };

export function DecisionDialog({
  detail,
  initial,
  onClose,
  onRecorded,
  onStale,
  onConflict,
}: {
  detail: EvidenceDetail;
  initial: DecisionKind;
  onClose: () => void;
  onRecorded: (result: DecisionResponse) => void;
  /** Reload the evidence after a conflict. */
  onStale: () => void;
  /** The service refused the decision because the evidence changed (409). */
  onConflict: () => void;
}) {
  const { token, reviewer, setToken, serverNamesReviewer } = useReviewer();
  const { toast } = useToast();
  const attestId = useId();
  // The version the reviewer is looking at, fixed when the dialog opens.
  const [fingerprint] = useState(detail.fingerprint ?? "");
  const [decision, setDecision] = useState<DecisionKind>(initial);
  const [rationale, setRationale] = useState("");
  const [attested, setAttested] = useState(false);
  const [quote, setQuote] = useState("");
  const [note, setNote] = useState("");
  const [rows, setRows] = useState<CorrectionRow[]>([{ id: 1, field: "", value: "" }]);
  const [submitted, setSubmitted] = useState(false);
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<DecisionError | null>(null);

  const info = DECISIONS[decision];
  const snapshot = flatten(detail.snapshot ?? {});
  const correctable = detail.correctable_fields?.length ? detail.correctable_fields : Object.keys(snapshot).sort();
  const rationaleError = submitted ? rationaleProblem(rationale) : null;
  const attestError = submitted && info.needsAttestation && !attested ? "Confirm that you checked the original source. Without it the service refuses this decision." : null;
  const isCorrection = decision === "PROPOSE_CORRECTION";
  const parsed = rows.map((r) => ({ ...r, ...parseProposedValue(r.value, snapshot[r.field]) }));
  const correctionErrors = isCorrection && submitted
    ? {
        rows: parsed.map((r) => (!r.field ? "Choose the field to correct." : r.error)),
        quote: quote.trim().length < 5 ? "Paste the passage of the source that supports the correction, word for word." : null,
        duplicate: new Set(rows.map((r) => r.field)).size !== rows.length ? "Each field can be named once." : null,
      }
    : null;
  const hasErrors = !!rationaleError || !!attestError || (correctionErrors !== null && (correctionErrors.rows.some(Boolean) || !!correctionErrors.quote || !!correctionErrors.duplicate));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    setFailure(null);
    const problems =
      rationaleProblem(rationale) !== null ||
      (info.needsAttestation && !attested) ||
      (isCorrection && (parsed.some((r) => !r.field || r.error) || quote.trim().length < 5 || new Set(rows.map((r) => r.field)).size !== rows.length));
    if (problems || !token) return;
    const body: DecisionRequest = {
      decision,
      // With personal reviewer tokens the service records the token's own name; a typed name is not sent.
      ...(serverNamesReviewer ? {} : { reviewer: reviewer.trim() }),
      rationale: rationale.trim(),
      attest_human_review: attested,
      expected_fingerprint: fingerprint,
      ...(isCorrection
        ? { proposed_correction: { changes: Object.fromEntries(parsed.map((r) => [r.field, r.value])), supporting_quote: quote.trim(), ...(note.trim() ? { note: note.trim() } : {}) } }
        : {}),
    };
    setSending(true);
    try {
      const result = await postDecision(detail.evidence_type, detail.id, body, token);
      toast({ tone: "info", title: "Decision recorded", description: `${info.label} was written to the audit log${result.reviewer ? ` as ${result.reviewer}` : ""}. Status: ${result.status_label ?? result.status_after}.` });
      onRecorded(result);
    } catch (err) {
      const error = err instanceof DecisionError ? err : new DecisionError(String(err), 0, "other");
      if (error.kind === "token") setToken(null); // the token was refused: forget it so it is not sent again
      if (error.kind === "changed") onConflict();
      setFailure(error);
    } finally {
      setSending(false);
    }
  };

  const changed = failure?.kind === "changed";

  return (
    <Modal
      open
      onOpenChange={(open) => (!open ? onClose() : undefined)}
      size="lg"
      title={`${info.label}: record a decision`}
      description={info.effect}
      footer={
        changed ? (
          <>
            <Button variant="secondary" onClick={onClose}>
              Close
            </Button>
            <Button
              Icon={RefreshCcw}
              onClick={() => {
                onStale();
                onClose();
              }}
            >
              Reload the current version
            </Button>
          </>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" form="decision-form" loading={sending} variant={decision === "REJECT" || decision === "FLAG_WRONG_ATTRIBUTION" ? "danger" : "primary"}>
              Record: {info.verb}
            </Button>
          </>
        )
      }
    >
      <div className="mb-3 rounded-control border border-hairline bg-sunken px-3 py-2 text-data">
        <p className="font-medium text-ink">{detail.label}</p>
        <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-caption text-ink-2">
          Now <VerificationBadge status={detail.status.verification_status} compact explain={false} />
          <span className="mono" title={fingerprint}>
            version {shortHash(fingerprint, 12)}
          </span>
          <span>{serverNamesReviewer ? "recording under the name bound to your token" : `recording as ${reviewer.trim() || "—"}`}</span>
        </p>
      </div>

      {failure ? (
        <Callout tone={changed ? "warn" : "critical"} title={changed ? "The evidence changed after you opened it" : "The decision was not recorded"} className="mb-3">
          <span data-testid="decision-error" data-kind={failure.kind}>
            {changed
              ? "Someone or something updated this record (or its source) after you opened it, so the service refused a decision on the version you saw. Nothing was recorded. Reload the current version, check it against the source again, and decide on that."
              : failure.kind === "token"
                ? `${failure.message} The token has been forgotten; enter it again.`
                : failure.kind === "read-only"
                  ? `${failure.message}`
                  : failure.message}
          </span>
          {changed && failure.currentFingerprint ? <span className="mono mt-1 block text-caption">Current version {shortHash(failure.currentFingerprint, 12)}</span> : null}
        </Callout>
      ) : null}

      <form id="decision-form" onSubmit={submit} noValidate className="flex flex-col gap-3">
        <Select label="Decision" value={decision} onChange={(v) => setDecision(v as DecisionKind)} options={DECISION_ORDER.map((d) => ({ value: d, label: DECISIONS[d].label }))} />

        {isCorrection ? (
          <fieldset className="flex flex-col gap-2 rounded-control border border-hairline p-3">
            <legend className="px-1 text-caption font-medium text-ink-2">Proposed values</legend>
            {rows.map((r, i) => {
              const current = snapshot[r.field];
              return (
                <div key={r.id} className="grid grid-cols-1 items-end gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
                  <Select
                    label={`Field ${rows.length > 1 ? i + 1 : ""}`.trim()}
                    value={r.field}
                    onChange={(field) => setRows((all) => all.map((x) => (x.id === r.id ? { ...x, field } : x)))}
                    options={[{ value: "", label: "Choose a field…" }, ...correctable.map((f) => ({ value: f, label: fieldLabel(f) }))]}
                    error={correctionErrors?.rows[i] && !r.field ? correctionErrors.rows[i] : null}
                    hint={r.field ? `Stored now: ${current === null || current === undefined ? "not reported" : plainValue(current)}` : undefined}
                  />
                  <TextField
                    label="Proposed value"
                    value={r.value}
                    onChange={(value) => setRows((all) => all.map((x) => (x.id === r.id ? { ...x, value } : x)))}
                    error={correctionErrors?.rows[i] && r.field ? correctionErrors.rows[i] : null}
                    hint="Exactly as the source reports it. Leave empty for not reported."
                    placeholder="e.g. -8.4…"
                  />
                  {rows.length > 1 ? <IconButton label={`Remove field ${i + 1}`} Icon={Trash2} onClick={() => setRows((all) => all.filter((x) => x.id !== r.id))} /> : <span />}
                </div>
              );
            })}
            {correctionErrors?.duplicate ? <p role="alert" className="text-caption text-critical">{correctionErrors.duplicate}</p> : null}
            <div>
              <Button size="sm" variant="quiet" Icon={Plus} onClick={() => setRows((all) => [...all, { id: Math.max(...all.map((x) => x.id)) + 1, field: "", value: "" }])}>
                Add another field
              </Button>
            </div>
            <TextArea label="Supporting quote from the source" value={quote} onChange={setQuote} rows={3} required error={correctionErrors?.quote} placeholder="The passage, word for word…" />
            <TextField label="Note (optional)" value={note} onChange={setNote} />
          </fieldset>
        ) : null}

        <TextArea
          label="Rationale: what you checked against the source"
          value={rationale}
          onChange={setRationale}
          rows={4}
          required
          error={rationaleError}
          hint="For example: the value, arm, time point and population in the table on page 135 match; the estimand is stated in the methods. Recorded with the decision and in the audit log."
        />

        <div className="flex flex-col gap-1">
          <div className="flex items-start gap-2">
            <input
              id={attestId}
              type="checkbox"
              checked={attested}
              onChange={(e) => setAttested(e.target.checked)}
              aria-invalid={attestError ? true : undefined}
              aria-describedby={attestError ? `${attestId}-error` : undefined}
              className="mt-0.5 size-4 shrink-0 accent-(--accent)"
            />
            <label htmlFor={attestId} className="text-data text-ink">
              I, a person, read the original source for this record and checked the stored value, arm, endpoint, time point, estimand and population against it.
              {info.needsAttestation ? <span className="text-ink-3"> Required for this decision.</span> : <span className="text-ink-3"> Optional for this decision.</span>}
            </label>
          </div>
          {attestError ? (
            <p id={`${attestId}-error`} role="alert" className="text-caption text-critical">
              {attestError}
            </p>
          ) : null}
        </div>
        {hasErrors ? <p className="sr-only" role="status">The form has errors; they are listed next to the fields.</p> : null}
      </form>
    </Modal>
  );
}
