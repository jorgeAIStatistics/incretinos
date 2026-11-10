"use client";

import { CheckSquare, GitCommitVertical, Square } from "lucide-react";
import Link from "next/link";
import { Callout, Disclosure, Missing, TierBadge, VerificationBadge } from "@/components/ui";
import { titleCase } from "@/lib/api";
import { decisionLabel, type DecisionOut, type EvidenceDetail, fieldLabel, recordHref, type RelatedRow, shortHash, stamp } from "@/lib/review";
import { estimandLabel } from "@/lib/rows";
import { Block, Diff, JsonBlock, Severity, Stored } from "./bits";

/*
 * RIGHT panel (below the review actions): what the automated checks found, what other sources say about the same
 * quantity, how the record changed over time, every decision recorded on it and the audit trail.
 */

const num = (x: unknown): string | null => (typeof x === "number" ? String(x).replace(/^-/, "−") : null);

function SameQuantity({ rows }: { rows: RelatedRow[] }) {
  if (!rows.length) return <p className="text-data text-ink-3">No other source reports this quantity for the same study, arm, endpoint and time point.</p>;
  return (
    <ul className="flex flex-col gap-2" data-testid="same-quantity">
      {rows.map((r) => {
        const v = r.values ?? {};
        const ci = num(v.ci_low) !== null || num(v.ci_high) !== null ? `${v.ci_level != null ? `${num(v.ci_level)}% ` : ""}CI ${num(v.ci_low) ?? "—"} to ${num(v.ci_high) ?? "—"}` : "CI not reported";
        return (
          <li key={`${r.evidence_type}-${r.id}`} className="rounded-control border border-hairline px-2.5 py-2 text-data">
            <div className="flex flex-wrap items-center gap-1.5">
              <VerificationBadge status={r.verification_status} compact />
              <TierBadge tier={r.tier} />
              {r.usable === false ? <span className="text-caption font-medium text-critical">Excluded from analyses</span> : null}
            </div>
            <p className="num mt-1 text-body font-semibold text-ink">
              {num(v.value) ?? num(v.estimate) ?? <Missing className="!whitespace-normal">Value not reported</Missing>} <span className="text-data font-normal text-ink-2">{typeof v.unit === "string" ? v.unit : ""}</span>
            </p>
            <p className="num text-caption text-ink-2">
              {[ci, v.n_analyzed != null ? `n ${num(v.n_analyzed)}` : "n not reported", v.timepoint_weeks != null ? `week ${num(v.timepoint_weeks)}` : null, typeof v.estimand === "string" ? estimandLabel(v.estimand) : null]
                .filter(Boolean)
                .join(" · ")}
            </p>
            <p className="mt-0.5 text-caption text-ink-3">
              {r.evidence_key ? (
                <Link href={recordHref(r.evidence_type, r.evidence_key)} className="text-accent hover:underline">
                  {r.label}
                </Link>
              ) : (
                r.label
              )}
              {r.source_key ? <span className="mono block break-all">{r.source_key}</span> : null}
            </p>
          </li>
        );
      })}
    </ul>
  );
}

function DecisionItem({ d }: { d: DecisionOut }) {
  const correction = d.proposed_correction ?? null;
  return (
    <li className="rounded-control border border-hairline px-2.5 py-2 text-data" data-testid="decision-entry">
      <p className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-semibold text-ink">{decisionLabel(d.decision)}</span>{" "}
        <span className="text-ink-2">by {d.reviewer}</span>{" "}
        <span className="num text-caption text-ink-3">{stamp(d.created_at)}</span>
      </p>
      {d.status_before || d.status_after ? (
        <p className="mt-1 flex flex-wrap items-center gap-1 text-caption text-ink-2">
          Status: <VerificationBadge status={d.status_before} compact explain={false} /> <span aria-hidden>→</span>
          <span className="sr-only">became</span> <VerificationBadge status={d.status_after} compact explain={false} />
        </p>
      ) : null}
      <p className="mt-1 whitespace-pre-line text-ink-2">{d.rationale}</p>
      <p className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-caption text-ink-3">
        <span className="inline-flex items-center gap-1">
          {d.attested ? <CheckSquare aria-hidden size={12} /> : <Square aria-hidden size={12} />}
          {d.attested ? "Reviewer attested checking the source" : "No attestation"}
        </span>
        <span>{d.on_current_version ? "On the current version" : "On an earlier version"}</span>
        <span>{d.decisive ? "Settles the status" : "Does not change the status"}</span>
        {d.actor_kind && d.actor_kind !== "HUMAN" ? <span>Actor: {titleCase(d.actor_kind)}</span> : null}
        {d.imported ? <span>Imported from the review ledger</span> : null}
        {d.recorded_under_other_key ? <span>Recorded under an earlier evidence key</span> : null}
        {d.fingerprint ? <span className="mono" title={d.fingerprint}>version {shortHash(d.fingerprint, 10)}</span> : null}
        {d.source_snapshot_version != null ? <span>source snapshot {d.source_snapshot_version}</span> : null}
      </p>
      {correction ? (
        <div className="mt-1.5 rounded-control bg-sunken px-2 py-1.5 text-caption">
          <p className="font-medium text-ink">Proposed correction</p>
          <ul>
            {Object.entries(correction.changes ?? {}).map(([field, value]) => (
              <li key={field}>
                <span className="mono">{field}</span> → <Stored value={value} />
              </li>
            ))}
          </ul>
          {correction.supporting_quote ? <p className="mt-1 italic text-ink-2">“{correction.supporting_quote}”</p> : null}
          {correction.note ? <p className="mt-1 text-ink-2">{correction.note}</p> : null}
        </div>
      ) : null}
    </li>
  );
}

export function AssurancePanel({ detail }: { detail: EvidenceDetail }) {
  const flags = detail.check_flags ?? [];
  const versions = detail.versions ?? [];
  const decisions = detail.decisions ?? [];
  const audit = detail.audit ?? [];
  const changed = detail.changed_since_review ?? null;

  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="assurance-panel">
      <Block title="Automated checks" level={3} action={<Severity level={detail.risk_level} prefix="Risk" />} testId="checks">
        {flags.length ? (
          <ul className="flex flex-col gap-2">
            {flags.map((f, i) => (
              <li key={`${f.code}-${i}`} className="rounded-control border border-hairline px-2.5 py-2 text-data">
                <p className="flex flex-wrap items-center gap-1.5">
                  <Severity level={f.severity} />
                  <span className="mono text-caption text-ink-2">{f.code}</span>
                </p>
                <p className="mt-1 text-ink-2">{f.explanation}</p>
                {f.fields?.length ? <p className="mt-0.5 text-caption text-ink-3">Fields: {f.fields.map(fieldLabel).join(", ")}</p> : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-data text-ink-2">No automated check raised a flag. That does not mean the record was checked by a person.</p>
        )}
        <p className="mt-1.5 text-caption text-ink-3">A flag explains a risk. It never changes the stored data.</p>
      </Block>

      {changed ? (
        <Block title="Changed since the last review" level={3} testId="changed-since-review">
          <Callout tone="warn">
            {decisionLabel(changed.decision)} by {changed.reviewer} on {stamp(changed.decided_at)} covered an earlier version. The decision does not cover this version.
          </Callout>
          {changed.diff?.length ? (
            <ul className="mt-2 flex flex-col gap-1 text-data">
              {changed.diff.map((c) => (
                <li key={c.field}>
                  <span className="mono text-caption text-ink-3">{c.field}</span> <Diff before={c.before} after={c.after} />
                </li>
              ))}
            </ul>
          ) : changed.reviewed_version_available === false ? (
            <p className="mt-1 text-caption text-ink-3">The reviewed version is no longer stored, so the fields that changed cannot be listed.</p>
          ) : null}
        </Block>
      ) : null}

      <Block title="Same quantity from other sources" level={3}>
        <SameQuantity rows={detail.same_quantity_other_sources ?? []} />
      </Block>

      <Block title="Version history" level={3} action={`${versions.length} ${versions.length === 1 ? "version" : "versions"}`} testId="versions">
        {versions.length ? (
          <ol className="flex flex-col gap-2">
            {versions.map((v) => (
              <li key={v.version_id} className="rounded-control border border-hairline px-2.5 py-2 text-data">
                <p className="flex flex-wrap items-center gap-x-2">
                  <GitCommitVertical aria-hidden size={13} className="text-ink-3" />
                  <span className="mono text-ink" title={v.fingerprint}>
                    {shortHash(v.fingerprint, 10)}
                  </span>
                  {v.is_current ? <span className="rounded-chip border border-control px-1 text-caption font-medium text-ink">Current</span> : null}
                  <span className="num text-caption text-ink-3">first seen {stamp(v.first_seen_at)}</span>
                </p>
                <p className="mt-0.5 text-caption text-ink-3">
                  Source snapshot {v.source_snapshot_version ?? "—"}
                  {v.last_source_snapshot_version != null && v.last_source_snapshot_version !== v.source_snapshot_version ? `, last seen with ${v.last_source_snapshot_version}` : ""}
                  {v.status_when_last_current ? ` · status then: ${titleCase(v.status_when_last_current)}` : ""}
                </p>
                {v.changed_fields?.length ? (
                  <ul className="mt-1 flex flex-col gap-0.5 text-caption">
                    {v.changed_fields.map((c) => (
                      <li key={c.field}>
                        <span className="mono text-ink-3">{c.field}</span> <Diff before={c.before} after={c.after} />
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="mt-0.5 text-caption text-ink-3">{v.supersedes_version_id ? "No field-level changes listed." : "First version on record."}</p>
                )}
              </li>
            ))}
          </ol>
        ) : (
          <Missing className="!whitespace-normal">No version is recorded</Missing>
        )}
      </Block>

      <Block title="Decision history" level={3} action={`${decisions.length} recorded`} testId="decision-history">
        {decisions.length ? (
          <ol className="flex flex-col gap-2">
            {decisions.map((d) => (
              <DecisionItem key={d.id} d={d} />
            ))}
          </ol>
        ) : (
          <p className="text-data text-ink-3">No decision has been recorded on this record.</p>
        )}
      </Block>

      <Block title="Audit trail" level={3} action={`${audit.length} ${audit.length === 1 ? "event" : "events"}`} testId="audit-trail">
        {audit.length ? (
          <ol className="flex flex-col gap-1.5">
            {audit.map((a) => (
              <li key={a.id} className="text-data" data-testid="audit-entry">
                <p className="flex flex-wrap gap-x-2">
                  <span className="mono text-caption text-ink-2">{a.action}</span>{" "}
                  <span className="text-ink">{a.actor}</span>{" "}
                  <span className="num text-caption text-ink-3">{stamp(a.at)}</span>
                </p>
                {a.detail && Object.keys(a.detail).length ? (
                  <Disclosure summary="Event detail">
                    <JsonBlock value={a.detail} label={`Audit event ${a.id}`} />
                  </Disclosure>
                ) : null}
              </li>
            ))}
          </ol>
        ) : (
          <p className="text-data text-ink-3">No audit event concerns this record yet.</p>
        )}
      </Block>
    </div>
  );
}
