"""A drug's regulatory status as every surface must show it: never resting on a statement a reviewer excluded.

The curated loader copies the reviewed statement ``{slug}:regulatory-status`` (an ``EvidenceClaim``) into the
drug's own columns (``regulatory_status``, ``regulatory_summary``, ``regulatory_as_of``, ``regulatory_source_id``)
so that pages can show it cheaply. Those columns are a copy, not evidence. When the statement is rejected in
review, or held after a rejection of an earlier version, the copy must not be shown either, and nothing may be
reconstructed from other columns and presented in its place: the status is then UNKNOWN, with no summary, date
or source, and a note that says why.

Every surface that shows a regulatory status (drug list and profile, overview, the review dossier, the
assistant's drug facts) goes through ``regulatory_view``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import INCLUDE_REJECTED
from ..enums import RegulatoryStatus
from ..models import Drug, EvidenceClaim
from .policy import canonical_status, is_usable

UNAVAILABLE_NOTE = (
    "The regulatory-status statement on record for this drug was rejected in review (or is held after a rejected earlier version). "
    "No regulatory status is shown until a reviewer approves a statement."
)


def claim_key(slug: str) -> str:
    return f"{slug}:regulatory-status"


def status_claims(session: Session, slugs: Iterable[str]) -> dict[str, tuple[str | None, bool]]:
    """slug -> (verification status, review hold) of each drug's regulatory-status statement, rejected ones included."""
    keys = {claim_key(slug): slug for slug in slugs}
    if not keys:
        return {}
    rows = session.execute(
        select(EvidenceClaim.key, EvidenceClaim.verification_status, EvidenceClaim.review_hold)
        .where(EvidenceClaim.key.in_(sorted(keys))).execution_options(**{INCLUDE_REJECTED: True})
    ).all()
    return {keys[key]: (canonical_status(status), bool(hold)) for key, status, hold in rows}


def statement_usable(claim: tuple[str | None, bool] | None) -> bool:
    """True when the drug's regulatory status may be shown: its statement exists and is not excluded in review."""
    if claim is None:
        return True  # no reviewable statement exists for this drug: the curated columns are all there is (not a rejection)
    status, hold = claim
    return is_usable(status, hold)


def regulatory_view(drug: Drug, claim: tuple[str | None, bool] | None) -> dict[str, Any]:
    """The regulatory fields of a drug for display. ``claim`` comes from ``status_claims``."""
    if statement_usable(claim):
        return {
            "regulatory_status": drug.regulatory_status,
            "regulatory_summary": drug.regulatory_summary,
            "regulatory_as_of": drug.regulatory_as_of,
            "regulatory_source_id": drug.regulatory_source_id,
            "regulatory_status_available": True,
            "regulatory_status_note": None,
            "regulatory_statement_status": claim[0] if claim else None,
        }
    return {
        "regulatory_status": RegulatoryStatus.UNKNOWN.value,
        "regulatory_summary": None,
        "regulatory_as_of": None,
        "regulatory_source_id": None,
        "regulatory_status_available": False,
        "regulatory_status_note": UNAVAILABLE_NOTE,
        "regulatory_statement_status": claim[0] if claim else None,
    }


def drug_regulatory_view(session: Session, drug: Drug) -> dict[str, Any]:
    return regulatory_view(drug, status_claims(session, [drug.slug]).get(drug.slug))
