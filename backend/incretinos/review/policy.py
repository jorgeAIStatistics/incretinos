"""Vocabulary shared by everything that reads or writes review state.

A verification status describes how far one stored evidence record has been checked. Three statuses are
*mechanical* (assigned by code, never by a person) and three exist only as the result of a recorded human
decision or of a change detected after one:

- MACHINE_EXTRACTED: parsed from a structured payload (registry JSON) whose snapshot hash is stored.
- QUOTE_ANCHORED: the locator quote was found verbatim in the source text and contains the stored numbers.
  This establishes that the number exists in the source. It does NOT establish that it belongs to the
  stated study, arm, endpoint, estimand or population.
- PENDING_HUMAN_REVIEW: no mechanical support could be established.
- HUMAN_VERIFIED: an authorized person approved this exact evidence version (fingerprint).
- REJECTED: an authorized person rejected it. Rejected evidence is kept for audit and excluded from use.
- SOURCE_CHANGED_RECHECK_REQUIRED: a decision exists, but the record or its source changed afterwards.

The stored column ``verification_status`` holds the *effective* status (what every reader should act on).
The mechanical status the row would have without any decision is kept beside it in
``base_verification_status`` so the interface can say "quote-anchored; human-verified by ... on ...".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..enums import ActorKind, ReviewDecisionKind, VerificationStatus

#: Evidence object types that carry a verification status and can be reviewed individually.
EVIDENCE_TYPES: tuple[str, ...] = ("result", "analysis", "adverse_event", "claim")
#: Drug-level curated statements reviewed with the same lifecycle (kept separate so code written against the
#: four core types keeps working).
DRUG_EVIDENCE_TYPES: tuple[str, ...] = ("mechanism", "indication", "equivalence")
#: Every type the review lifecycle keys, fingerprints, versions and accepts decisions for.
REVIEWABLE_TYPES: tuple[str, ...] = EVIDENCE_TYPES + DRUG_EVIDENCE_TYPES

MECHANICAL_STATUSES = frozenset(
    {VerificationStatus.MACHINE_EXTRACTED.value, VerificationStatus.QUOTE_ANCHORED.value, VerificationStatus.PENDING_HUMAN_REVIEW.value}
)
#: Statuses that only an attested decision by an authenticated human can produce.
HUMAN_DECISION_STATUSES = frozenset({VerificationStatus.HUMAN_VERIFIED.value, VerificationStatus.REJECTED.value})
#: The six statuses that may be stored. Anything else (including the legacy alias) is invalid in the database.
STORED_STATUSES = frozenset(
    {*MECHANICAL_STATUSES, *HUMAN_DECISION_STATUSES, VerificationStatus.SOURCE_CHANGED_RECHECK_REQUIRED.value}
)
#: Statuses that remove a record from comparisons, summaries, calculations and exported reports.
EXCLUDED_STATUSES = frozenset({VerificationStatus.REJECTED.value})
#: Statuses that mean a person has to look before the record is relied on.
ATTENTION_STATUSES = frozenset({VerificationStatus.PENDING_HUMAN_REVIEW.value, VerificationStatus.SOURCE_CHANGED_RECHECK_REQUIRED.value})

LEGACY_STATUS_ALIASES = {VerificationStatus.CURATED_PENDING_REVIEW.value: VerificationStatus.PENDING_HUMAN_REVIEW.value}

STATUS_LABEL = {
    VerificationStatus.MACHINE_EXTRACTED.value: "Machine-extracted",
    VerificationStatus.QUOTE_ANCHORED.value: "Quote-anchored",
    VerificationStatus.PENDING_HUMAN_REVIEW.value: "Pending human review",
    VerificationStatus.HUMAN_VERIFIED.value: "Human-verified",
    VerificationStatus.REJECTED.value: "Rejected in review",
    VerificationStatus.SOURCE_CHANGED_RECHECK_REQUIRED.value: "Changed since review — recheck required",
}

#: Order used when a list is sorted "by status": what needs a person first.
STATUS_ATTENTION_ORDER = {
    VerificationStatus.SOURCE_CHANGED_RECHECK_REQUIRED.value: 0,
    VerificationStatus.PENDING_HUMAN_REVIEW.value: 1,
    VerificationStatus.QUOTE_ANCHORED.value: 2,
    VerificationStatus.MACHINE_EXTRACTED.value: 3,
    VerificationStatus.HUMAN_VERIFIED.value: 4,
    VerificationStatus.REJECTED.value: 5,
}


def canonical_status(status: str | None) -> str | None:
    """Map a stored or user-supplied status to its current name."""
    if status is None:
        return None
    return LEGACY_STATUS_ALIASES.get(status, status)


def is_usable(status: str | None) -> bool:
    """False for evidence a reviewer rejected: it must not feed comparisons, summaries or reports."""
    return canonical_status(status) not in EXCLUDED_STATUSES


def is_human_verified(status: str | None) -> bool:
    return canonical_status(status) == VerificationStatus.HUMAN_VERIFIED.value


# --------------------------------------------------------------------------- decisions

DECISION_KINDS: tuple[str, ...] = tuple(k.value for k in ReviewDecisionKind)
#: Decisions that settle the verification status of the evidence version they were made on.
DECISIVE_DECISIONS = frozenset(
    {ReviewDecisionKind.APPROVE.value, ReviewDecisionKind.REJECT.value, ReviewDecisionKind.FLAG_WRONG_ATTRIBUTION.value}
)
#: Decisive decisions that reject. A wrong-attribution flag is a rejection with a stated reason.
REJECTING_DECISIONS = frozenset({ReviewDecisionKind.REJECT.value, ReviewDecisionKind.FLAG_WRONG_ATTRIBUTION.value})
#: Decisions that never change the verification status; they stay visible as open requests.
REQUEST_DECISIONS = frozenset({ReviewDecisionKind.REQUEST_INFO.value, ReviewDecisionKind.PROPOSE_CORRECTION.value})

MIN_RATIONALE_CHARS = 20
MIN_RATIONALE_WORDS = 4
MIN_RATIONALE_DISTINCT_WORDS = 3
_BOILERPLATE_RATIONALE = re.compile(
    r"^(approved?|reject(ed)?|decision|review(ed)?)\s+recorded\s+(from|in|via)\s+the\s+review\s+(queue|page|screen)\.?$", re.IGNORECASE
)
RESERVED_REVIEWER_NAMES = frozenset({"system", "api", "reviewer", "admin", "automation", "anonymous", "unknown"})


def rationale_problem(rationale: str | None) -> str | None:
    """Why a rationale is not acceptable, or None when it is.

    A rationale is the reviewer's own statement of what was checked. It must be long enough to say
    something and must not be filler: one word repeated, a string of the same character, or the fixed
    sentence an interface could send on the reviewer's behalf.
    """
    text = re.sub(r"\s+", " ", rationale or "").strip()
    if len(text) < MIN_RATIONALE_CHARS:
        return f"The rationale must be at least {MIN_RATIONALE_CHARS} characters and say what was checked."
    words = re.findall(r"[^\W\d_]{2,}", text.lower())
    if len(words) < MIN_RATIONALE_WORDS:
        return f"The rationale must contain at least {MIN_RATIONALE_WORDS} words."
    if len(set(words)) < MIN_RATIONALE_DISTINCT_WORDS:
        return "The rationale repeats the same word; state what was checked against the source."
    if _BOILERPLATE_RATIONALE.match(text):
        return "The rationale is a fixed boilerplate sentence; state what was checked against the source."
    return None


def reviewer_problem(reviewer: str | None) -> str | None:
    """Why a reviewer name is not acceptable, or None when it is."""
    name = re.sub(r"\s+", " ", reviewer or "").strip()
    if len(name) < 2 or not re.search(r"[^\W\d_]", name):
        return "The reviewer name must identify a person (at least two characters including a letter)."
    if len(name) > 120:
        return "The reviewer name is too long (120 characters at most)."
    if name.lower() in RESERVED_REVIEWER_NAMES:
        return f"'{name}' is a reserved name and cannot be used as a reviewer identity."
    return None


def is_decisive(decision: str, actor_kind: str | None, attested: bool | None) -> bool:
    """True only for APPROVE / REJECT / FLAG_WRONG_ATTRIBUTION recorded by a human who attested the review.

    This is the single gate between a recorded decision and the two human statuses: a decision by an
    automated actor, or one without the attestation flag, is kept as history and changes nothing.
    """
    return decision in DECISIVE_DECISIONS and actor_kind == ActorKind.HUMAN.value and attested is True


# --------------------------------------------------------------------------- automated check flags


@dataclass(frozen=True)
class CheckFlag:
    """One automated finding about a stored evidence record. A flag explains a risk; it never corrects data."""

    code: str
    severity: str  # HIGH | MEDIUM | LOW
    explanation: str
    fields: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "severity": self.severity, "explanation": self.explanation, "fields": list(self.fields)}


SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
RISK_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "NONE": 3}


def risk_level(flags: list[CheckFlag]) -> str:
    """Highest severity among the flags, or NONE."""
    if not flags:
        return "NONE"
    return min((f.severity for f in flags), key=lambda s: SEVERITY_ORDER.get(s, 9))
