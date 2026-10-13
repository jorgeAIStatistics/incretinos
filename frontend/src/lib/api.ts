"use client";

import { useEffect, useState } from "react";

export type Json = any; // API payloads are documented by the backend's OpenAPI schema

export async function apiGet<T = Json>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`, { headers: { Accept: "application/json" }, cache: "no-store" });
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}

export async function apiPost<T = Json>(path: string, body: unknown, method: "POST" | "PATCH" = "POST", headers: Record<string, string> = {}): Promise<T> {
  const res = await fetch(`/api${path}`, { method, headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}

async function errorText(res: Response): Promise<string> {
  try {
    const data = await res.json();
    const detail = data?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((d: any) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`).join("; ");
  } catch {
    /* fall through */
  }
  return `Request failed (${res.status})`;
}

export function useApi<T = Json>(path: string | null): { data: T | null; error: string | null; loading: boolean; reload: () => void } {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({ data: null, error: null, loading: !!path });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!path) {
      setState({ data: null, error: null, loading: false });
      return;
    }
    let live = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    apiGet<T>(path)
      .then((data) => live && setState({ data, error: null, loading: false }))
      .catch((e) => live && setState({ data: null, error: String(e.message ?? e), loading: false }));
    return () => {
      live = false;
    };
  }, [path, tick]);
  return { ...state, reload: () => setTick((t) => t + 1) };
}

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
