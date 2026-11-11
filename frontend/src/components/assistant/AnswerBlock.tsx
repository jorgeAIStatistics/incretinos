"use client";

import clsx from "clsx";
import { ArrowRight, Ban, CircleSlash, FileText, Lightbulb, type LucideIcon, Scale, Sparkles } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { Badge, Disclosure, Term, TierBadge, VerificationBadge } from "@/components/ui";
import { type AnswerBlock, blockAnchor, type BlockRecord, nctIds, RECORD_LABEL, reviewHref, splitMarkers } from "@/lib/assistant";
import type { GlossaryKey } from "@/lib/glossary";

/*
 * One block of an answer. The kind is said in words and with an icon; the outline follows the evidence-state
 * language (docs/design-system.md, section 12): reported evidence and model text sit in a solid frame, a rule-based
 * interpretation in an inset frame, a hypothesis in a dashed frame, a refusal in a critical frame, and what the
 * stored sources do not contain has no frame and grey text, like a missing value.
 */

type KindLook = { label: string; Icon: LucideIcon; frame: string; icon: string; term?: GlossaryKey };

export const KIND: Record<string, KindLook> = {
  REPORTED_EVIDENCE: { label: "Reported evidence", Icon: FileText, frame: "border border-hairline bg-surface", icon: "text-ink-2" },
  INTERPRETATION: { label: "Rule-based interpretation", Icon: Scale, frame: "border border-hairline bg-sunken", icon: "text-ink-2", term: "rule-based-interpretation" },
  UNAVAILABLE: { label: "Not available in stored sources", Icon: CircleSlash, frame: "border border-transparent", icon: "text-ink-3" },
  HYPOTHESIS: { label: "Hypothesis, untested", Icon: Lightbulb, frame: "border border-dashed border-ink-2 bg-surface", icon: "text-ink-2" },
  REFUSAL: { label: "Not answered", Icon: Ban, frame: "border border-critical-border bg-surface", icon: "text-critical" },
  MODEL_SUMMARY: { label: "Model-drafted summary", Icon: Sparkles, frame: "border border-control bg-surface", icon: "text-ink-2" },
};
export const kindLook = (kind: string): KindLook => KIND[kind] ?? { label: kind, Icon: FileText, frame: "border border-hairline bg-surface", icon: "text-ink-2" };

/**
 * How far the records behind a block have been checked, in the shared evidence-state language: one chip per
 * distinct stored status (a block can rest on several records), then the tier of its source. "Reported evidence"
 * says only that a source reports it; nothing reads as verified unless every record behind the block is
 * HUMAN_VERIFIED, the one status the badge draws in green.
 */
export function BlockVerification({ block }: { block: AnswerBlock }) {
  const v = block.verification;
  const statuses: string[] = v ? Array.from(new Set(v.statuses ?? [])) : [];
  if (!statuses.length && block.kind !== "REPORTED_EVIDENCE") return block.tier ? <TierBadge tier={block.tier} /> : null;
  return (
    <span className="flex flex-wrap items-center gap-1" data-testid="block-verification">
      {statuses.length ? (
        statuses.map((status) => <VerificationBadge key={status} status={status} compact />)
      ) : (
        <Badge tone="muted" icon={false} title="This block states registry record facts or rule-based text; no reviewable evidence record is behind it.">
          No review status
        </Badge>
      )}
      {statuses.length > 1 ? <span className="text-caption font-normal text-ink-3">{v?.records ?? statuses.length} records; not all at the same stage</span> : null}
      {block.tier ? <TierBadge tier={block.tier} /> : null}
    </span>
  );
}

/** Text with each [C#] marker turned into a link to the block it names. Used for model-drafted text. */
export function MarkedText({ text, known }: { text: string; known: Set<string> }) {
  return (
    <>
      {splitMarkers(text).map((part, i) =>
        typeof part === "string" ? (
          <span key={i}>{part}</span>
        ) : known.has(part.marker) ? (
          <MarkerLink key={i} id={part.marker} />
        ) : (
          <span key={i} className="mono text-critical" title="This marker names no block of the answer">
            [{part.marker}]
          </span>
        ),
      )}
    </>
  );
}

export function MarkerLink({ id, className }: { id: string; className?: string }) {
  return (
    <a href={`#card-${id}`} className={clsx("mono rounded-chip px-0.5 font-medium", className)} aria-label={`Marker ${id}: go to the block it names`} data-testid="marker-link">
      [{id}]
    </a>
  );
}

function RecordLine({ r }: { r: BlockRecord }) {
  const href = reviewHref(r);
  return (
    <li className="flex flex-wrap items-center gap-x-2 gap-y-1" data-testid="block-record">
      <span className="text-ink-2">
        {RECORD_LABEL[r.type] ?? r.type}{" "}
        <span className="mono text-ink-3" translate="no">
          #{r.id}
        </span>
      </span>
      {r.verification_status ? <VerificationBadge status={r.verification_status} compact /> : <span className="text-caption text-ink-3">No review status</span>}
      {href ? (
        <Link href={href} className="text-caption font-medium" data-testid="review-link" data-evidence-key={r.evidence_key ?? ""}>
          Open in Evidence Review
        </Link>
      ) : null}
    </li>
  );
}

function FollowUp({ href, children, testId }: { href: string; children: ReactNode; testId?: string }) {
  return (
    <Link href={href} className="inline-flex items-center gap-1 text-caption font-medium" data-testid={testId}>
      {children}
      <ArrowRight aria-hidden size={12} />
    </Link>
  );
}

export function AnswerBlockView({ block, index, sourceNumber, markers }: { block: AnswerBlock; index: number; sourceNumber: Map<number, number>; markers: Set<string> }) {
  const look = kindLook(block.kind);
  const anchor = blockAnchor(block, index);
  const trials = nctIds(block.text);
  const records = block.evidence ?? [];
  const muted = block.kind === "UNAVAILABLE";
  return (
    <article id={anchor} aria-labelledby={`${anchor}-title`} className={clsx("answer-block rounded-panel px-4 py-3", look.frame, muted && "px-0")} data-kind={block.kind} data-testid="answer-block">
      <header className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <h3 id={`${anchor}-title`} className="flex items-center gap-1.5 text-data font-semibold text-ink-2">
          <look.Icon aria-hidden size={14} className={clsx("shrink-0", look.icon)} />
          {look.term ? <Term k={look.term}>{look.label}</Term> : look.label}
        </h3>
        {block.card_id ? (
          <a href={`#${anchor}`} className="mono rounded-chip border border-control px-1 text-caption text-ink-2 hover:border-ink-2" aria-label={`Citation marker ${block.card_id}: link to this block`} data-testid="block-marker">
            {block.card_id}
          </a>
        ) : null}
        <BlockVerification block={block} />
      </header>
      <p className={clsx("mt-1.5 max-w-[78ch] text-body leading-relaxed", muted ? "text-ink-2" : "text-ink")}>{block.kind === "MODEL_SUMMARY" ? <MarkedText text={block.text} known={markers} /> : block.text}</p>
      {block.note ? <p className="mt-1 max-w-[78ch] text-data text-ink-3">{block.note}</p> : null}

      {block.citations.length || records.length || block.result_ids.length || trials.length ? (
        <footer className="mt-2 flex flex-col gap-1.5 border-t border-hairline pt-2 text-caption text-ink-3">
          {block.citations.length ? (
            <p>
              Sources{" "}
              {block.citations.map((id) => (
                <a key={id} href={`#source-${sourceNumber.get(id) ?? id}`} className="num mr-1 font-medium" aria-label={`Source ${sourceNumber.get(id) ?? id} in the list of sources cited`} data-testid="source-ref">
                  [{sourceNumber.get(id) ?? id}]
                </a>
              ))}
            </p>
          ) : null}
          {records.length === 1 ? (
            <ul>
              <RecordLine r={records[0]} />
            </ul>
          ) : records.length > 1 ? (
            <Disclosure summary={`${records.length} records behind this block`}>
              <ul className="space-y-1">
                {records.map((r) => (
                  <RecordLine key={`${r.type}:${r.id}`} r={r} />
                ))}
              </ul>
            </Disclosure>
          ) : null}
          {block.result_ids.length || trials.length ? (
            <p className="flex flex-wrap gap-x-4 gap-y-1">
              {block.result_ids.map((id) => (
                <FollowUp key={id} href={`/compare?a=${id}`} testId="open-finding">
                  Open the finding
                </FollowUp>
              ))}
              {trials.map((nct) => (
                <FollowUp key={nct} href={`/trials/${nct}`} testId="open-trial">
                  Open trial <span className="mono" translate="no">{nct}</span>
                </FollowUp>
              ))}
            </p>
          ) : null}
        </footer>
      ) : null}
    </article>
  );
}
