"use client";

import clsx from "clsx";
import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, ChevronsUpDown } from "lucide-react";
import { Fragment, type ReactNode, useId, useMemo, useState } from "react";
import { Button } from "./button";

/*
 * Tables. Three rules hold for every table in the product:
 *   1. Sideways scrolling happens inside the table's own container, never on the page.
 *   2. The header row stays visible while the body scrolls (the container is height-bounded so `sticky` works).
 *   3. A list is never cut short silently: the footer states "x–y of total" and offers the remaining pages.
 */

export type Density = "comfortable" | "compact";

/**
 * Frame for a `<table className="tbl">`: border, bounded height, internal scrolling in both directions, and
 * keyboard access to the scroll area. `label` names the region for assistive technology.
 */
export function TableCard({
  label,
  children,
  toolbar,
  footer,
  maxHeight = "min(72vh, 760px)",
  className,
  refreshing,
}: {
  label: string;
  children: ReactNode;
  /** Row above the table: counts, density switch, column controls. */
  toolbar?: ReactNode;
  /** Row below the table: normally <Pagination>. */
  footer?: ReactNode;
  /** CSS max-height of the scroll area, or "none" to let the table grow with the page (the header then scrolls away). */
  maxHeight?: string;
  className?: string;
  refreshing?: boolean;
}) {
  return (
    <div className={clsx("overflow-hidden rounded-panel border border-hairline bg-surface", className)}>
      {toolbar ? <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-hairline px-3 py-2 text-data text-ink-2">{toolbar}</div> : null}
      {/* Focusable so a keyboard user can scroll it; it is a named region, so the focus stop is announced. */}
      {/* oxlint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <div role="region" aria-label={label} tabIndex={0} aria-busy={refreshing || undefined} data-refreshing={refreshing ? "true" : "false"} className="relative scroll-pt-10 overflow-auto focus-visible:outline-offset-[-2px]" style={{ maxHeight: maxHeight === "none" ? undefined : maxHeight }}>
        {children}
      </div>
      {footer ? <div className="border-t border-hairline px-3 py-2">{footer}</div> : null}
    </div>
  );
}

/* ---------- sorting ---------- */

export type SortDirection = "ascending" | "descending";
export type SortState<K extends string> = { key: K; direction: SortDirection } | null;

/** Header cell that sorts its column. Exposes the state with `aria-sort` and is operated by a real button. */
export function SortHeader<K extends string>({ column, sort, onSort, children, align, className }: { column: K; sort: SortState<K>; onSort: (next: SortState<K>) => void; children: ReactNode; align?: "right"; className?: string }) {
  const active = sort?.key === column ? sort.direction : null;
  const next = (): SortState<K> => (active === null ? { key: column, direction: "ascending" } : active === "ascending" ? { key: column, direction: "descending" } : null);
  const Icon = active === "ascending" ? ArrowUp : active === "descending" ? ArrowDown : ChevronsUpDown;
  return (
    <th scope="col" aria-sort={active ?? "none"} className={clsx(align === "right" && "r", className)}>
      <button type="button" onClick={() => onSort(next())} className={clsx("-mx-1 -my-1 inline-flex min-h-6 items-center gap-1 rounded-chip px-1 font-semibold transition-colors duration-(--dur-fast) hover:text-ink", active ? "text-ink" : "text-ink-2", align === "right" && "flex-row-reverse")}>
        {children}
        <Icon aria-hidden size={12} className={clsx("shrink-0", active ? "text-accent" : "text-ink-3")} />
      </button>
    </th>
  );
}

type SortValue = string | number | boolean | null | undefined;

/** Stable sort by one column. Missing values always sort last, in both directions: they are not small numbers. */
export function sortRows<T, K extends string>(rows: T[], sort: SortState<K>, accessors: Partial<Record<K, (row: T) => SortValue>>): T[] {
  const accessor = sort ? accessors[sort.key] : undefined;
  if (!sort || !accessor) return rows;
  const sign = sort.direction === "ascending" ? 1 : -1;
  return rows
    .map((row, index) => ({ row, index, value: accessor(row) }))
    .sort((a, b) => {
      const am = a.value === null || a.value === undefined || a.value === "";
      const bm = b.value === null || b.value === undefined || b.value === "";
      if (am || bm) return am === bm ? a.index - b.index : am ? 1 : -1;
      const cmp = typeof a.value === "number" && typeof b.value === "number" ? a.value - b.value : String(a.value).localeCompare(String(b.value), undefined, { numeric: true, sensitivity: "base" });
      return cmp !== 0 ? cmp * sign : a.index - b.index;
    })
    .map((x) => x.row);
}

/* ---------- data table ---------- */

export type Column<T, K extends string = string> = {
  key: K;
  header: ReactNode;
  cell: (row: T, index: number) => ReactNode;
  /** Makes the column sortable. Return null for a missing value. */
  sortValue?: (row: T) => SortValue;
  align?: "right";
  /** Classes for the body cell (width limits, wrapping). */
  className?: string;
  headerClassName?: string;
  /** Marks the cell that identifies the row: rendered as a row header (`th scope="row"`). */
  rowHeader?: boolean;
};

/**
 * A table built from column definitions: caption, sticky header, sortable columns (`aria-sort`), density,
 * selected and expanded rows, and a footer slot. Rows are whatever page the caller passes; pair it with
 * `usePagedList` or `useClientPaging` and a <Pagination> footer.
 */
export function DataTable<T, K extends string = string>({
  label,
  caption,
  columns,
  rows,
  rowKey,
  sort,
  onSort,
  density = "comfortable",
  toolbar,
  footer,
  maxHeight,
  empty,
  selectedKey,
  expandedKey,
  renderExpanded,
  refreshing,
  minWidth,
  pinFirstColumn,
  className,
}: {
  /** Names the table for assistive technology. Shown as a visible caption when `caption` is not given. */
  label: string;
  /** Visible caption above the header row. */
  caption?: ReactNode;
  columns: Column<T, K>[];
  rows: T[];
  rowKey: (row: T) => string | number;
  /** Controlled sort. Omit both to make the table unsortable; or let the table own the state with `useSort`. */
  sort?: SortState<K>;
  onSort?: (next: SortState<K>) => void;
  density?: Density;
  toolbar?: ReactNode;
  footer?: ReactNode;
  maxHeight?: string;
  /** Shown in place of the body when `rows` is empty. Loading and errors are the caller's to handle before this. */
  empty?: ReactNode;
  selectedKey?: string | number | null;
  expandedKey?: string | number | null;
  renderExpanded?: (row: T) => ReactNode;
  refreshing?: boolean;
  /** Minimum table width in px before it scrolls sideways inside its container. */
  minWidth?: number;
  /** Keep the first column in view while the table scrolls sideways. Use it when that column names the row. */
  pinFirstColumn?: boolean;
  className?: string;
}) {
  const captionId = useId();
  return (
    <TableCard label={label} toolbar={toolbar} footer={footer} maxHeight={maxHeight} refreshing={refreshing} className={className}>
      <table className={clsx("tbl", density === "compact" && "tbl-compact", pinFirstColumn && "tbl-pin-first")} aria-labelledby={captionId} style={minWidth ? { minWidth } : undefined}>
        <caption id={captionId} className={caption ? "border-b border-hairline px-3 py-2 text-data text-ink-2" : "sr-only"}>
          {caption ?? label}
        </caption>
        <thead>
          <tr>
            {columns.map((c) =>
              c.sortValue && onSort ? (
                <SortHeader key={c.key} column={c.key} sort={sort ?? null} onSort={onSort} align={c.align} className={c.headerClassName}>
                  {c.header}
                </SortHeader>
              ) : (
                <th key={c.key} scope="col" className={clsx(c.align === "right" && "r", c.headerClassName)}>
                  {c.header}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="!border-b-0 !p-0">
                {empty ?? <p className="px-3 py-5 text-ink-3">No rows match.</p>}
              </td>
            </tr>
          ) : (
            rows.map((row, index) => {
              const key = rowKey(row);
              const expanded = expandedKey !== undefined && expandedKey !== null && expandedKey === key;
              return (
                <Fragment key={key}>
                  <tr data-selected={selectedKey === key ? "true" : undefined} data-expanded={expanded ? "true" : undefined}>
                    {columns.map((c) =>
                      c.rowHeader ? (
                        <th key={c.key} scope="row" className={c.className}>
                          {c.cell(row, index)}
                        </th>
                      ) : (
                        <td key={c.key} className={clsx(c.align === "right" && "r num", c.className)}>
                          {c.cell(row, index)}
                        </td>
                      ),
                    )}
                  </tr>
                  {expanded && renderExpanded ? (
                    <tr className="!bg-sunken">
                      <td colSpan={columns.length} className="!p-0">
                        <div className="disclosure-content px-3 py-4">{renderExpanded(row)}</div>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })
          )}
        </tbody>
      </table>
    </TableCard>
  );
}

/** Sort state plus the sorted rows, for tables whose rows are all in memory. */
export function useSort<T, K extends string>(rows: T[], columns: Column<T, K>[], initial: SortState<K> = null) {
  const [sort, setSort] = useState<SortState<K>>(initial);
  const sorted = useMemo(() => {
    const accessors = Object.fromEntries(columns.filter((c) => c.sortValue).map((c) => [c.key, c.sortValue])) as Partial<Record<K, (row: T) => SortValue>>;
    return sortRows(rows, sort, accessors);
    // Column definitions are rebuilt on each render; only their keys matter here.
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, sort]);
  return { sort, setSort, sorted };
}

/* ---------- pagination ---------- */

const numberFormat = new Intl.NumberFormat("en-US");

/**
 * States exactly which rows are on screen out of how many exist, and moves between pages.
 * `total` is the full number of matching rows; `unfiltered`, when larger, is mentioned so a filtered count is
 * never mistaken for the size of the whole set; `truncated` says the service returned fewer rows than it reports.
 */
export function Pagination({
  page,
  pageSize,
  total,
  onPage,
  onPageSize,
  pageSizes = [25, 50, 100],
  noun = "rows",
  unfiltered,
  truncated,
  reachable,
}: {
  /** Zero-based. */
  page: number;
  pageSize: number;
  total: number;
  onPage: (page: number) => void;
  onPageSize?: (size: number) => void;
  pageSizes?: number[];
  /** Plural name of what is listed: "trials", "sources". */
  noun?: string;
  unfiltered?: number;
  truncated?: boolean;
  reachable?: number;
}) {
  const reach = reachable ?? total;
  const pageCount = Math.max(1, Math.ceil(reach / pageSize));
  const from = reach === 0 ? 0 : page * pageSize + 1;
  const to = Math.min(reach, (page + 1) * pageSize);
  const sizeId = useId();
  return (
    <nav aria-label={`Pages of ${noun}`} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-data text-ink-2">
      <p role="status" aria-live="polite" className="num min-w-0">
        {total === 0 ? (
          <>No {noun}</>
        ) : (
          <>
            <span className="font-semibold text-ink">
              {numberFormat.format(from)}–{numberFormat.format(to)}
            </span>{" "}
            of <span className="font-semibold text-ink">{numberFormat.format(total)}</span> {noun}
          </>
        )}
        {unfiltered !== undefined && unfiltered > total ? <span className="text-ink-3"> (filtered from {numberFormat.format(unfiltered)})</span> : null}
        {truncated ? <span className="font-medium text-warn"> Only the first {numberFormat.format(reach)} can be shown: the service did not return the rest.</span> : null}
      </p>
      <div className="flex items-center gap-2">
        {onPageSize ? (
          <label htmlFor={sizeId} className="flex items-center gap-1.5 text-caption text-ink-3">
            Per page
            <select id={sizeId} value={pageSize} onChange={(e) => onPageSize(Number(e.target.value))} className="h-(--control-h-sm) cursor-pointer rounded-control border border-control bg-surface px-1.5 text-data text-ink">
              {pageSizes.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <span className="num whitespace-nowrap text-caption text-ink-3">
          Page {numberFormat.format(page + 1)} of {numberFormat.format(pageCount)}
        </span>
        <Button variant="secondary" size="sm" Icon={ChevronLeft} onClick={() => onPage(page - 1)} disabled={page <= 0} aria-label={`Previous page of ${noun}`}>
          Previous
        </Button>
        <Button variant="secondary" size="sm" onClick={() => onPage(page + 1)} disabled={page >= pageCount - 1} aria-label={`Next page of ${noun}`}>
          Next
          <ChevronRight aria-hidden size={13} />
        </Button>
      </div>
    </nav>
  );
}
