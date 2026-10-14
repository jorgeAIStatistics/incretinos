/**
 * Shapes of the main API responses, written from backend/incretinos/api/main.py and engines/findings.py.
 * They are hand-maintained: the FastAPI routes return `dict[str, Any]`, so there is no schema to generate from yet.
 * A field typed `T | null` is null when the source does not report it. Null never means zero.
 */

/** Label and tone the backend attaches to a number: a qualifier that must travel with it. */
export type Label = { code: string; text: string; tone: string; detail: string };

export type SourceRef = {
  id: number;
  key: string;
  tier: string;
  tier_label: string;
  title: string;
  publisher?: string | null;
  url?: string | null;
  published_date?: string | null;
  retrieved_at?: string | null;
  version_label?: string | null;
  content_hash?: string | null;
  stores_full_text?: boolean;
};

export type Meta = {
  version: string;
  mode: string;
  last_ingestion: { mode: string; finished_at: string | null } | null;
  counts: {
    drugs: number;
    studies: number;
    discovered_trials: number;
    results: number;
    analyses: number;
    adverse_event_rows: number;
    publications: number;
    sources: number;
    gaps: number;
    open_review_items: number;
  };
  /** Stored results per verification status. Keys are status names, including legacy ones. */
  result_verification: Record<string, number>;
  result_tiers: Record<string, number>;
  tier_labels: Record<string, string>;
  llm: { provider: string; enabled: boolean };
  review_enabled: boolean;
  notice: string;
};

export type ValidationRule = { rule_id: string; title: string; passed: boolean; checked: number; violations: string[]; detail: string | null };
export type ValidationReport = { passed: number; total: number; rules: ValidationRule[] };

export type DrugClassification = {
  class_label: string;
  agonist_targets: string[];
  blocked_targets: string[];
  is_glp1_receptor_agonist: boolean;
  is_amylin_receptor_agonist: boolean;
  is_gip_receptor_agonist: boolean;
  is_glucagon_receptor_agonist: boolean;
  basis: string;
};

export type DrugSummary = {
  slug: string;
  name: string;
  code_name: string | null;
  brand_names: string[] | null;
  sponsor: string | null;
  modality: string | null;
  route: string | null;
  tier_group: "CORE" | "ADJACENT" | "COMPARATOR" | string;
  is_combination: boolean;
  component_slugs: string[];
  regulatory_status: string;
  regulatory_summary: string | null;
  regulatory_as_of: string | null;
  regulatory_source: SourceRef | null;
  classification: DrugClassification;
  n_studies: number;
  notes: string | null;
};

export type StudyDrug = { slug: string; name: string; role: string };

export type StudyBrief = {
  id: number;
  nct_id: string;
  display_name: string;
  acronym: string | null;
  brief_title: string;
  phase: string | null;
  overall_status: string | null;
  has_results: boolean;
  enrollment: number | null;
  enrollment_type: string | null;
  start_date: string | null;
  primary_completion_date: string | null;
  primary_completion_type: string | null;
  last_update_posted: string | null;
  program: string | null;
  allocation: string | null;
  masking: string | null;
  conditions: string[];
  design_features: Record<string, unknown>;
  diabetes_status: string;
  drugs: StudyDrug[];
};

export type StudyListItem = StudyBrief & {
  evidence_tiers: string[];
  n_results: number;
  n_publications: number;
  primary_publication: { pmid: string; journal: string | null; year: number | null } | null;
};

export type DiscoveredTrial = {
  nct_id: string;
  brief_title: string;
  acronym: string | null;
  phase: string | null;
  overall_status: string | null;
  has_results: boolean;
  enrollment: number | null;
  primary_completion_date: string | null;
  last_update_posted: string | null;
  drug_slugs: string[] | null;
  ingested_in_depth: boolean;
};

export type GapPriority = { score: number; components: Record<string, number>; weights: Record<string, number>; formula: string };

export type Gap = {
  id: number;
  key: string;
  rule_id: string;
  drug: { slug: string; name: string } | null;
  program: string | null;
  category: string;
  question: string;
  available_evidence: string | null;
  missing_evidence: string | null;
  gap_nature: string;
  gap_nature_label: string | null;
  clinical_importance: number | null;
  evidence_certainty: string | null;
  tractability: number | null;
  analytic_approach: string | null;
  data_required: string | null;
  source_ids: number[];
  related_ncts: string[];
  review_status: string;
  reviewer_note: string | null;
  priority: GapPriority;
};

/** Fields of the /gaps envelope other than the rows. */
export type GapListExtra = {
  weights?: Record<string, number>;
  default_weights?: Record<string, number>;
  nature_labels?: Record<string, string>;
  categories?: string[];
  note?: string;
};

export type ComparePreset = { title: string; question: string; a: number; b: number; classification: string };

export type ChangeField = { label: string; before: unknown; after: unknown; significance: string; note: string | null };
export type ChangeEvent = {
  id: number;
  source: SourceRef;
  detected_at: string;
  summary: string;
  significance: string;
  review_status: string;
  is_synthetic: boolean;
  synthetic_notice: string | null;
  changes: ChangeField[];
};

export type Overview = {
  meta: Meta;
  drugs: DrugSummary[];
  pipeline_by_status: Record<string, number>;
  studies_by_program: Record<string, number>;
  top_gaps: Gap[];
  gaps_by_nature: Record<string, number>;
  changes: ChangeEvent[];
  upcoming_readouts: StudyBrief[];
  completed_without_registry_results: StudyBrief[];
  presets: ComparePreset[];
};

export type SourceListItem = SourceRef & { source_type: string; license_note: string | null; n_snapshots: number; n_results: number; external_ids: Record<string, unknown> | null };
export type SourceListExtra = { by_type?: Record<string, number> };

export type SourceSnapshot = {
  version_no: number;
  content_hash: string;
  retrieved_at: string;
  version_label: string | null;
  byte_size: number | null;
  content_stored: boolean;
  is_synthetic: boolean;
  anchored_excerpts: string[] | null;
};

export type DependentFinding = {
  result_id: number;
  study: string;
  arm: string;
  endpoint: string;
  value: number | null;
  verification_status: string;
  locator_quote: string | null;
  source_location: string | null;
};

export type SourceDetail = SourceRef & {
  source_type: string;
  license_note: string | null;
  external_ids: Record<string, unknown> | null;
  classification: string | null;
  snapshots: SourceSnapshot[];
  dependent_findings: DependentFinding[];
  /** Sent by newer API versions; older ones cap `dependent_findings` without saying how many exist. */
  dependent_findings_total?: number;
  changes: { id: number; detected_at: string; summary: string; significance: string; review_status: string; is_synthetic: boolean }[];
};

export type AuditEvent = { id: number; at: string; actor: string; action: string; target_type: string | null; target_id: string | number | null; detail: unknown };

export type ReviewQueueItem = {
  id: number;
  item_type: string;
  target_type: string | null;
  target_id: number | null;
  title: string;
  reason: string;
  priority: string;
  status: string;
  payload: { quote?: string; source_key?: string } & Record<string, unknown> | null;
  created_at: string;
};
export type ReviewQueueExtra = { counts?: { item_type: string; status: string; n: number }[]; review_enabled?: boolean };

/** One stored result with everything needed to read it responsibly (engines/findings.py `Finding`, compact form). */
export type Finding = {
  result_id: number;
  origin: string;
  study_id: number;
  nct_id: string;
  study_name: string;
  phase: string | null;
  study_status: string | null;
  diabetes_status: string;
  arm_id: number | null;
  arm_label: string;
  scope: string;
  drug_slug: string | null;
  dose_amount: number | null;
  dose_unit: string | null;
  is_placebo: boolean;
  is_pooled: boolean;
  endpoint_id: number;
  endpoint_title: string;
  category: string;
  role: string;
  multiplicity_controlled: string;
  estimand_label: string;
  estimand_basis: string;
  estimand_quote: string | null;
  param_type: string | null;
  value: number | null;
  value_low: number | null;
  value_high: number | null;
  unit: string | null;
  ci_low: number | null;
  ci_high: number | null;
  ci_level: number | null;
  dispersion_type: string | null;
  dispersion_value: number | null;
  n_analyzed: number | null;
  baseline_definition: string | null;
  timepoint_weeks: number | null;
  timeframe_text: string | null;
  tier: string;
  verification_status: string;
  source: SourceRef | null;
  source_location: string | null;
  locator_quote: string | null;
  missing_fields: string[];
  notes: string | null;
  labels: Label[];
  statement: string;
  /** Present on list responses: an interval calculated here from a reported standard error. An inference, not a reported value. */
  derived_interval?: { ci_low: number; ci_high: number; display?: string } | null;
};

export type FindingInterpretation = {
  analysis: string[];
  does_not_establish: string[];
  missing: string[];
  derived_interval: { ci_low: number; ci_high: number; display: string } | null;
};
export type FindingDetail = { finding: Finding; interpretation: FindingInterpretation };

export type EstimandStrategy = { event: string; strategy: string; basis: string | null; quote: string | null };
export type EstimandDefinition = { label: string; basis: string; description: string | null; quote: string | null; explainer: string | null; strategies: EstimandStrategy[]; source: SourceRef | null };
export type EstimandRow = {
  category: string;
  category_label: string | null;
  arm: string;
  timepoint_weeks: number | null;
  baseline_definition: string | null;
  /** Results keyed by estimand label. Several results in one cell come from different sources and stay separate. */
  by_estimand: Record<string, Finding[]>;
};
export type EstimandInspector = { study: StudyBrief; estimands: EstimandDefinition[]; rows: EstimandRow[]; explainers: Record<string, string>; rule: string };
