"use client";

import { Table2 } from "lucide-react";
import Link from "next/link";
import { type ReactNode, useEffect, useId, useLayoutEffect, useMemo, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis } from "recharts";
import { fmt } from "@/lib/api";
import { ciLabel, withUnit } from "@/lib/rows";

export type ChartRow = {
  key: string;
  label: string;
  /** One line under the label. Kept for callers that pass a single line; `meta` takes precedence. */
  sub?: string;
  /** Lines the reader needs to interpret the number — estimand, evidence tier, time point, source. Always visible; they wrap. */
  meta?: string[];
  value: number | null;
  /** The value as text when the caller holds the source's own text; otherwise the number is printed unrounded. */
  valueText?: string;
  /** Unit printed beside this row's number; falls back to the chart's `unit`. Rows with different units are never drawn on one axis. */
  unit?: string;
  lo?: number | null;
  hi?: number | null;
  intervalKind?: "reported" | "derived";
  /** Confidence level in percent (e.g. 95) when it is stated. */
  intervalLevel?: number | null;
  range?: [number, number] | null; // a disclosed range across arms (no point value)
  group: "active" | "control" | "comparator" | "other";
  /** The value was calculated here from reported counts; it is not a figure the source reported. */
  calculated?: boolean;
  /** Text under the value in place of the interval line (e.g. "25 of 55 participants"). */
  valueNote?: string;
  tip?: string[];
  href?: string;
};

/** Arm roles. Colour is never the only cue: each row also names its role in text. */
const GROUP = {
  active: { color: "var(--series-1)", name: "Study drug", outline: false },
  comparator: { color: "var(--series-2)", name: "Active comparator", outline: false },
  control: { color: "var(--series-control)", name: "Placebo", outline: true },
  other: { color: "var(--series-3)", name: "Arm role not classified", outline: true },
} as const;

export type BarFill = "solid" | "hatched" | "outline";

/* ---------- text helpers shared by the chart and its table ---------- */

const rowUnit = (r: ChartRow, unit: string) => r.unit ?? unit;

function valueText(r: ChartRow, unit: string): string {
  const u = rowUnit(r, unit);
  if (r.value != null) return withUnit(r.valueText ?? fmt(r.value), u);
  if (r.range) return `${fmt(r.range[0])} to ${withUnit(fmt(r.range[1]), u)}`;
  return "not reported";
}

function intervalBounds(r: ChartRow, unit: string): string | null {
  if (r.lo == null || r.hi == null) return null;
  const digits = r.intervalKind === "derived" ? 2 : undefined; // reported limits are printed as reported; derived ones are calculations
  return `${fmt(r.lo, digits)} to ${withUnit(fmt(r.hi, digits), rowUnit(r, unit))}`;
}

function intervalText(r: ChartRow, unit: string): string | null {
  const bounds = intervalBounds(r, unit);
  if (!bounds) return null;
  if (r.intervalKind === "derived") return `derived ${fmt(r.intervalLevel ?? 95)}% interval ${bounds}`;
  return `${ciLabel(r.intervalLevel)} ${bounds}`;
}

/** Second line of the value block: the interval, a caller's note, or an explicit statement that no interval exists. */
function noteText(r: ChartRow, unit: string): string | null {
  if (r.valueNote) return r.valueNote;
  const interval = intervalText(r, unit);
  if (interval) return interval;
  if (r.range) return "range across arms";
  return r.value != null ? "CI not reported" : null;
}

/* ---------- layout ---------- */

function useWidth<T extends HTMLElement>(): [(element: T | null) => void, number | null] {
  const [element, setElement] = useState<T | null>(null);
  const [width, setWidth] = useState<number | null>(null);
  // Measured before the first paint, so a chart never forces its container wider than the page.
  useLayoutEffect(() => {
    if (!element) return;
    const measure = () => setWidth(Math.max(220, Math.floor(element.getBoundingClientRect().width)));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);
  return [setElement, width];
}

function niceTicks(min: number, max: number, count = 5): number[] {
  const span = max - min || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const start = Math.ceil(min / step) * step;
  const ticks: number[] = [];
  for (let v = start; v <= max + step * 1e-6; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

export function ChartFrame({ title, subtitle, footnote, children, table, controls }: { title: string; subtitle?: ReactNode; footnote?: ReactNode; children: ReactNode; table?: ReactNode; /** Shown above the chart and above its table view alike. */ controls?: ReactNode }) {
  const [showTable, setShowTable] = useState(false);
  return (
    <figure className="min-w-0 rounded-lg border border-hairline bg-surface">
      {/* The title block keeps at least 16rem; on a narrow card the table toggle moves below it instead of squeezing the caption. */}
      <figcaption className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2 px-4 pt-3.5">
        <div className="min-w-0 flex-1 basis-64">
          <p className="text-[14.5px] font-semibold text-ink">{title}</p>
          {subtitle ? <div className="mt-0.5 max-w-3xl text-[12.5px] leading-snug text-ink-3">{subtitle}</div> : null}
        </div>
        {table ? (
          <button
            type="button"
            onClick={() => setShowTable((s) => !s)}
            aria-pressed={showTable}
            className="inline-flex h-7 shrink-0 items-center gap-1.5 whitespace-nowrap rounded border border-hairline-strong px-2 text-[12px] font-medium text-ink-2 hover:bg-sunken"
          >
            <Table2 aria-hidden size={13} /> {showTable ? "Show chart" : "View as table"}
          </button>
        ) : null}
      </figcaption>
      {controls ? <div className="min-w-0 px-4 pt-2">{controls}</div> : null}
      <div className="min-w-0 px-4 pb-2 pt-2">{showTable && table ? <div className="overflow-x-auto">{table}</div> : children}</div>
      {footnote ? <div className="border-t border-hairline px-4 py-2.5 text-[12px] leading-snug text-ink-3">{footnote}</div> : null}
    </figure>
  );
}

const ROW_PLOT_H = 30;
const GAP = 12;

/**
 * Horizontal value chart, laid out like a forest plot: one row per arm (or analysis) with its label,
 * a thin zero-based bar or a dot, a whisker for the interval, and the number with its unit.
 *
 * - Labels are text, not SVG: they wrap at any width and nothing is truncated.
 * - Every row prints its value with its unit and says whether an interval was reported.
 * - A reported interval is a solid line with bar caps. An interval derived here from a reported standard error is a
 *   dashed line with open round caps: the two differ in shape, not in colour, and the legend says which is which.
 * - Rows are a list; a row's label is a button that shows its details on focus, hover or click and hides them on Escape.
 * - Rows in different units are refused: two values are never placed on one axis unless their unit matches.
 */
export function IntervalChart({
  rows,
  unit = "",
  mark = "bar",
  reference = 0,
  referenceLabel,
  extraLine,
  logScale = false,
  axisLabel,
  domain,
  fill = "solid",
  label,
}: {
  rows: ChartRow[];
  unit?: string;
  mark?: "bar" | "dot";
  reference?: number;
  referenceLabel?: string;
  extraLine?: { value: number; label: string };
  logScale?: boolean;
  axisLabel?: string;
  /** Extra values the axis must span, so panels that share a unit can share a scale. */
  domain?: [number, number];
  /** Bar texture. Used to tell kinds of comparison apart without colour. */
  fill?: BarFill;
  /** Accessible name of the chart. */
  label?: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [open, setOpen] = useState<string | null>(null);
  const [pinned, setPinned] = useState(false);
  const uid = useId();
  const close = () => {
    setOpen(null);
    setPinned(false);
  };
  // Escape dismisses the details wherever the pointer or focus is (WCAG 1.4.13).
  useEffect(() => {
    if (open === null) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  const units = Array.from(new Set(rows.map((r) => rowUnit(r, unit))));
  const axisUnit = units.length === 1 ? units[0] : "";
  const hasNotes = rows.some((r) => noteText(r, unit));
  // Below 700px the label sits above its bar at full width, so long arm names and their qualifiers wrap instead of being cut.
  const narrow = width !== null && width < 700;
  const labelW = width === null ? 0 : narrow ? width : Math.round(Math.min(380, Math.max(240, width * 0.36)));
  const valueW = width === null ? 0 : narrow ? Math.min(hasNotes ? 160 : 104, Math.round(width * 0.42)) : hasNotes ? 168 : 104;
  const plotW = width === null ? 0 : Math.max(60, narrow ? width - valueW - GAP : width - labelW - valueW - 2 * GAP);

  const { scale, ticks } = useMemo(() => {
    const values = rows.flatMap((r) => [r.value, r.lo, r.hi, ...(r.range ?? [])]).filter((v): v is number => v != null && Number.isFinite(v));
    values.push(reference);
    if (extraLine) values.push(extraLine.value);
    if (domain) values.push(...domain.filter((v) => Number.isFinite(v)));
    let min = Math.min(...values);
    let max = Math.max(...values);
    const inner = Math.max(1, plotW - 12); // room for interval caps at either end
    if (logScale) {
      min = Math.min(min, 0.5) * 0.9;
      max = Math.max(max, 1.5) * 1.1;
      const lmin = Math.log(min);
      const lmax = Math.log(max);
      const candidates = [0.2, 0.25, 0.33, 0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 3, 4].filter((t) => t >= min && t <= max);
      return { scale: (v: number) => 6 + ((Math.log(v) - lmin) / (lmax - lmin)) * inner, ticks: candidates };
    }
    const pad = (max - min || 1) * 0.06;
    min = min < reference ? min - pad : reference; // bars always start at the reference value (zero)
    max = max > reference ? max + pad : reference;
    const t = niceTicks(min, max, plotW < 220 ? 3 : plotW < 380 ? 4 : 6);
    const lo = Math.min(min, t[0] ?? min);
    const hi = Math.max(max, t[t.length - 1] ?? max);
    return { scale: (v: number) => 6 + ((v - lo) / (hi - lo || 1)) * inner, ticks: t };
  }, [rows, reference, extraLine, logScale, plotW, domain]);

  if (!rows.length) return null;
  if (units.length > 1) {
    return (
      <p role="note" data-testid="chart-refused" className="rounded border border-hairline bg-sunken px-3 py-2 text-[12.5px] text-ink-2">
        These rows are in different units ({units.map((u) => u || "unit not recorded").join("; ")}) and are not drawn on one axis. Use the table view to read them.
      </p>
    );
  }

  // A range across arms belongs to no single arm, so it carries no arm role.
  const groups = (Object.keys(GROUP) as ChartRow["group"][]).filter((g) => rows.some((r) => r.group === g && !(r.range && r.value == null)));
  const showRoles = groups.length > 1 || (groups.length === 1 && groups[0] !== "active");
  const hasDerived = rows.some((r) => r.intervalKind === "derived" && r.lo != null && r.hi != null);
  const hasReported = rows.some((r) => r.intervalKind !== "derived" && r.lo != null && r.hi != null);
  const hasRange = rows.some((r) => r.range);
  const hasCalculated = rows.some((r) => r.calculated);
  const tickSuffix = axisUnit.startsWith("%") ? "%" : "";
  const columns = narrow ? `${plotW}px ${valueW}px` : `${labelW}px ${plotW}px ${valueW}px`;
  const gridLines = (
    <>
      {ticks.map((t) => (
        <span key={t} aria-hidden className="absolute inset-y-0 w-px bg-[var(--grid)]" style={{ left: scale(t) }} />
      ))}
      <span aria-hidden className="absolute inset-y-0 w-px bg-[var(--hairline-strong)]" style={{ left: scale(reference) }} />
      {extraLine ? <span aria-hidden className="absolute inset-y-0 w-px bg-[var(--serious)]" style={{ left: scale(extraLine.value) }} /> : null}
    </>
  );

  return (
    <div ref={ref} className="min-w-0" data-chart-fill={fill}>
      {width === null ? (
        <div aria-hidden style={{ height: rows.length * 44 + 30 }} />
      ) : (
        <>
          <ul role="list" aria-label={label ?? `Chart of ${rows.length} rows; a table view is available.`} className="m-0 list-none p-0">
            {rows.map((r, index) => {
              const { color, name, outline } = GROUP[r.group];
              const tipId = `${uid}-details-${index}`;
              const patternId = `${uid}-hatch-${index}`.replace(/[^a-zA-Z0-9_-]/g, "");
              const isOpen = open === r.key;
              const x0 = scale(reference);
              const xv = r.value != null ? scale(r.value) : null;
              const cy = ROW_PLOT_H / 2;
              const barH = 12;
              const derived = r.intervalKind === "derived";
              const lines = r.meta ?? (r.sub ? [r.sub] : []);
              const note = noteText(r, unit);
              const isRange = !!r.range && r.value == null;
              const role = showRoles && !isRange ? (
                <span className="whitespace-nowrap text-[11.5px] font-normal text-ink-2" data-testid="chart-row-role">
                  <span aria-hidden className="mr-1 inline-block h-2 w-2 rounded-[2px]" style={{ background: color, outline: outline ? "1px solid var(--ink-3)" : undefined }} />
                  {name}
                </span>
              ) : null;
              const labelBlock = (
                <div className="relative min-w-0 py-1" data-testid="chart-row-label">
                  <p className="text-[12.5px] font-medium leading-snug text-ink">
                    {r.tip?.length ? (
                      <button
                        type="button"
                        aria-expanded={isOpen}
                        aria-controls={isOpen ? tipId : undefined}
                        onFocus={() => setOpen(r.key)}
                        onBlur={close}
                        onClick={() => {
                          if (isOpen && pinned) close();
                          else {
                            setOpen(r.key);
                            setPinned(true);
                          }
                        }}
                        className="max-w-full break-words text-left font-medium hover:text-accent"
                      >
                        {r.label}
                      </button>
                    ) : (
                      <span className="break-words">{r.label}</span>
                    )}
                    {role ? <> {role}</> : null}
                  </p>
                  {lines.map((line, i) => (
                    <p key={i} className="break-words text-[11.5px] leading-snug text-ink-2">{line}</p>
                  ))}
                  {isOpen && r.tip?.length ? (
                    <div id={tipId} data-testid="chart-row-details" className="absolute left-0 top-full z-20 w-max max-w-[min(340px,86vw)] rounded-md border border-hairline-strong bg-surface px-3 py-2 text-[12px] leading-snug shadow-lg">
                      {r.tip.filter(Boolean).map((line, i) => (
                        <p key={i} className="text-ink-2">{line}</p>
                      ))}
                      <p className="mt-1 text-[11px] text-ink-3">Press Esc to close.</p>
                    </div>
                  ) : null}
                </div>
              );
              const plot = (
                <div className="relative" style={{ minHeight: ROW_PLOT_H }}>
                  {gridLines}
                  <svg aria-hidden width={plotW} height={ROW_PLOT_H} className="relative block">
                    {fill === "hatched" ? (
                      <defs>
                        <pattern id={patternId} width={5} height={5} patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
                          <rect width={5} height={5} fill="var(--surface)" />
                          <rect width={2.5} height={5} fill={color} />
                        </pattern>
                      </defs>
                    ) : null}
                    {r.range ? (
                      <rect x={scale(r.range[0])} y={cy - 5} width={Math.max(2, scale(r.range[1]) - scale(r.range[0]))} height={10} rx={5} fill="var(--series-control)" fillOpacity={0.3} stroke="var(--ink-3)" strokeWidth={1} />
                    ) : null}
                    {xv != null && mark === "bar" ? (
                      <path
                        d={roundedBar(x0, xv, cy - barH / 2, barH, 3)}
                        fill={fill === "outline" ? "var(--surface)" : fill === "hatched" ? `url(#${patternId})` : color}
                        stroke={fill !== "solid" ? color : outline ? "var(--ink-3)" : "none"}
                        strokeWidth={fill !== "solid" ? 1.5 : 0.75}
                      />
                    ) : null}
                    {r.lo != null && r.hi != null ? (
                      derived ? (
                        <g stroke="var(--ink)" strokeWidth={1.3} fill="var(--surface)" data-interval="derived">
                          <line x1={scale(r.lo) + 3} x2={scale(r.hi) - 3} y1={cy} y2={cy} strokeDasharray="4 3" />
                          <circle cx={scale(r.lo)} cy={cy} r={3} />
                          <circle cx={scale(r.hi)} cy={cy} r={3} />
                        </g>
                      ) : (
                        <g stroke="var(--ink)" strokeWidth={1.6} fill="none" data-interval="reported">
                          <line x1={scale(r.lo)} x2={scale(r.hi)} y1={cy} y2={cy} />
                          <line x1={scale(r.lo)} x2={scale(r.lo)} y1={cy - 4.5} y2={cy + 4.5} />
                          <line x1={scale(r.hi)} x2={scale(r.hi)} y1={cy - 4.5} y2={cy + 4.5} />
                        </g>
                      )
                    ) : null}
                    {xv != null && mark === "dot" ? <circle cx={xv} cy={cy} r={5} fill={color} stroke={outline ? "var(--ink-3)" : "var(--surface)"} strokeWidth={outline ? 1 : 2} /> : null}
                  </svg>
                </div>
              );
              const valueBlock = (
                <div className="min-w-0 py-1" data-testid="chart-row-value">
                  <p className={`num break-words text-[12.5px] font-semibold leading-snug ${r.value != null || r.range ? "text-ink" : "font-normal text-ink-2"}`}>
                    {valueText(r, unit)}
                    {r.calculated ? <span className="font-normal" title="Calculated here from the reported counts"> †</span> : null}
                  </p>
                  {note ? <p className="num break-words text-[11.5px] leading-snug text-ink-2">{note}</p> : null}
                </div>
              );
              return (
                <li
                  key={r.key}
                  data-testid="chart-row"
                  data-row-key={r.key}
                  data-group={r.group}
                  data-calculated={r.calculated ? "true" : undefined}
                  onMouseEnter={() => {
                    if (!pinned) setOpen(r.key);
                  }}
                  onMouseLeave={() => {
                    if (!pinned) setOpen(null);
                  }}
                  className={`relative border-t border-hairline first:border-t-0 ${isOpen ? "z-10 bg-sunken" : ""}`}
                >
                  {narrow ? (
                    <>
                      {labelBlock}
                      <div className="grid items-stretch" style={{ gridTemplateColumns: columns, columnGap: GAP }}>
                        {plot}
                        {valueBlock}
                      </div>
                    </>
                  ) : (
                    <div className="grid items-stretch" style={{ gridTemplateColumns: columns, columnGap: GAP }}>
                      {labelBlock}
                      {plot}
                      {valueBlock}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
          <div aria-hidden className="grid border-t border-hairline-strong" style={{ gridTemplateColumns: columns, columnGap: GAP }}>
            {narrow ? null : <span />}
            <svg width={plotW} height={20} className="block overflow-visible">
              {ticks.map((t) => (
                <text key={t} x={scale(t)} y={14} textAnchor="middle" fontSize={11} fill="var(--ink-3)" className="num">
                  {fmt(t)}{tickSuffix}
                </text>
              ))}
            </svg>
            <span />
          </div>
          {axisLabel || axisUnit || extraLine ? (
            <p className="mt-0.5 text-[11.5px] leading-snug text-ink-2" style={{ paddingLeft: narrow ? 0 : labelW + GAP }} data-testid="chart-axis-title">
              {axisLabel ?? "Value"}
              {axisUnit ? ` (${axisUnit})` : ""}
              {referenceLabel ? ` · ${referenceLabel}` : ""}
              {extraLine ? ` · vertical line: ${extraLine.label}` : ""}
            </p>
          ) : null}
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11.5px] text-ink-2" data-testid="chart-legend">
            {showRoles
              ? groups.map((g) => (
                  <span key={g} className="inline-flex items-center gap-1.5">
                    <span aria-hidden className="inline-block h-2 w-2 rounded-[2px]" style={{ background: GROUP[g].color, outline: GROUP[g].outline ? "1px solid var(--ink-3)" : undefined }} /> {GROUP[g].name}
                  </span>
                ))
              : null}
            {hasReported ? (
              <span className="inline-flex items-center gap-1.5">
                <svg aria-hidden width={26} height={10}><g stroke="var(--ink)" strokeWidth={1.6}><line x1={1} x2={25} y1={5} y2={5} /><line x1={1} x2={1} y1={0.5} y2={9.5} /><line x1={25} x2={25} y1={0.5} y2={9.5} /></g></svg>
                Solid line, bar caps: interval reported by the source
              </span>
            ) : null}
            {hasDerived ? (
              <span className="inline-flex items-center gap-1.5">
                <svg aria-hidden width={26} height={10}><g stroke="var(--ink)" strokeWidth={1.3} fill="var(--surface)"><line x1={6} x2={20} y1={5} y2={5} strokeDasharray="4 3" /><circle cx={4} cy={5} r={3} /><circle cx={22} cy={5} r={3} /></g></svg>
                Dashed line, open round caps: interval derived here from the reported SE (an inference, not reported by the source)
              </span>
            ) : null}
            {hasRange ? (
              <span className="inline-flex items-center gap-1.5">
                <svg aria-hidden width={26} height={10}><rect x={1} y={1} width={24} height={8} rx={4} fill="var(--series-control)" fillOpacity={0.3} stroke="var(--ink-3)" /></svg>
                Rounded band: range across arms (no arm-level value disclosed)
              </span>
            ) : null}
            {hasCalculated ? <span>† Calculated here from the reported counts; not a percentage reported by the source</span> : null}
          </div>
        </>
      )}
    </div>
  );
}

/** Bar from the baseline with a rounded data-end and a square baseline end. */
function roundedBar(x0: number, x1: number, y: number, h: number, r: number): string {
  const w = Math.abs(x1 - x0);
  const rr = Math.min(r, w);
  if (x1 >= x0) return `M${x0},${y} H${x1 - rr} Q${x1},${y} ${x1},${y + rr} V${y + h - rr} Q${x1},${y + h} ${x1 - rr},${y + h} H${x0} Z`;
  return `M${x0},${y} H${x1 + rr} Q${x1},${y} ${x1},${y + rr} V${y + h - rr} Q${x1},${y + h} ${x1 + rr},${y + h} H${x0} Z`;
}

export type Facet = {
  key: string;
  /** Says exactly what this panel plots. It must be true of every row in the panel. */
  caption: ReactNode;
  rows: ChartRow[];
  unit?: string;
  axisLabel?: string;
  /** Panels share an axis scale only when this matches (default: the unit). Give endpoint and unit when endpoints can differ. */
  scale?: string;
  footnote?: ReactNode;
};

/**
 * Several panels, one per coherent set of rows. Rows that differ in estimand, evidence tier, source, time point,
 * baseline or unit are never drawn against each other: each set has its own panel, caption and axis. Panels with
 * the same `scale` use the same axis range, so bar lengths can be read across them; others are independent.
 */
export function FacetedChart({ facets, mark = "bar", fill = "solid", note, divider }: { facets: Facet[]; mark?: "bar" | "dot"; fill?: BarFill; note?: ReactNode; divider?: ReactNode }) {
  const domains = useMemo(() => {
    const byScale = new Map<string, [number, number]>();
    for (const facet of facets) {
      const values = facet.rows.flatMap((r) => [r.value, r.lo, r.hi, ...(r.range ?? [])]).filter((v): v is number => v != null && Number.isFinite(v));
      if (!values.length) continue;
      const key = facet.scale ?? facet.unit ?? "";
      const current = byScale.get(key);
      byScale.set(key, [Math.min(current?.[0] ?? Infinity, ...values), Math.max(current?.[1] ?? -Infinity, ...values)]);
    }
    return byScale;
  }, [facets]);
  const scales = new Set(facets.map((f) => f.scale ?? f.unit ?? ""));
  return (
    <div className="min-w-0" data-testid="faceted-chart" data-facets={facets.length}>
      {facets.length > 1 ? (
        <p className="mb-2 text-[12px] leading-snug text-ink-2" data-testid="facet-note">
          {note ?? `Shown as ${facets.length} separate panels: rows in different panels differ in estimand, evidence tier, source, time point, baseline or unit, and are not comparable with one another.`}
          {scales.size < facets.length ? " Panels in the same unit use the same axis range." : ""}
          {scales.size > 1 ? " Panels in different units have their own axes." : ""}
        </p>
      ) : null}
      {facets.map((facet, index) => (
        <div key={facet.key}>
          {index > 0 && divider ? <div data-testid="facet-divider">{divider}</div> : null}
          <section data-testid="chart-facet" className={`rounded-md border border-hairline px-3 pb-2 pt-2 ${index > 0 && !divider ? "mt-3" : ""}`}>
            <div className="mb-1.5 text-[12px] leading-snug text-ink-2" data-testid="facet-caption">{facet.caption}</div>
            <IntervalChart rows={facet.rows} unit={facet.unit} axisLabel={facet.axisLabel} mark={mark} fill={fill} domain={domains.get(facet.scale ?? facet.unit ?? "")} />
            {facet.footnote ? <div className="mt-1.5 border-t border-hairline pt-1.5 text-[12px] leading-snug text-ink-3">{facet.footnote}</div> : null}
          </section>
        </div>
      ))}
    </div>
  );
}

export function ChartTable({ rows, unit = "", valueHeader = "Value" }: { rows: ChartRow[]; unit?: string; valueHeader?: string }) {
  const hasCalculated = rows.some((r) => r.calculated);
  return (
    <>
      <table className="tbl">
        <thead>
          <tr>
            <th>Row</th>
            <th className="r">{valueHeader}</th>
            <th className="r">Interval</th>
            <th>Value and interval basis</th>
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td>
                {r.href ? <Link href={r.href}>{r.label}</Link> : r.label}
                {(r.meta ?? (r.sub ? [r.sub] : [])).map((line, i) => (
                  <span key={i} className="block text-[11.5px] text-ink-3">{line}</span>
                ))}
                {r.range && r.value == null ? null : <span className="block text-[11.5px] text-ink-3">{GROUP[r.group].name}</span>}
              </td>
              <td className="r num whitespace-nowrap">{valueText(r, unit)}{r.calculated ? " †" : ""}</td>
              <td className="r num whitespace-nowrap">{intervalText(r, unit) ?? "not reported"}</td>
              <td>
                {r.calculated ? "Value calculated here from reported counts. " : r.value != null ? "Value as reported. " : r.range ? "Range across arms as reported. " : ""}
                {r.lo != null && r.hi != null ? (r.intervalKind === "derived" ? "Interval derived here from the reported SE (inference)." : "Interval reported by the source.") : "No interval."}
              </td>
              <td className="text-ink-3">{[r.valueNote, ...(r.tip ?? [])].filter(Boolean).join(" · ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {hasCalculated ? <p className="px-2.5 py-1.5 text-[12px] text-ink-3">† Calculated here from the reported counts; not a percentage reported by the source.</p> : null}
    </>
  );
}

/** Report counts over time. The y-axis is a count of reports — never a rate. */
export function ReportsTrend({ data }: { data: { period: string; count: number }[] }) {
  const first = data[0];
  const last = data[data.length - 1];
  // The plot is one image for assistive technology and is not a tab stop; the table view carries every value.
  return (
    <div className="h-[230px] w-full" role="img" aria-label={first && last ? `Line chart of reports received per quarter, ${first.period} to ${last.period} (${data.length} quarters). Use the table view for the counts.` : "Line chart of reports received per quarter."}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 4 }} accessibilityLayer={false}>
          <CartesianGrid stroke="var(--grid)" vertical={false} />
          <XAxis dataKey="period" tick={{ fontSize: 11, fill: "var(--ink-3)" }} tickLine={false} axisLine={{ stroke: "var(--hairline-strong)" }} interval="preserveStartEnd" minTickGap={28} />
          <YAxis tick={{ fontSize: 11, fill: "var(--ink-3)" }} tickLine={false} axisLine={false} width={52} tickFormatter={(v: number) => v.toLocaleString()} />
          <RTooltip
            cursor={{ stroke: "var(--hairline-strong)" }}
            contentStyle={{ borderRadius: 6, border: "1px solid var(--hairline-strong)", fontSize: 12, padding: "6px 10px" }}
            formatter={(value) => [`${Number(value).toLocaleString()} reports received`, ""]}
            separator=""
          />
          <Line type="monotone" dataKey="count" stroke="var(--series-1)" strokeWidth={2} dot={false} activeDot={{ r: 4, stroke: "var(--surface)", strokeWidth: 2 }} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Thin inline bar used inside tables (e.g. priority components). The breakdown is text for assistive technology, not hover-only. */
export function MicroBar({ value, label }: { value: number; label?: string }) {
  return (
    <span className="inline-flex items-center gap-2" title={label}>
      <span aria-hidden className="relative inline-block h-1.5 w-16 overflow-hidden rounded-full bg-[var(--grid)]">
        <span className="absolute inset-y-0 left-0 rounded-full bg-[var(--series-1)]" style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} />
      </span>
      <span className="num text-[12px] text-ink-2">{fmt(value, 2)}</span>
      {label ? <span className="sr-only"> ({label})</span> : null}
    </span>
  );
}

/* ---------- reported numbers in tables and cards ---------- */

/**
 * A reported number with its unit and, where the source gives one, its interval and confidence level.
 * The value is printed as reported (never rounded), the unit is always beside it, and the interval is
 * separated from the value so "−10.79" and "95% CI" cannot be read as "−10.795%".
 */
export function Reported({ text, unit, lo, hi, level, missingUnit = "unit not recorded" }: { text: string; unit?: string | null; lo?: number | null; hi?: number | null; level?: number | null; missingUnit?: string | null }) {
  const hasUnit = !!(unit ?? "").trim();
  return (
    <span className="num">
      <span className="whitespace-nowrap font-semibold text-ink">{withUnit(text, unit)}</span>
      {!hasUnit && missingUnit ? <span className="text-[11.5px] text-ink-3"> ({missingUnit})</span> : null}
      {lo != null && hi != null ? (
        <>
          {" "}
          <span className={`text-[12px] text-ink-2 ${level == null || (level > 0 && level < 100) ? "whitespace-nowrap" : ""}`}>
            {ciLabel(level)} {fmt(lo)} to {withUnit(fmt(hi), unit)}
          </span>
        </>
      ) : null}
    </span>
  );
}
