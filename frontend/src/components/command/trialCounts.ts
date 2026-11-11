import type { StudyListItem } from "@/lib/types";

/*
 * Counting helpers for the command center's clinical-development view. Every count is made from the trial list the
 * Clinical Trial Atlas filters (GET /api/studies), with the same rules as its filters, so a count here and the
 * filtered atlas it links to always agree.
 */

/** Registry statuses of a trial that is still running (it can still report). */
export const ONGOING = new Set(["RECRUITING", "ACTIVE_NOT_RECRUITING", "NOT_YET_RECRUITING", "ENROLLING_BY_INVITATION"]);

/** Column order of the phase-by-status table; any other status the data holds follows in alphabetical order. */
export const STATUS_ORDER = ["NOT_YET_RECRUITING", "RECRUITING", "ENROLLING_BY_INVITATION", "ACTIVE_NOT_RECRUITING", "COMPLETED", "SUSPENDED", "TERMINATED", "WITHDRAWN"];

/** Ordering value of a registry phase ("PHASE2/PHASE3" ranks as phase 3; "EARLY_PHASE1" below phase 1). Null when no phase is reported. */
export function phaseRank(phase: string | null | undefined): number | null {
  if (!phase) return null;
  if (/EARLY_PHASE1/.test(phase)) return 0.5;
  const digits = Array.from(phase.matchAll(/PHASE(\d)/g), (m) => Number(m[1]));
  return digits.length ? Math.max(...digits) : null;
}

export type DevelopmentSummary = {
  /** The registry phase string of the most advanced tracked trial in which the molecule is a study drug. */
  phase: string | null;
  ongoing: number;
  completed: number;
  /** Trials in which the molecule is a study drug (not only a comparator). */
  asStudyDrug: number;
};

/** Per molecule slug: the most advanced tracked trial in which it is a study drug, and how many are ongoing or completed. */
export function developmentByDrug(studies: StudyListItem[]): Map<string, DevelopmentSummary> {
  const out = new Map<string, DevelopmentSummary>();
  for (const s of studies) {
    for (const d of s.drugs) {
      if (d.role === "COMPARATOR") continue;
      const cur = out.get(d.slug) ?? { phase: null, ongoing: 0, completed: 0, asStudyDrug: 0 };
      cur.asStudyDrug += 1;
      if (ONGOING.has(s.overall_status ?? "")) cur.ongoing += 1;
      if (s.overall_status === "COMPLETED") cur.completed += 1;
      const rank = phaseRank(s.phase);
      if (rank !== null && (phaseRank(cur.phase) ?? -1) < rank) cur.phase = s.phase;
      out.set(d.slug, cur);
    }
  }
  return out;
}

export type PhaseStatusMatrix = {
  phases: string[];
  statuses: string[];
  /** `${phase}|${status}` -> count; "" stands for "not reported". */
  cells: Map<string, number>;
  byPhase: Map<string, number>;
  byStatus: Map<string, number>;
  resultsByPhase: Map<string, number>;
  total: number;
  results: number;
};

const bump = (m: Map<string, number>, k: string) => m.set(k, (m.get(k) ?? 0) + 1);

/** Tracked trials counted by registry phase and recruitment status, and by whether results are posted. */
export function phaseStatusMatrix(studies: StudyListItem[]): PhaseStatusMatrix {
  const cells = new Map<string, number>();
  const byPhase = new Map<string, number>();
  const byStatus = new Map<string, number>();
  const resultsByPhase = new Map<string, number>();
  let results = 0;
  for (const s of studies) {
    const phase = s.phase ?? "";
    const status = s.overall_status ?? "";
    bump(cells, `${phase}|${status}`);
    bump(byPhase, phase);
    bump(byStatus, status);
    if (s.has_results) {
      bump(resultsByPhase, phase);
      results += 1;
    }
  }
  const phases = Array.from(byPhase.keys()).sort((a, b) => (phaseRank(a) ?? 99) - (phaseRank(b) ?? 99) || a.localeCompare(b));
  const statuses = Array.from(byStatus.keys()).sort((a, b) => {
    const ia = STATUS_ORDER.indexOf(a);
    const ib = STATUS_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
  return { phases, statuses, cells, byPhase, byStatus, resultsByPhase, total: studies.length, results };
}

/** The atlas filter value for a phase or status ("none" when the registry reports none). */
export const filterValue = (v: string): string => v || "none";
