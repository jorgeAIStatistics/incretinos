"""Source conflicts: groups of rows from different public sources that purport to report the same quantity.

Read-only. Every group is classified by ``engines.conflicts`` as a true conflict, an insufficient-information
difference, an explainable difference or a consistent report, with the fields that decide it quoted. The evidence
tier of each source is returned as context and is never part of the classification. Rows a reviewer rejected (or
holds after a rejection) are invisible here, as on every analytic surface (``incretinos.db``).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...engines import conflicts as engine
from ...enums import EndpointCategory
from ...models import Drug, Study

router = APIRouter(prefix="/api/conflicts", tags=["conflicts"])

Db = Annotated[Session, Depends(get_db)]
MAX_OFFSET = 1_000_000  # the same bound as api.main.Offset (main imports this module)
ConflictClass = Literal["TRUE_CONFLICT", "INSUFFICIENT_INFORMATION", "EXPLAINABLE_DIFFERENCE", "CONSISTENT"]
EvidenceType = Literal["result", "analysis", "adverse_event"]
assert set(ConflictClass.__args__) == set(engine.CLASSES)  # type: ignore[attr-defined]
assert set(EvidenceType.__args__) == set(engine.EVIDENCE_TYPES)  # type: ignore[attr-defined]


def _check_filters(db: Session, drug: str | None, study: str | None, endpoint_category: str | None) -> None:
    """A misspelt filter value is an error, never an empty answer that reads as 'no conflicts'."""
    if drug and db.scalar(select(Drug.id).where(Drug.slug == drug)) is None:
        raise HTTPException(status_code=422, detail=f"Unknown drug '{drug[:60]}'.")
    if study and db.scalar(select(Study.id).where(Study.nct_id == study.upper())) is None:
        raise HTTPException(status_code=422, detail=f"Unknown trial '{study[:20]}'.")
    if endpoint_category is not None and endpoint_category not in EndpointCategory._value2member_map_:
        raise HTTPException(status_code=422, detail=f"Unknown endpoint category '{endpoint_category[:30]}'.")


@router.get("")
def list_conflicts(
    db: Db,
    conflict_class: Annotated[ConflictClass | None, Query(alias="class", description="Classification of the group (its most action-requiring pair).")] = None,
    drug: Annotated[str | None, Query(max_length=60, description="Drug slug: trials in which the drug is not only a comparator.")] = None,
    study: Annotated[str | None, Query(pattern=r"^[Nn][Cc][Tt]\d{8}$")] = None,
    endpoint_category: Annotated[str | None, Query(max_length=30)] = None,
    evidence_type: EvidenceType | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
) -> dict[str, Any]:
    """Multi-source groups with their classification, filtered and paged. ``counts`` are per class under every filter except ``class``."""
    _check_filters(db, drug, study, endpoint_category)
    groups = engine.all_groups(db)
    scoped = [g for g in groups if (drug is None or drug in g.drugs) and (study is None or g.study["nct_id"] == study.upper())
              and (endpoint_category is None or g.category == endpoint_category) and (evidence_type is None or g.evidence_type == evidence_type)]
    counts = engine.group_counts(scoped)
    rows = [g for g in scoped if conflict_class is None or g.cls == conflict_class]
    rows.sort(key=lambda g: (engine.CLASSES.index(g.cls), g.study["nct_id"], engine.EVIDENCE_TYPES.index(g.evidence_type), g.category, g.title()["arm"], g.group_id))
    return {
        "total": len(rows), "limit": limit, "offset": offset, "rows": [g.summary() for g in rows[offset : offset + limit]],
        "counts": counts, "all_groups": len(groups), "class_labels": engine.CLASS_LABEL, "class_definitions": engine.CLASS_DEFINITION,
        "evidence_type_labels": engine.EVIDENCE_TYPE_LABEL, "categories": sorted({g.category for g in groups}), "method": engine.METHOD,
        "studies": sorted(({"nct_id": nct, "display_name": name} for nct, name in {(g.study["nct_id"], g.study["display_name"]) for g in groups}), key=lambda s: s["display_name"]),
        "drugs": sorted({d for g in groups for d in g.drugs}),
        "note": "Evidence tier is shown as context only; it never decides a classification or which value is right. Rows rejected in review are not included.",
    }


@router.get("/{group_id}")
def conflict_detail(group_id: Annotated[str, Path(pattern=engine.GROUP_ID_PATTERN)], db: Db) -> dict[str, Any]:
    """One group: the rows side by side, every pairwise comparison with the fields that decide it, and what to do next."""
    group = engine.find_group(db, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="No group of rows from several sources has this identifier now. The evidence may have changed or been rejected in review.")
    return group.detail()
