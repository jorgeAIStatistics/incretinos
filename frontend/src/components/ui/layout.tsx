"use client";

import clsx from "clsx";
import { ChevronRight, FlaskConical } from "lucide-react";
import { createElement, type ReactNode, useId, useState } from "react";
import { TONE, type Tone } from "./badge";

/*
 * Page scaffolding. The heading outline is fixed: PageHeader renders the single h1, Section an h2, and a titled
 * Panel an h3 (or the level passed to it). Titles are real heading elements, never bold paragraphs.
 */

export function PageHeader({ eyebrow, title, lede, children }: { eyebrow?: string; title: string; lede?: ReactNode; children?: ReactNode }) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-x-6 gap-y-4 border-b border-hairline pb-5">
      <div className="min-w-0 max-w-3xl">
        {/* Context line: sentence case, quiet. One per page, never repeated above section headings. */}
        {eyebrow ? <p className="text-data font-medium text-ink-3">{eyebrow}</p> : null}
        <h1 className="mt-0.5 text-stat font-semibold text-ink sm:text-display">{title}</h1>
        {lede ? <p className="mt-1.5 max-w-[68ch] text-body text-ink-2">{lede}</p> : null}
      </div>
      {children ? <div className="flex flex-wrap items-center gap-2">{children}</div> : null}
    </header>
  );
}

export function Section({ title, hint, action, children, id, level = 2, className }: { title: string; hint?: ReactNode; action?: ReactNode; children: ReactNode; id?: string; level?: 2 | 3; className?: string }) {
  // Deliberately not a named region: a page has many sections, and naming each one would bury the real landmarks.
  return (
    <section id={id} className={clsx("mb-8", className)}>
      <div className="mb-3 flex flex-wrap items-end justify-between gap-x-4 gap-y-1">
        <div className="min-w-0">
          {createElement(`h${level}`, { className: level === 2 ? "text-title font-semibold text-ink" : "text-body font-semibold text-ink" }, title)}
          {hint ? <p className="mt-0.5 max-w-[72ch] text-data text-ink-3">{hint}</p> : null}
        </div>
        {action ? <div className="shrink-0 text-data">{action}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function Panel({
  children,
  className,
  pad = true,
  title,
  description,
  action,
  level = 3,
  as = "div",
}: {
  children: ReactNode;
  className?: string;
  pad?: boolean;
  /** Renders a real heading. Pass `level` to keep the outline in order (h2 directly under the page title, h3 inside a Section). */
  title?: string;
  description?: ReactNode;
  action?: ReactNode;
  level?: 2 | 3 | 4;
  as?: "div" | "section" | "article" | "aside";
}) {
  return createElement(
    as,
    { className: clsx("rounded-panel border border-hairline bg-surface", pad && "p-4", className) },
    title ? (
      <div className={clsx("mb-3 flex flex-wrap items-start justify-between gap-x-4 gap-y-1", !pad && "px-4 pt-4")}>
        <div className="min-w-0">
          {createElement(`h${level}`, { className: "text-body font-semibold text-ink" }, title)}
          {description ? <p className="mt-0.5 text-data text-ink-3">{description}</p> : null}
        </div>
        {action ? <div className="shrink-0 text-data">{action}</div> : null}
      </div>
    ) : null,
    children,
  );
}

/**
 * A headline figure. Label in sentence case; the value in proportional figures (tabular figures are for columns);
 * a missing value is written out. It has no delta or trend slot on purpose: the platform does not track change
 * over time for these counts, and a decorative trend would imply data that does not exist.
 */
export function StatTile({ label, value, note, className }: { label: string; value: ReactNode; note?: ReactNode; className?: string }) {
  return (
    <div className={clsx("min-w-0", className)}>
      <p className="text-caption font-medium text-ink-3">{label}</p>
      <p className="mt-0.5 text-stat font-semibold text-ink">{value}</p>
      {note ? <p className="mt-0.5 text-caption text-ink-3">{note}</p> : null}
    </div>
  );
}
/** Earlier name of StatTile. */
export const Stat = StatTile;

export function KV({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-1 gap-x-5 gap-y-1.5 text-data sm:grid-cols-[minmax(120px,max-content)_minmax(0,1fr)]">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-ink-3 max-sm:mt-1.5 max-sm:first:mt-0">{k}</dt>
          <dd className="min-w-0 break-words text-ink">{v ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Callout({ tone = "info", title, children, level, className }: { tone?: Tone; title?: string; children?: ReactNode; /** Render the title as a heading of this level. */ level?: 2 | 3 | 4; className?: string }) {
  const { cls, Icon } = TONE[tone];
  return (
    <div className={clsx("flex gap-2.5 rounded-control border px-3.5 py-2.5 text-data leading-relaxed", cls, className)}>
      <Icon aria-hidden size={15} className="mt-[3px] shrink-0" />
      <div className="min-w-0 text-ink-2">
        {title ? createElement(level ? `h${level}` : "p", { className: "font-semibold text-ink" }, title) : null}
        {children}
      </div>
    </div>
  );
}

/**
 * Container for anything that is not evidence: a hypothetical scenario, a simulation, synthetic demonstration data.
 * Dashed outline plus a hatch texture, so it reads as "not data" without relying on colour.
 */
export function Hypothetical({ children, label }: { children: ReactNode; label?: string }) {
  return (
    <div className="hatch flex items-start gap-2 rounded-control border border-dashed border-ink-2 bg-sunken px-3.5 py-2.5 text-data text-ink-2">
      <FlaskConical aria-hidden size={15} className="mt-[3px] shrink-0 text-ink-2" />
      <div className="min-w-0">
        {label ? <p className="font-semibold text-ink">{label}</p> : null}
        {children}
      </div>
    </div>
  );
}

/** A collapsible region with a real button. The content fades in; its height is not animated. */
export function Disclosure({ summary, children, defaultOpen = false, className }: { summary: ReactNode; children: ReactNode; defaultOpen?: boolean; className?: string }) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  return (
    <div className={className}>
      <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen((o) => !o)} className="inline-flex min-h-6 items-center gap-1 rounded-chip text-left text-data font-medium text-accent hover:underline">
        <ChevronRight aria-hidden size={14} className={clsx("shrink-0 transition-transform duration-(--dur-base) ease-standard", open && "rotate-90")} />
        {summary}
      </button>
      {open ? (
        <div id={id} className="disclosure-content mt-1.5">
          {children}
        </div>
      ) : null}
    </div>
  );
}

/**
 * A 0 to 1 score as a thin bar with its value written beside it. The bar starts at zero and has no decoration;
 * the number is the content and the bar only helps comparison down a column.
 */
export function Meter({ value, label, digits = 2 }: { value: number; /** Names what is measured, for assistive technology. */ label: string; digits?: number }) {
  const clamped = Math.max(0, Math.min(1, value));
  return (
    <span className="inline-flex items-center gap-2 align-middle">
      <span role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={1} aria-valuenow={Number(value.toFixed(3))} className="relative inline-block h-1.5 w-16 shrink-0 overflow-hidden rounded-full bg-grid">
        <span className="absolute inset-y-0 left-0 rounded-full bg-series-1" style={{ width: `${clamped * 100}%` }} />
      </span>
      <span className="num text-caption text-ink-2">{value.toFixed(digits)}</span>
    </span>
  );
}
