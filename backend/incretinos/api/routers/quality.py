"""Evidence Quality API: separate quality dimensions of the stored evidence, their records and per-entity assessments.

Every route is a GET and reads only (the request session refuses to commit). The counts are computed from the stored
rows on each request by ``engines/quality.py``; rejected and held rows are excluded from every analytic count by the
session rule in ``incretinos.db`` and counted only in the review-status dimension, through its explicit opt-in.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import get_db
from ...engines import quality

router = APIRouter(prefix="/api/quality", tags=["quality"])

Db = Annotated[Session, Depends(get_db)]
MAX_OFFSET = 1_000_000
Breakdown = Literal["drug", "study", "tier", "source_type", "endpoint_category"]
assert set(Breakdown.__args__) == set(quality.BREAKDOWNS)  # type: ignore[attr-defined]

DrugFilter = Annotated[str | None, Query(max_length=60, description="A molecule slug, as in Evidence Review.")]
StudyFilter = Annotated[str | None, Query(pattern=r"^NCT\d{8}$", description="A trial's NCT number.")]
TierFilter = Annotated[str | None, Query(max_length=40)]
SourceTypeFilter = Annotated[str | None, Query(max_length=40)]
CategoryFilter = Annotated[str | None, Query(max_length=30)]


def _facts(db: Session) -> quality.Facts:
    return quality.collect(db, refresh_days=quality.parse_refresh_rule(get_settings().quality_refresh_days))


@router.get("")
def quality_overview(db: Db, by: Breakdown = "drug") -> dict[str, Any]:
    """Every metric with its true total, rule and drill-down links; the distributions; and one breakdown (``by``)."""
    return quality.overview(_facts(db), by)


@router.get("/records")
def quality_records(
    db: Db,
    metric: Annotated[str, Query(max_length=60, description="A metric id from GET /api/quality.")],
    value: Annotated[str | None, Query(max_length=300, description="The check code, interval group or age bucket, for a metric that counts one at a time.")] = None,
    drug: DrugFilter = None,
    study: StudyFilter = None,
    tier: TierFilter = None,
    source_type: SourceTypeFilter = None,
    endpoint_category: CategoryFilter = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
) -> dict[str, Any]:
    """Exactly the records one metric counted under the filters, with the true total, in a stable order."""
    given = {"drug": drug, "study": study, "tier": tier, "source_type": source_type, "endpoint_category": endpoint_category}
    filters = {k: v for k, v in given.items() if v}
    try:
        quality.check_records_query(metric, value, filters)  # a wrong query is refused before anything is read
        return quality.records(_facts(db), metric, value=value, filters=filters, limit=limit, offset=offset)
    except quality.QualityQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/assessment")
def quality_assessment(db: Db, drug: DrugFilter = None, study: StudyFilter = None) -> dict[str, Any]:
    """One molecule or one trial, each quality dimension separately with its rule and counts. No combined score."""
    if bool(drug) == bool(study):
        raise HTTPException(status_code=422, detail="Name exactly one molecule (drug) or one trial (study).")
    try:
        return quality.assessment(_facts(db), "drug" if drug else "study", drug or study or "")
    except quality.QualityQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
