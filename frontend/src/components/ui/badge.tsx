"use client";

import * as Tooltip from "@radix-ui/react-tooltip";
import clsx from "clsx";
import { AlertTriangle, CheckCircle2, CircleDashed, Info, type LucideIcon, ShieldAlert } from "lucide-react";
import { type ReactNode, useState } from "react";
import { titleCase } from "@/lib/api";

/* ---------- tone: a meaning, always carried by an icon and a text label as well as by colour ---------- */

export type Tone = "ok" | "info" | "warn" | "muted" | "critical";

export const TONE: Record<Tone, { cls: string; Icon: LucideIcon }> = {
  ok: { cls: "border-ok-border bg-ok-soft text-ok", Icon: CheckCircle2 },
  info: { cls: "border-accent-border bg-accent-soft text-accent", Icon: Info },
  warn: { cls: "border-warn-border bg-warn-soft text-warn", Icon: AlertTriangle },
  muted: { cls: "border-hairline bg-sunken text-ink-2", Icon: CircleDashed },
  critical: { cls: "border-critical-border bg-critical-soft text-critical", Icon: ShieldAlert },
};

/** A tone name from the API. Anything unknown falls back to "muted" instead of throwing. */
export const toneOf = (tone: string | null | undefined): Tone => (tone && tone in TONE ? (tone as Tone) : "muted");

/**
 * A chip whose explanation opens on hover, on keyboard focus and on tap, and closes on Escape or on moving away.
 * The chip itself is a real button, so it has a role, a name and a place in the tab order.
 */
export function Hint({ content, children, label }: { content: ReactNode; children: ReactNode; label?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <Tooltip.Root delayDuration={120} open={open} onOpenChange={setOpen}>
      <Tooltip.Trigger asChild>
        <button type="button" aria-label={label} onClick={() => setOpen((o) => !o)} className="inline-flex max-w-full cursor-help rounded-chip align-middle">
          {children}
        </button>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content sideOffset={6} collisionPadding={8} className="popover-panel z-(--z-popover) max-w-xs rounded-control bg-ink px-3 py-2 text-data leading-snug text-white shadow-raised" onPointerDownOutside={() => setOpen(false)}>
          {content}
          <Tooltip.Arrow className="fill-(--ink)" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

/** Outline of a chip. Dashed, dotted and hatched are part of the evidence-state language: see docs/design-system.md. */
export type ChipShape = "solid" | "dashed" | "dotted" | "hatch" | "strong";

const SHAPE: Record<ChipShape, string> = {
  solid: "border",
  dashed: "border border-dashed",
  dotted: "border border-dotted",
  hatch: "border border-dashed hatch",
  strong: "border-[1.5px]",
};

export const CHIP = "inline-flex max-w-full items-center gap-1 whitespace-nowrap rounded-chip px-1.5 py-px text-caption font-medium leading-5";

export function Badge({
  tone = "muted",
  children,
  title,
  icon = true,
  Icon,
  shape = "solid",
  className,
}: {
  tone?: Tone;
  children: ReactNode;
  /** Plain-English explanation. Makes the badge focusable and opens on hover, focus and tap. */
  title?: ReactNode;
  icon?: boolean;
  /** Replaces the tone's default icon. */
  Icon?: LucideIcon;
  shape?: ChipShape;
  className?: string;
}) {
  const { cls, Icon: ToneIcon } = TONE[tone];
  const Glyph = Icon ?? ToneIcon;
  const chip = (
    <span className={clsx(CHIP, SHAPE[shape], cls, className)}>
      {icon ? <Glyph aria-hidden size={12} strokeWidth={2.2} className="shrink-0" /> : null}
      {children}
    </span>
  );
  return title ? <Hint content={title}>{chip}</Hint> : chip;
}

const CLASS_TONE: Record<string, Tone> = {
  "DIRECT RANDOMIZED COMPARISON": "ok",
  "PARTIALLY COMPARABLE": "info",
  "DESCRIPTIVE CROSS-STUDY COMPARISON": "warn",
  "NOT SUITABLE FOR EFFICACY COMPARISON": "critical",
  "INSUFFICIENT EVIDENCE": "muted",
};
export function ClassificationBadge({ value }: { value: string }) {
  return <Badge tone={CLASS_TONE[value] ?? "muted"}>{titleCase(value)}</Badge>;
}

/**
 * `available` is the API's `regulatory_status_available`: false when the statement the status rests on was rejected in
 * review (or is held), in which case the API withholds the status and `note` says why.
 */
export function RegulatoryBadge({ status, available, note }: { status: string; available?: boolean | null; note?: string | null }) {
  if (available === false) return <Badge tone="muted" title={note ?? "The regulatory-status statement on record was rejected in review; no status is shown."}>Status withheld in review</Badge>;
  if (status === "APPROVED") return <Badge tone="ok" title="FDA labeling for this drug is ingested and cited.">FDA-approved</Badge>;
  if (status === "INVESTIGATIONAL") return <Badge tone="warn" title="Not approved for any indication. Status is re-checked against the FDA label database at each ingestion.">Investigational</Badge>;
  if (status === "SUBMITTED") return <Badge tone="info">Under regulatory review</Badge>;
  if (status === "DISCONTINUED") return <Badge tone="muted">Discontinued</Badge>;
  return <Badge tone="muted" title="Regulatory status is not tracked for this agent.">Status not tracked</Badge>;
}
