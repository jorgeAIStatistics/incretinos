import type { ChartRow } from "@/components/charts";
import { fmt, titleCase } from "@/lib/api";

const COMPARATORS = new Set(["semaglutide", "insulin-glargine"]);

const ESTIMAND: Record<string, string> = { EFFICACY: "Efficacy estimand", TREATMENT_REGIMEN: "Treatment-regimen estimand", NOT_STATED: "Estimand not stated" };
export const estimandLabel = (label: string, basis?: string) => `${ESTIMAND[label] ?? titleCase(label)}${basis?.includes("INFERRED") ? " (inferred)" : ""}`;

/** A finding becomes a chart row without losing its interval basis or its provenance. */
export function findingRow(f: any, opts: { studyInLabel?: boolean; investigational?: string[] } = {}): ChartRow {
  const reported = f.ci_low != null && f.ci_high != null;
  const derived = !reported && f.derived_interval;
  const isComparator = !f.is_placebo && f.drug_slug && (COMPARATORS.has(f.drug_slug) || (opts.investigational && !opts.investigational.includes(f.drug_slug)));
  return {
    key: String(f.result_id),
    label: opts.studyInLabel ? `${f.study_name} · ${f.arm_label}` : f.arm_label,
    sub: opts.studyInLabel ? estimandLabel(f.estimand_label, f.estimand_basis) : undefined,
    value: f.value,
    lo: reported ? f.ci_low : derived ? f.derived_interval.ci_low : null,
    hi: reported ? f.ci_high : derived ? f.derived_interval.ci_high : null,
    intervalKind: reported ? "reported" : derived ? "derived" : undefined,
    range: f.value == null && f.value_low != null ? [f.value_low, f.value_high] : null,
    group: f.is_placebo ? "control" : isComparator ? "comparator" : "active",
    tip: [
      `${estimandLabel(f.estimand_label, f.estimand_basis)}${f.timepoint_weeks != null ? ` · week ${fmt(f.timepoint_weeks, 0)}` : ""}`,
      f.n_analyzed != null ? `n = ${f.n_analyzed}` : "n not reported",
      reported ? `${fmt(f.ci_level, 1)}% CI ${fmt(f.ci_low)} to ${fmt(f.ci_high)} (reported)` : derived ? `SE ${fmt(f.dispersion_value)} reported; interval derived here` : "No interval reported",
      `${f.source?.tier_label ?? f.tier} · ${titleCase(f.verification_status)}`,
    ],
    href: `/compare?a=${f.result_id}`,
  };
}

export function aeRowValue(ae: any): number | null {
  return ae.percent_reported ?? ae.percent_calculated ?? null;
}
