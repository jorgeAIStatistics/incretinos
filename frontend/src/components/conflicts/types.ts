/*
 * Shapes of /api/conflicts and /api/conflicts/{group_id} (backend/incretinos/engines/conflicts.py). The classification,
 * every explanation and every printed value come from the API; nothing here computes or rounds a value.
 */

export type ConflictClass = "TRUE_CONFLICT" | "INSUFFICIENT_INFORMATION" | "EXPLAINABLE_DIFFERENCE" | "CONSISTENT";
export type ConflictEvidenceType = "result" | "analysis" | "adverse_event";

export type ConflictValue = {
  evidence_key: string | null;
  value_printed: string;
  unit: string | null;
  tier: string;
  estimand_label: string | null;
  verification_status: string;
};

export type ConflictStudy = { id: number; nct_id: string; display_name: string; program: string | null };

export type ConflictSummary = {
  group_id: string;
  evidence_type: ConflictEvidenceType;
  evidence_type_label: string;
  class: ConflictClass;
  class_label: string;
  pair_counts: Record<ConflictClass, number>;
  study: ConflictStudy;
  drugs: string[];
  category: string;
  category_label: string;
  quantity: string;
  arm: string;
  time_point: string | null;
  n_rows: number;
  n_sources: number;
  values: ConflictValue[];
  explanation: string;
};

export type ConflictListExtra = {
  counts?: Record<ConflictClass, number>;
  all_groups?: number;
  class_labels?: Record<ConflictClass, string>;
  class_definitions?: Record<ConflictClass, string>;
  evidence_type_labels?: Record<ConflictEvidenceType, string>;
  categories?: string[];
  studies?: { nct_id: string; display_name: string }[];
  drugs?: string[];
  method?: string;
  note?: string;
};

export type SourceRefLite = { id: number; key: string; tier: string; tier_label?: string | null; title: string; publisher?: string | null; url?: string | null; published_date?: string | null };

export type ConflictRow = {
  evidence_type: ConflictEvidenceType;
  id: number;
  evidence_key: string | null;
  review_href: string | null;
  origin: string;
  source: SourceRefLite | null;
  tier: string;
  tier_label: string;
  verification_status: string;
  status_label: string;
  arm_label: string;
  arm_group: string;
  arm_group_basis: string;
  arm_group_resolved: boolean;
  formulation: string | null;
  formulation_name: string | null;
  formulation_basis: string;
  dose_reported: string | null;
  endpoint_title: string;
  category: string;
  class_title: string | null;
  value: number | null;
  value_printed: string;
  decimals: number | null;
  precision_basis: "REPORTED_TEXT" | "LOCATOR_QUOTE" | "STORED_NUMBER" | "COUNT" | "NOT_REPORTED";
  unit: string | null;
  ci_low: number | null;
  ci_high: number | null;
  ci_level: number | null;
  n: number | null;
  n_label: string;
  estimand_label: string | null;
  estimand_basis: string | null;
  estimand_quote: string | null;
  analysis_set: string | null;
  analysis_set_label: string | null;
  analysis_set_field: string | null;
  analysis_set_text: string | null;
  analysis_population: string | null;
  missing_data_handling: string | null;
  missing_data_field: string | null;
  missing_data_text: string | null;
  timepoint_weeks: number | null;
  timeframe_text: string | null;
  baseline_definition: string | null;
  baseline_class: string | null;
  baseline_label: string | null;
  locator_quote: string | null;
  source_location: string | null;
  notes: string | null;
  term: string | null;
  measure_kind: string | null;
  is_serious: boolean;
  n_affected: number | null;
  n_at_risk: number | null;
  percent_reported: number | null;
  percent_calculated: number | null;
  period: string | null;
  param_type: string | null;
  effect_measure: string | null;
  direction_basis: string | null;
  p_value_text: string | null;
  comparator_label: string | null;
};

export type DimensionStatus = "SAME" | "DIFFERENT" | "UNKNOWN" | "UNCONFIRMED" | "NOTED";

export type Dimension = {
  dimension: string;
  label: string;
  status: DimensionStatus;
  a: string | null;
  b: string | null;
  a_field: string | null;
  b_field: string | null;
  explains: boolean;
  decisive: boolean;
  note: string;
};

export type MissingDetail = { evidence_key: string | null; source: string; field: string; detail: string };

export type Comparison = {
  a: string;
  b: string;
  a_evidence_key: string | null;
  b_evidence_key: string | null;
  class: ConflictClass;
  class_label: string;
  values: { a: string; b: string; agree: boolean; difference: string | null; detail: string };
  dimensions: Dimension[];
  explanation: string;
  possible_reasons: string[];
  missing_details: MissingDetail[];
  next_actions: string[];
};

export type ConflictDetail = ConflictSummary & {
  natural_key: string;
  method: string;
  class_definitions: Record<ConflictClass, string>;
  rows: ConflictRow[];
  comparisons: Comparison[];
  possible_reasons: string[];
  missing_details: MissingDetail[];
  next_actions: string[];
  source_hierarchy: { note: string; tiers: { tier: string; tier_label: string; evidence_keys: (string | null)[]; values: string[] }[] };
  endpoint_titles: string[];
};
