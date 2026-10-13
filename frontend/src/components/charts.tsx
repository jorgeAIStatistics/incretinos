"use client";

import { Table2 } from "lucide-react";
import Link from "next/link";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis } from "recharts";
import { fmt } from "@/lib/api";

export type ChartRow = {
  key: string;
  label: string;
  sub?: string;
  value: number | null;
  lo?: number | null;
  hi?: number | null;
  intervalKind?: "reported" | "derived";
  range?: [number, number] | null; // a disclosed range across arms (no point value)
  group: "active" | "control" | "comparator";
  tip?: string[];
  href?: string;
};

const GROUP = {
  active: { color: "var(--series-1)", name: "Investigational arm" },
  comparator: { color: "var(--series-2)", name: "Active comparator" },
  control: { color: "var(--series-control)", name: "Placebo" },
} as const;

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T | null>(null);
  const [width, setWidth] = useState(720);
  useEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver((entries) => setWidth(Math.max(280, Math.floor(entries[0].contentRect.width))));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
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

export function ChartFrame({ title, subtitle, footnote, children, table }: { title: string; subtitle?: ReactNode; footnote?: ReactNode; children: ReactNode; table?: ReactNode }) {
  const [showTable, setShowTable] = useState(false);
  return (
    <figure className="rounded-lg border border-hairline bg-surface">
      <figcaption className="flex flex-wrap items-start justify-between gap-3 px-4 pt-3.5">
        <div className="min-w-0">
          <p className="text-[14.5px] font-semibold text-ink">{title}</p>
          {subtitle ? <p className="mt-0.5 max-w-3xl text-[12.5px] leading-snug text-ink-3">{subtitle}</p> : null}
        </div>
        {table ? (
          <button
            type="button"
            onClick={() => setShowTable((s) => !s)}
            aria-pressed={showTable}
            className="inline-flex h-7 items-center gap-1.5 rounded border border-hairline-strong px-2 text-[12px] font-medium text-ink-2 hover:bg-sunken"
          >
            <Table2 aria-hidden size={13} /> {showTable ? "Show chart" : "View as table"}
          </button>
        ) : null}
      </figcaption>
      <div className="px-4 pb-2 pt-2">{showTable && table ? <div className="overflow-x-auto">{table}</div> : children}</div>
      {footnote ? <div className="border-t border-hairline px-4 py-2.5 text-[12px] leading-snug text-ink-3">{footnote}</div> : null}
    </figure>
  );
}

/**
 * Horizontal value chart: one row per arm (or analysis), a thin bar or dot for the value,
 * and a whisker for the interval. Reported intervals are solid; intervals derived here from a
 * reported standard error are hollow-capped and lighter, and are named as such in the legend.
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
}: {
  rows: ChartRow[];
  unit?: string;
  mark?: "bar" | "dot";
  reference?: number;
  referenceLabel?: string;
  extraLine?: { value: number; label: string };
  logScale?: boolean;
  axisLabel?: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<string | null>(null);
  const narrow = width < 560;
  const gutter = narrow ? 18 : Math.min(250, Math.max(150, width * 0.3));
  const right = 64;
  const rowH = narrow ? 48 : 34;
  const top = 8;
  const plotW = Math.max(120, width - gutter - right);
  const height = top + rows.length * rowH + 30;

  const { scale, ticks } = useMemo(() => {
    const values = rows.flatMap((r) => [r.value, r.lo, r.hi, ...(r.range ?? [])]).filter((v): v is number => v != null && Number.isFinite(v));
    values.push(reference);
    if (extraLine) values.push(extraLine.value);
    let min = Math.min(...values);
    let max = Math.max(...values);
    if (logScale) {
      min = Math.min(min, 0.5) * 0.9;
      max = Math.max(max, 1.5) * 1.1;
      const lmin = Math.log(min);
      const lmax = Math.log(max);
      const candidates = [0.2, 0.25, 0.33, 0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 3, 4].filter((t) => t >= min && t <= max);
      return { scale: (v: number) => ((Math.log(v) - lmin) / (lmax - lmin)) * plotW, ticks: candidates };
    }
    const pad = (max - min || 1) * 0.08;
    min = min < reference ? min - pad : reference;
    max = max > reference ? max + pad : reference;
    const t = niceTicks(min, max, narrow ? 4 : 6);
    const lo = Math.min(min, t[0] ?? min);
    const hi = Math.max(max, t[t.length - 1] ?? max);
    return { scale: (v: number) => ((v - lo) / (hi - lo || 1)) * plotW, ticks: t };
  }, [rows, reference, extraLine, logScale, plotW, narrow]);

  const groups = Array.from(new Set(rows.map((r) => r.group)));
  const hasDerived = rows.some((r) => r.intervalKind === "derived" && r.lo != null);
  const hasReported = rows.some((r) => r.intervalKind === "reported" && r.lo != null);
  const hasRange = rows.some((r) => r.range);
  const active = rows.find((r) => r.key === hover);
  const activeIndex = active ? rows.indexOf(active) : -1;

  return (
    <div ref={ref} className="relative">
      <svg width={width} height={height} role="img" aria-label={`Chart of ${rows.length} rows; a table view is available.`} className="block">
        <g transform={`translate(${gutter},${top})`}>
          {ticks.map((t) => (
            <g key={t} transform={`translate(${scale(t)},0)`}>
              <line y2={rows.length * rowH} stroke="var(--grid)" strokeWidth={1} />
              <text y={rows.length * rowH + 15} textAnchor="middle" fontSize={11} fill="var(--ink-3)" className="num">
                {fmt(t)}{unit === "%" ? "%" : ""}
              </text>
            </g>
          ))}
          <line x1={scale(reference)} x2={scale(reference)} y2={rows.length * rowH} stroke="var(--hairline-strong)" strokeWidth={1} />
          {extraLine ? (
            <g transform={`translate(${scale(extraLine.value)},0)`}>
              <line y2={rows.length * rowH} stroke="var(--serious)" strokeWidth={1} />
              <text y={-0} dy={-1} textAnchor="middle" fontSize={10.5} fill="var(--ink-2)" transform={`translate(0,${rows.length * rowH + 27})`}>
                {extraLine.label}
              </text>
            </g>
          ) : null}
          {rows.map((r, i) => {
            const cy = i * rowH + (narrow ? rowH - 15 : rowH / 2);
            const color = GROUP[r.group].color;
            const x0 = scale(reference);
            const xv = r.value != null ? scale(r.value) : null;
            const dim = hover && hover !== r.key ? 0.45 : 1;
            const barH = 14;
            const tipX = Math.max(xv ?? x0, r.hi != null ? scale(r.hi) : -Infinity, r.range ? scale(r.range[1]) : -Infinity);
            return (
              <g key={r.key} opacity={dim} style={{ transition: "opacity 120ms" }}>
                {narrow ? (
                  <text x={-gutter} y={i * rowH + 14} fontSize={12} fill="var(--ink)" fontWeight={500}>
                    {r.label}
                  </text>
                ) : (
                  <>
                    <text x={-12} y={cy + (r.sub ? -2 : 4)} textAnchor="end" fontSize={12.5} fill="var(--ink)" fontWeight={500}>
                      {r.label.length > 34 ? `${r.label.slice(0, 33)}…` : r.label}
                    </text>
                    {r.sub ? (
                      <text x={-12} y={cy + 11} textAnchor="end" fontSize={10.5} fill="var(--ink-3)">
                        {r.sub}
                      </text>
                    ) : null}
                  </>
                )}
                {r.range ? <rect x={scale(r.range[0])} y={cy - 5} width={Math.max(2, scale(r.range[1]) - scale(r.range[0]))} height={10} rx={5} fill={color} opacity={0.28} /> : null}
                {xv != null && mark === "bar" ? (
                  <path d={roundedBar(x0, xv, cy - barH / 2, barH, 4)} fill={color} />
                ) : null}
                {r.lo != null && r.hi != null ? (
                  <g stroke={r.intervalKind === "derived" ? "var(--ink-3)" : "var(--ink)"} strokeWidth={r.intervalKind === "derived" ? 1 : 1.6} fill="none">
                    <line x1={scale(r.lo)} x2={scale(r.hi)} y1={cy} y2={cy} />
                    <line x1={scale(r.lo)} x2={scale(r.lo)} y1={cy - 4} y2={cy + 4} />
                    <line x1={scale(r.hi)} x2={scale(r.hi)} y1={cy - 4} y2={cy + 4} />
                  </g>
                ) : null}
                {xv != null && mark === "dot" ? <circle cx={xv} cy={cy} r={5} fill={color} stroke="var(--surface)" strokeWidth={2} /> : null}
                {r.value != null || r.range ? (
                  <text x={Math.min(plotW + right - 4, Math.max(tipX, x0) + 8)} y={cy + 4} fontSize={12} fill="var(--ink)" className="num" fontWeight={600}>
                    {r.value != null ? `${fmt(r.value)}${unit === "%" ? "%" : ""}` : `${fmt(r.range![0])}–${fmt(r.range![1])}${unit === "%" ? "%" : ""}`}
                  </text>
                ) : (
                  <text x={x0 + 8} y={cy + 4} fontSize={11.5} fill="var(--ink-3)">
                    not reported
                  </text>
                )}
                <rect
                  x={-gutter}
                  y={i * rowH}
                  width={width}
                  height={rowH}
                  fill="transparent"
                  tabIndex={0}
                  aria-label={`${r.label}: ${r.value != null ? fmt(r.value) + unit : "not reported"}`}
                  onMouseEnter={() => setHover(r.key)}
                  onMouseLeave={() => setHover(null)}
                  onFocus={() => setHover(r.key)}
                  onBlur={() => setHover(null)}
                />
              </g>
            );
          })}
        </g>
      </svg>
      {axisLabel ? <p className="-mt-1 text-center text-[11.5px] text-ink-3" style={{ paddingLeft: gutter }}>{axisLabel}{referenceLabel ? ` · ${referenceLabel}` : ""}</p> : null}
      {active ? (
        <div
          className="pointer-events-none absolute z-10 max-w-[330px] rounded-md border border-hairline-strong bg-surface px-3 py-2 text-[12px] leading-snug shadow-lg"
          style={{ left: Math.min(width - 340, gutter + 12), top: top + (activeIndex + 1) * rowH + 4 }}
        >
          <p className="font-semibold text-ink">{active.label}</p>
          {(active.tip ?? []).map((line) => (
            <p key={line} className="text-ink-2">{line}</p>
          ))}
        </div>
      ) : null}
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11.5px] text-ink-2">
        {groups.length > 1
          ? groups.map((g) => (
              <span key={g} className="inline-flex items-center gap-1.5">
                <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: GROUP[g].color }} /> {GROUP[g].name}
              </span>
            ))
          : null}
        {hasReported ? (
          <span className="inline-flex items-center gap-1.5">
            <svg aria-hidden width={22} height={10}><g stroke="var(--ink)" strokeWidth={1.6}><line x1={1} x2={21} y1={5} y2={5} /><line x1={1} x2={1} y1={1} y2={9} /><line x1={21} x2={21} y1={1} y2={9} /></g></svg>
            Interval reported by the source
          </span>
        ) : null}
        {hasDerived ? (
          <span className="inline-flex items-center gap-1.5">
            <svg aria-hidden width={22} height={10}><g stroke="var(--ink-3)" strokeWidth={1}><line x1={1} x2={21} y1={5} y2={5} /><line x1={1} x2={1} y1={1} y2={9} /><line x1={21} x2={21} y1={1} y2={9} /></g></svg>
            95% interval derived here from the reported SE (inference)
          </span>
        ) : null}
        {hasRange ? (
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden className="inline-block h-2 w-5 rounded-full bg-[var(--series-1)] opacity-30" /> Range across arms (no arm-level value disclosed)
          </span>
        ) : null}
      </div>
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

export function ChartTable({ rows, unit, valueHeader = "Value" }: { rows: ChartRow[]; unit?: string; valueHeader?: string }) {
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>Row</th>
          <th className="r">{valueHeader}</th>
          <th className="r">Interval</th>
          <th>Interval basis</th>
          <th>Detail</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key}>
            <td>{r.href ? <Link href={r.href}>{r.label}</Link> : r.label}{r.sub ? <span className="block text-[11.5px] text-ink-3">{r.sub}</span> : null}</td>
            <td className="r num">{r.value != null ? `${fmt(r.value)}${unit === "%" ? "%" : ""}` : r.range ? `${fmt(r.range[0])} to ${fmt(r.range[1])}${unit === "%" ? "%" : ""}` : "not reported"}</td>
            <td className="r num">{r.lo != null && r.hi != null ? `${fmt(r.lo)} to ${fmt(r.hi)}` : "not reported"}</td>
            <td>{r.lo != null ? (r.intervalKind === "derived" ? "Derived from reported SE (inference)" : "Reported by source") : r.range ? "Range across arms" : "—"}</td>
            <td className="text-ink-3">{(r.tip ?? []).join(" · ")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Report counts over time. The y-axis is a count of reports — never a rate. */
export function ReportsTrend({ data }: { data: { period: string; count: number }[] }) {
  return (
    <div className="h-[230px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 4 }}>
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

/** Thin inline bar used inside tables (e.g. priority components). */
export function MicroBar({ value, label }: { value: number; label?: string }) {
  return (
    <span className="inline-flex items-center gap-2" title={label}>
      <span className="relative inline-block h-1.5 w-16 overflow-hidden rounded-full bg-[var(--grid)]">
        <span className="absolute inset-y-0 left-0 rounded-full bg-[var(--series-1)]" style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} />
      </span>
      <span className="num text-[12px] text-ink-2">{fmt(value, 2)}</span>
    </span>
  );
}
