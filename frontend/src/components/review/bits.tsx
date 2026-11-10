"use client";

import clsx from "clsx";
import { AlertTriangle, CircleDot, Info, type LucideIcon, ShieldAlert } from "lucide-react";
import { createElement, type ReactNode } from "react";
import { Missing } from "@/components/ui";
import { highlightNumbers, plainValue, SEVERITY_LABEL, type StoredNumber } from "@/lib/review";

/*
 * Small pieces shared by the three panels of the workbench. Nothing here formats a scientific value: stored
 * numbers are written with every stored digit (`plainValue`), and a missing value is written out as "Not reported".
 */

/** A titled block inside a panel. `level` keeps the heading outline in order (h2 for a panel, h3 inside). */
export function Block({ title, level = 3, action, children, className, id, testId }: { title: string; level?: 2 | 3 | 4; action?: ReactNode; children: ReactNode; className?: string; id?: string; testId?: string }) {
  return (
    <section id={id} data-testid={testId} className={clsx("@container min-w-0 border-t border-hairline pt-3 first:border-t-0 first:pt-0", className)}>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        {createElement(`h${level}`, { className: level === 2 ? "text-body font-semibold text-ink" : "text-data font-semibold text-ink" }, title)}
        {action ? <div className="text-caption text-ink-3">{action}</div> : null}
      </div>
      {children}
    </section>
  );
}

/** Label above value: narrow panels cannot afford a two-column key/value grid. */
export function Fields({ rows, className }: { rows: [label: ReactNode, value: ReactNode, key?: string][]; className?: string }) {
  return (
    <dl className={clsx("grid grid-cols-1 gap-x-4 gap-y-2 text-data @md:grid-cols-2", className)}>
      {rows.map(([label, value, key], i) => (
        <div key={key ?? (typeof label === "string" ? label : i)} className="min-w-0">
          <dt className="text-caption text-ink-3">{label}</dt>
          <dd className="min-w-0 break-words text-ink">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A stored value exactly as stored. Null is "Not reported", never blank and never 0. */
export function Stored({ value, mono, missing }: { value: unknown; mono?: boolean; missing?: string }) {
  if (value === null || value === undefined || value === "") return <Missing className="!whitespace-normal">{missing}</Missing>;
  if (typeof value === "boolean") return <>{value ? "Yes" : "No"}</>;
  const text = plainValue(value);
  return <span className={clsx(mono && "mono break-all", typeof value === "number" && "num")}>{text.replace(/^-(?=\d)/, "−")}</span>;
}

/**
 * A passage with the stored numbers marked. The passage text stays verbatim; the mark is shaded and underlined (not
 * colour alone) and names its field on hover. The panel lists the marked fields in words after the passage.
 */
export function MarkedText({ text, numbers, className, testId }: { text: string; numbers: StoredNumber[]; className?: string; testId?: string }) {
  const { segments } = highlightNumbers(text, numbers);
  return (
    <span className={className} data-testid={testId}>
      {segments.map((s, i) =>
        s.fields ? (
          <mark key={i} title={`Stored as: ${s.fields.join(", ")}`} data-fields={s.fields.join(" ")} className="rounded-[2px] bg-accent-soft px-0.5 font-semibold text-ink underline decoration-accent decoration-2 underline-offset-[3px]">
            {s.text}
          </mark>
        ) : (
          <span key={i}>{s.text}</span>
        ),
      )}
    </span>
  );
}

const SEVERITY_LOOK: Record<string, { Icon: LucideIcon; cls: string }> = {
  HIGH: { Icon: ShieldAlert, cls: "border-critical-border bg-critical-soft text-critical" },
  MEDIUM: { Icon: AlertTriangle, cls: "border-warn-border bg-warn-soft text-warn" },
  LOW: { Icon: Info, cls: "border-hairline bg-sunken text-ink-2" },
  NONE: { Icon: CircleDot, cls: "border-hairline bg-surface text-ink-3" },
};

/** Severity of an automated check, or the risk level of a record: icon and word, colour only reinforces. */
export function Severity({ level, prefix }: { level: string | null | undefined; prefix?: string }) {
  const key = (level ?? "NONE").toUpperCase();
  const look = SEVERITY_LOOK[key] ?? SEVERITY_LOOK.NONE;
  return (
    <span className={clsx("inline-flex items-center gap-1 whitespace-nowrap rounded-chip border px-1.5 py-px text-caption font-medium leading-5", look.cls)}>
      <look.Icon aria-hidden size={12} strokeWidth={2.2} />
      {prefix ? `${prefix}: ${(SEVERITY_LABEL[key] ?? key).toLowerCase()}` : (SEVERITY_LABEL[key] ?? key)}
    </span>
  );
}

/** A JSON value, pretty-printed, with stored numbers marked. Scrolls inside its own box. */
export function JsonBlock({ value, text, numbers, label }: { value?: unknown; text?: string | null; numbers?: StoredNumber[]; label: string }) {
  const body = text ?? JSON.stringify(value, null, 2) ?? "null";
  return (
    // oxlint-disable-next-line jsx-a11y/no-noninteractive-tabindex
    <pre tabIndex={0} role="region" aria-label={label} className="mono max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-control border border-hairline bg-sunken p-2.5 text-caption leading-relaxed text-ink">
      {numbers ? <MarkedText text={body} numbers={numbers} /> : body}
    </pre>
  );
}

/** "before → after" for a field change; both sides exactly as stored. */
export function Diff({ before, after }: { before: unknown; after: unknown }) {
  return (
    <span className="inline-flex flex-wrap items-baseline gap-x-1.5">
      <span className="text-ink-2 line-through decoration-ink-3">
        <Stored value={before} missing="empty" />
      </span>
      <span aria-hidden className="text-ink-3">→</span>
      <span className="sr-only">changed to</span>
      <span className="font-medium text-ink">
        <Stored value={after} missing="empty" />
      </span>
    </span>
  );
}
