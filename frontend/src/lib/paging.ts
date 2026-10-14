"use client";

import { useCallback, useRef, useState } from "react";
import { type ApiState, apiGet, useResource, withQuery } from "@/lib/api";

/**
 * List endpoints answer in one of three shapes while the API moves to explicit paging:
 *   1. a bare array                                  (old: /studies, /drugs, /changes, /audit)
 *   2. { total, rows, ...extra }                     (old: `total` may be the truncated count)
 *   3. { total, limit, offset, rows, ...extra }      (new: `total` is the full match count)
 * Everything that reads a list goes through `readList`, so a page works with any of them.
 */
export type ListEnvelope<T> = {
  rows: T[];
  /** The count the service reported, or null when it sent a bare array. */
  total: number | null;
  /** Present only in the new shape; this is how server-side paging is recognised. */
  offset: number | null;
  limit: number | null;
  /** Every other field of the envelope (labels, notes, counters). */
  extra: Record<string, unknown>;
};

export function readList<T>(payload: unknown): ListEnvelope<T> {
  if (Array.isArray(payload)) return { rows: payload as T[], total: null, offset: null, limit: null, extra: {} };
  if (payload && typeof payload === "object") {
    const { rows, total, offset, limit, ...extra } = payload as Record<string, unknown>;
    return {
      rows: Array.isArray(rows) ? (rows as T[]) : [],
      total: typeof total === "number" ? total : null,
      offset: typeof offset === "number" ? offset : null,
      limit: typeof limit === "number" ? limit : null,
      extra,
    };
  }
  return { rows: [], total: null, offset: null, limit: null, extra: {} };
}

/** Rows of a list that is small enough to be read whole (old array shape or new envelope). */
export function rowsOf<T>(payload: unknown): T[] {
  return readList<T>(payload).rows;
}

export type ListPage<T, X> = {
  /** Rows of the current page only. */
  rows: T[];
  /** The page these rows belong to. It can be lower than the page asked for when the list has become shorter. */
  page: number;
  /** Full number of matching rows as far as the service has told us. */
  total: number;
  /** True when the service reported more rows than it returned and offers no way to fetch them. Say so in the UI. */
  truncated: boolean;
  /** How many rows are reachable through paging (equals `total` unless `truncated`). */
  reachable: number;
  serverPaged: boolean;
  extra: X;
};

export type PagedList<T, X> = ApiState<ListPage<T, X>> & {
  page: number;
  pageSize: number;
  pageCount: number;
  setPage: (page: number) => void;
  setPageSize: (size: number) => void;
};

type Whole<T> = { base: string; rows: T[]; total: number; /** A bare array exactly as long as the limit: more rows may exist and the service cannot say. */ capped: boolean; extra: Record<string, unknown> };

/**
 * A paged view of a list endpoint. With the new API shape each page is one request (`limit` and `offset`).
 * With an old shape the whole list is fetched once, using the largest `limit` the old endpoint accepts, and
 * paged in the browser. Either way the caller gets the same result and can always show "n of total".
 *
 * `base` is the path with its filters and without paging parameters; changing it returns to the first page.
 */
export function usePagedList<T, X = Record<string, unknown>>(base: string | null, options: { pageSize?: number; legacyLimit?: number } = {}): PagedList<T, X> {
  const { legacyLimit } = options;
  const [pageSize, setPageSizeState] = useState(options.pageSize ?? 25);
  const [position, setPosition] = useState<{ base: string | null; page: number }>({ base, page: 0 });
  const page = position.base === base ? position.page : 0;
  const whole = useRef<Whole<T> | null>(null);

  const load = async (signal: AbortSignal): Promise<ListPage<T, X>> => {
    const path = base as string;
    const slice = (w: Whole<T>): ListPage<T, X> => {
      // After a reload the list can be shorter than the page being viewed: fall back to the last page that exists.
      const at = Math.min(page, Math.max(0, Math.ceil(w.rows.length / pageSize) - 1));
      return { rows: w.rows.slice(at * pageSize, (at + 1) * pageSize), page: at, total: w.total, truncated: w.total > w.rows.length || w.capped, reachable: w.rows.length, serverPaged: false, extra: w.extra as X };
    };
    if (whole.current?.base === path) return slice(whole.current);
    const fetchPage = async (at: number) => readList<T>(await apiGet<unknown>(withQuery(path, { limit: pageSize, offset: at * pageSize }), signal));
    let at = page;
    let first = await fetchPage(at);
    if (first.offset !== null) {
      let total = first.total ?? first.offset + first.rows.length;
      if (first.rows.length === 0 && total > 0 && at > 0) {
        at = Math.ceil(total / pageSize) - 1;
        first = await fetchPage(at);
        total = first.total ?? total;
      }
      return { rows: first.rows, page: at, total, truncated: false, reachable: total, serverPaged: true, extra: first.extra as X };
    }
    // Old shape: `offset` was ignored. If the answer may have been cut at `limit`, ask once for everything.
    let all = first;
    if (first.rows.length === pageSize || (first.total !== null && first.total > first.rows.length)) {
      all = readList<T>(await apiGet<unknown>(legacyLimit ? withQuery(path, { limit: legacyLimit }) : path, signal));
    }
    const capped = all !== first && all.total === null && legacyLimit !== undefined && all.rows.length >= legacyLimit;
    whole.current = { base: path, rows: all.rows, total: Math.max(all.total ?? 0, all.rows.length), capped, extra: all.extra };
    return slice(whole.current);
  };

  const state = useResource<ListPage<T, X>>(base === null ? null : `${base}|${page}|${pageSize}`, load, { keepPrevious: true });
  const shownPage = state.data?.page ?? page;
  const pageCount = Math.max(1, Math.ceil((state.data?.reachable ?? 0) / pageSize));

  const setPage = useCallback((next: number) => setPosition({ base, page: Math.max(0, next) }), [base]);
  const setPageSize = useCallback(
    (size: number) => {
      setPageSizeState(size);
      setPosition({ base, page: 0 });
    },
    [base],
  );
  const reloadState = state.reload;
  const reload = useCallback(() => {
    whole.current = null;
    reloadState();
  }, [reloadState]);

  return { ...state, reload, page: shownPage, pageSize, pageCount, setPage, setPageSize };
}

/** Browser-side paging for a list that is already in memory (after filtering or sorting). */
export function useClientPaging<T>(rows: T[], options: { pageSize?: number; resetKey?: string } = {}) {
  const [pageSize, setPageSizeState] = useState(options.pageSize ?? 25);
  const resetKey = options.resetKey ?? "";
  const [position, setPosition] = useState({ key: resetKey, page: 0 });
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const page = Math.min(position.key === resetKey ? position.page : 0, pageCount - 1);
  const setPage = useCallback((next: number) => setPosition({ key: resetKey, page: Math.max(0, next) }), [resetKey]);
  const setPageSize = useCallback(
    (size: number) => {
      setPageSizeState(size);
      setPosition({ key: resetKey, page: 0 });
    },
    [resetKey],
  );
  return { rows: rows.slice(page * pageSize, (page + 1) * pageSize), page, pageSize, pageCount, total: rows.length, offset: page * pageSize, setPage, setPageSize };
}

export type WholeList<T, X> = { rows: T[]; total: number; /** The service reported more rows than could be fetched. */ truncated: boolean; extra: X };

/**
 * Every row of a list that is filtered or sorted in the browser. An old endpoint answers with everything at once;
 * a paged endpoint is read page by page until all rows are in, up to `cap` rows. If the cap is reached the result
 * is marked `truncated` and the caller must say so.
 */
export function useWholeList<T, X = Record<string, unknown>>(base: string | null, options: { cap?: number } = {}): ApiState<WholeList<T, X>> {
  const cap = options.cap ?? 2000;
  return useResource<WholeList<T, X>>(base, async (signal) => {
    const path = base as string;
    const first = readList<T>(await apiGet<unknown>(path, signal));
    const rows = [...first.rows];
    const total = Math.max(first.total ?? 0, rows.length);
    if (first.offset !== null) {
      const step = Math.max(1, first.limit ?? rows.length);
      while (rows.length < total && rows.length < cap) {
        const next = readList<T>(await apiGet<unknown>(withQuery(path, { limit: step, offset: rows.length }), signal));
        if (next.rows.length === 0) break;
        rows.push(...next.rows);
      }
    }
    return { rows, total, truncated: total > rows.length, extra: first.extra as X };
  });
}
