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

export const fmt = (v: number | null | undefined, digits = 2): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const fixed = v.toFixed(digits);
  // Trim trailing decimal zeros only — never the zeros of a whole number (80 must stay 80).
  const s = fixed.includes(".") ? fixed.replace(/\.?0+$/, "") : fixed;
  return s === "-0" ? "0" : s.replace("-", "−");
};
export const pct = (v: number | null | undefined, digits = 1): string => (v === null || v === undefined ? "—" : `${fmt(v, digits)}%`);
export const titleCase = (s: string | null | undefined): string => (s ?? "—").toLowerCase().replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
export const phaseLabel = (p: string | null | undefined): string => (p ? p.replace(/PHASE/g, "Phase ").replace("/", " / ") : "—");
export const day = (iso: string | null | undefined): string => (iso ? iso.slice(0, 10) : "—");

const DIABETES: Record<string, string> = { NO_T2D: "Without type 2 diabetes", T2D: "Type 2 diabetes", MIXED: "With or without type 2 diabetes", NOT_STATED: "Diabetes status not stated" };
export const diabetesLabel = (s: string | null | undefined): string => DIABETES[s ?? "NOT_STATED"] ?? titleCase(s);
