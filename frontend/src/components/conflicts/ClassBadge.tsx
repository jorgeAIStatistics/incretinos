"use client";

import { CircleHelp, Equal, type LucideIcon, OctagonAlert, Split } from "lucide-react";
import { Badge, type Tone } from "@/components/ui";
import type { ConflictClass, DimensionStatus } from "./types";

/*
 * Presentation of the four classes. Each has its own icon and its own words; the tone only reinforces them. No class
 * uses the green "ok" tone or a check mark: those belong to a human verification, and agreement between sources is
 * not one. The labels and definitions themselves come from the API.
 */

export const CLASS_ORDER: ConflictClass[] = ["TRUE_CONFLICT", "INSUFFICIENT_INFORMATION", "EXPLAINABLE_DIFFERENCE", "CONSISTENT"];

export const CLASS_LOOK: Record<ConflictClass, { tone: Tone; Icon: LucideIcon; fallback: string }> = {
  TRUE_CONFLICT: { tone: "critical", Icon: OctagonAlert, fallback: "True conflict" },
  INSUFFICIENT_INFORMATION: { tone: "warn", Icon: CircleHelp, fallback: "Insufficient information" },
  EXPLAINABLE_DIFFERENCE: { tone: "info", Icon: Split, fallback: "Explainable difference" },
  CONSISTENT: { tone: "muted", Icon: Equal, fallback: "Consistent" },
};

export function ClassBadge({ value, label, definition }: { value: ConflictClass; label?: string | null; definition?: string | null }) {
  const look = CLASS_LOOK[value] ?? CLASS_LOOK.INSUFFICIENT_INFORMATION;
  return (
    <Badge tone={look.tone} Icon={look.Icon} title={definition ?? undefined}>
      <span data-testid="conflict-class">{label ?? look.fallback}</span>
    </Badge>
  );
}

const STATUS_TEXT: Record<DimensionStatus, { text: string; tone: Tone }> = {
  SAME: { text: "Same", tone: "muted" },
  DIFFERENT: { text: "Differs", tone: "info" },
  UNKNOWN: { text: "Not determinable", tone: "warn" },
  UNCONFIRMED: { text: "Not confirmed", tone: "muted" },
  NOTED: { text: "Described differently", tone: "muted" },
};

export function DimensionStatusBadge({ status, explains }: { status: DimensionStatus; explains: boolean }) {
  const look = STATUS_TEXT[status] ?? STATUS_TEXT.UNCONFIRMED;
  return (
    <Badge tone={look.tone} icon={false}>
      {look.text}
      {explains ? ": explains the difference" : ""}
    </Badge>
  );
}
