"""Anchored indirect comparison API (Bucher method).

Read-only calculations over stored between-arm analyses. Rejected evidence and evidence under a review hold are invisible to
the request session (``incretinos.db``), so neither can be chosen; everything else that cannot enter a calculation is listed
with the reason. A calculation is returned only when every requirement passes; otherwise the response is a refusal that
names each failed requirement. The estimate is always labelled as an anchored indirect estimate, never as a randomized
comparison or a ranking.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ...db import get_db
from ...engines import indirect
from ...enums import EndpointCategory

router = APIRouter(prefix="/api/indirect", tags=["indirect comparison"])
usage_log = logging.getLogger("incretinos.usage")

Db = Annotated[Session, Depends(get_db)]
# The same bound as api.main (DbId): an unbounded integer overflows the SQLite driver and the PostgreSQL INTEGER type.
MAX_DB_ID = 2**31 - 1
AnalysisId = Annotated[int, Field(ge=1, le=MAX_DB_ID)]
Category = Annotated[str | None, Query(max_length=40, description="Endpoint category, e.g. WEIGHT_PCT_CHANGE.")]
Slug = Annotated[str, Query(min_length=1, max_length=80)]


def _category(value: str | None) -> str | None:
    if value is not None and value not in EndpointCategory._value2member_map_:
        raise HTTPException(status_code=422, detail=f"Unknown endpoint category '{value}'.")
    return value


class AssessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The stored analysis of drug A versus the common comparator.
    a: AnalysisId
    #: The stored analysis of drug B versus the same comparator, in another trial.
    b: AnalysisId
    ci_level: float = Field(0.95, ge=0.5, le=0.999, description="Confidence level of the indirect interval (two-sided).")
    followup_tolerance_weeks: float = Field(0.0, ge=0.0, le=indirect.MAX_FOLLOW_UP_TOLERANCE_WEEKS,
                                            description="Largest accepted difference in assessment week. Default 0 (the same week); above 0 a justification is required.")
    followup_tolerance_justification: str | None = Field(None, max_length=600)


@router.get("/options")
def indirect_options(db: Db, category: Category = None) -> dict[str, Any]:
    """Drugs and common comparators with stored between-arm analyses (on one endpoint category when given)."""
    return indirect.options(db, _category(category))


@router.get("/candidates")
def indirect_candidates(db: Db, drug: Slug, comparator: Slug, category: Category = None) -> dict[str, Any]:
    """Stored analyses of one drug against one comparator ('placebo' or 'active:<drug>'), each with what would keep it out of a calculation."""
    rows = indirect.candidates(db, drug, comparator, _category(category))
    return {"total": len(rows), "limit": len(rows), "offset": 0, "rows": rows}


@router.get("/feasible")
def indirect_feasible(db: Db, category: Category = "WEIGHT_PCT_CHANGE") -> dict[str, Any]:
    """Every cross-drug pair of stored contrasts that share a comparator on one endpoint category, with the pairs that meet every requirement."""
    result = indirect.scan(db, _category(category) or "WEIGHT_PCT_CHANGE")
    return {key: value for key, value in result.items() if key != "pairs"}


@router.post("/assess")
def indirect_assess(body: AssessRequest, db: Db) -> dict[str, Any]:
    """Feasibility checklist for an anchored (Bucher) comparison of two stored analyses, and the estimate only when every requirement allows it."""
    try:
        result = indirect.assess(db, body.a, body.b, body.ci_level, body.followup_tolerance_weeks, body.followup_tolerance_justification)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except indirect.IndirectInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    usage_log.info("indirect.assess a=%s b=%s status=%s", body.a, body.b, result["status"])
    return result
