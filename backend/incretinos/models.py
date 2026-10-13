"""Normalized evidence model.

The conceptual evidence graph (Drug → Study → Arm → Endpoint → Estimand → Result → Source)
is implemented with ordinary relational tables. Two conventions hold everywhere:

* A numeric field that the source did not report is NULL — never 0 — and is listed in
  ``missing_fields`` so the UI can say "not reported" instead of guessing.
* Every evidence-bearing row points at an ``EvidenceSource`` and carries an evidence
  tier and a verification status.

Reviewable evidence rows (results, analyses, adverse events, claims, mechanisms, indications,
exposure equivalences) are rebuilt by every load, so their primary keys are not an identity.
``ReviewableMixin`` gives each of them a stable ``evidence_key``, a content ``fingerprint`` and the
mechanical status beside the effective one; ``incretinos.review.lifecycle.reconcile`` fills these in
after every load. Human-authored state (``ReviewDecision``, ``EvidenceVersion``, ``AuditEvent``) is
keyed by evidence key and fingerprint, never by primary key, and survives a rebuild.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from .db import Base
from .enums import (
    ActorKind,
    ClaimKind,
    DataClassification,
    EstimandBasis,
    EstimandLabel,
    Origin,
    RegulatoryStatus,
    ReviewStatus,
    TriState,
    VerificationStatus,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class ReviewableMixin:
    """Columns the review lifecycle maintains on every reviewable evidence row.

    All are NULL on a freshly inserted row and are filled by ``review.lifecycle.reconcile``:

    * ``evidence_key``             stable identity (hash of the natural key; never the value, never a primary key)
    * ``fingerprint``              hash of the content a reviewer signs off on
    * ``base_verification_status`` the mechanical status, kept beside the effective ``verification_status``
    * ``source_snapshot_id``       the source snapshot the row was extracted from or anchored against
    * ``check_flags`` / ``risk_level``  automated findings about the row (explanations, never corrections)
    """

    evidence_key: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    base_verification_status: Mapped[str | None] = mapped_column(String(40))
    check_flags: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    risk_level: Mapped[str | None] = mapped_column(String(10))

    @declared_attr
    def source_snapshot_id(cls) -> Mapped[int | None]:  # noqa: N805 - SQLAlchemy declared attribute
        return mapped_column(
            ForeignKey("source_snapshot.id", name=f"fk_{cls.__tablename__}_source_snapshot", ondelete="SET NULL"), index=True
        )


# --------------------------------------------------------------------------- provenance


class EvidenceSource(Base):
    __tablename__ = "evidence_source"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(300), unique=True, index=True)  # e.g. ctgov:NCT04184622
    source_type: Mapped[str] = mapped_column(String(40))  # CTGOV | PUBMED | OPENFDA_LABEL | OPENFDA_FAERS | WEB | GUIDANCE
    tier: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    published_date: Mapped[str | None] = mapped_column(String(20))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    version_label: Mapped[str | None] = mapped_column(Text)
    license_note: Mapped[str | None] = mapped_column(Text)
    stores_full_text: Mapped[bool] = mapped_column(Boolean, default=False)
    classification: Mapped[str] = mapped_column(String(20), default=DataClassification.PUBLIC.value)
    external_ids: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    snapshots: Mapped[list[SourceSnapshot]] = relationship(
        back_populates="source", cascade="all, delete-orphan", order_by="SourceSnapshot.version_no"
    )


class SourceSnapshot(Base):
    __tablename__ = "source_snapshot"
    __table_args__ = (UniqueConstraint("source_id", "version_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("evidence_source.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version_label: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(40), default="application/json")
    # Full content only where licensing permits (US-government works, CC0). Otherwise NULL + excerpts.
    content: Mapped[str | None] = mapped_column(Text)
    excerpts: Mapped[list[str]] = mapped_column(JSON, default=list)
    byte_size: Mapped[int | None] = mapped_column(Integer)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)

    source: Mapped[EvidenceSource] = relationship(back_populates="snapshots")


class ChangeEvent(Base):
    __tablename__ = "change_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("evidence_source.id", ondelete="CASCADE"), index=True)
    from_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id"))
    to_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id"))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    summary: Mapped[str] = mapped_column(Text)
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    significance: Mapped[str] = mapped_column(String(10), default="LOW")  # HIGH | MEDIUM | LOW
    review_status: Mapped[str] = mapped_column(String(20), default=ReviewStatus.OPEN.value)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)

    source: Mapped[EvidenceSource] = relationship()


class ReviewQueueItem(Base):
    __tablename__ = "review_queue_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_type: Mapped[str] = mapped_column(String(40))  # SOURCE_CHANGE | CURATED_CLAIM | CONFLICT | RECONCILIATION
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(10), default="MEDIUM")
    status: Mapped[str] = mapped_column(String(20), default=ReviewStatus.OPEN.value, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewDecision(Base):
    """One recorded decision. Append-only: a later decision supersedes an earlier one, nothing is edited.

    Three kinds of row share this table. Gap triage and review-queue decisions fill only the first
    block. An evidence decision also names the exact evidence object and version it was made on
    (``evidence_type``, ``evidence_key``, ``fingerprint``) and the source version the reviewer saw, so
    it can be re-applied after the evidence rows are rebuilt and can be recognised as out of date when
    the evidence or its source changes. ``target_id`` on an evidence decision is the primary key at
    decision time and is informational only.
    """

    __tablename__ = "review_decision"
    __table_args__ = (Index("ix_review_decision_evidence", "evidence_type", "evidence_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    queue_item_id: Mapped[int | None] = mapped_column(ForeignKey("review_queue_item.id"))
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[int | None] = mapped_column(Integer)
    # Evidence: APPROVE | REJECT | REQUEST_INFO | FLAG_WRONG_ATTRIBUTION | PROPOSE_CORRECTION.
    # Queue: APPROVE | REJECT | NEEDS_INFO | REOPEN. Gap triage: the gap's new review status.
    decision: Mapped[str] = mapped_column(String(30))
    reviewer: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # -- evidence decisions only (NULL for gap and queue decisions)
    uid: Mapped[str | None] = mapped_column(String(36), unique=True, index=True)  # stable across databases (ledger export / import)
    evidence_type: Mapped[str | None] = mapped_column(String(20))
    evidence_key: Mapped[str | None] = mapped_column(String(64))
    natural_key: Mapped[str | None] = mapped_column(String(64), index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    evidence_version_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_version.id", name="fk_review_decision_evidence_version"))
    source_key: Mapped[str | None] = mapped_column(String(300))
    source_content_hash: Mapped[str | None] = mapped_column(String(64))
    source_snapshot_version: Mapped[int | None] = mapped_column(Integer)
    proposed_correction: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    actor_kind: Mapped[str | None] = mapped_column(String(20), default=ActorKind.HUMAN.value)
    attested: Mapped[bool | None] = mapped_column(Boolean, default=False)
    status_before: Mapped[str | None] = mapped_column(String(40))
    status_after: Mapped[str | None] = mapped_column(String(40))
    imported_from: Mapped[str | None] = mapped_column(String(80))  # ledger file hash when restored by `review import`


class EvidenceVersion(Base):
    """Every (evidence_type, evidence_key, fingerprint) ever seen, with what it said.

    Evidence rows are deleted and re-inserted by every load, so "what did this row say when it was
    reviewed?" cannot be answered from the evidence tables. This table can: it keeps the full field
    snapshot of every version, the source version it was seen with, and when. It is never rebuilt
    (``seed --reset`` preserves it) and rows are never deleted. The rows with ``is_current`` are also
    the index the review list is served from, so they carry a few denormalised filter columns.
    """

    __tablename__ = "evidence_version"
    __table_args__ = (
        UniqueConstraint("evidence_type", "evidence_key", "fingerprint", name="uq_evidence_version_identity"),
        Index("ix_evidence_version_current", "is_current", "evidence_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    evidence_type: Mapped[str] = mapped_column(String(20))
    evidence_key: Mapped[str] = mapped_column(String(64), index=True)
    natural_key: Mapped[str] = mapped_column(String(64), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    # The fingerprinted fields, human-readable: study, arm, endpoint, estimand, values, locator, quote, source.
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    label: Mapped[str] = mapped_column(Text)
    search_text: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str | None] = mapped_column(String(10))
    study_nct: Mapped[str | None] = mapped_column(String(11), index=True)
    study_name: Mapped[str | None] = mapped_column(Text)
    drug_slugs: Mapped[str | None] = mapped_column(Text)  # "|slug|slug|"
    source_key: Mapped[str | None] = mapped_column(String(300))
    source_type: Mapped[str | None] = mapped_column(String(40))
    tier: Mapped[str | None] = mapped_column(String(40))
    endpoint_category: Mapped[str | None] = mapped_column(String(30))
    missing_fields: Mapped[str | None] = mapped_column(Text)  # "|field|field|"
    # > 0 when the natural key alone did not identify the row and a tie-breaker was needed (a data-quality finding).
    collision_level: Mapped[int] = mapped_column(Integer, default=0)
    # Source version this evidence version was first seen with, and the one it was most recently seen with.
    source_content_hash: Mapped[str | None] = mapped_column(String(64))
    source_snapshot_version: Mapped[int | None] = mapped_column(Integer)
    last_source_content_hash: Mapped[str | None] = mapped_column(String(64))
    last_source_snapshot_version: Mapped[int | None] = mapped_column(Integer)
    mechanical_status: Mapped[str | None] = mapped_column(String(40))
    effective_status: Mapped[str | None] = mapped_column(String(40))
    risk_level: Mapped[str | None] = mapped_column(String(10))
    check_flags: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    # The version of the same evidence key that this one replaced, and what changed (field, before, after).
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_version.id", name="fk_evidence_version_supersedes"))
    changed_fields: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    current_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, index=True)
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class IngestionRun(Base):
    __tablename__ = "ingestion_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    adapter: Mapped[str] = mapped_column(String(40))
    mode: Mapped[str] = mapped_column(String(10))  # OFFLINE | LIVE
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- drugs


class Drug(Base):
    __tablename__ = "drug"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    code_name: Mapped[str | None] = mapped_column(String(40))
    brand_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    sponsor: Mapped[str | None] = mapped_column(Text)
    modality: Mapped[str | None] = mapped_column(Text)
    route: Mapped[str | None] = mapped_column(Text)
    is_combination: Mapped[bool] = mapped_column(Boolean, default=False)
    component_slugs: Mapped[list[str]] = mapped_column(JSON, default=list)
    tier_group: Mapped[str] = mapped_column(String(20), default="CORE")  # CORE | ADJACENT | COMPARATOR
    regulatory_status: Mapped[str] = mapped_column(String(20), default=RegulatoryStatus.UNKNOWN.value)
    regulatory_summary: Mapped[str | None] = mapped_column(Text)
    regulatory_source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"))
    regulatory_as_of: Mapped[str | None] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text)

    mechanisms: Mapped[list[DrugMechanism]] = relationship(back_populates="drug", cascade="all, delete-orphan")
    indications: Mapped[list[Indication]] = relationship(back_populates="drug", cascade="all, delete-orphan")
    formulations: Mapped[list[Formulation]] = relationship(back_populates="drug", cascade="all, delete-orphan")
    regulatory_source: Mapped[EvidenceSource | None] = relationship()


class DrugMechanism(ReviewableMixin, Base):
    __tablename__ = "drug_mechanism"

    id: Mapped[int] = mapped_column(primary_key=True)
    drug_id: Mapped[int] = mapped_column(ForeignKey("drug.id", ondelete="CASCADE"), index=True)
    target: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"))
    quote: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(String(40), default=VerificationStatus.PENDING_HUMAN_REVIEW.value)

    drug: Mapped[Drug] = relationship(back_populates="mechanisms")
    source: Mapped[EvidenceSource | None] = relationship()


class Indication(ReviewableMixin, Base):
    __tablename__ = "indication"

    id: Mapped[int] = mapped_column(primary_key=True)
    drug_id: Mapped[int] = mapped_column(ForeignKey("drug.id", ondelete="CASCADE"), index=True)
    brand: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    jurisdiction: Mapped[str] = mapped_column(String(20), default="US")
    approval_date: Mapped[str | None] = mapped_column(String(20))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"))
    quote: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(String(40), default=VerificationStatus.PENDING_HUMAN_REVIEW.value)

    drug: Mapped[Drug] = relationship(back_populates="indications")
    source: Mapped[EvidenceSource | None] = relationship()


class Formulation(Base):
    __tablename__ = "formulation"

    id: Mapped[int] = mapped_column(primary_key=True)
    drug_id: Mapped[int] = mapped_column(ForeignKey("drug.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(Text)
    dosage_form: Mapped[str | None] = mapped_column(Text)
    route: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(String(20))  # INVESTIGATIONAL | MARKETED
    notes: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"))

    drug: Mapped[Drug] = relationship(back_populates="formulations")
    doses: Mapped[list[Dose]] = relationship(back_populates="formulation", cascade="all, delete-orphan")
    source: Mapped[EvidenceSource | None] = relationship()


class Dose(Base):
    __tablename__ = "dose"

    id: Mapped[int] = mapped_column(primary_key=True)
    formulation_id: Mapped[int] = mapped_column(ForeignKey("formulation.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(100), unique=True)
    amount: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(Text, default="mg")
    frequency: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)  # STARTING | ESCALATION | MAINTENANCE

    formulation: Mapped[Formulation] = relationship(back_populates="doses")


class ExposureEquivalence(ReviewableMixin, Base):
    """A documented exposure relationship between two doses of different formulations.

    Rows exist only when a source states the relationship. There is deliberately no
    code path that derives an equivalence from the milligram amounts.
    """

    __tablename__ = "exposure_equivalence"

    id: Mapped[int] = mapped_column(primary_key=True)
    dose_a_id: Mapped[int] = mapped_column(ForeignKey("dose.id", ondelete="CASCADE"))
    dose_b_id: Mapped[int] = mapped_column(ForeignKey("dose.id", ondelete="CASCADE"))
    basis: Mapped[str] = mapped_column(String(40))
    statement: Mapped[str] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"))
    quote: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(String(40), default=VerificationStatus.PENDING_HUMAN_REVIEW.value)

    dose_a: Mapped[Dose] = relationship(foreign_keys=[dose_a_id])
    dose_b: Mapped[Dose] = relationship(foreign_keys=[dose_b_id])
    source: Mapped[EvidenceSource | None] = relationship()


# --------------------------------------------------------------------------- studies


class Study(Base):
    __tablename__ = "study"

    id: Mapped[int] = mapped_column(primary_key=True)
    nct_id: Mapped[str] = mapped_column(String(11), unique=True, index=True)
    acronym: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    brief_title: Mapped[str] = mapped_column(Text)
    official_title: Mapped[str | None] = mapped_column(Text)
    phase: Mapped[str | None] = mapped_column(Text)
    overall_status: Mapped[str | None] = mapped_column(String(40))
    study_type: Mapped[str | None] = mapped_column(String(40))
    allocation: Mapped[str | None] = mapped_column(String(40))
    masking: Mapped[str | None] = mapped_column(String(40))
    intervention_model: Mapped[str | None] = mapped_column(String(40))
    primary_purpose: Mapped[str | None] = mapped_column(String(40))
    enrollment: Mapped[int | None] = mapped_column(Integer)
    enrollment_type: Mapped[str | None] = mapped_column(String(20))
    start_date: Mapped[str | None] = mapped_column(String(20))
    primary_completion_date: Mapped[str | None] = mapped_column(String(20))
    primary_completion_type: Mapped[str | None] = mapped_column(String(20))
    completion_date: Mapped[str | None] = mapped_column(String(20))
    last_update_posted: Mapped[str | None] = mapped_column(String(20))
    results_first_posted: Mapped[str | None] = mapped_column(String(20))
    has_results: Mapped[bool] = mapped_column(Boolean, default=False)
    lead_sponsor: Mapped[str | None] = mapped_column(Text)
    conditions: Mapped[list[str]] = mapped_column(JSON, default=list)
    brief_summary: Mapped[str | None] = mapped_column(Text)
    location_count: Mapped[int | None] = mapped_column(Integer)
    countries: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Curated design descriptors (e.g. randomized_withdrawal, comparator_type, treatment_weeks).
    design_features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    program: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"))

    source: Mapped[EvidenceSource | None] = relationship()
    versions: Mapped[list[StudyVersion]] = relationship(back_populates="study", cascade="all, delete-orphan")
    arms: Mapped[list[StudyArm]] = relationship(back_populates="study", cascade="all, delete-orphan")
    population: Mapped[Population | None] = relationship(back_populates="study", cascade="all, delete-orphan", uselist=False)
    inclusion: Mapped[list[InclusionCriterion]] = relationship(cascade="all, delete-orphan", order_by="InclusionCriterion.ordinal")
    exclusion: Mapped[list[ExclusionCriterion]] = relationship(cascade="all, delete-orphan", order_by="ExclusionCriterion.ordinal")
    endpoints: Mapped[list[Endpoint]] = relationship(back_populates="study", cascade="all, delete-orphan")
    estimands: Mapped[list[Estimand]] = relationship(back_populates="study", cascade="all, delete-orphan")
    results: Mapped[list[OutcomeResult]] = relationship(back_populates="study", cascade="all, delete-orphan")
    analyses: Mapped[list[StatisticalAnalysis]] = relationship(back_populates="study", cascade="all, delete-orphan")
    adverse_events: Mapped[list[AdverseEvent]] = relationship(back_populates="study", cascade="all, delete-orphan")
    drug_links: Mapped[list[StudyDrug]] = relationship(back_populates="study", cascade="all, delete-orphan")
    publication_links: Mapped[list[StudyPublication]] = relationship(back_populates="study", cascade="all, delete-orphan")


class StudyDrug(Base):
    __tablename__ = "study_drug"
    __table_args__ = (UniqueConstraint("study_id", "drug_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"), index=True)
    drug_id: Mapped[int] = mapped_column(ForeignKey("drug.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(Text, default="INVESTIGATIONAL")  # INVESTIGATIONAL | COMPARATOR | BACKGROUND

    study: Mapped[Study] = relationship(back_populates="drug_links")
    drug: Mapped[Drug] = relationship()


class StudyVersion(Base):
    __tablename__ = "study_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id"))
    version_label: Mapped[str | None] = mapped_column(Text)  # registry "last update posted" date
    content_hash: Mapped[str] = mapped_column(String(64))
    overall_status: Mapped[str | None] = mapped_column(String(40))
    has_results: Mapped[bool] = mapped_column(Boolean, default=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)

    study: Mapped[Study] = relationship(back_populates="versions")


class StudyArm(Base):
    __tablename__ = "study_arm"

    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"), index=True)
    origin: Mapped[str] = mapped_column(String(10), default=Origin.REGISTRY.value)
    label: Mapped[str] = mapped_column(Text)
    norm_label: Mapped[str] = mapped_column(Text, index=True)
    arm_type: Mapped[str | None] = mapped_column(Text)  # EXPERIMENTAL | PLACEBO_COMPARATOR | ACTIVE_COMPARATOR ...
    description: Mapped[str | None] = mapped_column(Text)
    drug_slug: Mapped[str | None] = mapped_column(String(60))
    is_placebo: Mapped[bool] = mapped_column(Boolean, default=False)
    is_pooled: Mapped[bool] = mapped_column(Boolean, default=False)
    dose_amount: Mapped[float | None] = mapped_column(Float)
    dose_unit: Mapped[str | None] = mapped_column(String(20))
    dose_text: Mapped[str | None] = mapped_column(Text)
    formulation_key: Mapped[str | None] = mapped_column(String(80))
    n_randomized: Mapped[int | None] = mapped_column(Integer)
    interventions: Mapped[list[str]] = mapped_column(JSON, default=list)

    study: Mapped[Study] = relationship(back_populates="arms")


class Population(Base):
    __tablename__ = "population"

    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    diabetes_status: Mapped[str] = mapped_column(String(20), default="NOT_STATED")
    diabetes_basis: Mapped[str | None] = mapped_column(Text)
    sex: Mapped[str | None] = mapped_column(String(20))
    min_age: Mapped[str | None] = mapped_column(String(20))
    max_age: Mapped[str | None] = mapped_column(String(20))
    healthy_volunteers: Mapped[bool | None] = mapped_column(Boolean)
    # Baseline characteristics as reported: [{measure, group, value, unit, dispersion, param, source_location}]
    baseline: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Overall-population summary used by the comparability engine; each entry keeps its own source.
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"))

    study: Mapped[Study] = relationship(back_populates="population")


class InclusionCriterion(Base):
    __tablename__ = "inclusion_criterion"

    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)


class ExclusionCriterion(Base):
    __tablename__ = "exclusion_criterion"

    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)


class Endpoint(Base):
    __tablename__ = "endpoint"

    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"), index=True)
    origin: Mapped[str] = mapped_column(String(10), default=Origin.REGISTRY.value)
    # PROTOCOL = registered outcome; RESULTS = outcome measure with posted data; CURATED = from publication/release
    kind: Mapped[str] = mapped_column(String(10), default="PROTOCOL")
    title: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, default="NOT_STATED")
    category: Mapped[str] = mapped_column(String(30), default="OTHER", index=True)
    threshold: Mapped[float | None] = mapped_column(Float)  # responder threshold, e.g. 5 for ≥5 %
    unit: Mapped[str | None] = mapped_column(Text)
    timeframe_text: Mapped[str | None] = mapped_column(Text)
    timepoint_weeks: Mapped[float | None] = mapped_column(Float)
    description: Mapped[str | None] = mapped_column(Text)
    hierarchy_rank: Mapped[int | None] = mapped_column(Integer)
    multiplicity_controlled: Mapped[str] = mapped_column(String(12), default=TriState.NOT_STATED.value)
    is_pooled_arms: Mapped[bool] = mapped_column(Boolean, default=False)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"))
    source_location: Mapped[str | None] = mapped_column(Text)

    study: Mapped[Study] = relationship(back_populates="endpoints")
    source: Mapped[EvidenceSource | None] = relationship()


class Estimand(Base):
    __tablename__ = "estimand"

    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(Text, default=EstimandLabel.NOT_STATED.value)
    basis: Mapped[str] = mapped_column(String(40), default=EstimandBasis.NOT_DETERMINABLE.value)
    description: Mapped[str | None] = mapped_column(Text)
    population_text: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"))
    quote: Mapped[str | None] = mapped_column(Text)

    study: Mapped[Study] = relationship(back_populates="estimands")
    strategies: Mapped[list[IntercurrentEventStrategy]] = relationship(back_populates="estimand", cascade="all, delete-orphan")
    source: Mapped[EvidenceSource | None] = relationship()


class IntercurrentEventStrategy(Base):
    __tablename__ = "intercurrent_event_strategy"

    id: Mapped[int] = mapped_column(primary_key=True)
    estimand_id: Mapped[int] = mapped_column(ForeignKey("estimand.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(String(40))
    strategy: Mapped[str] = mapped_column(String(30))
    basis: Mapped[str] = mapped_column(String(40))
    quote: Mapped[str | None] = mapped_column(Text)

    estimand: Mapped[Estimand] = relationship(back_populates="strategies")


class OutcomeResult(ReviewableMixin, Base):
    __tablename__ = "outcome_result"

    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"), index=True)
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("endpoint.id", ondelete="CASCADE"), index=True)
    arm_id: Mapped[int | None] = mapped_column(ForeignKey("study_arm.id", ondelete="SET NULL"))
    estimand_id: Mapped[int | None] = mapped_column(ForeignKey("estimand.id", ondelete="SET NULL"))
    origin: Mapped[str] = mapped_column(String(10), default=Origin.REGISTRY.value)
    arm_label: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(20), default="ARM")  # ARM | POOLED | RANGE_ACROSS_ARMS
    class_title: Mapped[str | None] = mapped_column(Text)
    param_type: Mapped[str | None] = mapped_column(Text)
    value: Mapped[float | None] = mapped_column(Float)
    value_text: Mapped[str | None] = mapped_column(Text)  # exactly as reported
    value_low: Mapped[float | None] = mapped_column(Float)  # for range-only disclosures
    value_high: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(Text)
    dispersion_type: Mapped[str | None] = mapped_column(Text)
    dispersion_value: Mapped[float | None] = mapped_column(Float)
    ci_low: Mapped[float | None] = mapped_column(Float)
    ci_high: Mapped[float | None] = mapped_column(Float)
    ci_level: Mapped[float | None] = mapped_column(Float)
    n_analyzed: Mapped[int | None] = mapped_column(Integer)
    analysis_population: Mapped[str | None] = mapped_column(Text)
    analysis_method: Mapped[str | None] = mapped_column(Text)
    baseline_definition: Mapped[str | None] = mapped_column(Text)
    timepoint_weeks: Mapped[float | None] = mapped_column(Float)
    timeframe_text: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"), index=True)
    source_location: Mapped[str | None] = mapped_column(Text)
    locator_quote: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(String(40))
    verification_status: Mapped[str] = mapped_column(String(40))
    missing_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text)

    study: Mapped[Study] = relationship(back_populates="results")
    endpoint: Mapped[Endpoint] = relationship()
    arm: Mapped[StudyArm | None] = relationship()
    estimand: Mapped[Estimand | None] = relationship()
    source: Mapped[EvidenceSource | None] = relationship()


class StatisticalAnalysis(ReviewableMixin, Base):
    __tablename__ = "statistical_analysis"

    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"), index=True)
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("endpoint.id", ondelete="CASCADE"), index=True)
    estimand_id: Mapped[int | None] = mapped_column(ForeignKey("estimand.id", ondelete="SET NULL"))
    origin: Mapped[str] = mapped_column(String(10), default=Origin.REGISTRY.value)
    treatment_arm_id: Mapped[int | None] = mapped_column(ForeignKey("study_arm.id", ondelete="SET NULL"))
    comparator_arm_id: Mapped[int | None] = mapped_column(ForeignKey("study_arm.id", ondelete="SET NULL"))
    treatment_label: Mapped[str | None] = mapped_column(Text)
    comparator_label: Mapped[str | None] = mapped_column(Text)
    hypothesis_type: Mapped[str] = mapped_column(String(20), default="NOT_STATED")
    param_type: Mapped[str | None] = mapped_column(Text)
    estimate: Mapped[float | None] = mapped_column(Float)
    estimate_text: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(Text)
    ci_low: Mapped[float | None] = mapped_column(Float)
    ci_high: Mapped[float | None] = mapped_column(Float)
    ci_level: Mapped[float | None] = mapped_column(Float)
    ci_sides: Mapped[str | None] = mapped_column(Text)
    p_value_text: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str | None] = mapped_column(Text)
    ni_margin: Mapped[float | None] = mapped_column(Float)
    ni_margin_text: Mapped[str | None] = mapped_column(Text)
    ni_met: Mapped[str] = mapped_column(String(12), default=TriState.NOT_STATED.value)
    superiority_tested: Mapped[str] = mapped_column(String(12), default=TriState.NOT_STATED.value)
    superiority_met: Mapped[str] = mapped_column(String(12), default=TriState.NOT_STATED.value)
    multiplicity_controlled: Mapped[str] = mapped_column(String(12), default=TriState.NOT_STATED.value)
    timepoint_weeks: Mapped[float | None] = mapped_column(Float)
    comment: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"), index=True)
    source_location: Mapped[str | None] = mapped_column(Text)
    locator_quote: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(String(40))
    verification_status: Mapped[str] = mapped_column(String(40))
    missing_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text)

    study: Mapped[Study] = relationship(back_populates="analyses")
    endpoint: Mapped[Endpoint] = relationship()
    estimand: Mapped[Estimand | None] = relationship()
    treatment_arm: Mapped[StudyArm | None] = relationship(foreign_keys=[treatment_arm_id])
    comparator_arm: Mapped[StudyArm | None] = relationship(foreign_keys=[comparator_arm_id])
    source: Mapped[EvidenceSource | None] = relationship()


class AdverseEvent(ReviewableMixin, Base):
    __tablename__ = "adverse_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"), index=True)
    arm_id: Mapped[int | None] = mapped_column(ForeignKey("study_arm.id", ondelete="SET NULL"))
    origin: Mapped[str] = mapped_column(String(10), default=Origin.REGISTRY.value)
    arm_label: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(20), default="ARM")  # ARM | RANGE_ACROSS_ARMS | POOLED
    term: Mapped[str] = mapped_column(Text, index=True)
    organ_system: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(30), default="OTHER", index=True)
    measure_kind: Mapped[str] = mapped_column(String(40), index=True)
    is_serious: Mapped[bool] = mapped_column(Boolean, default=False)
    severity: Mapped[str | None] = mapped_column(Text)
    n_affected: Mapped[int | None] = mapped_column(Integer)
    n_at_risk: Mapped[int | None] = mapped_column(Integer)
    n_events: Mapped[int | None] = mapped_column(Integer)
    percent: Mapped[float | None] = mapped_column(Float)  # only when the source reports a percentage
    percent_low: Mapped[float | None] = mapped_column(Float)
    percent_high: Mapped[float | None] = mapped_column(Float)
    period: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"), index=True)
    source_location: Mapped[str | None] = mapped_column(Text)
    locator_quote: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(String(40))
    verification_status: Mapped[str] = mapped_column(String(40))
    missing_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text)

    study: Mapped[Study] = relationship(back_populates="adverse_events")
    arm: Mapped[StudyArm | None] = relationship()
    source: Mapped[EvidenceSource | None] = relationship()


# --------------------------------------------------------------------------- literature


class Publication(Base):
    __tablename__ = "publication"

    id: Mapped[int] = mapped_column(primary_key=True)
    pmid: Mapped[str | None] = mapped_column(String(12), unique=True, index=True)
    doi: Mapped[str | None] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(Text)
    journal: Mapped[str | None] = mapped_column(Text)
    pub_date: Mapped[str | None] = mapped_column(String(20))
    year: Mapped[int | None] = mapped_column(Integer)
    authors: Mapped[list[str]] = mapped_column(JSON, default=list)
    pub_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    databank_ncts: Mapped[list[str]] = mapped_column(JSON, default=list)
    abstract_sha256: Mapped[str | None] = mapped_column(String(64))
    abstract_chars: Mapped[int | None] = mapped_column(Integer)
    has_abstract_text_cached: Mapped[bool] = mapped_column(Boolean, default=False)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"))

    source: Mapped[EvidenceSource | None] = relationship()
    study_links: Mapped[list[StudyPublication]] = relationship(back_populates="publication", cascade="all, delete-orphan")


class StudyPublication(Base):
    __tablename__ = "study_publication"
    __table_args__ = (UniqueConstraint("study_id", "publication_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"), index=True)
    publication_id: Mapped[int] = mapped_column(ForeignKey("publication.id", ondelete="CASCADE"), index=True)
    bases: Mapped[list[str]] = mapped_column(JSON, default=list)  # LinkBasis values
    relation: Mapped[str] = mapped_column(String(20), default="RELATED")  # PRIMARY_RESULTS | RELATED
    confidence: Mapped[str] = mapped_column(String(10), default="MEDIUM")  # HIGH | MEDIUM | LOW

    study: Mapped[Study] = relationship(back_populates="publication_links")
    publication: Mapped[Publication] = relationship(back_populates="study_links")


# --------------------------------------------------------------------------- claims & gaps


class EvidenceClaim(ReviewableMixin, Base):
    """A non-numeric assertion (mechanism, status, design fact, limitation) with its support."""

    __tablename__ = "evidence_claim"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(120), unique=True)
    drug_id: Mapped[int | None] = mapped_column(ForeignKey("drug.id", ondelete="CASCADE"), index=True)
    study_id: Mapped[int | None] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"), index=True)
    topic: Mapped[str] = mapped_column(Text, index=True)
    statement: Mapped[str] = mapped_column(Text)
    claim_kind: Mapped[str] = mapped_column(String(20), default=ClaimKind.DOCUMENTED.value)
    tier: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"))
    locator: Mapped[str | None] = mapped_column(Text)
    quote: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(String(40), default=VerificationStatus.PENDING_HUMAN_REVIEW.value)
    origin: Mapped[str] = mapped_column(String(10), default=Origin.CURATED.value)

    drug: Mapped[Drug | None] = relationship()
    study: Mapped[Study | None] = relationship()
    source: Mapped[EvidenceSource | None] = relationship()


class EvidenceGap(Base):
    __tablename__ = "evidence_gap"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(160), unique=True)
    rule_id: Mapped[str] = mapped_column(String(60), index=True)
    drug_id: Mapped[int | None] = mapped_column(ForeignKey("drug.id", ondelete="CASCADE"), index=True)
    program: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), index=True)
    question: Mapped[str] = mapped_column(Text)
    available_evidence: Mapped[str] = mapped_column(Text)
    missing_evidence: Mapped[str] = mapped_column(Text)
    gap_nature: Mapped[str] = mapped_column(String(40))
    clinical_importance: Mapped[int] = mapped_column(Integer)  # 1–5, rule default; reviewer-editable
    evidence_certainty: Mapped[str] = mapped_column(String(20))  # HIGH | MODERATE | LOW | VERY_LOW
    tractability: Mapped[int] = mapped_column(Integer, default=3)  # 1–5: can it be addressed with obtainable data
    analytic_approach: Mapped[str] = mapped_column(Text)
    data_required: Mapped[str] = mapped_column(Text)
    source_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    related_ncts: Mapped[list[str]] = mapped_column(JSON, default=list)
    review_status: Mapped[str] = mapped_column(String(20), default=ReviewStatus.OPEN.value)
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    drug: Mapped[Drug | None] = relationship()


# --------------------------------------------------------------------------- safety & labels


class LabelRecord(Base):
    __tablename__ = "label_record"

    id: Mapped[int] = mapped_column(primary_key=True)
    drug_id: Mapped[int | None] = mapped_column(ForeignKey("drug.id", ondelete="CASCADE"), index=True)
    brand: Mapped[str] = mapped_column(Text, index=True)
    set_id: Mapped[str | None] = mapped_column(String(80))
    label_version: Mapped[str | None] = mapped_column(String(20))
    effective_time: Mapped[str | None] = mapped_column(String(20))
    sections: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"))

    drug: Mapped[Drug | None] = relationship()
    source: Mapped[EvidenceSource | None] = relationship()


class SpontaneousReportSeries(Base):
    """Counts of spontaneous reports. These are report counts — not incidence, not rates."""

    __tablename__ = "spontaneous_report_series"

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL drug_id = whole-database background totals used for disproportionality tables.
    drug_id: Mapped[int | None] = mapped_column(ForeignKey("drug.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # TOTAL | BY_REACTION | BY_QUARTER | BACKGROUND_REACTION
    query: Mapped[str] = mapped_column(Text)
    total: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    database_last_updated: Mapped[str | None] = mapped_column(String(20))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"))

    drug: Mapped[Drug | None] = relationship()
    source: Mapped[EvidenceSource | None] = relationship()


class DiscoveredTrial(Base):
    """Lightweight registry index used by the Trial Atlas for trials not ingested in depth."""

    __tablename__ = "discovered_trial"

    id: Mapped[int] = mapped_column(primary_key=True)
    nct_id: Mapped[str] = mapped_column(String(11), unique=True, index=True)
    brief_title: Mapped[str] = mapped_column(Text)
    acronym: Mapped[str | None] = mapped_column(Text)
    phase: Mapped[str | None] = mapped_column(Text)
    overall_status: Mapped[str | None] = mapped_column(String(40))
    has_results: Mapped[bool] = mapped_column(Boolean, default=False)
    enrollment: Mapped[int | None] = mapped_column(Integer)
    start_date: Mapped[str | None] = mapped_column(String(20))
    primary_completion_date: Mapped[str | None] = mapped_column(String(20))
    last_update_posted: Mapped[str | None] = mapped_column(String(20))
    lead_sponsor: Mapped[str | None] = mapped_column(Text)
    conditions: Mapped[list[str]] = mapped_column(JSON, default=list)
    interventions: Mapped[list[str]] = mapped_column(JSON, default=list)
    drug_slugs: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_source.id"))


# --------------------------------------------------------------------------- review guard rails

#: Evidence type name -> mapped class, for everything the review lifecycle can key, version and decide on.
REVIEWABLE_MODELS: dict[str, type[Base]] = {
    "result": OutcomeResult,
    "analysis": StatisticalAnalysis,
    "adverse_event": AdverseEvent,
    "claim": EvidenceClaim,
    "mechanism": DrugMechanism,
    "indication": Indication,
    "equivalence": ExposureEquivalence,
}

_HUMAN_ONLY = (VerificationStatus.HUMAN_VERIFIED.value, VerificationStatus.REJECTED.value)


class HumanStatusWriteError(PermissionError):
    """Raised when ordinary code tries to mark evidence human-verified or rejected."""


def _refuse_human_status(target: Any, value: Any, oldvalue: Any, initiator: Any) -> None:
    if value in _HUMAN_ONLY:
        raise HumanStatusWriteError(
            f"{type(target).__name__}.verification_status cannot be set to {value} here: that status exists only as the "
            "result of an attested human review decision and is written by incretinos.review.lifecycle alone."
        )


def _refuse_change(mapper: Any, connection: Any, target: Any) -> None:
    raise PermissionError(f"{type(target).__name__} rows are append-only: record a new row instead of changing or deleting one.")


for _model in REVIEWABLE_MODELS.values():
    # No loader, adapter, fixture or script can assign the two human statuses through the ORM. The review
    # lifecycle writes them with one audited statement after deriving them from the decision history.
    event.listen(_model.verification_status, "set", _refuse_human_status)
for _model in (ReviewDecision, AuditEvent):
    event.listen(_model, "before_update", _refuse_change)
    event.listen(_model, "before_delete", _refuse_change)
