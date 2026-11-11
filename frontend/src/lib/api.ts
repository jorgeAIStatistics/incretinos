"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Untyped payload. It remains the default so pages that have not been typed yet keep compiling;
 * new code passes an explicit type from `@/lib/types` instead: `useApi<StudyListItem[]>("/studies")`.
 */
// oxlint-disable-next-line typescript/no-explicit-any
export type Json = any;

/** A failed request: `status` is the HTTP status, or 0 when the service could not be reached at all. */
export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const UNREACHABLE = "The evidence service could not be reached. Check that it is running, then try again.";

async function request<T>(path: string, init: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, init);
  } catch (cause) {
    if (init.signal?.aborted) throw cause;
    throw new ApiError(UNREACHABLE, 0);
  }
  if (!res.ok) throw new ApiError(await errorText(res), res.status);
  return res.json() as Promise<T>;
}

export function apiGet<T = Json>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { headers: { Accept: "application/json" }, cache: "no-store", signal });
}

export function apiPost<T = Json>(path: string, body: unknown, method: "POST" | "PATCH" = "POST", headers: Record<string, string> = {}, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { method, headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body), signal });
}

type ValidationIssue = { loc?: (string | number)[]; msg?: string };

async function errorText(res: Response): Promise<string> {
  try {
    const data: unknown = await res.json();
    const detail = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : null;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return (detail as ValidationIssue[]).map((d) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg ?? "invalid"}`).join("; ");
  } catch {
    /* not JSON: fall through to the status line */
  }
  if (res.status === 502 || res.status === 503 || res.status === 504) return UNREACHABLE;
  return `Request failed (${res.status})`;
}

export const errorMessage = (e: unknown): string => (e instanceof Error ? e.message : String(e));

/* ---------- request state ---------- */

/**
 * The four states a view must tell apart. "success" with nothing in it is an *empty* result and is the
 * caller's to detect (see `Async` in components/ui); it is never inferred from "loading" or "error".
 */
export type ApiStatus = "idle" | "loading" | "success" | "error";

export type ApiState<T> = {
  status: ApiStatus;
  /** Non-null only when `status` is "success". */
  data: T | null;
  /** Non-null only when `status` is "error". */
  error: string | null;
  /** True while there is nothing to show yet (first load, or a new path without `keepPrevious`). */
  loading: boolean;
  /** True while a newer answer is being fetched and `data` is still the previous one. */
  refreshing: boolean;
  /** True when `data` belongs to the previous key (only possible with `keepPrevious`). */
  stale: boolean;
  reload: () => void;
};

type Settled<T> = { key: string | null; tick: number; data: T | null; error: string | null };

export type ResourceOptions = {
  /**
   * Keep showing the previous answer while a different key loads. Use it only when the two answers describe the
   * same thing (the next page of a list, the same list re-ranked). Never across entities: a new molecule or trial
   * must not appear above the previous one's numbers.
   */
  keepPrevious?: boolean;
};

/**
 * Loads one resource per key. The in-flight request is aborted when the key changes or the component unmounts,
 * and an answer for an old key is never stored.
 */
export function useResource<T>(key: string | null, load: (signal: AbortSignal) => Promise<T>, options: ResourceOptions = {}): ApiState<T> {
  const [tick, setTick] = useState(0);
  const [settled, setSettled] = useState<Settled<T>>({ key: null, tick: -1, data: null, error: null });
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });
  useEffect(() => {
    if (key === null) return;
    const controller = new AbortController();
    loadRef.current(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setSettled({ key, tick, data, error: null });
      },
      (e: unknown) => {
        if (!controller.signal.aborted) setSettled({ key, tick, data: null, error: errorMessage(e) });
      },
    );
    return () => controller.abort();
  }, [key, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  if (key === null) return { status: "idle", data: null, error: null, loading: false, refreshing: false, stale: false, reload };
  const sameKey = settled.key === key;
  const current = sameKey && settled.tick === tick;
  if (current) {
    if (settled.error !== null) return { status: "error", data: null, error: settled.error, loading: false, refreshing: false, stale: false, reload };
    return { status: "success", data: settled.data, error: null, loading: false, refreshing: false, stale: false, reload };
  }
  // A request is in flight. Show the previous answer only if it is for this key (a reload) or the caller opted in.
  const reusable = settled.data !== null && settled.error === null && (sameKey || (options.keepPrevious === true && settled.key !== null));
  if (reusable) return { status: "success", data: settled.data, error: null, loading: false, refreshing: true, stale: !sameKey, reload };
  return { status: "loading", data: null, error: null, loading: true, refreshing: false, stale: false, reload };
}

/** GET `/api{path}`. Pass `null` to make no request. See `ApiState` for what each state means. */
export function useApi<T = Json>(path: string | null, options: ResourceOptions = {}): ApiState<T> {
  return useResource<T>(path, (signal) => apiGet<T>(path as string, signal), options);
}

/** The value, delayed until it has stopped changing for `ms`. Use it for search boxes and sliders that drive a request. */
export function useDebounced<T>(value: T, ms = 250): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(value), ms);
    return () => window.clearTimeout(id);
  }, [value, ms]);
  return debounced;
}

/** Appends query parameters, skipping empty ones. */
export function withQuery(path: string, params: Record<string, string | number | boolean | null | undefined>): string {
  const pairs = Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== "");
  if (pairs.length === 0) return path;
  const query = pairs.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`).join("&");
  return `${path}${path.includes("?") ? "&" : "?"}${query}`;
}

/* ---------- formatting ---------- */

/**
 * A number as text.
 *
 * Without `digits` the value is shown as it was reported: every stored digit is kept and nothing is
 * rounded (7.973 stays 7.973, 0.0046 stays 0.0046). Use this for values that come from a source.
 * With `digits` the value is rounded to that many decimals — for numbers calculated here — but a
 * non-zero value is never shown as 0: its first two significant digits are kept instead.
 * Whole numbers keep their zeros (80 stays 80); only trailing decimal zeros are trimmed.
 */
export const fmt = (v: number | null | undefined, digits?: number): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  if (!Number.isFinite(v)) return v > 0 ? "∞" : "−∞";
  // Decimal text without an exponent, so small values such as 4.6e-7 read as 0.00000046.
  const plain = (n: number): string => {
    const s = String(n);
    const m = /^(-?)(\d)(?:\.(\d+))?e-(\d+)$/.exec(s);
    return m ? n.toFixed(Math.min(100, Number(m[4]) + (m[3]?.length ?? 0))) : s;
  };
  let s: string;
  if (digits === undefined) {
    // 15 significant digits is what a double holds exactly: this removes binary noise (0.1 + 0.2) and nothing else.
    s = plain(Number(v.toPrecision(15)));
  } else {
    const fixed = v.toFixed(digits);
    s = fixed.includes(".") ? fixed.replace(/\.?0+$/, "") : fixed;
    if (v !== 0 && Number(s) === 0) s = plain(Number(v.toPrecision(2)));
  }
  return s === "-0" ? "0" : s.replace("-", "−");
};
export const pct = (v: number | null | undefined, digits = 1): string => (v === null || v === undefined ? "—" : `${fmt(v, digits)}%`);
export const titleCase = (s: string | null | undefined): string => (s ?? "—").toLowerCase().replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
export const phaseLabel = (p: string | null | undefined): string => (p ? p.replace(/PHASE/g, "Phase ").replace("/", " / ") : "—");
export const day = (iso: string | null | undefined): string => (iso ? iso.slice(0, 10) : "—");

const DIABETES: Record<string, string> = { NO_T2D: "Without type 2 diabetes", T2D: "Type 2 diabetes", MIXED: "With or without type 2 diabetes", NOT_STATED: "Diabetes status not stated" };
export const diabetesLabel = (s: string | null | undefined): string => DIABETES[s ?? "NOT_STATED"] ?? titleCase(s);
