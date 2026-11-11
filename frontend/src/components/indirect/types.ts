/** Response shapes of /api/indirect/* (backend/incretinos/engines/indirect.py). */
import type { SourceRef } from "@/lib/types";

export type CheckStatus = "PASS" | "FAIL" | "CANNOT_ASSESS";

export type Check = {
  key: string;
  label: string;
  status: CheckStatus;
  a: string;
  b: string;
  detail: string;
  evidence: { supported_by?: string[]; undermined_by?: string[]; unverifiable?: string[]; [key: string]: unknown };
};

export type EffectModifier = { key: string; label: string; a: string; b: string; assessment: string; note: string };

export type NetworkNode = { id: string; label: string; detail: string };
export type NetworkEdge = { from: string; to: string; trial: string | null; nct_id: string | null; analysis_id: number | null; kind: string };
export type Network = { nodes: NetworkNode[]; edges: NetworkEdge[]; text: string };

export type IndirectInput = {
  side: "A" | "B";
  analysis_id: number;
  nct_id: string;
  study_name: string;
  orientation: string;
  param_type: string | null;
  scale: string;
  unit: string | null;
  estimate: number | null;
  estimate_text: string | null;
  ci_low: number | null;
  ci_high: number | null;
  ci_level: number | null;
  ci_sides: string;
  p_value_text: string | null;
  timepoint_weeks: number | null;
  estimand_label: string;
  estimand_basis: string;
  diabetes_status: string;
  se: number | null;
  se_formula: string | null;
  se_rounding_note: string | null;
  n_treatment: number | null;
  n_comparator: number | null;
  analysis_population: string[];
  verification_status: string;
  origin: string;
  source: SourceRef | null;
  source_location: string | null;
};

export type IndirectResult = {
  label: string;
  scale: "DIFFERENCE" | "RATIO";
  orientation: string;
  unit: string | null;
  estimate: number;
  ci_low: number;
  ci_high: number;
  ci_level: number;
  se: number;
  z_statistic: number;
  p_value_text: string;
  formula: string;
  reading: string;
  assumptions: string[];
};

export type Assessment = {
  method: string;
  label: string;
  reference: string;
  feasible: boolean;
  status: "CALCULATED" | "REFUSED";
  headline: string;
  checks: Check[];
  refusal_reasons: string[];
  unverified_assumptions: { key: string; label: string; detail: string }[];
  effect_modifiers: EffectModifier[];
  network: Network;
  inputs: IndirectInput[];
  result: IndirectResult | null;
  not_a_randomized_comparison: string;
  not_a_ranking: string;
  multiplicity_note: string | null;
};

export type Candidate = {
  analysis_id: number;
  study_name: string;
  treatment_label: string;
  comparator_label: string;
  estimate: number | null;
  ci_low: number | null;
  ci_high: number | null;
  ci_level: number | null;
  timepoint_weeks: number | null;
  estimand_label: string;
  verification_status: string;
  usable_alone: boolean;
  problems: string[];
};

export type Option = { key: string; label: string; analyses: number };
export type Options = { categories: Option[]; comparators: Option[]; drugs: { slug: string; name: string; analyses: number; comparators: Option[] }[] };

/** `reported_effect` of an analysis interpretation (engines/interpretation.py). */
export type ReportedEffect = {
  scale: string;
  orientation: string;
  ci: { low: number | null; high: number | null; level: number | null; level_status: string; sides: string; text: string };
  p_value: string;
  estimand: { label: string; basis: string };
  analysis_population: string;
  hypothesis: { text: string };
  multiplicity: { text: string };
  plain_reading: string;
};
