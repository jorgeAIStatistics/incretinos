/**
 * Evidence Quality (`/api/quality`): types and the few helpers the quality pages share.
 *
 * Every count comes from the API, which computes it from the stored records when asked. The interface never adds
 * dimensions together and never derives a score: each dimension is shown on its own with its rule.
 */
import { withQuery } from "@/lib/api";

export type QualityDimensionId = "review_status" | "completeness" | "authority" | "reliability" | "precision" | "freshness";
export type BreakdownId = "drug" | "study" | "tier" | "source_type" | "endpoint_category";

export type ApiLink = { path: string; params: Record<string, string>; total_field: string };
export type MetricLinks = {
  /** Where a click on the count goes: the exact page when one exists, otherwise the record list. */
  href: string;
  records: { href: string; api: ApiLink };
  /** Another page that shows exactly the same records with its own filters. */
  page: { href: string; api: ApiLink } | null;
  /** A related page that is not an exact filter of the same records. */
  context: { label: string; href: string } | null;
};

export type QualityMetric = {
  id: string;
  dimension: QualityDimensionId;
  label: string;
  rule: string;
  noun: string;
  universe: string;
  universe_label: string;
  denominator_label: string;
  count: number;
  denominator: number;
  share: number | null;
  param: string | null;
  breakdowns: BreakdownId[];
  includes_excluded_rows: boolean;
  links: MetricLinks;
  /** Evidence type -> [count, denominator]. */
  by_type?: Record<string, [number, number]>;
};

export type QualityDimension = { id: QualityDimensionId; label: string; question: string; caveat: string };

export type DistributionRow = { value: string | null; label: string; count: number; links: MetricLinks | null };
export type FlagRow = { value: string; title: string; severity: string | null; help: string | null; known: boolean; count: number; links: MetricLinks };
export type AgeRow = { value: string; label: string; count: number; links: MetricLinks };

export type WidthGroup = {
  key: string;
  evidence_type: string;
  endpoint_category: string | null;
  endpoint_label: string | null;
  param_type: string | null;
  unit: string | null;
  ci_level: number | null;
  n: number;
  min: number;
  p25: number;
  median: number;
  p75: number;
  max: number;
  links: MetricLinks;
};

export type BreakdownGroup = { value: string; label: string; counts: Record<string, [number, number]> };
export type Breakdown = { by: BreakdownId; label: string; metrics: string[]; not_applicable: string[]; groups: BreakdownGroup[] };

export type QualityOverview = {
  as_of: string;
  dimensions: QualityDimension[];
  breakdown_dimensions: { id: BreakdownId; label: string }[];
  metrics: QualityMetric[];
  distributions: {
    records_by_tier: DistributionRow[];
    records_by_source_type: DistributionRow[];
    sources_by_tier: DistributionRow[];
    sources_by_source_type: DistributionRow[];
    flag_codes: FlagRow[];
    retrieval_age: AgeRow[];
  };
  interval_widths: { note: string; min_group_size: number; groups: WidthGroup[]; groups_too_small: number; intervals_in_small_groups: number };
  refresh_rule: { rule: string; days: { source_type: string; label: string; days: number; default: number }[]; fallback_days: number; setting: string };
  safety_rule: { label_sections: string[]; measure_kind: string };
  breakdown: Breakdown;
  notes: string[];
};

export type QualityRecord = {
  key: string;
  kind: "evidence" | "source" | "drug" | "decision" | "change_event" | "queue_item" | "conflict";
  label: string;
  evidence_type: string | null;
  evidence_type_label: string | null;
  evidence_key: string | null;
  id: number | null;
  study_nct: string | null;
  study_name: string | null;
  drugs: string[];
  tier: string | null;
  source_type: string | null;
  endpoint_category: string | null;
  verification_status: string | null;
  status_label: string | null;
  review_hold: boolean | null;
  risk_level: string | null;
  note: string | null;
  links: { label: string; href: string }[];
};

export type QualityRecordsExtra = {
  denominator: number;
  metric: QualityMetric;
  value: string | null;
  value_label: string | null;
  filters: Partial<Record<BreakdownId, string>>;
  filter_labels: Partial<Record<BreakdownId, string>>;
  dimension_label: string;
  dimension_caveat: string;
  as_of: string;
};

export type AssessmentSection = QualityDimension & {
  metrics: QualityMetric[];
  by_tier?: DistributionRow[];
  by_source_type?: DistributionRow[];
  flag_codes?: FlagRow[];
  not_applicable: string[];
};
export type QualityAssessment = { by: "drug" | "study"; value: string; label: string; as_of: string; sections: AssessmentSection[]; notes: string[] };

export const BREAKDOWN_IDS: BreakdownId[] = ["drug", "study", "tier", "source_type", "endpoint_category"];
export const isBreakdown = (value: string): value is BreakdownId => (BREAKDOWN_IDS as string[]).includes(value);

export const DIMENSION_IDS: QualityDimensionId[] = ["review_status", "completeness", "authority", "reliability", "precision", "freshness"];
export const isDimension = (value: string): value is QualityDimensionId => (DIMENSION_IDS as string[]).includes(value);

/** The record list of one metric, with filters and (for a metric counted one value at a time) its value. */
export function recordsHref(metricId: string, filters: Partial<Record<BreakdownId, string>> = {}, value?: string | null): string {
  return withQuery("/quality/records", { metric: metricId, value: value ?? undefined, ...filters });
}

/** A share as a percentage with one decimal; a non-zero share never reads as 0. */
export function shareText(count: number, denominator: number): string {
  if (denominator <= 0) return "no records";
  const pct = (100 * count) / denominator;
  if (count > 0 && pct < 0.05) return "<0.1%";
  if (count < denominator && pct > 99.95) return ">99.9%";
  return `${pct.toFixed(1)}%`;
}

export const EVIDENCE_TYPE_LABEL: Record<string, string> = {
  result: "Results",
  analysis: "Between-arm analyses",
  adverse_event: "Adverse-event rows",
  claim: "Claims",
  mechanism: "Mechanism statements",
  indication: "Indications",
  equivalence: "Exposure equivalences",
};
