"""Evidence-level review API.

A reviewer decides on one exact evidence version: the row identified by its evidence key and the
content identified by its fingerprint. Reads are public and free of side effects (the product holds
public data only); recording a decision needs a reviewer credential and writes the decision, the new
status, the refreshed evidence gaps and an audit event in one transaction. The token is compared and
discarded: it is never logged, stored or echoed. With personal reviewer tokens the reviewer name is the
server's; under the shared development token it is the client's and every decision says so
(``identity_basis``).

Rejected evidence is visible here, labelled, because this is where it is inspected. It is hidden
from every analytic endpoint by the session-level rule in ``incretinos.db``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy.orm import Session

from ...config import ReviewerIdentity
from ...db import get_db, rejected_visible
from ...review import queries
from ...review.lifecycle import (
    REASON_TEXT,
    EvidenceNotFound,
    FingerprintMismatch,
    InvalidDecision,
    NotReconciled,
    record_decision,
)
from ...review.policy import (
    IDENTITY_LABEL,
    REVIEWABLE_TYPES,
    STATUS_LABEL,
    STORED_STATUSES,
    canonical_status,
    exclusion_reason,
    is_usable,
)
from ..auth import require_reviewer, reviewer_name

router = APIRouter(prefix="/api/review", tags=["review"])

Db = Annotated[Session, Depends(get_db)]
# The same bounds as api.main (PathId, Offset), repeated here because main imports this module: an unbounded integer
# overflows the SQLite driver and the PostgreSQL INTEGER type, and both would surface as a 500.
MAX_DB_ID = 2**31 - 1
MAX_OFFSET = 1_000_000
RowId = Annotated[int, Path(ge=1, le=MAX_DB_ID)]
EvidenceType = Literal["result", "analysis", "adverse_event", "claim", "mechanism", "indication", "equivalence"]
assert set(EvidenceType.__args__) == set(REVIEWABLE_TYPES)  # type: ignore[attr-defined]


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- response models


class DecisionBrief(_Out):
    decision_id: int
    decision: str
    reviewer: str
    decided_at: datetime
    fingerprint: str | None = None
    evidence_key: str | None = Field(None, description="The evidence key the decision was recorded under (another key when it followed a relabel).")


class EvidenceListItem(_Out):
    evidence_type: str
    id: int | None = Field(description="Primary key of the row now. It changes when evidence is rebuilt; use evidence_key for links that must last.")
    evidence_key: str
    fingerprint: str
    label: str
    value_summary: str | None = None
    study_nct: str | None = None
    study_name: str | None = None
    drugs: list[str] = []
    origin: str | None = None
    source_key: str | None = None
    source_type: str | None = None
    tier: str | None = None
    tier_label: str | None = None
    endpoint_category: str | None = None
    verification_status: str | None
    status_label: str | None = None
    base_verification_status: str | None = None
    usable: bool
    review_hold: bool = Field(False, description="Excluded from analytic use although not REJECTED: a rejection of an earlier version awaits a new decision.")
    exclusion_reason: str | None = None
    risk_level: str
    n_flags: int
    flag_codes: list[str] = []
    missing_fields: list[str] = []
    changed_since_last_seen: bool
    changed_since_review: bool
    changed_at: datetime | None = None
    first_seen_at: datetime | None = None
    open_requests: int
    last_decision: DecisionBrief | None = None
    identity_collision: bool = False


class EvidenceListResponse(_Out):
    total: int = Field(description="Number of rows matching the filters (not the page size).")
    all_rows: int = Field(description="Number of reviewable rows before filtering.")
    limit: int
    offset: int
    sort: str
    descending: bool
    rows: list[EvidenceListItem]
    facets: dict[str, dict[str, int]] = Field(description="Per filter: value -> number of rows that would match with the other filters kept.")


class SourceBlock(_Out):
    id: int
    key: str
    title: str
    url: str | None = None
    source_type: str
    tier: str
    tier_label: str | None = None
    publisher: str | None = None
    published_date: str | None = None
    retrieved_at: datetime | None = None
    content_hash: str | None = None
    snapshot_id: int | None = None
    snapshot_version: int | None = None
    latest_snapshot_version: int | None = None
    snapshot_retrieved_at: datetime | None = None
    version_label: str | None = None
    license_note: str | None = None
    stores_full_text: bool
    is_synthetic: bool = False


class AnchoringBlock(_Out):
    basis: Literal["FULL_TEXT", "VERIFIED_EXCERPT", "NOT_ANCHORED", "NO_QUOTE", "STRUCTURED_PAYLOAD"]
    basis_label: str
    quote: str | None = None
    locator: str | None = None
    context: str | None = Field(None, description="The quote with surrounding text from the stored source (whitespace-normalised), when full text is stored.")
    checked_against_snapshot_version: int | None = None
    note: str


class RegistryBlock(_Out):
    location: str
    resolved: bool
    fragment: Any | None = Field(None, description="The JSON at that location in the stored registry snapshot.")
    fragment_text: str | None = None
    fragment_truncated: bool = False
    labels: dict[str, Any] = {}
    note: str


class DecisionOut(_Out):
    id: int
    uid: str | None = None
    decision: str
    reviewer: str
    rationale: str
    attested: bool
    actor_kind: str | None = None
    created_at: datetime
    fingerprint: str | None = None
    on_current_version: bool
    decisive: bool
    source_key: str | None = None
    source_content_hash: str | None = None
    source_snapshot_version: int | None = None
    status_before: str | None = None
    status_after: str | None = None
    proposed_correction: dict[str, Any] | None = None
    recorded_under_other_key: bool = False
    imported: bool = False
    imported_from: str | None = None
    ledger_match: str | None = None
    identity_basis: str | None = Field(None, description="How the reviewer name was established: AUTHENTICATED_REVIEWER_TOKEN, SELF_DECLARED_SHARED_TOKEN or IMPORTED_UNRECORDED.")
    identity_label: str | None = None


class StatusBlock(_Out):
    verification_status: str
    label: str | None = None
    base_verification_status: str | None = None
    base_label: str | None = None
    usable: bool
    review_hold: bool = Field(False, description="Excluded from every analytic surface until a reviewer decides on the current version.")
    exclusion_reason: str | None = None
    reason: str | None = None
    reason_text: str | None = None
    derived_status: str | None = Field(None, description="What the decision history yields now; equals verification_status once reconciled.")
    derived_hold: bool | None = None
    decided_by: DecisionBrief | None = None
    open_requests: list[DecisionOut] = []


class FieldChange(_Out):
    field: str
    before: Any | None = None
    after: Any | None = None


class VersionOut(_Out):
    version_id: int
    fingerprint: str
    is_current: bool
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    current_since: datetime | None = None
    source_content_hash: str | None = None
    source_snapshot_version: int | None = None
    last_source_content_hash: str | None = None
    last_source_snapshot_version: int | None = None
    status_when_last_current: str | None = None
    supersedes_version_id: int | None = None
    changed_fields: list[FieldChange] = []
    snapshot: dict[str, Any] = {}


class ChangedSinceReview(_Out):
    decision_id: int
    decision: str
    reviewer: str
    decided_at: datetime
    reviewed_fingerprint: str | None = None
    current_fingerprint: str | None = None
    reviewed_version_available: bool
    diff: list[FieldChange] = []


class ArmBlock(_Out):
    role: Literal["arm", "treatment", "comparator"]
    label: str | None = None
    registered_label: str | None = None
    arm_type: str | None = None
    drug_slug: str | None = None
    dose_amount: float | None = None
    dose_unit: str | None = None
    dose_text: str | None = None
    formulation_key: str | None = None
    is_placebo: bool | None = None
    is_pooled: bool | None = None
    n_randomized: int | None = None
    origin: str | None = None
    linked: bool


class EndpointBlock(_Out):
    title: str
    category: str
    category_label: str | None = None
    threshold: float | None = None
    role: str
    unit: str | None = None
    timeframe_text: str | None = None
    timepoint_weeks: float | None = None
    hierarchy_rank: int | None = None
    multiplicity_controlled: str
    kind: str | None = None
    origin: str | None = None
    description: str | None = None
    is_pooled_arms: bool = False


class StrategyOut(_Out):
    event: str
    strategy: str
    basis: str
    quote: str | None = None


class EstimandBlock(_Out):
    label: str
    basis: str
    description: str | None = None
    population_text: str | None = None
    quote: str | None = None
    strategies: list[StrategyOut] = []


class StudyBlock(_Out):
    nct_id: str
    display_name: str
    brief_title: str | None = None
    phase: str | None = None
    overall_status: str | None = None
    allocation: str | None = None
    masking: str | None = None
    program: str | None = None
    design_features: dict[str, Any] = {}
    has_results: bool = False
    conditions: list[str] = []


class PopulationBlock(_Out):
    diabetes_status: str | None = None
    diabetes_basis: str | None = None
    sex: str | None = None
    min_age: str | None = None
    max_age: str | None = None
    description: str | None = None
    baseline_summary: dict[str, Any] = {}


class DrugBlock(_Out):
    slug: str
    name: str
    code_name: str | None = None
    regulatory_status: str | None = None
    sponsor: str | None = None
    route: str | None = None


class FormulationBlock(_Out):
    key: str
    name: str
    dosage_form: str | None = None
    route: str | None = None
    stage: str | None = None


class TimePoint(_Out):
    weeks: float | None = None
    text: str | None = None
    baseline_definition: str | None = None


class ConfidenceInterval(_Out):
    low: float | None = None
    high: float | None = None
    level: float | None = None
    sides: str | None = None
    status: Literal["REPORTED", "NOT_REPORTED"]


class RelatedRow(_Out):
    evidence_type: str
    id: int
    evidence_key: str | None = None
    relation: str
    label: str
    verification_status: str
    usable: bool
    tier: str | None = None
    source_key: str | None = None
    origin: str | None = None
    values: dict[str, Any] = {}


class CheckFlagOut(_Out):
    code: str
    severity: str
    explanation: str
    fields: list[str] = []


class IdentityCollision(_Out):
    level: int
    explanation: str | None = None


class AuditOut(_Out):
    id: int
    at: datetime
    actor: str
    action: str
    detail: dict[str, Any] = {}


class EvidenceDetail(_Out):
    evidence_type: str
    id: int
    evidence_key: str | None
    natural_key: str | None = None
    fingerprint: str | None
    label: str | None = None
    origin: str | None = None
    reconciled: bool = Field(description="False until the review lifecycle has keyed this row; decisions are refused until then.")
    status: StatusBlock
    source: SourceBlock | None = None
    anchoring: AnchoringBlock
    registry: RegistryBlock | None = None
    values: dict[str, Any] = Field(description="The extracted values exactly as stored; null means the source did not report it.")
    confidence_interval: ConfidenceInterval | None = None
    missing_fields: list[str] = []
    study: StudyBlock | None = None
    population: PopulationBlock | None = None
    drugs: list[DrugBlock] = []
    formulations: list[FormulationBlock] = []
    arms: list[ArmBlock] = []
    endpoint: EndpointBlock | None = None
    time_point: TimePoint | None = None
    estimand: EstimandBlock | None = None
    related_analyses: list[RelatedRow] = []
    same_quantity_other_sources: list[RelatedRow] = []
    check_flags: list[CheckFlagOut] = []
    risk_level: str
    identity_collision: IdentityCollision | None = None
    snapshot: dict[str, Any] = Field(description="The fields the fingerprint covers. Their dotted names are the fields a correction may name.")
    correctable_fields: list[str] = []
    versions: list[VersionOut] = []
    changed_since_review: ChangedSinceReview | None = None
    decisions: list[DecisionOut] = []
    decisions_total: int = Field(0, description="Every decision in the history; the list holds the most recent ones.")
    audit: list[AuditOut] = []


class StatusCount(_Out):
    status: str
    evidence_type: str
    tier: str | None = None
    drug: str | None = None
    n: int


class OpenRequest(_Out):
    evidence_type: str
    evidence_key: str
    label: str
    study_nct: str | None = None
    decision_id: int
    decision: str
    reviewer: str
    decided_at: datetime
    on_current_version: bool


class OrphanedDecision(_Out):
    decision_id: int
    uid: str | None = None
    evidence_type: str
    evidence_key: str
    fingerprint: str | None = None
    decision: str
    reviewer: str
    decided_at: datetime
    label: str | None = None
    study_nct: str | None = None
    applicable: bool = Field(True, description="False for an imported decision that matched no known evidence version: never applied.")
    excluded_value_may_be_in_use: bool = Field(False, description="A rejection that reaches no stored row: the value it excluded may be back under another identity.")


class AnchoredDecision(_Out):
    decision_id: int
    decision: str
    reviewer: str
    decided_at: datetime
    evidence_type: str
    from_evidence_key: str
    to_evidence_key: str
    to_label: str | None = None
    study_nct: str | None = None
    to_status: str | None = None
    review_hold: bool = False


class ChangedRow(_Out):
    evidence_type: str
    evidence_key: str
    label: str
    study_nct: str | None = None
    changed_at: datetime | None = None
    last_decision: DecisionBrief | None = None


class ReviewSummary(_Out):
    total: int
    by_status: dict[str, int]
    by_type: dict[str, dict[str, int]]
    counts: list[StatusCount] = Field(description="Status x evidence type x evidence tier x drug. A row linked to several drugs is counted under each.")
    status_labels: dict[str, str]
    decisions_recorded: int
    open_requests_total: int
    open_requests: list[OpenRequest]
    orphaned_decisions_total: int
    orphaned_rejections_total: int = Field(0, description="Orphaned rejections: the value each one excluded may be in use again. Each has a HIGH review-queue item.")
    orphaned_decisions: list[OrphanedDecision] = Field(description="Decisions whose evidence no longer exists. Kept as history; never deleted.")
    held_total: int = Field(0, description="Rows excluded from use because a rejection of an earlier version awaits a new decision.")
    decisions_carried_by_anchor_total: int = 0
    decisions_carried_by_anchor: list[AnchoredDecision] = Field([], description="Decisions that followed a relabel to the row now holding the same measurement.")
    changed_since_review_total: int
    changed_since_review: list[ChangedRow]
    changed_since_last_seen_total: int
    identity_collisions: int
    unreconciled_rows: dict[str, int]
    review_enabled: bool
    reviewer_identity: Literal["PERSONAL_TOKENS", "SHARED_TOKEN_SELF_DECLARED", "READ_ONLY"] = "READ_ONLY"


CorrectionValue = Annotated[str, Field(max_length=2000)] | Annotated[float, Field(allow_inf_nan=False)] | Annotated[StrictInt, Field(ge=-(2**53), le=2**53)] | StrictBool | None


class ProposedCorrection(Strict):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    changes: dict[Annotated[str, Field(min_length=1, max_length=200)], CorrectionValue] = Field(
        min_length=1, max_length=40, description="Field name (as in the row's snapshot) -> proposed value: text (2,000 characters at most), a finite number, true/false or null."
    )
    supporting_quote: str = Field(min_length=5, max_length=4000, description="The passage in the source that supports the correction, verbatim.")
    note: str | None = Field(None, max_length=2000)


class EvidenceDecisionRequest(Strict):
    decision: Literal["APPROVE", "REJECT", "REQUEST_INFO", "FLAG_WRONG_ATTRIBUTION", "PROPOSE_CORRECTION"]
    reviewer: str | None = Field(None, min_length=2, max_length=120, description="Required under the shared development token. With a personal reviewer token the name is the token's; omit it.")
    rationale: str = Field(min_length=1, max_length=4000, description="What was checked against the source. Boilerplate is refused.")
    attest_human_review: StrictBool = Field(False, description="Must be the JSON boolean true for APPROVE, REJECT and FLAG_WRONG_ATTRIBUTION: a person checked this evidence against its source.")
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$", description="The fingerprint of the version that was reviewed. A mismatch means the evidence changed.")
    proposed_correction: ProposedCorrection | None = None


class EvidenceDecisionResponse(_Out):
    decision_id: int
    uid: str
    evidence_type: str
    id: int
    evidence_key: str
    fingerprint: str
    decision: str
    status_before: str
    status_after: str
    status_label: str | None = None
    usable: bool
    review_hold: bool = False
    exclusion_reason: str | None = None
    reason: str
    reason_text: str | None = None
    reviewer: str
    identity_basis: str
    identity_label: str | None = None
    gaps_refreshed: int | None = Field(None, description="Number of evidence gaps after the refresh this decision triggered (null when the row's usability did not change).")


# --------------------------------------------------------------------------- endpoints


@router.get("/evidence", response_model=EvidenceListResponse)
def list_evidence(
    db: Db,
    evidence_type: Annotated[list[EvidenceType] | None, Query()] = None,
    status: Annotated[list[str] | None, Query(description="One or more verification statuses.")] = None,
    drug: Annotated[str | None, Query(max_length=60)] = None,
    nct: Annotated[str | None, Query(pattern=r"^[Nn][Cc][Tt]\d{8}$")] = None,
    source_type: Annotated[str | None, Query(max_length=40)] = None,
    source_key: Annotated[str | None, Query(max_length=300)] = None,
    tier: Annotated[str | None, Query(max_length=40)] = None,
    endpoint_category: Annotated[str | None, Query(max_length=30)] = None,
    risk_level: Annotated[list[Literal["HIGH", "MEDIUM", "LOW", "NONE"]] | None, Query()] = None,
    missing_field: Annotated[str | None, Query(max_length=60)] = None,
    origin: Literal["REGISTRY", "CURATED"] | None = None,
    changed_since_last_seen: bool | None = None,
    changed_since_review: bool | None = None,
    has_open_request: bool | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    sort: Literal["status", "risk", "study", "last_change"] = "status",
    descending: bool = False,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
) -> dict[str, Any]:
    """Reviewable evidence with filters, sorting, pagination and facet counts. Read-only."""
    statuses = None
    if status:
        statuses = [canonical_status(s) or s for s in status]
        unknown = sorted(set(statuses) - STORED_STATUSES)
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown verification status: {', '.join(unknown)}")
    filters = queries.EvidenceFilters(
        evidence_type=list(evidence_type) if evidence_type else None, status=statuses, drug=drug, nct=nct, source_type=source_type, source_key=source_key,
        tier=tier, endpoint_category=endpoint_category, risk_level=list(risk_level) if risk_level else None, missing_field=missing_field, origin=origin,
        changed_since_last_seen=changed_since_last_seen, changed_since_review=changed_since_review, has_open_request=has_open_request, q=q,
    )
    return queries.list_evidence(db, filters, sort=sort, descending=descending, limit=limit, offset=offset)


def _detail(db: Session, evidence_type: str, row: Any) -> dict[str, Any]:
    with rejected_visible(db):  # the dossier lists related and competing rows whatever their status
        detail = queries.evidence_detail(db, evidence_type, row)
    detail["correctable_fields"] = sorted(queries.identity.flatten(detail["snapshot"]))
    return detail


@router.get("/evidence/by-key/{evidence_type}/{evidence_key}", response_model=EvidenceDetail)
def evidence_by_key(evidence_type: EvidenceType, evidence_key: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")], db: Db) -> dict[str, Any]:
    """The same dossier addressed by evidence key: a link that survives the evidence being rebuilt."""
    row = queries.find_row(db, evidence_type, evidence_key=evidence_key)
    if row is None:
        raise HTTPException(status_code=404, detail="No evidence with this key exists now. If it was reviewed, its decisions are listed as orphaned in /api/review/summary.")
    return _detail(db, evidence_type, row)


@router.get("/evidence/{evidence_type}/{row_id}", response_model=EvidenceDetail)
def evidence_by_id(evidence_type: EvidenceType, row_id: RowId, db: Db) -> dict[str, Any]:
    """Everything a reviewer needs to decide on one evidence row. Read-only."""
    row = queries.find_row(db, evidence_type, row_id=row_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No {evidence_type} with id {row_id}")
    return _detail(db, evidence_type, row)


@router.post("/evidence/{evidence_type}/{row_id}/decisions", response_model=EvidenceDecisionResponse)
def decide_on_evidence(
    evidence_type: EvidenceType, row_id: RowId, body: EvidenceDecisionRequest, db: Db, identity: Annotated[ReviewerIdentity, Depends(require_reviewer)]
) -> dict[str, Any]:
    """Record a decision on this exact evidence version. One transaction: decision, status, dependent gaps, audit event."""
    reviewer = reviewer_name(identity, body.reviewer)
    try:
        recorded = record_decision(
            db, evidence_type=evidence_type, row_id=row_id, decision=body.decision, reviewer=reviewer, rationale=body.rationale,
            attested=body.attest_human_review, expected_fingerprint=body.expected_fingerprint,
            proposed_correction=body.proposed_correction.model_dump() if body.proposed_correction else None, identity_basis=identity.basis,
        )
    except EvidenceNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FingerprintMismatch as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"message": str(exc), "current_fingerprint": exc.current_fingerprint}) from exc
    except NotReconciled as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidDecision as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return {
        "decision_id": recorded.decision_id, "uid": recorded.uid, "evidence_type": evidence_type, "id": row_id, "evidence_key": recorded.evidence_key,
        "fingerprint": recorded.fingerprint, "decision": recorded.decision, "status_before": recorded.status_before, "status_after": recorded.status_after,
        "status_label": STATUS_LABEL.get(recorded.status_after), "usable": is_usable(recorded.status_after, recorded.hold_after),
        "review_hold": recorded.hold_after, "exclusion_reason": exclusion_reason(recorded.status_after, recorded.hold_after), "reason": recorded.reason,
        "reason_text": REASON_TEXT.get(recorded.reason, "Recorded as an open request; the verification status is unchanged."),
        "reviewer": reviewer, "identity_basis": recorded.identity_basis, "identity_label": IDENTITY_LABEL.get(recorded.identity_basis),
        "gaps_refreshed": recorded.gaps_refreshed,
    }


@router.get("/summary", response_model=ReviewSummary)
def review_summary(db: Db) -> dict[str, Any]:
    """Counts by status, type, tier and drug; open requests; orphaned decisions; rows changed since review."""
    from ...config import get_settings

    settings = get_settings()
    return {**queries.summarize(db), "review_enabled": settings.review_enabled, "reviewer_identity": settings.reviewer_identity_mode}
