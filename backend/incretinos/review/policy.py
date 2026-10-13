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
"""

from __future__ import annotations

from dataclasses import dataclass

from ..enums import VerificationStatus

#: Evidence object types that carry a verification status and can be reviewed individually.
EVIDENCE_TYPES: tuple[str, ...] = ("result", "analysis", "adverse_event", "claim")

MECHANICAL_STATUSES = frozenset(
    {VerificationStatus.MACHINE_EXTRACTED.value, VerificationStatus.QUOTE_ANCHORED.value, VerificationStatus.PENDING_HUMAN_REVIEW.value}
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


def risk_level(flags: list[CheckFlag]) -> str:
    """Highest severity among the flags, or NONE."""
    if not flags:
        return "NONE"
    return min((f.severity for f in flags), key=lambda s: SEVERITY_ORDER.get(s, 9))
