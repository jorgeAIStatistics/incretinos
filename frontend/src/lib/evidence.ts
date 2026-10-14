/**
 * The evidence-state vocabulary: what each state means, in plain English, and how it may be described.
 * Names and labels mirror backend/incretinos/review/policy.py and enums.py so the interface, the API and the
 * exported reports use the same words. The visual treatment of each state lives in components/ui/evidence.tsx.
 *
 * Three independent questions are kept apart and must never be merged into one badge:
 *   1. Verification status: how far has this stored record been checked?
 *   2. Evidence tier: what kind of source did it come from?
 *   3. Epistemic kind: is it reported, calculated here, hypothesised, simulated, or missing?
 */

/* ---------- 1. verification status ---------- */

export type VerificationStatus = "MACHINE_EXTRACTED" | "QUOTE_ANCHORED" | "PENDING_HUMAN_REVIEW" | "HUMAN_VERIFIED" | "REJECTED" | "SOURCE_CHANGED_RECHECK_REQUIRED";

/** Stored values written before the rename. */
const LEGACY_STATUS: Record<string, VerificationStatus> = { CURATED_PENDING_REVIEW: "PENDING_HUMAN_REVIEW" };

export type VerificationInfo = {
  label: string;
  /** Short form for dense tables. */
  short: string;
  /** What the state establishes and, as importantly, what it does not. */
  meaning: string;
  /** Assigned by code. Only the other three exist because of a recorded human decision or a change after one. */
  mechanical: boolean;
  /** A person has to look before this record is relied on. */
  needsAttention: boolean;
  /** Removed from comparisons, summaries, calculations and reports. */
  excluded: boolean;
};

export const VERIFICATION: Record<VerificationStatus, VerificationInfo> = {
  MACHINE_EXTRACTED: {
    label: "Machine-extracted",
    short: "Machine-extracted",
    meaning: "Parsed by code from a structured registry record whose snapshot hash is stored. No person has checked it.",
    mechanical: true,
    needsAttention: false,
    excluded: false,
  },
  QUOTE_ANCHORED: {
    label: "Quote-anchored",
    short: "Quote-anchored",
    meaning:
      "The stored number and its quote were found word for word in the source text. That shows the number exists in the source. It does not show that it belongs to this study, arm, endpoint, estimand or population: no person has checked that.",
    mechanical: true,
    needsAttention: false,
    excluded: false,
  },
  PENDING_HUMAN_REVIEW: {
    label: "Pending human review",
    short: "Pending review",
    meaning: "No automatic support could be established for this value. It is waiting for a reviewer and should not be relied on yet.",
    mechanical: true,
    needsAttention: true,
    excluded: false,
  },
  HUMAN_VERIFIED: {
    label: "Human-verified",
    short: "Human-verified",
    meaning: "An authorized reviewer approved this exact version of the record. The decision is in the audit log.",
    mechanical: false,
    needsAttention: false,
    excluded: false,
  },
  REJECTED: {
    label: "Rejected in review",
    short: "Rejected",
    meaning: "A reviewer rejected this record. It is kept for audit and is excluded from comparisons, summaries, calculations and reports.",
    mechanical: false,
    needsAttention: false,
    excluded: true,
  },
  SOURCE_CHANGED_RECHECK_REQUIRED: {
    label: "Changed since review — recheck required",
    short: "Recheck required",
    meaning: "A review decision exists, but the record or its source changed afterwards. The earlier decision does not cover this version until a reviewer looks again.",
    mechanical: false,
    needsAttention: true,
    excluded: false,
  },
};

/** Display order: the one human-confirmed state first, then mechanical states, then states that need a person. */
export const VERIFICATION_ORDER: VerificationStatus[] = ["HUMAN_VERIFIED", "MACHINE_EXTRACTED", "QUOTE_ANCHORED", "PENDING_HUMAN_REVIEW", "SOURCE_CHANGED_RECHECK_REQUIRED", "REJECTED"];

/** Maps a stored or legacy status to its current name. Returns null for a status this interface does not know. */
export function canonicalStatus(status: string | null | undefined): VerificationStatus | null {
  if (!status) return null;
  const name = LEGACY_STATUS[status] ?? status;
  return name in VERIFICATION ? (name as VerificationStatus) : null;
}

export const isHumanVerified = (status: string | null | undefined): boolean => canonicalStatus(status) === "HUMAN_VERIFIED";

/**
 * Totals per current status name from a `{status: count}` record. Legacy names are folded into their current name;
 * statuses this interface does not know are returned separately so no count is ever dropped silently.
 */
export function countByStatus(counts: Record<string, number> | null | undefined): { known: Record<VerificationStatus, number>; unknown: { status: string; n: number }[]; total: number } {
  const known = Object.fromEntries(VERIFICATION_ORDER.map((s) => [s, 0])) as Record<VerificationStatus, number>;
  const unknown: { status: string; n: number }[] = [];
  let total = 0;
  for (const [status, n] of Object.entries(counts ?? {})) {
    total += n;
    const name = canonicalStatus(status);
    if (name) known[name] += n;
    else unknown.push({ status, n });
  }
  return { known, unknown, total };
}

/* ---------- 2. evidence tier ---------- */

export type EvidenceTier =
  | "REGULATORY_LABEL"
  | "REGULATORY_REVIEW"
  | "PEER_REVIEWED"
  | "REGISTRY_RESULTS"
  | "REGISTRY_PROTOCOL"
  | "PRESS_RELEASE"
  | "SPONSOR_PIPELINE"
  | "CONFERENCE"
  | "GUIDANCE"
  | "SPONTANEOUS_REPORTS"
  | "SECONDARY";

export type TierInfo = { label: string; short: string; meaning: string };

/** `label` mirrors TIER_LABEL in backend/incretinos/enums.py. The API also sends it as `tier_label`. */
export const TIERS: Record<EvidenceTier, TierInfo> = {
  REGULATORY_LABEL: { label: "FDA prescribing information", short: "FDA label", meaning: "FDA-approved prescribing information: reviewed by the regulator and written for prescribers." },
  REGULATORY_REVIEW: { label: "FDA review document", short: "FDA review", meaning: "The regulator’s own written assessment of a marketing application." },
  PEER_REVIEWED: { label: "Peer-reviewed publication", short: "Peer-reviewed", meaning: "A journal article that passed peer review." },
  REGISTRY_RESULTS: {
    label: "Registry-posted results (sponsor-submitted)",
    short: "Registry results",
    meaning: "Results the sponsor posted on ClinicalTrials.gov. Quality-controlled by the registry, not peer reviewed.",
  },
  REGISTRY_PROTOCOL: { label: "Registry protocol record", short: "Registry protocol", meaning: "Design information registered on ClinicalTrials.gov: what the trial planned to do. It is not a result." },
  PRESS_RELEASE: { label: "Sponsor press release (topline)", short: "Press release", meaning: "A sponsor’s topline announcement. Selective, not peer reviewed, and usually without full statistical detail." },
  SPONSOR_PIPELINE: { label: "Sponsor pipeline disclosure", short: "Sponsor pipeline", meaning: "The sponsor’s own pipeline page. It says what is in development, not how well it works." },
  CONFERENCE: { label: "Conference presentation", short: "Conference", meaning: "A congress abstract or presentation. Usually not peer reviewed in full." },
  GUIDANCE: { label: "Regulatory guidance", short: "Guidance", meaning: "Regulatory or ICH guidance. A standard for methods, not evidence about a drug." },
  SPONTANEOUS_REPORTS: {
    label: "Spontaneous adverse-event reports",
    short: "Spontaneous reports",
    meaning: "Adverse-event reports sent to the regulator outside clinical trials. These are counts of reports, not rates: the number of people exposed is unknown, and a report does not show that the drug caused the event.",
  },
  SECONDARY: { label: "Secondary reporting", short: "Secondary", meaning: "News or other reporting of someone else’s source." },
};

export const isTier = (tier: string | null | undefined): tier is EvidenceTier => !!tier && tier in TIERS;

/* ---------- 3. epistemic kind ---------- */

/** Mirrors ClaimKind in backend/incretinos/enums.py, plus "MISSING" for a value the source does not report. */
export type EpistemicKind = "DOCUMENTED" | "INFERENCE" | "HYPOTHESIS" | "SIMULATION" | "UNSUPPORTED" | "MISSING";

export type EpistemicInfo = { label: string; meaning: string };

export const EPISTEMIC: Record<EpistemicKind, EpistemicInfo> = {
  DOCUMENTED: { label: "Reported by the source", meaning: "Stated by the cited source. Nothing was calculated or assumed here." },
  INFERENCE: { label: "Inference", meaning: "Calculated here from reported values by a fixed rule. The source did not report this number itself." },
  HYPOTHESIS: { label: "Hypothesis", meaning: "Plausible but untested. It is not evidence and must not be cited as a finding." },
  SIMULATION: { label: "Synthetic", meaning: "Produced by a simulation or generated for demonstration. It is not trial data and describes no real participant." },
  UNSUPPORTED: { label: "No supporting source", meaning: "No source was located that supports this statement." },
  MISSING: { label: "Not reported", meaning: "The source does not report this value. It is shown as missing: it is not zero and nothing was filled in." },
};

/** The single visible wording for a missing value. Never render a missing value as 0 or as an empty cell. */
export const NOT_REPORTED = "Not reported";
