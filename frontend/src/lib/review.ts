/**
 * Evidence review: the types of /api/review/*, the decision vocabulary, and the pure helpers the workbench uses.
 *
 * Every field the API may add, rename or drop on another branch is optional here and is read defensively: the
 * workbench shows what the API returns and never fills in a value of its own. Nothing in this file assigns or infers
 * a verification status; statuses are displayed exactly as the API reports them (see lib/evidence.ts).
 *
 * The reviewer token never passes through this module except as an argument to `postDecision`, which sends it as a
 * request header and keeps no copy.
 */

import { withQuery } from "@/lib/api";

/* ---------- types: list ---------- */

export const EVIDENCE_TYPES = ["result", "analysis", "adverse_event", "claim", "mechanism", "indication", "equivalence"] as const;
export type EvidenceType = (typeof EVIDENCE_TYPES)[number];
export const isEvidenceType = (v: string | null | undefined): v is EvidenceType => !!v && (EVIDENCE_TYPES as readonly string[]).includes(v);

export const EVIDENCE_TYPE_LABEL: Record<string, string> = {
  result: "Result",
  analysis: "Statistical analysis",
  adverse_event: "Adverse event",
  claim: "Claim",
  mechanism: "Mechanism",
  indication: "Indication",
  equivalence: "Dose equivalence",
};
export const evidenceTypeLabel = (t: string | null | undefined): string => (t ? (EVIDENCE_TYPE_LABEL[t] ?? t.replace(/_/g, " ")) : "Evidence");

export type DecisionBrief = { decision_id?: number; decision: string; reviewer?: string | null; decided_at?: string | null; fingerprint?: string | null };

export type EvidenceListItem = {
  evidence_type: string;
  id: number | null;
  evidence_key: string;
  fingerprint?: string | null;
  label: string;
  value_summary?: string | null;
  study_nct?: string | null;
  study_name?: string | null;
  drugs?: string[];
  origin?: string | null;
  source_key?: string | null;
  source_type?: string | null;
  tier?: string | null;
  tier_label?: string | null;
  endpoint_category?: string | null;
  verification_status: string | null;
  status_label?: string | null;
  base_verification_status?: string | null;
  usable?: boolean;
  risk_level?: string | null;
  n_flags?: number;
  flag_codes?: string[];
  missing_fields?: string[];
  changed_since_last_seen?: boolean;
  changed_since_review?: boolean;
  changed_at?: string | null;
  first_seen_at?: string | null;
  open_requests?: number;
  last_decision?: DecisionBrief | null;
  identity_collision?: boolean;
  /** Excluded from analytic use although not REJECTED: a rejection of an earlier version awaits a new decision. */
  review_hold?: boolean;
  /** Why the row is excluded from analytic use, as the API states it (null when it is usable). */
  exclusion_reason?: string | null;
};

export type EvidenceListExtra = { all_rows?: number; sort?: string; descending?: boolean; facets?: Record<string, Record<string, number>> };
export type EvidenceListResponse = { total: number; limit: number; offset: number; rows: EvidenceListItem[] } & EvidenceListExtra;

/* ---------- types: detail ---------- */

export type SourceBlock = {
  id?: number;
  key: string;
  title: string;
  url?: string | null;
  source_type?: string | null;
  tier?: string | null;
  tier_label?: string | null;
  publisher?: string | null;
  published_date?: string | null;
  retrieved_at?: string | null;
  content_hash?: string | null;
  snapshot_id?: number | null;
  snapshot_version?: number | null;
  latest_snapshot_version?: number | null;
  snapshot_retrieved_at?: string | null;
  version_label?: string | null;
  license_note?: string | null;
  stores_full_text?: boolean;
  is_synthetic?: boolean;
};

export type AnchoringBlock = {
  basis: string;
  basis_label?: string | null;
  quote?: string | null;
  locator?: string | null;
  context?: string | null;
  checked_against_snapshot_version?: number | null;
  note?: string | null;
};

/** A full-text document, the table as extracted and the exact cells a FULLTEXT row was read from (review API `fulltext`). */
export type FullTextHeaderCell = { text: string; col: number; colspan?: number; rowspan?: number };
export type FullTextBodyRow = { stub: string; cells: Record<string, string>; group?: string | null; kind?: string; footnote_refs?: string[]; lines?: number; panel?: number; indent?: number };
export type FullTextCell = { role: string; row_index?: number | null; col_index?: number | null; row_label?: string | null; column_header?: string | null; text?: string | null; marker?: string; panel?: string | null; via?: string };
export type FullTextBlock = {
  document?: {
    key: string;
    kind: string;
    title: string;
    url?: string | null;
    pmcid?: string | null;
    pmid?: string | null;
    doi?: string | null;
    application_number?: string | null;
    licence?: string | null;
    licence_url?: string | null;
    licence_basis?: string | null;
    open_access_flag?: string | null;
    full_text_storable: boolean;
    full_text_stored: boolean;
    storage_basis?: string | null;
    content_hash?: string | null;
    byte_size?: number | null;
    page_count?: number | null;
    retrieved_at?: string | null;
    fetch_status: string;
    parse_status: string;
    extraction_scope?: string | null;
    limitations?: { code: string; where: string; text: string }[];
  } | null;
  table?: {
    label: string;
    caption?: string | null;
    locator: string;
    section?: string | null;
    page_start?: number | null;
    page_end?: number | null;
    printed_page?: string | null;
    authorship: string;
    source_line?: string | null;
    header_rows?: FullTextHeaderCell[][];
    body_rows?: FullTextBodyRow[];
    footnotes?: { marker?: string | null; kind?: string; text: string }[];
    abbreviations?: Record<string, string>;
    n_columns?: number;
    parse_method: string;
    parse_version: string;
    structural_notes?: string[];
  } | null;
  extraction?: {
    target: string;
    row_label?: string | null;
    column_header?: string | null;
    comparator_header?: string | null;
    cells?: FullTextCell[];
    passage?: Record<string, unknown> | null;
    parse_method: string;
    parse_version: string;
    quality?: Record<string, unknown>;
    confidence: string;
    notes?: string | null;
  } | null;
  used_cells?: number[][];
  used_header_columns?: number[];
  note: string;
};

export type RegistryBlock = { location: string; resolved?: boolean; fragment?: unknown; fragment_text?: string | null; fragment_truncated?: boolean; labels?: Record<string, unknown>; note?: string | null };

export type DecisionOut = {
  id: number;
  uid?: string | null;
  decision: string;
  reviewer: string;
  rationale: string;
  attested?: boolean;
  actor_kind?: string | null;
  created_at: string;
  fingerprint?: string | null;
  on_current_version?: boolean;
  decisive?: boolean;
  source_key?: string | null;
  source_content_hash?: string | null;
  source_snapshot_version?: number | null;
  status_before?: string | null;
  status_after?: string | null;
  proposed_correction?: { changes?: Record<string, unknown>; supporting_quote?: string; note?: string | null } | null;
  recorded_under_other_key?: boolean;
  imported?: boolean;
  imported_from?: string | null;
  ledger_match?: string | null;
  /** How the reviewer name was established (AUTHENTICATED_REVIEWER_TOKEN, SELF_DECLARED_SHARED_TOKEN, IMPORTED_UNRECORDED). */
  identity_basis?: string | null;
  identity_label?: string | null;
};

export type StatusBlock = {
  verification_status: string;
  label?: string | null;
  base_verification_status?: string | null;
  base_label?: string | null;
  usable?: boolean;
  reason?: string | null;
  reason_text?: string | null;
  derived_status?: string | null;
  decided_by?: DecisionBrief | null;
  open_requests?: DecisionOut[];
  /** Excluded from every analytic surface until a reviewer decides on the current version. */
  review_hold?: boolean;
  exclusion_reason?: string | null;
  derived_hold?: boolean | null;
};

export type FieldChange = { field: string; before?: unknown; after?: unknown };

export type VersionOut = {
  version_id: number;
  fingerprint: string;
  is_current?: boolean;
  first_seen_at?: string | null;
  last_seen_at?: string | null;
  current_since?: string | null;
  source_content_hash?: string | null;
  source_snapshot_version?: number | null;
  last_source_content_hash?: string | null;
  last_source_snapshot_version?: number | null;
  status_when_last_current?: string | null;
  supersedes_version_id?: number | null;
  changed_fields?: FieldChange[];
};

export type ChangedSinceReview = {
  decision_id: number;
  decision: string;
  reviewer: string;
  decided_at: string;
  reviewed_fingerprint?: string | null;
  current_fingerprint?: string | null;
  reviewed_version_available?: boolean;
  diff?: FieldChange[];
};

export type ArmBlock = {
  role: string;
  label?: string | null;
  registered_label?: string | null;
  arm_type?: string | null;
  drug_slug?: string | null;
  dose_amount?: number | null;
  dose_unit?: string | null;
  dose_text?: string | null;
  formulation_key?: string | null;
  is_placebo?: boolean | null;
  is_pooled?: boolean | null;
  n_randomized?: number | null;
  origin?: string | null;
  linked?: boolean;
};

export type EndpointBlock = {
  title: string;
  category?: string | null;
  category_label?: string | null;
  threshold?: number | null;
  role?: string | null;
  unit?: string | null;
  timeframe_text?: string | null;
  timepoint_weeks?: number | null;
  hierarchy_rank?: number | null;
  multiplicity_controlled?: string | null;
  kind?: string | null;
  origin?: string | null;
  description?: string | null;
  is_pooled_arms?: boolean;
};

export type EstimandBlock = {
  label: string;
  basis?: string | null;
  description?: string | null;
  population_text?: string | null;
  quote?: string | null;
  strategies?: { event: string; strategy: string; basis?: string | null; quote?: string | null }[];
};

export type RelatedRow = {
  evidence_type: string;
  id: number;
  evidence_key?: string | null;
  relation?: string | null;
  label: string;
  verification_status: string;
  usable?: boolean;
  tier?: string | null;
  source_key?: string | null;
  origin?: string | null;
  values?: Record<string, unknown>;
};

export type CheckFlag = { code: string; severity: string; explanation: string; fields?: string[] };
export type AuditEntry = { id: number; at: string; actor: string; action: string; detail?: Record<string, unknown> };

export type EvidenceDetail = {
  evidence_type: string;
  id: number;
  evidence_key: string | null;
  natural_key?: string | null;
  fingerprint: string | null;
  label?: string | null;
  origin?: string | null;
  reconciled?: boolean;
  status: StatusBlock;
  source?: SourceBlock | null;
  anchoring?: AnchoringBlock | null;
  registry?: RegistryBlock | null;
  fulltext?: FullTextBlock | null;
  values?: Record<string, unknown>;
  confidence_interval?: { low?: number | null; high?: number | null; level?: number | null; sides?: string | null; status?: string | null } | null;
  missing_fields?: string[];
  study?: {
    nct_id: string;
    display_name: string;
    brief_title?: string | null;
    phase?: string | null;
    overall_status?: string | null;
    allocation?: string | null;
    masking?: string | null;
    program?: string | null;
    design_features?: Record<string, unknown>;
    has_results?: boolean;
    conditions?: string[];
  } | null;
  population?: {
    diabetes_status?: string | null;
    diabetes_basis?: string | null;
    sex?: string | null;
    min_age?: string | null;
    max_age?: string | null;
    description?: string | null;
    baseline_summary?: Record<string, unknown>;
  } | null;
  drugs?: { slug: string; name: string; code_name?: string | null; regulatory_status?: string | null; sponsor?: string | null; route?: string | null }[];
  formulations?: { key: string; name: string; dosage_form?: string | null; route?: string | null; stage?: string | null }[];
  arms?: ArmBlock[];
  endpoint?: EndpointBlock | null;
  time_point?: { weeks?: number | null; text?: string | null; baseline_definition?: string | null } | null;
  estimand?: EstimandBlock | null;
  related_analyses?: RelatedRow[];
  same_quantity_other_sources?: RelatedRow[];
  check_flags?: CheckFlag[];
  risk_level?: string | null;
  identity_collision?: { level: number; explanation?: string | null } | null;
  snapshot?: Record<string, unknown>;
  correctable_fields?: string[];
  versions?: VersionOut[];
  changed_since_review?: ChangedSinceReview | null;
  decisions?: DecisionOut[];
  audit?: AuditEntry[];
};

export type ReviewSummary = {
  total?: number;
  by_status?: Record<string, number>;
  decisions_recorded?: number;
  open_requests_total?: number;
  changed_since_review_total?: number;
  changed_since_last_seen_total?: number;
  orphaned_decisions_total?: number;
  /** Orphaned rejections: the value each one excluded may be in use again. */
  orphaned_rejections_total?: number;
  /** Rows excluded because a rejection of an earlier version awaits a new decision. */
  held_total?: number;
  identity_collisions?: number;
  review_enabled?: boolean;
  reviewer_identity?: ReviewerIdentityMode;
};

/**
 * How the service establishes the reviewer name (`reviewer_identity` of /api/meta and /api/review/summary).
 * PERSONAL_TOKENS: the name is bound to the reviewer's own token on the server, so the client sends none.
 * SHARED_TOKEN_SELF_DECLARED: one development token; the reviewer states a name, labelled self-declared.
 */
export type ReviewerIdentityMode = "PERSONAL_TOKENS" | "SHARED_TOKEN_SELF_DECLARED" | "READ_ONLY";

/* ---------- decisions ---------- */

export type DecisionKind = "APPROVE" | "REJECT" | "REQUEST_INFO" | "FLAG_WRONG_ATTRIBUTION" | "PROPOSE_CORRECTION";

export type DecisionInfo = {
  label: string;
  /** Imperative wording for the submit button: "Record: Reject". */
  verb: string;
  /** What the decision does to the record, in the reader's terms. */
  effect: string;
  /** The API refuses it without the attestation that a person checked the source. */
  needsAttestation: boolean;
  /** Keyboard shortcut that opens it. */
  key?: string;
};

export const DECISIONS: Record<DecisionKind, DecisionInfo> = {
  APPROVE: {
    label: "Approve",
    verb: "Approve",
    effect: "Marks this exact version human-verified. If the record or its source changes later, it returns to review.",
    needsAttestation: true,
    key: "a",
  },
  REJECT: {
    label: "Reject",
    verb: "Reject",
    effect: "Marks this version rejected. It stays visible here for audit and is excluded from every comparison, summary, calculation and report.",
    needsAttestation: true,
    key: "r",
  },
  REQUEST_INFO: {
    label: "Request information",
    verb: "Request information",
    effect: "Records an open request. The verification status does not change.",
    needsAttestation: false,
    key: "i",
  },
  FLAG_WRONG_ATTRIBUTION: {
    label: "Flag wrong attribution",
    verb: "Flag wrong attribution",
    effect: "A rejection with a stated reason: the number exists in the source but belongs to another study, arm, endpoint, time point, estimand or population.",
    needsAttestation: true,
    key: "f",
  },
  PROPOSE_CORRECTION: {
    label: "Propose a correction",
    verb: "Propose correction",
    effect: "Records an open request with the proposed values and the supporting quote. Nothing is changed until the curated data are corrected and reloaded.",
    needsAttestation: false,
    key: "c",
  },
};
export const DECISION_ORDER: DecisionKind[] = ["APPROVE", "REJECT", "REQUEST_INFO", "FLAG_WRONG_ATTRIBUTION", "PROPOSE_CORRECTION"];
export const decisionLabel = (d: string | null | undefined): string => (d && d in DECISIONS ? DECISIONS[d as DecisionKind].label : (d ?? "—").replace(/_/g, " ").toLowerCase());

export type DecisionRequest = {
  decision: DecisionKind;
  /** Only under the shared development token. With personal reviewer tokens the service records the token's name. */
  reviewer?: string;
  rationale: string;
  attest_human_review: boolean;
  expected_fingerprint: string;
  proposed_correction?: { changes: Record<string, string | number | boolean | null>; supporting_quote: string; note?: string | null } | null;
};

export type DecisionResponse = {
  decision_id: number;
  uid?: string;
  evidence_type: string;
  id: number;
  evidence_key: string;
  fingerprint: string;
  decision: string;
  status_before: string;
  status_after: string;
  status_label?: string | null;
  usable?: boolean;
  reason?: string;
  reason_text?: string | null;
  review_hold?: boolean;
  exclusion_reason?: string | null;
  /** The name the service recorded (with personal tokens, the token's; never the client's guess). */
  reviewer?: string | null;
  identity_basis?: string | null;
  identity_label?: string | null;
};

/** A refused or failed decision. `kind` says what the reviewer should do next. */
export class DecisionError extends Error {
  readonly status: number;
  readonly kind: "changed" | "not-reconciled" | "token" | "read-only" | "invalid" | "not-found" | "unreachable" | "other";
  readonly currentFingerprint: string | null;
  constructor(message: string, status: number, kind: DecisionError["kind"], currentFingerprint: string | null = null) {
    super(message);
    this.name = "DecisionError";
    this.status = status;
    this.kind = kind;
    this.currentFingerprint = currentFingerprint;
  }
}

type Issue = { loc?: (string | number)[]; msg?: string };

/** The message of an API error body: `detail` may be a string, a list of validation issues, or `{ message, ... }`. */
export function detailMessage(body: unknown): { message: string | null; currentFingerprint: string | null } {
  const detail = body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : null;
  if (typeof detail === "string") return { message: detail, currentFingerprint: null };
  if (Array.isArray(detail)) return { message: (detail as Issue[]).map((d) => `${(d.loc ?? []).slice(1).join(".") || "request"}: ${d.msg ?? "invalid"}`).join("; "), currentFingerprint: null };
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    return { message: typeof d.message === "string" ? d.message : null, currentFingerprint: typeof d.current_fingerprint === "string" ? d.current_fingerprint : null };
  }
  return { message: null, currentFingerprint: null };
}

/**
 * Records one decision. The token is sent as the `X-Reviewer-Token` header and nowhere else; it is not logged and
 * not kept. Throws a `DecisionError` whose `kind` the dialog turns into an instruction.
 */
export async function postDecision(evidenceType: string, rowId: number, body: DecisionRequest, token: string): Promise<DecisionResponse> {
  let res: Response;
  try {
    res = await fetch(`/api/review/evidence/${encodeURIComponent(evidenceType)}/${rowId}/decisions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-Reviewer-Token": token },
      body: JSON.stringify(body),
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch {
    throw new DecisionError("The evidence service could not be reached. Nothing was recorded. Check that it is running, then try again.", 0, "unreachable");
  }
  if (res.ok) return (await res.json()) as DecisionResponse;
  let parsed: unknown = null;
  try {
    parsed = await res.json();
  } catch {
    /* not JSON */
  }
  const { message, currentFingerprint } = detailMessage(parsed);
  const text = message ?? `Request failed (${res.status})`;
  if (res.status === 409) return Promise.reject(new DecisionError(text, 409, currentFingerprint || /changed/i.test(text) ? "changed" : "not-reconciled", currentFingerprint));
  if (res.status === 401) return Promise.reject(new DecisionError(text, 401, "token"));
  if (res.status === 403) return Promise.reject(new DecisionError(text, 403, "read-only"));
  if (res.status === 404) return Promise.reject(new DecisionError(text, 404, "not-found"));
  if (res.status === 422 || res.status === 400) return Promise.reject(new DecisionError(text, res.status, "invalid"));
  if (res.status >= 502 && res.status <= 504) return Promise.reject(new DecisionError("The evidence service could not be reached. Nothing was recorded.", res.status, "unreachable"));
  return Promise.reject(new DecisionError(text, res.status, "other"));
}

/*
 * Client-side mirror of the rationale rule in backend/incretinos/review/policy.py, used only to explain the rule
 * before submitting. The server applies the authoritative check (and may be stricter); its message is shown as is.
 */
export const MIN_RATIONALE_CHARS = 20;
export function rationaleProblem(rationale: string): string | null {
  const text = rationale.replace(/\s+/g, " ").trim();
  if (text.length < MIN_RATIONALE_CHARS) return `Say what you checked against the source (at least ${MIN_RATIONALE_CHARS} characters).`;
  const words = text.toLowerCase().match(/\p{L}{2,}/gu) ?? [];
  if (words.length < 4) return "Use at least four words to say what you checked.";
  if (new Set(words).size < 3) return "The rationale repeats the same word; say what you checked against the source.";
  return null;
}

/** Turns the text typed for a proposed value into the JSON type the current value has. Returns an error for a mismatch. */
export function parseProposedValue(text: string, current: unknown): { value: string | number | boolean | null; error: string | null } {
  const t = text.trim();
  if (t === "" || t.toLowerCase() === "null") return { value: null, error: null };
  if (typeof current === "number") {
    const normalized = t.replace(/[−–]/g, "-");
    const n = Number(normalized);
    if (!/^-?\d+(\.\d+)?([eE][-+]?\d+)?$/.test(normalized) || !Number.isFinite(n)) return { value: null, error: "Enter a number exactly as the source reports it (for example -8.4), or leave it empty for not reported." };
    return { value: n, error: null };
  }
  if (typeof current === "boolean") {
    if (/^(true|yes)$/i.test(t)) return { value: true, error: null };
    if (/^(false|no)$/i.test(t)) return { value: false, error: null };
    return { value: null, error: "Enter true or false." };
  }
  return { value: t, error: null };
}

/* ---------- snapshot fields ---------- */

/** Dotted paths of a nested snapshot, the names a correction may use ("arm.dose_amount"). */
export function flatten(snapshot: Record<string, unknown> | null | undefined, prefix = ""): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(snapshot ?? {})) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object" && !Array.isArray(v)) Object.assign(out, flatten(v as Record<string, unknown>, path));
    else out[path] = v;
  }
  return out;
}

/** A stored value as text: JSON for structures, the exact stored digits for numbers, "null" never shown as 0. */
export function plainValue(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : String(v);
  if (typeof v === "string") return v;
  if (typeof v === "boolean") return v ? "true" : "false";
  return JSON.stringify(v);
}

/** Field name in reading form: "arm.dose_amount" → "arm › dose amount". */
export const fieldLabel = (field: string): string => field.replace(/_/g, " ").replace(/\./g, " › ");

/* ---------- number highlighting in a source passage ---------- */

export type StoredNumber = { field: string; value: number };

/** Numeric fields of `values` (and the time point) that a passage may contain. Booleans and nulls are left out. */
export function storedNumbers(detail: Pick<EvidenceDetail, "values" | "time_point" | "endpoint">): StoredNumber[] {
  const out: StoredNumber[] = [];
  for (const [field, v] of Object.entries(detail.values ?? {})) {
    if (typeof v === "number" && Number.isFinite(v)) out.push({ field, value: v });
  }
  if (typeof detail.time_point?.weeks === "number") out.push({ field: "time point (weeks)", value: detail.time_point.weeks });
  if (typeof detail.endpoint?.threshold === "number") out.push({ field: "endpoint threshold", value: detail.endpoint.threshold });
  return out;
}

export type Segment = { text: string; fields: string[] | null };

// A number in running text: optional sign (ASCII hyphen, minus or en dash), digits with optional thousands
// separators, optional decimals. Digits glued to letters ("HbA1c") are not numbers, and a dash between two digits
// ("weeks 4-8") is a range, not a minus: the lookbehind refuses a match that starts right after a digit.
const NUMBER = /(?<![\p{L}\d.])([-−–]?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?![\d\p{L}])/gu;

/**
 * Splits `text` into plain parts and parts that equal a stored number. Only exact numeric equality marks a part:
 * "-8.3" marks a stored -8.3, never a stored 8.3, and nothing is rounded to make a match.
 */
export function highlightNumbers(text: string, numbers: StoredNumber[]): { segments: Segment[]; found: Set<string> } {
  const segments: Segment[] = [];
  const found = new Set<string>();
  if (!numbers.length) return { segments: [{ text, fields: null }], found };
  let last = 0;
  for (const m of text.matchAll(NUMBER)) {
    const start = m.index ?? 0;
    const sign = m[1] ? "-" : "";
    const value = Number(`${sign}${m[2].replace(/,/g, "")}${m[3] ?? ""}`);
    const fields = numbers.filter((n) => n.value === value).map((n) => n.field);
    if (!fields.length) continue;
    if (start > last) segments.push({ text: text.slice(last, start), fields: null });
    segments.push({ text: m[0], fields });
    fields.forEach((f) => found.add(f));
    last = start + m[0].length;
  }
  if (last < text.length) segments.push({ text: text.slice(last), fields: null });
  return { segments, found };
}

/* ---------- queue filters and links ---------- */

/** Query-string keys of the queue. The same keys are used in the page address and (renamed) in the API call. */
export const FILTER_KEYS = ["q", "etype", "status", "drug", "nct", "source_type", "source_key", "tier", "category", "risk", "missing", "origin", "changed"] as const;
export type FilterKey = (typeof FILTER_KEYS)[number];
export type QueueFilters = Partial<Record<FilterKey, string>> & { sort?: string; desc?: string };

export const SORTS = [
  { value: "status", label: "Needs a person first" },
  { value: "risk", label: "Risk of the automated checks" },
  { value: "study", label: "Trial" },
  { value: "last_change", label: "Most recently changed" },
] as const;

export const CHANGED_OPTIONS = [
  { value: "", label: "Any" },
  { value: "review", label: "Changed since review" },
  { value: "new", label: "Newly updated, not yet seen" },
  { value: "request", label: "Has an open request" },
] as const;

/** The API path for the queue with the given filters (without paging). */
export function queuePath(f: QueueFilters): string {
  const changed = f.changed ?? "";
  return withQuery("/review/evidence", {
    q: f.q,
    evidence_type: f.etype,
    status: f.status,
    drug: f.drug,
    nct: f.nct && /^NCT\d{8}$/i.test(f.nct) ? f.nct.toUpperCase() : undefined,
    source_type: f.source_type,
    source_key: f.source_key,
    tier: f.tier,
    endpoint_category: f.category,
    risk_level: f.risk,
    missing_field: f.missing,
    origin: f.origin,
    changed_since_review: changed === "review" ? true : undefined,
    changed_since_last_seen: changed === "new" ? true : undefined,
    has_open_request: changed === "request" ? true : undefined,
    sort: f.sort && f.sort !== "status" ? f.sort : undefined,
    descending: f.desc === "1" ? true : undefined,
  });
}

/** The workbench address of one record, by its lasting evidence key. */
export function recordHref(evidenceType: string, evidenceKey: string, extra: Record<string, string | number | null | undefined> = {}): string {
  return withQuery("/review", { ...extra, type: evidenceType, key: evidenceKey });
}

/** Where the workbench lists the review summary (counts, held rows, orphaned decisions). */
export const REVIEW_SUMMARY_HREF = "/review#review-summary";

/**
 * Link from an item of the legacy review queue (Sources & Audit) to the workbench: an evidence item opens its
 * record, a source change opens the queue filtered to the evidence that cites that source, and an orphaned review
 * decision (its evidence no longer exists) opens the review summary. Null when the item is about something the
 * workbench does not hold (an ingestion failure, a drug's regulatory status).
 */
export function legacyQueueReviewHref(item: { item_type?: string | null; target_type?: string | null; target_id?: number | null; payload?: Record<string, unknown> | null }): string | null {
  if (item.item_type === "ORPHANED_DECISION" || item.target_type === "review_decision") return REVIEW_SUMMARY_HREF;
  if (isEvidenceType(item.target_type) && typeof item.target_id === "number") return withQuery("/review", { type: item.target_type, id: item.target_id });
  const sourceKey = item.payload && typeof item.payload.source_key === "string" ? item.payload.source_key : null;
  return sourceKey ? withQuery("/review", { source_key: sourceKey }) : null;
}

export const shortHash = (h: string | null | undefined, n = 12): string => (h ? `${h.slice(0, n)}…` : "—");

/** Date and time in UTC as stored, to the minute: "2026-11-09 03:20 UTC". */
export function stamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(iso);
  return m ? `${m[1]} ${m[2]} UTC` : iso;
}

export const SEVERITY_LABEL: Record<string, string> = { HIGH: "High", MEDIUM: "Medium", LOW: "Low", NONE: "None" };
