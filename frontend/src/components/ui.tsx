"use client";

import * as Tooltip from "@radix-ui/react-tooltip";
import clsx from "clsx";
import { AlertTriangle, CheckCircle2, CircleDashed, ExternalLink, FlaskConical, Info, ShieldAlert } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { day, fmt, titleCase } from "@/lib/api";

/* ---------- page scaffolding ---------- */

export function PageHeader({ eyebrow, title, lede, children }: { eyebrow: string; title: string; lede: string; children?: ReactNode }) {
  return (
    <header className="mb-7 flex flex-wrap items-end justify-between gap-4 border-b border-hairline pb-5">
      <div className="max-w-3xl">
        <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-teal">{eyebrow}</p>
        <h1 className="mt-1 text-[26px] font-semibold leading-tight text-ink">{title}</h1>
        <p className="mt-1.5 text-[14.5px] leading-relaxed text-ink-2">{lede}</p>
      </div>
      {children ? <div className="flex flex-wrap items-center gap-2">{children}</div> : null}
    </header>
  );
}

export function Section({ title, hint, action, children, id }: { title: string; hint?: ReactNode; action?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section id={id} className="mb-8">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="text-[16px] font-semibold text-ink">{title}</h2>
          {hint ? <p className="mt-0.5 max-w-3xl text-[13px] text-ink-3">{hint}</p> : null}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export function Panel({ children, className, pad = true }: { children: ReactNode; className?: string; pad?: boolean }) {
  return <div className={clsx("rounded-lg border border-hairline bg-surface", pad && "p-4", className)}>{children}</div>;
}

export function Stat({ label, value, note }: { label: string; value: ReactNode; note?: ReactNode }) {
  return (
    <div className="min-w-0">
      <p className="text-[11.5px] font-medium uppercase tracking-wide text-ink-3">{label}</p>
      <p className="num mt-0.5 text-[22px] font-semibold leading-tight text-ink">{value}</p>
      {note ? <p className="mt-0.5 text-[12px] text-ink-3">{note}</p> : null}
    </div>
  );
}

/* ---------- status & qualifiers (never colour alone: icon + text) ---------- */

type Tone = "ok" | "info" | "warn" | "muted" | "critical";
const TONE: Record<Tone, { cls: string; Icon: typeof Info }> = {
  ok: { cls: "border-[#cfe8cf] bg-[var(--ok-soft)] text-[var(--ok)]", Icon: CheckCircle2 },
  info: { cls: "border-[#cfdff5] bg-accent-soft text-accent", Icon: Info },
  warn: { cls: "border-[#f1dfb0] bg-[var(--warn-soft)] text-[var(--warn)]", Icon: AlertTriangle },
  muted: { cls: "border-hairline bg-sunken text-ink-2", Icon: CircleDashed },
  critical: { cls: "border-[#f2c9c5] bg-[var(--critical-soft)] text-[var(--critical)]", Icon: ShieldAlert },
};

export function Badge({ tone = "muted", children, title, icon = true }: { tone?: Tone; children: ReactNode; title?: string; icon?: boolean }) {
  const { cls, Icon } = TONE[tone];
  const chip = (
    <span className={clsx("inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 py-[1px] text-[11.5px] font-medium leading-5", cls)}>
      {icon ? <Icon aria-hidden size={12} strokeWidth={2.2} /> : null}
      {children}
    </span>
  );
  if (!title) return chip;
  return (
    <Tooltip.Root delayDuration={120}>
      <Tooltip.Trigger asChild>
        <span tabIndex={0} className="cursor-help rounded">
          {chip}
        </span>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content sideOffset={6} className="z-50 max-w-xs rounded-md bg-ink px-3 py-2 text-[12.5px] leading-snug text-white shadow-lg">
          {title}
          <Tooltip.Arrow className="fill-[var(--ink)]" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

export type Label = { code: string; text: string; tone: string; detail: string };

/** Mandatory qualifiers that travel with every number. */
export function Qualifiers({ labels, max }: { labels: Label[]; max?: number }) {
  const shown = max ? labels.slice(0, max) : labels;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {shown.map((l) => (
        <Badge key={l.code} tone={(l.tone as Tone) ?? "muted"} title={l.detail}>
          {l.text}
        </Badge>
      ))}
      {max && labels.length > max ? <Badge tone="muted" icon={false} title={labels.slice(max).map((l) => l.text).join(" · ")}>+{labels.length - max}</Badge> : null}
    </span>
  );
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

export function RegulatoryBadge({ status }: { status: string }) {
  if (status === "APPROVED") return <Badge tone="ok" title="FDA labeling for this drug is ingested and cited.">FDA-approved</Badge>;
  if (status === "INVESTIGATIONAL") return <Badge tone="warn" title="Not approved for any indication. Status is re-checked against the FDA label database at each ingestion.">Investigational</Badge>;
  if (status === "SUBMITTED") return <Badge tone="info">Under regulatory review</Badge>;
  return <Badge tone="muted" title="Regulatory status is not tracked for this agent.">Status not tracked</Badge>;
}

const VERIFY: Record<string, { tone: Tone; text: string; title: string }> = {
  MACHINE_EXTRACTED: { tone: "ok", text: "Machine-extracted", title: "Parsed directly from the structured registry payload whose content hash is stored." },
  QUOTE_ANCHORED: { tone: "ok", text: "Quote-anchored", title: "The locator quote was matched verbatim against the source text." },
  CURATED_PENDING_REVIEW: { tone: "muted", text: "Pending human review", title: "Curated value that could not be anchored automatically; queued for review." },
  HUMAN_VERIFIED: { tone: "ok", text: "Human-verified", title: "Approved through a recorded review decision." },
  CONFLICT: { tone: "warn", text: "Conflict", title: "Disagrees with another source." },
};
export function VerificationBadge({ status }: { status: string }) {
  const v = VERIFY[status] ?? { tone: "muted" as Tone, text: titleCase(status), title: "" };
  return <Badge tone={v.tone} title={v.title}>{v.text}</Badge>;
}

/* ---------- provenance ---------- */

export type SourceRef = { id: number; key: string; tier: string; tier_label: string; title: string; publisher?: string; url?: string; published_date?: string; retrieved_at?: string; version_label?: string; content_hash?: string };

export function SourceLine({ source, quote, location, compact }: { source: SourceRef | null | undefined; quote?: string | null; location?: string | null; compact?: boolean }) {
  if (!source) return <span className="text-ink-3">Source not recorded</span>;
  return (
    <span className="text-[12.5px] leading-snug text-ink-2">
      <Link href={`/sources?id=${source.id}`} className="font-medium">
        {compact ? source.tier_label : source.title}
      </Link>
      {!compact ? <span className="text-ink-3"> · {source.tier_label}</span> : null}
      <span className="text-ink-3"> · retrieved {day(source.retrieved_at)}</span>
      {source.url ? (
        <a href={source.url} target="_blank" rel="noopener noreferrer" className="ml-1.5 inline-flex items-center gap-0.5 text-[12px]">
          original <ExternalLink aria-hidden size={11} />
        </a>
      ) : null}
      {quote ? <span className="mt-0.5 block text-ink-3">“{quote}”</span> : null}
      {location ? <span className="mono mt-0.5 block text-ink-3">{location}</span> : null}
    </span>
  );
}

/* ---------- states ---------- */

export function Loading({ rows = 4 }: { rows?: number }) {
  return (
    <div role="status" aria-label="Loading" className="space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton h-9" style={{ width: `${100 - i * 7}%` }} />
      ))}
    </div>
  );
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div role="alert" className="flex items-start gap-2 rounded-md border border-[#f2c9c5] bg-[var(--critical-soft)] px-3 py-2 text-[13px] text-[var(--critical)]">
      <ShieldAlert aria-hidden size={15} className="mt-0.5 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

export function Callout({ tone = "info", title, children }: { tone?: Tone; title?: string; children: ReactNode }) {
  const { cls, Icon } = TONE[tone];
  return (
    <div className={clsx("flex gap-2.5 rounded-md border px-3.5 py-2.5 text-[13px] leading-relaxed", cls)}>
      <Icon aria-hidden size={15} className="mt-[3px] shrink-0" />
      <div className="text-ink-2">
        {title ? <p className="font-semibold text-ink">{title}</p> : null}
        {children}
      </div>
    </div>
  );
}

export function Hypothetical({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-start gap-2 rounded-md border border-dashed border-[#c9b37a] bg-[#fffaf0] px-3.5 py-2.5 text-[13px] text-ink-2">
      <FlaskConical aria-hidden size={15} className="mt-[3px] shrink-0 text-[var(--warn)]" />
      <div>{children}</div>
    </div>
  );
}

/* ---------- controls ---------- */

export function Select({ label, value, onChange, options, className }: { label: string; value: string; onChange: (v: string) => void; options: { value: string; label: string }[]; className?: string }) {
  return (
    <label className={clsx("flex min-w-0 flex-col gap-1 text-[12px] font-medium text-ink-2", className)}>
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-9 min-w-0 rounded-md border border-hairline-strong bg-surface px-2.5 text-[13.5px] font-normal text-ink shadow-[0_1px_0_rgba(16,24,32,0.03)] transition-colors hover:border-ink-3"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function NumberField({ label, value, onChange, step = 1, min, max, suffix }: { label: string; value: number; onChange: (v: number) => void; step?: number; min?: number; max?: number; suffix?: string }) {
  return (
    <label className="flex flex-col gap-1 text-[12px] font-medium text-ink-2">
      {label}
      <span className="flex items-center gap-1.5">
        <input
          type="number"
          value={Number.isFinite(value) ? value : ""}
          step={step}
          min={min}
          max={max}
          onChange={(e) => onChange(e.target.value === "" ? NaN : Number(e.target.value))}
          className="num h-9 w-full min-w-0 rounded-md border border-hairline-strong bg-surface px-2.5 text-[13.5px] font-normal text-ink"
        />
        {suffix ? <span className="text-[12px] font-normal text-ink-3">{suffix}</span> : null}
      </span>
    </label>
  );
}

export function Button({ children, onClick, variant = "primary", disabled, type = "button" }: { children: ReactNode; onClick?: () => void; variant?: "primary" | "ghost"; disabled?: boolean; type?: "button" | "submit" }) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={clsx(
        "inline-flex h-9 items-center gap-1.5 rounded-md px-3.5 text-[13.5px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        variant === "primary" ? "bg-accent text-white hover:bg-[#184f9f]" : "border border-hairline-strong bg-surface text-ink hover:bg-sunken",
      )}
    >
      {children}
    </button>
  );
}

export function KV({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(120px,max-content)_1fr] gap-x-5 gap-y-1.5 text-[13px]">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-ink-3">{k}</dt>
          <dd className="min-w-0 text-ink">{v ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

export function ValueCell({ value, unit, lo, hi, level }: { value: number | null; unit?: string | null; lo?: number | null; hi?: number | null; level?: number | null }) {
  const isPct = !!unit && /percent|%/i.test(unit);
  return (
    <span className="num whitespace-nowrap">
      <span className="font-semibold text-ink">{fmt(value)}{isPct ? "%" : ""}</span>
      {lo != null && hi != null ? (
        <span className="ml-1.5 text-[12px] text-ink-3">
          {level ? `${fmt(level, 1)}% CI ` : "CI "}
          {fmt(lo)} to {fmt(hi)}
        </span>
      ) : null}
    </span>
  );
}
