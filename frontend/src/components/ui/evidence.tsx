"use client";

import clsx from "clsx";
import {
  BadgeCheck,
  Ban,
  BookMarked,
  BookOpenCheck,
  Braces,
  Building2,
  CircleHelp,
  CircleSlash,
  ClipboardCheck,
  ClipboardList,
  FileText,
  FlaskConical,
  Hourglass,
  Landmark,
  Lightbulb,
  type LucideIcon,
  Megaphone,
  Newspaper,
  Presentation,
  Quote,
  RefreshCcwDot,
  Sigma,
  Siren,
  Stamp,
} from "lucide-react";
import type { ReactNode } from "react";
import { titleCase } from "@/lib/api";
import { canonicalStatus, EPISTEMIC, type EpistemicKind, type EvidenceTier, isTier, NOT_REPORTED, TIERS, VERIFICATION, type VerificationStatus } from "@/lib/evidence";
import type { Label } from "@/lib/types";
import { CHIP, type ChipShape, Hint, type Tone, Badge, toneOf } from "./badge";

/*
 * The evidence-state visual language. Every state is told apart by an icon, a text label and an outline shape;
 * colour only reinforces. The full table is in docs/design-system.md, "Evidence-state language".
 *
 *   solid thin outline   a mechanical state: code produced it, nobody has checked it
 *   dashed outline       a person has to look (pending review, recheck required) or it is not evidence (hypothesis)
 *   dotted outline       calculated here (inference)
 *   dashed + hatch       synthetic or simulated
 *   heavier outline      a recorded human decision (verified or rejected)
 *
 * Only HUMAN_VERIFIED uses the green tone and a check mark. Quote-anchored is deliberately neutral: it shows the
 * number exists in the source, not that it was attributed to the right arm, endpoint or estimand.
 */

type Look = { Icon: LucideIcon; shape: ChipShape; cls: string };

const NEUTRAL = "border-control bg-surface text-ink-2";

const VERIFICATION_LOOK: Record<VerificationStatus, Look> = {
  MACHINE_EXTRACTED: { Icon: Braces, shape: "solid", cls: NEUTRAL },
  QUOTE_ANCHORED: { Icon: Quote, shape: "solid", cls: NEUTRAL },
  PENDING_HUMAN_REVIEW: { Icon: Hourglass, shape: "dashed", cls: "border-warn bg-surface text-warn" },
  HUMAN_VERIFIED: { Icon: BadgeCheck, shape: "strong", cls: "border-ok bg-ok-soft text-ok" },
  REJECTED: { Icon: Ban, shape: "strong", cls: "border-critical bg-critical-soft text-critical" },
  SOURCE_CHANGED_RECHECK_REQUIRED: { Icon: RefreshCcwDot, shape: "dashed", cls: "border-warn bg-warn-soft text-warn" },
};

const SHAPE_CLS: Record<ChipShape, string> = { solid: "border", dashed: "border border-dashed", dotted: "border border-dotted", hatch: "border border-dashed hatch", strong: "border-[1.5px]" };

function StateChip({ look, children, detail, className }: { look: Look; children: ReactNode; detail?: ReactNode; className?: string }) {
  const chip = (
    <span className={clsx(CHIP, SHAPE_CLS[look.shape], look.cls, className)}>
      <look.Icon aria-hidden size={12} strokeWidth={2.2} className="shrink-0" />
      {children}
    </span>
  );
  return detail ? <Hint content={detail}>{chip}</Hint> : chip;
}

/** How far a stored record has been checked. `compact` uses the short label; the explanation is always available. */
export function VerificationBadge({ status, compact, explain = true }: { status: string | null | undefined; compact?: boolean; explain?: boolean }) {
  const name = canonicalStatus(status);
  if (!name) {
    // A status this interface does not know is shown as itself and flagged, never dressed up as a known state.
    return (
      <StateChip look={{ Icon: CircleHelp, shape: "dashed", cls: NEUTRAL }} detail={explain ? "This verification status is not recognised by the interface. Treat the record as unchecked." : undefined}>
        {status ? titleCase(status) : "Status not recorded"}
      </StateChip>
    );
  }
  const info = VERIFICATION[name];
  return (
    <StateChip look={VERIFICATION_LOOK[name]} detail={explain ? info.meaning : undefined}>
      {compact ? info.short : info.label}
    </StateChip>
  );
}

/** The icon of a verification status alone, for legends. Decorative: pair it with the label. */
export function VerificationIcon({ status, size = 14 }: { status: VerificationStatus; size?: number }) {
  const { Icon } = VERIFICATION_LOOK[status];
  return <Icon aria-hidden size={size} strokeWidth={2} />;
}

/* ---------- evidence tier: what kind of source a number came from ---------- */

const TIER_ICON: Record<EvidenceTier, LucideIcon> = {
  REGULATORY_LABEL: Stamp,
  REGULATORY_REVIEW: Landmark,
  PEER_REVIEWED: BookOpenCheck,
  REGISTRY_RESULTS: ClipboardCheck,
  REGISTRY_PROTOCOL: ClipboardList,
  PRESS_RELEASE: Megaphone,
  SPONSOR_PIPELINE: Building2,
  CONFERENCE: Presentation,
  GUIDANCE: BookMarked,
  SPONTANEOUS_REPORTS: Siren,
  SECONDARY: Newspaper,
};

/*
 * A tier is a category of source, not a verdict, so every tier has the same neutral colour. It differs from a
 * verification chip in outline: a tier chip has a heavier left edge, like a file tab.
 */
const TIER_CLS = "border border-l-[3px] border-rule border-l-ink-3 bg-surface text-ink-2";

export function TierBadge({ tier, label, full, explain = true }: { tier: string | null | undefined; /** Full label from the API (`tier_label`), used with `full`. */ label?: string | null; full?: boolean; explain?: boolean }) {
  if (!isTier(tier)) {
    return (
      <span className={clsx(CHIP, TIER_CLS)}>
        <FileText aria-hidden size={12} strokeWidth={2.2} className="shrink-0" />
        {label ?? (tier ? titleCase(tier) : "Tier not recorded")}
      </span>
    );
  }
  const info = TIERS[tier];
  const Icon = TIER_ICON[tier];
  const chip = (
    <span className={clsx(CHIP, TIER_CLS)}>
      <Icon aria-hidden size={12} strokeWidth={2.2} className="shrink-0" />
      {full ? (label ?? info.label) : info.short}
    </span>
  );
  return explain ? (
    <Hint
      content={
        <>
          <span className="block font-semibold">{label ?? info.label}</span>
          {info.meaning}
        </>
      }
    >
      {chip}
    </Hint>
  ) : (
    chip
  );
}

/* ---------- epistemic kind: reported, calculated here, hypothesised, simulated, or missing ---------- */

const EPISTEMIC_LOOK: Record<EpistemicKind, Look> = {
  DOCUMENTED: { Icon: FileText, shape: "solid", cls: NEUTRAL },
  INFERENCE: { Icon: Sigma, shape: "dotted", cls: "border-ink-2 bg-surface text-ink-2" },
  HYPOTHESIS: { Icon: Lightbulb, shape: "dashed", cls: "border-ink-2 bg-surface text-ink-2" },
  SIMULATION: { Icon: FlaskConical, shape: "hatch", cls: "border-ink-2 bg-sunken text-ink" },
  UNSUPPORTED: { Icon: CircleHelp, shape: "dashed", cls: "border-critical bg-surface text-critical" },
  MISSING: { Icon: CircleSlash, shape: "solid", cls: "border-transparent bg-transparent text-ink-3" },
};

export function EpistemicBadge({ kind, children, explain = true }: { kind: EpistemicKind | Lowercase<EpistemicKind>; /** Replaces the default label, for example "Derived here from the reported SE". */ children?: ReactNode; explain?: boolean }) {
  const name = kind.toUpperCase() as EpistemicKind;
  const info = EPISTEMIC[name];
  return (
    <StateChip look={EPISTEMIC_LOOK[name]} detail={explain ? info.meaning : undefined}>
      {children ?? info.label}
    </StateChip>
  );
}

/**
 * A value the source does not report. Use it wherever a number would otherwise be absent: a missing value is
 * written out, never left blank and never shown as 0.
 */
export function Missing({ children = NOT_REPORTED, className }: { children?: ReactNode; className?: string }) {
  return (
    <span className={clsx("inline-flex items-center gap-1 whitespace-nowrap text-ink-3", className)}>
      <CircleSlash aria-hidden size={12} strokeWidth={2} className="shrink-0" />
      {children}
    </span>
  );
}

/* ---------- qualifiers: the labels the backend attaches to a number ---------- */

/** Label codes that restate the verification status or the tier. They are drawn in the evidence-state language. */
const STATUS_CODES: Record<string, VerificationStatus> = {
  MACHINE_EXTRACTED: "MACHINE_EXTRACTED",
  QUOTE_ANCHORED: "QUOTE_ANCHORED",
  PENDING_REVIEW: "PENDING_HUMAN_REVIEW",
  PENDING_HUMAN_REVIEW: "PENDING_HUMAN_REVIEW",
  HUMAN_VERIFIED: "HUMAN_VERIFIED",
  REJECTED: "REJECTED",
  SOURCE_CHANGED_RECHECK_REQUIRED: "SOURCE_CHANGED_RECHECK_REQUIRED",
};
const TIER_CODE_ICON: Record<string, LucideIcon> = { PRESS_RELEASE: Megaphone, REGISTRY: ClipboardCheck, PEER_REVIEWED: BookOpenCheck, REGULATORY: Landmark };

const SEVERITY: Record<Tone, number> = { critical: 0, warn: 1, info: 2, ok: 2, muted: 3 };

function Qualifier({ label }: { label: Label }) {
  const status = STATUS_CODES[label.code];
  if (status) return <VerificationBadge status={status} />;
  const TierIcon = TIER_CODE_ICON[label.code];
  if (TierIcon) {
    // A source tier is a category, not an approval: a caution keeps its caution tone, everything else is neutral.
    const cautious = toneOf(label.tone) === "warn" || toneOf(label.tone) === "critical";
    const chip = (
      <span className={clsx(CHIP, cautious ? "border border-l-[3px] border-warn-border border-l-warn bg-warn-soft text-warn" : TIER_CLS)}>
        <TierIcon aria-hidden size={12} strokeWidth={2.2} className="shrink-0" />
        {label.text}
      </span>
    );
    return label.detail ? <Hint content={label.detail}>{chip}</Hint> : chip;
  }
  // No qualifier may look like a completed verification: "ok" from the API is drawn as plain information.
  const tone = toneOf(label.tone);
  return (
    <Badge tone={tone === "ok" ? "info" : tone} title={label.detail || undefined}>
      {label.text}
    </Badge>
  );
}

/**
 * The qualifiers that travel with a number. With `max`, cautions and critical labels are listed first and are
 * never among the collapsed ones; whatever is collapsed is listed by name in the "+N" explanation.
 */
export function Qualifiers({ labels, max, omitVerification }: { labels: Label[]; max?: number; /** Leave out the verification label when a VerificationBadge is already shown next to it. */ omitVerification?: boolean }) {
  const all = omitVerification ? labels.filter((l) => !STATUS_CODES[l.code]) : labels;
  const ordered = max ? [...all].sort((a, b) => SEVERITY[toneOf(a.tone)] - SEVERITY[toneOf(b.tone)]) : all;
  const mustShow = ordered.filter((l) => SEVERITY[toneOf(l.tone)] <= 1).length;
  const visible = max ? Math.max(max, mustShow) : ordered.length;
  const shown = ordered.slice(0, visible);
  const hidden = ordered.slice(visible);
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {shown.map((l) => (
        <Qualifier key={l.code} label={l} />
      ))}
      {hidden.length ? (
        <Badge
          tone="muted"
          icon={false}
          title={
            <>
              <span className="block font-semibold">{hidden.length} more</span>
              {hidden.map((l) => l.text).join("; ")}
            </>
          }
        >
          +{hidden.length} more
        </Badge>
      ) : null}
    </span>
  );
}

export type { Tone };
