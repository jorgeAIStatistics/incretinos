"use client";

import clsx from "clsx";
import { Search, X } from "lucide-react";
import { type HTMLInputTypeAttribute, type ReactNode, useId } from "react";

/*
 * Form controls. The label sits above the control and is always visible (a placeholder is an example, never a label).
 * Helper text sits below; an error replaces it, is announced, and is linked with aria-describedby.
 * Control edges use --border-control (3:1 against every surface); hover darkens the edge, focus shows the global ring.
 */
export const CONTROL =
  "h-(--control-h) w-full min-w-0 rounded-control border border-control bg-surface px-2.5 text-body text-ink " +
  "transition-[border-color,box-shadow] duration-(--dur-fast) ease-standard hover:border-ink-2 " +
  "disabled:cursor-not-allowed disabled:bg-sunken disabled:text-ink-3 aria-invalid:border-critical";

/* The label is tied to its control by id, not by wrapping it: a wrapping label would add the control's own value to its accessible name. */
/* `justify-end` keeps the controls of one row on a line when their labels wrap to different heights. */
const FIELD = "flex min-w-0 flex-col justify-end gap-1 text-caption font-medium text-ink-2";

function Messages({ hint, error, hintId, errorId }: { hint?: ReactNode; error?: string | null; hintId: string; errorId: string }) {
  if (error) {
    return (
      <span id={errorId} role="alert" className="text-caption font-normal text-critical">
        {error}
      </span>
    );
  }
  return hint ? (
    <span id={hintId} className="text-caption font-normal text-ink-3">
      {hint}
    </span>
  ) : null;
}

export type FieldCommon = { label: string; hint?: ReactNode; error?: string | null; className?: string; disabled?: boolean; name?: string };

export function TextField({
  label,
  value,
  onChange,
  hint,
  error,
  className,
  disabled,
  name,
  type = "text",
  placeholder,
  autoComplete = "off",
  inputMode,
  spellCheck,
  required,
}: FieldCommon & {
  value: string;
  onChange: (value: string) => void;
  type?: HTMLInputTypeAttribute;
  /** An example of the expected input, ending with an ellipsis. It disappears on typing, so it never replaces the label. */
  placeholder?: string;
  autoComplete?: string;
  inputMode?: "text" | "search" | "numeric" | "decimal" | "email" | "url";
  spellCheck?: boolean;
  required?: boolean;
}) {
  const id = useId();
  return (
    <div className={clsx(FIELD, className)}>
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type={type}
        name={name}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoComplete={autoComplete}
        inputMode={inputMode}
        spellCheck={spellCheck}
        required={required}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : hint ? `${id}-hint` : undefined}
        className={clsx(CONTROL, "font-normal")}
      />
      <Messages hint={hint} error={error} hintId={`${id}-hint`} errorId={`${id}-error`} />
    </div>
  );
}

/** Search box with a leading icon and a clear button. The request it drives should be debounced (`useDebounced`). */
export function SearchField({ label, value, onChange, placeholder, className, name = "q", hint }: FieldCommon & { value: string; onChange: (value: string) => void; placeholder?: string }) {
  const id = useId();
  return (
    <div className={clsx(FIELD, className)}>
      <label htmlFor={id}>{label}</label>
      <span className="relative block">
        <Search aria-hidden size={15} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-3" />
        <input
          id={id}
          type="search"
          name={name}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          autoComplete="off"
          spellCheck={false}
          enterKeyHint="search"
          aria-describedby={hint ? `${id}-hint` : undefined}
          className={clsx(CONTROL, "pl-8 pr-8 font-normal [&::-webkit-search-cancel-button]:hidden")}
        />
        {value ? (
          <button type="button" aria-label={`Clear ${label.toLowerCase()}`} onClick={() => onChange("")} className="absolute right-1 top-1/2 grid size-7 -translate-y-1/2 place-items-center rounded-chip text-ink-3 transition-colors duration-(--dur-fast) hover:bg-hover hover:text-ink">
            <X aria-hidden size={14} />
          </button>
        ) : null}
      </span>
      <Messages hint={hint} hintId={`${id}-hint`} errorId={`${id}-error`} />
    </div>
  );
}

export function Select({
  label,
  value,
  onChange,
  options,
  className,
  hint,
  error,
  disabled,
  name,
}: FieldCommon & { value: string; onChange: (v: string) => void; options: { value: string; label: string; disabled?: boolean }[] }) {
  const id = useId();
  return (
    <div className={clsx(FIELD, "max-w-full", className)}>
      <label htmlFor={id}>{label}</label>
      <select
        id={id}
        name={name}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : hint ? `${id}-hint` : undefined}
        className={clsx(CONTROL, "cursor-pointer pr-7 font-normal")}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value} disabled={o.disabled}>
            {o.label}
          </option>
        ))}
      </select>
      <Messages hint={hint} error={error} hintId={`${id}-hint`} errorId={`${id}-error`} />
    </div>
  );
}

export function NumberField({
  label,
  value,
  onChange,
  step = 1,
  min,
  max,
  suffix,
  hint,
  error,
  className,
  disabled,
  name,
}: FieldCommon & { value: number; onChange: (v: number) => void; step?: number; min?: number; max?: number; suffix?: string }) {
  const id = useId();
  return (
    <div className={clsx(FIELD, className)}>
      <label htmlFor={id}>{label}</label>
      <span className="flex items-center gap-1.5">
        <input
          id={id}
          type="number"
          inputMode="decimal"
          name={name}
          autoComplete="off"
          value={Number.isFinite(value) ? value : ""}
          step={step}
          min={min}
          max={max}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value === "" ? NaN : Number(e.target.value))}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-error` : hint ? `${id}-hint` : undefined}
          className={clsx(CONTROL, "num font-normal")}
        />
        {suffix ? <span className="text-caption font-normal text-ink-3">{suffix}</span> : null}
      </span>
      <Messages hint={hint} error={error} hintId={`${id}-hint`} errorId={`${id}-error`} />
    </div>
  );
}

export function TextArea({ label, value, onChange, hint, error, className, disabled, name, rows = 3, placeholder, required }: FieldCommon & { value: string; onChange: (value: string) => void; rows?: number; placeholder?: string; required?: boolean }) {
  const id = useId();
  return (
    <div className={clsx(FIELD, className)}>
      <label htmlFor={id}>{label}</label>
      <textarea
        id={id}
        name={name}
        value={value}
        rows={rows}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        required={required}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : hint ? `${id}-hint` : undefined}
        className={clsx(CONTROL, "h-auto min-h-20 resize-y py-2 font-normal leading-snug")}
      />
      <Messages hint={hint} error={error} hintId={`${id}-hint`} errorId={`${id}-error`} />
    </div>
  );
}

/** Range input with its current value shown beside the label. `format` controls how the value is written. */
export function Slider({ label, value, onChange, min = 0, max = 1, step = 0.05, format = (v) => v.toFixed(2), className, name }: { label: string; value: number; onChange: (v: number) => void; min?: number; max?: number; step?: number; format?: (v: number) => string; className?: string; name?: string }) {
  const id = useId();
  return (
    <div className={clsx("flex min-w-0 flex-col gap-1 text-caption font-medium text-ink-2", className)}>
      <div className="flex items-baseline justify-between gap-2">
        <label htmlFor={id}>{label}</label>
        <output htmlFor={id} className="num font-normal text-ink-2">
          {format(value)}
        </output>
      </div>
      <input id={id} type="range" name={name} min={min} max={max} step={step} value={value} aria-valuetext={format(value)} onChange={(e) => onChange(Number(e.target.value))} className="h-5 w-full cursor-pointer accent-(--accent)" />
    </div>
  );
}

/**
 * A small set of mutually exclusive choices shown side by side. Built on native radio inputs, so arrow keys,
 * form participation and screen-reader semantics come from the browser.
 */
export function SegmentedControl<V extends string>({ label, value, onChange, options, size = "md", className, hideLabel }: { label: string; value: V; onChange: (value: V) => void; options: { value: V; label: string }[]; size?: "sm" | "md"; className?: string; hideLabel?: boolean }) {
  const name = useId();
  return (
    <fieldset className={clsx("min-w-0", className)}>
      <legend className={clsx("mb-1 text-caption font-medium text-ink-2", hideLabel && "sr-only")}>{label}</legend>
      <div className={clsx("inline-flex max-w-full rounded-control border border-control bg-sunken p-0.5", size === "sm" ? "h-(--control-h-sm)" : "h-(--control-h)")}>
        {options.map((o) => (
          <label
            key={o.value}
            className={clsx(
              "relative flex cursor-pointer select-none items-center whitespace-nowrap rounded-chip px-2.5 font-medium transition-[background-color,color,box-shadow] duration-(--dur-fast) ease-standard",
              "has-focus-visible:outline-2 has-focus-visible:outline-offset-1 has-focus-visible:outline-accent",
              size === "sm" ? "text-caption" : "text-data",
              // The selected segment is marked by an edge that meets 3:1 and by heavier text, not by a faint fill alone.
              value === o.value ? "bg-surface font-semibold text-ink shadow-[0_0_0_1px_var(--border-control)]" : "text-ink-2 hover:text-ink",
            )}
          >
            <input type="radio" name={name} value={o.value} checked={value === o.value} onChange={() => onChange(o.value)} className="sr-only" />
            {o.label}
          </label>
        ))}
      </div>
    </fieldset>
  );
}
