/**
 * Shapes of POST /api/assistant/ask (backend/incretinos/assistant/answer.py `Answer.as_dict`) and helpers that only
 * read them. Nothing here composes, rewords or scores an answer: every block's text, kind and verification come from
 * the API. The helpers find links that the answer already implies (a cited result, an NCT number in a block's text).
 */

import type { SourceRef } from "./types";

export type BlockKind = "REPORTED_EVIDENCE" | "INTERPRETATION" | "UNAVAILABLE" | "HYPOTHESIS" | "REFUSAL" | "MODEL_SUMMARY";

/** A stored record behind a block. `evidence_type` and `evidence_key` are its review identity (absent for a registry study record). */
export type BlockRecord = { type: string; id: number; verification_status: string | null; evidence_type?: string; evidence_key?: string | null };

export type BlockVerification = {
  statuses: string[];
  labels: string[];
  weakest: string;
  label: string;
  human_verified: boolean;
  needs_attention: boolean;
  records: number;
};

export type AnswerBlock = {
  kind: BlockKind | string;
  text: string;
  citations: number[];
  result_ids: number[];
  card_id: string | null;
  evidence: BlockRecord[];
  verification: BlockVerification | null;
  tier: string | null;
  note: string | null;
};

export type Marker = { block: number; kind: string; source_ids: number[]; evidence: BlockRecord[]; verification: string | null; human_verified: boolean; fact: string | null };

/** One sentence of a withheld model draft. The sentence itself is never returned. */
export type WithheldSentence = {
  index: number;
  verdict: string;
  cited: string[];
  reasons: string[];
  claims: { verdict: string | null; bound_card: string | null; mismatches: { field: string; claimed: unknown; actual: unknown; kind: string | null }[] }[];
};

export type CitationCheck = {
  passed: boolean;
  problems: string[];
  sentences_checked: number;
  verdicts?: Record<string, number>;
  method?: string;
  sentences?: WithheldSentence[] | Record<string, unknown>[];
  draft_withheld?: boolean;
};

export type LlmMeta = {
  provider: string;
  used: boolean;
  reason?: string;
  error?: string;
  rejected?: boolean;
  cards?: { offered: number; available: number; truncated: boolean; withheld_instruction_like?: number };
  citation_check?: CitationCheck;
  cited_markers?: string[];
};

export type Answer = {
  question: string;
  intent: string;
  blocks: AnswerBlock[];
  sources: SourceRef[];
  llm: LlmMeta;
  retrieved: { kind: string; ref: number; text: string; source_id: number | null; drug: string | null; tier: string | null }[];
  markers: Record<string, Marker>;
};

/** The kind of question the router recognised, in words. The code is shown beside it. */
export const INTENT_LABEL: Record<string, string> = {
  COMPARE_DRUGS: "Comparison of two molecules",
  EXPLORATORY_OUTCOMES: "Exploratory outcomes",
  UNRESOLVED_QUESTIONS: "Open evidence questions",
  RIGOROUS_COMPARISON: "What a rigorous comparison requires",
  HYPOTHETICAL_ESTIMAND: "Results under a hypothetical estimand",
  CVOT_PROGRAMS: "Cardiovascular outcomes trials",
  RETRIEVAL: "Evidence retrieval",
  REFUSED_RECOMMENDATION: "Treatment recommendation, declined",
  REFUSED_SPONTANEOUS_INFERENCE: "Inference from spontaneous reports, declined",
};

/** The record types of the assistant's facts, in words. */
export const RECORD_LABEL: Record<string, string> = {
  result: "Result",
  analysis: "Between-arm analysis",
  claim: "Statement",
  drug_mechanism: "Mechanism",
  mechanism: "Mechanism",
  adverse_event: "Adverse event",
  indication: "Indication",
  equivalence: "Dose equivalence",
  study: "Registry trial record",
};

export const VERDICT_LABEL: Record<string, string> = {
  SUPPORTED: "Supported",
  CONTRADICTED: "Contradicted by the record",
  UNVERIFIABLE: "Could not be verified",
  NOT_FACTUAL: "No factual claim",
};

/** The Evidence Review address of a record: by its lasting key when the API gives one, otherwise by row id (the workbench then moves to the key). */
export function reviewHref(r: BlockRecord): string | null {
  const type = r.evidence_type ?? (r.type === "drug_mechanism" ? "mechanism" : r.type);
  if (r.type === "study") return null;
  if (r.evidence_key) return `/review?type=${encodeURIComponent(type)}&key=${encodeURIComponent(r.evidence_key)}`;
  return `/review?type=${encodeURIComponent(type)}&id=${r.id}`;
}

/** NCT numbers a block's text names, in order of first mention. */
export function nctIds(text: string): string[] {
  return Array.from(new Set(Array.from(text.matchAll(/\bNCT\d{8}\b/g), (m) => m[0])));
}

/** Splits text at [C#] markers (also "[C2][C4]" and "[C2, C4]") so each marker can be rendered as a link. */
export function splitMarkers(text: string): (string | { marker: string })[] {
  const parts: (string | { marker: string })[] = [];
  let last = 0;
  for (const m of text.matchAll(/\[(C\d+(?:\s*,\s*C\d+)*)\]/g)) {
    const at = m.index ?? 0;
    if (at > last) parts.push(text.slice(last, at));
    for (const id of m[1].split(/\s*,\s*/)) parts.push({ marker: id });
    last = at + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

/** Anchor id of a block in the page. */
export const blockAnchor = (block: AnswerBlock, index: number): string => (block.card_id ? `card-${block.card_id}` : `block-${index + 1}`);
