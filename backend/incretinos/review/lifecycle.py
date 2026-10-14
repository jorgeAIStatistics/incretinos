"""Evidence-level review lifecycle: decisions, effective status, and the reconcile pass.

The model in one paragraph
--------------------------
Every reviewable row has a stable ``evidence_key`` and a content ``fingerprint`` (see
``review.identity``). A review decision is an append-only record attached to one
(evidence type, evidence key, fingerprint) and to the source version the reviewer saw. The status a
row shows is never stored as a fact of its own: it is *derived*, by ``effective_status``, from the
row's mechanical status, its decision history, its current fingerprint and the state of its source.
``reconcile`` re-derives it for every row after every load, which is why a decision survives the
rows being deleted and re-inserted, why a rejected value cannot come back by reseeding, and why an
approval stops counting the moment the approved content changes.

Writers of HUMAN_VERIFIED and REJECTED
--------------------------------------
Only this module writes them, in two statements. ``_write_rows`` stores what ``effective_status``
returned, and ``effective_status`` returns one of the two statuses only for a decision that passes
``policy.is_decisive`` (recorded by a human actor, with the attestation flag) on the row's current
fingerprint. ``hold_rejections`` re-hides rows whose latest decisive decision is a rejection while a
load is still in progress; it can only write REJECTED and only where such a decision exists. The ORM
refuses any other assignment (``models.HumanStatusWriteError``) and validation rule V16 re-derives
the status of every such row from the ledger.
"""

from __future__ import annotations

import importlib
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, insert, select, update
from sqlalchemy.orm import Session

from ..db import INCLUDE_REJECTED
from ..enums import ActorKind, ReviewDecisionKind, ReviewStatus, VerificationStatus
from ..ingestion.snapshots import audit, enqueue_review
from ..models import (
    REVIEWABLE_MODELS,
    ChangeEvent,
    EvidenceSource,
    EvidenceVersion,
    Population,
    ReviewDecision,
    ReviewQueueItem,
    SourceSnapshot,
    utcnow,
)
from . import identity
from .anchoring import ANCHOR_CACHE, STRUCTURED_PAYLOAD, AnchorResult, anchor
from .identity import EvidenceRecord, SnapshotRef
from .policy import (
    DECISION_KINDS,
    MECHANICAL_STATUSES,
    REJECTING_DECISIONS,
    REQUEST_DECISIONS,
    REVIEWABLE_TYPES,
    CheckFlag,
    canonical_status,
    is_decisive,
    rationale_problem,
    reviewer_problem,
    risk_level,
)

HUMAN_VERIFIED = VerificationStatus.HUMAN_VERIFIED.value
REJECTED = VerificationStatus.REJECTED.value
RECHECK = VerificationStatus.SOURCE_CHANGED_RECHECK_REQUIRED.value
MACHINE_EXTRACTED = VerificationStatus.MACHINE_EXTRACTED.value
QUOTE_ANCHORED = VerificationStatus.QUOTE_ANCHORED.value
PENDING = VerificationStatus.PENDING_HUMAN_REVIEW.value

# Reason codes returned with every derived status.
NO_DECISION = "NO_DECISION"
APPROVED = "APPROVED"
APPROVAL_CARRIED_FORWARD = "APPROVAL_CARRIED_FORWARD"
SUPPORT_NOT_FOUND = "SUPPORT_NOT_FOUND"
CHANGED_SINCE_APPROVAL = "EVIDENCE_CHANGED_SINCE_APPROVAL"
REJECTED_IN_REVIEW = "REJECTED_IN_REVIEW"
CHANGED_SINCE_REJECTION = "EVIDENCE_CHANGED_SINCE_REJECTION"

REASON_TEXT = {
    NO_DECISION: "No attested human decision exists for this evidence; the status is mechanical.",
    APPROVED: "Approved by a reviewer on this exact content and source version.",
    APPROVAL_CARRIED_FORWARD: "Approved by a reviewer; the source has a newer version and the support for this exact content is intact.",
    SUPPORT_NOT_FOUND: "Approved by a reviewer, but the source changed afterwards and the quote can no longer be found in its current version.",
    CHANGED_SINCE_APPROVAL: "A reviewer approved an earlier version; the stored content has changed since.",
    REJECTED_IN_REVIEW: "Rejected by a reviewer on this exact content. Excluded from every comparison, summary and report.",
    CHANGED_SINCE_REJECTION: "A reviewer rejected an earlier version; the stored content has changed since and must be looked at again.",
}

CHANGE_ENTRY_LIMIT = 400  # field-level entries listed in one change event; the totals are always exact
REPORT_LIST_LIMIT = 200


# --------------------------------------------------------------------------- pure core


@dataclass(frozen=True, slots=True)
class DecisionFact:
    """The part of a recorded decision that status derivation looks at."""

    decision: str
    fingerprint: str | None
    actor_kind: str | None
    attested: bool | None
    decided_at: datetime
    id: int = 0
    evidence_type: str = ""
    evidence_key: str = ""
    natural_key: str | None = None
    source_content_hash: str | None = None
    reviewer: str = ""
    uid: str | None = None

    @property
    def decisive(self) -> bool:
        return is_decisive(self.decision, self.actor_kind, self.attested)


@dataclass(frozen=True, slots=True)
class StatusOutcome:
    status: str
    reason: str
    decision: DecisionFact | None = None  # the decisive decision the status rests on
    open_requests: tuple[DecisionFact, ...] = ()

    @property
    def carried_forward(self) -> bool:
        return self.reason == APPROVAL_CARRIED_FORWARD


def effective_status(
    *,
    mechanical: str,
    decisions: Sequence[DecisionFact],
    fingerprint: str,
    structured: bool,
    source_content_hash: str | None,
) -> StatusOutcome:
    """Derive the status a row must show. Pure: same inputs, same answer, no database.

    Inputs
        mechanical           the status the row has without any decision (MACHINE_EXTRACTED,
                             QUOTE_ANCHORED or PENDING_HUMAN_REVIEW). For a curated row it is the result
                             of anchoring its quote against the *current* source version.
        decisions            the decision history of this evidence object, any order.
        fingerprint          the row's current content fingerprint.
        structured           True for rows re-extracted from a structured payload on every load
                             (registry rows): an unchanged fingerprint then means unchanged support.
        source_content_hash  content hash of the current version of the row's source.

    Only decisions that pass ``policy.is_decisive`` count: APPROVE, REJECT or FLAG_WRONG_ATTRIBUTION,
    recorded by a HUMAN actor with the attestation flag. "Latest" below means the most recent such
    decision; every other decision is ignored for the status.

    Truth table (one test per row in tests/test_review_lifecycle.py)

      #  latest decisive decision   made on           source since decision   support now            -> status                            reason
      1  none                       -                 -                       -                      -> the mechanical status             NO_DECISION
      2  APPROVE                    this fingerprint  same version            -                      -> HUMAN_VERIFIED                    APPROVED
      3  APPROVE                    this fingerprint  newer version           structured row         -> HUMAN_VERIFIED                    APPROVAL_CARRIED_FORWARD
      4  APPROVE                    this fingerprint  newer version           quote still anchored   -> HUMAN_VERIFIED                    APPROVAL_CARRIED_FORWARD
      5  APPROVE                    this fingerprint  newer version           quote not found        -> SOURCE_CHANGED_RECHECK_REQUIRED   SUPPORT_NOT_FOUND
      6  APPROVE                    older fingerprint any                     any                    -> SOURCE_CHANGED_RECHECK_REQUIRED   EVIDENCE_CHANGED_SINCE_APPROVAL
      7  REJECT / FLAG_WRONG_ATTR.  this fingerprint  any                     any                    -> REJECTED                          REJECTED_IN_REVIEW
      8  REJECT / FLAG_WRONG_ATTR.  older fingerprint any                     any                    -> SOURCE_CHANGED_RECHECK_REQUIRED   EVIDENCE_CHANGED_SINCE_REJECTION
      9  REQUEST_INFO / PROPOSE_CORRECTION, whenever made                                            -> no effect on the status; listed in ``open_requests``
                                                                                                        while newer than the latest decisive decision
     10  APPROVE / REJECT / FLAG by a non-human actor, or without attestation                        -> no effect at all (row 1 applies if nothing else exists)

    Row 4 is what "quote still anchored" means for a curated row: its quote is found in the current
    stored text or verified excerpts of the source, i.e. ``mechanical`` is QUOTE_ANCHORED. A curated
    row that a reviewer approved although it could not be anchored mechanically keeps its approval
    only while the source version the reviewer saw is the current one (row 2); once the source
    changes there is nothing mechanical to carry the approval forward (row 5).

    A rejected row stays rejected whatever happens to the source (row 7): only a change of the stored
    content itself (row 8) or a later decision ends a rejection, and row 8 never returns the row to a
    mechanical status silently.
    """
    mechanical = canonical_status(mechanical) or PENDING
    if mechanical not in MECHANICAL_STATUSES:
        raise ValueError(f"'{mechanical}' is not a mechanical status")
    ordered = sorted(decisions, key=lambda d: (d.decided_at, d.id))
    latest: DecisionFact | None = None
    for candidate in ordered:
        if candidate.decisive:
            latest = candidate
    requests = tuple(
        d for d in ordered if d.decision in REQUEST_DECISIONS and (latest is None or (d.decided_at, d.id) > (latest.decided_at, latest.id))
    )
    if latest is None:
        return StatusOutcome(mechanical, NO_DECISION, None, requests)
    on_current = latest.fingerprint == fingerprint
    if latest.decision in REJECTING_DECISIONS:
        if on_current:
            return StatusOutcome(REJECTED, REJECTED_IN_REVIEW, latest, requests)
        return StatusOutcome(RECHECK, CHANGED_SINCE_REJECTION, latest, requests)
    if not on_current:
        return StatusOutcome(RECHECK, CHANGED_SINCE_APPROVAL, latest, requests)
    if latest.source_content_hash == source_content_hash:
        return StatusOutcome(HUMAN_VERIFIED, APPROVED, latest, requests)
    if structured or mechanical == QUOTE_ANCHORED:
        return StatusOutcome(HUMAN_VERIFIED, APPROVAL_CARRIED_FORWARD, latest, requests)
    return StatusOutcome(RECHECK, SUPPORT_NOT_FOUND, latest, requests)


def attach_decisions(
    rows: Iterable[tuple[str, str, str, str]], decisions: Sequence[DecisionFact]
) -> tuple[dict[tuple[str, str], tuple[DecisionFact, ...]], list[DecisionFact]]:
    """Give every live row its decision history; return the decisions that belong to no live row.

    ``rows`` are (evidence_type, evidence_key, natural_key, fingerprint). A decision belongs to the
    row that has its evidence key. A decision whose key no longer exists is *detached*; it is looked
    at again through the natural key, because an evidence key changes without the evidence changing
    when rows that shared a natural key are added or removed around it (see ``identity``):

    * a detached decision made on a row's current fingerprint is that row's decision (same quantity,
      same content, new key), so an approved or rejected row keeps its status when it is re-keyed;
    * a row with no decision of its own inherits the other detached decisions of its natural key:
      they were made on an earlier version of the same quantity, so the row needs a recheck instead of
      silently returning to a mechanical status.

    Detached decisions that no row takes are orphans: reported, never deleted.
    """
    rows = list(rows)
    live = {(evidence_type, key) for evidence_type, key, _, _ in rows}
    own: dict[tuple[str, str], list[DecisionFact]] = defaultdict(list)
    detached: dict[tuple[str, str | None], list[DecisionFact]] = defaultdict(list)
    for decision in decisions:
        if (decision.evidence_type, decision.evidence_key) in live:
            own[(decision.evidence_type, decision.evidence_key)].append(decision)
        else:
            detached[(decision.evidence_type, decision.natural_key)].append(decision)
    attached: dict[tuple[str, str], tuple[DecisionFact, ...]] = {}
    taken: set[int] = set()
    for evidence_type, key, natural_key, fingerprint in rows:
        mine = own.get((evidence_type, key), [])
        loose = detached.get((evidence_type, natural_key)) if natural_key else None
        inherited: list[DecisionFact] = []
        if loose:
            same_content = [d for d in loose if d.fingerprint == fingerprint]
            inherited = same_content if (mine or same_content) else list(loose)
            taken.update(id(d) for d in inherited)
        if mine or inherited:
            attached[(evidence_type, key)] = tuple(sorted([*mine, *inherited], key=lambda d: (d.decided_at, d.id)))
    orphans = [d for group in detached.values() for d in group if id(d) not in taken]
    return attached, sorted(orphans, key=lambda d: (d.decided_at, d.id))


# --------------------------------------------------------------------------- reading the ledger


def load_decision_facts(session: Session) -> list[DecisionFact]:
    d = ReviewDecision
    rows = session.execute(
        select(d.id, d.decision, d.fingerprint, d.actor_kind, d.attested, d.created_at, d.evidence_type, d.evidence_key, d.natural_key, d.source_content_hash, d.reviewer, d.uid)
        .where(d.evidence_type.is_not(None), d.evidence_key.is_not(None))
        .order_by(d.created_at, d.id)
    ).all()
    return [
        DecisionFact(decision=r.decision, fingerprint=r.fingerprint, actor_kind=r.actor_kind, attested=r.attested, decided_at=r.created_at, id=r.id,
                     evidence_type=r.evidence_type, evidence_key=r.evidence_key, natural_key=r.natural_key, source_content_hash=r.source_content_hash,
                     reviewer=r.reviewer, uid=r.uid)
        for r in rows
    ]


def identity_flags(record: EvidenceRecord) -> list[CheckFlag]:
    """Data-quality findings about a row's identity: its natural key did not single it out."""
    if record.collision_level == 0:
        return []
    n, noun = record.collision_size, record.evidence_type.replace("_", " ")
    where = ("endpoint", "treatment", "comparator") if record.evidence_type == "analysis" else ("endpoint", "arm")
    if record.collision_level == 1:
        return [CheckFlag(
            "IDENTITY_SHARED_NATURAL_KEY", "MEDIUM",
            f"{n} stored {noun} rows describe the same quantity from the same source (same study, endpoint, arm, time point and estimand) and are told "
            "apart only by their locator quote. Check that each value is attached to the right endpoint, threshold and arm.", where,
        )]
    if record.collision_level == 2:
        return [CheckFlag(
            "IDENTITY_DISTINGUISHED_BY_VALUE_ONLY", "HIGH",
            f"{n} stored {noun} rows share every label and the same locator quote and differ only in their values: nothing stored says which "
            "quantity each value is.", where,
        )]
    return [CheckFlag("IDENTITY_EXACT_DUPLICATE", "HIGH", f"This {noun} row is stored more than once with identical content.", ())]


# --------------------------------------------------------------------------- reconcile


@dataclass
class ReconcileReport:
    rows: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    statuses: dict[str, int] = field(default_factory=dict)
    new_versions: int = 0
    first_seen_keys: int = 0
    rows_updated: int = 0
    disappeared_keys: int = 0
    changed_total: int = 0
    value_changes: int = 0
    changed: list[dict[str, Any]] = field(default_factory=list)
    transitions_total: int = 0
    transitions: list[dict[str, Any]] = field(default_factory=list)
    carried_forward: list[dict[str, Any]] = field(default_factory=list)
    orphaned_decisions: list[dict[str, Any]] = field(default_factory=list)
    open_requests: int = 0
    key_collisions: dict[str, int] = field(default_factory=dict)
    change_event_ids: list[int] = field(default_factory=list)
    retired_queue_items: int = 0
    population_entries_reanchored: int = 0
    checks: str = "NOT_INSTALLED"
    checks_error: str | None = None
    seconds: float = 0.0

    @property
    def changed_anything(self) -> bool:
        return bool(self.new_versions or self.rows_updated or self.transitions_total or self.disappeared_keys or self.carried_forward
                    or self.retired_queue_items or self.population_entries_reanchored)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def summary(self) -> dict[str, Any]:
        """The counts, without the per-row lists (for audit events and ingest reports)."""
        return {
            "rows": self.rows, "statuses": self.statuses, "new_versions": self.new_versions, "first_seen_keys": self.first_seen_keys,
            "rows_updated": self.rows_updated, "changed": self.changed_total, "value_changes": self.value_changes, "transitions": self.transitions_total,
            "carried_forward": len(self.carried_forward), "orphaned_decisions": len(self.orphaned_decisions), "open_requests": self.open_requests,
            "disappeared_keys": self.disappeared_keys, "key_collisions": self.key_collisions, "change_events": self.change_event_ids,
            "retired_queue_items": self.retired_queue_items, "checks": self.checks, "seconds": self.seconds,
        }


@dataclass(slots=True)
class _Version:
    id: int
    evidence_type: str
    evidence_key: str
    natural_key: str
    fingerprint: str
    is_current: bool
    effective_status: str | None
    mechanical_status: str | None
    risk_level: str | None
    check_flags: list[dict[str, Any]] | None
    last_source_content_hash: str | None
    last_source_snapshot_version: int | None
    label: str
    study_nct: str | None
    study_name: str | None
    drug_slugs: str | None
    missing_fields: str | None
    endpoint_category: str | None
    tier: str | None
    source_type: str | None
    source_key: str | None
    collision_level: int
    last_seen_at: datetime | None


_VERSION_COLUMNS = tuple(_Version.__dataclass_fields__)  # type: ignore[attr-defined]


def _load_versions(session: Session) -> list[_Version]:
    columns = [getattr(EvidenceVersion, name) for name in _VERSION_COLUMNS]
    return [_Version(*row) for row in session.execute(select(*columns)).all()]


def _packed(values: Iterable[str]) -> str | None:
    items = [v for v in values if v]
    return "|" + "|".join(items) + "|" if items else None


def retire_legacy_queue_items(session: Session) -> int:
    """Close queue items of the retired CURATED_CLAIM type: a pending evidence row is the review item now.

    They pointed at a source, not at the evidence under review. They are kept (with any decision
    recorded on them) as history and can no longer be decided.
    """
    result = session.execute(
        update(ReviewQueueItem)
        .where(ReviewQueueItem.item_type == "CURATED_CLAIM", ReviewQueueItem.status.in_([ReviewStatus.OPEN.value, ReviewStatus.IN_REVIEW.value]))
        .values(status=ReviewStatus.RETIRED.value)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


def _mechanical_statuses(session: Session, records: list[EvidenceRecord]) -> list[AnchorResult]:
    """Mechanical status of every record; curated quotes are anchored against the newest source snapshot."""
    machine = AnchorResult(MACHINE_EXTRACTED, STRUCTURED_PAYLOAD)
    needed = sorted({r.snapshot_ref.id for r in records if not r.is_registry and r.snapshot_ref is not None and r.quote})
    excerpts: dict[int, tuple[str, ...]] = {}
    for start in range(0, len(needed), 400):
        chunk = needed[start : start + 400]
        for snapshot_id, items in session.execute(select(SourceSnapshot.id, SourceSnapshot.excerpts).where(SourceSnapshot.id.in_(chunk))):
            excerpts[snapshot_id] = tuple(items or ())
    texts: dict[int, str | None] = {}

    def text_of(ref: SnapshotRef) -> str | None:
        if ref.id not in texts:
            texts[ref.id] = session.scalar(select(SourceSnapshot.content).where(SourceSnapshot.id == ref.id)) if ref.has_content else None
        return texts[ref.id]

    out: list[AnchorResult] = []
    for record in records:
        if record.is_registry:
            out.append(machine)
        elif record.snapshot_ref is None:
            out.append(anchor(record.quote, None, ()))
        else:
            ref = record.snapshot_ref
            out.append(ANCHOR_CACHE.get(ref.content_hash, excerpts.get(ref.id, ()), record.quote, lambda ref=ref: text_of(ref)))
    return out


def _reanchor_population_summaries(session: Session, snapshots: dict[int, SnapshotRef]) -> int:
    """Curated baseline entries inside ``population.summary`` carry a mechanical status too: keep it current."""
    source_ids = dict(session.execute(select(EvidenceSource.key, EvidenceSource.id)).all())
    changed = 0
    for population in session.scalars(select(Population)):
        summary = population.summary or {}
        updated: dict[str, Any] | None = None
        for name, entry in summary.items():
            if not isinstance(entry, dict) or entry.get("origin") != "CURATED" or "verification" not in entry:
                continue
            ref = snapshots.get(source_ids.get(entry.get("source_key"), -1))
            if ref is None:
                status = anchor(entry.get("quote"), None, ()).status
            else:
                snap = session.get(SourceSnapshot, ref.id)
                status = ANCHOR_CACHE.get(ref.content_hash, tuple(snap.excerpts or ()), entry.get("quote"), lambda snap=snap: snap.content).status
            if entry.get("verification") != status:
                updated = updated if updated is not None else dict(summary)
                updated[name] = {**entry, "verification": status}
                changed += 1
        if updated is not None:
            population.summary = updated
    return changed


def _automated_flags(session: Session, report: ReconcileReport, run_checks: bool) -> dict[tuple[str, int], list[CheckFlag]] | None:
    """Flags from ``review.checks.evaluate_all`` when that module is installed; None when they are unavailable."""
    if not run_checks:
        report.checks = "SKIPPED"
        return None
    module = f"{__package__}.checks"  # written on another track; optional
    try:
        checks = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name != module:
            raise
        report.checks = "NOT_INSTALLED"
        return {}
    evaluate = getattr(checks, "evaluate_all", None)
    if evaluate is None:
        report.checks = "NOT_INSTALLED"
        return {}
    try:
        with session.begin_nested():
            found = evaluate(session) or {}
    except Exception as exc:  # a failing check must not block a load; it is reported and audited instead
        report.checks, report.checks_error = "FAILED", f"{type(exc).__name__}: {exc}"[:500]
        audit(session, "review.checks_failed", target_type="review", error=report.checks_error)
        return None
    report.checks = "RAN"
    return found


def _flag_dicts(flags: Iterable[Any]) -> list[dict[str, Any]]:
    out = [f.as_dict() if hasattr(f, "as_dict") else dict(f) for f in flags]
    return sorted(out, key=lambda f: ({"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(str(f.get("severity")), 9), str(f.get("code"))))


def _risk(flags: list[dict[str, Any]]) -> str:
    return risk_level([CheckFlag(str(f.get("code")), str(f.get("severity")), "") for f in flags])


def reconcile(session: Session, *, run_checks: bool = True) -> ReconcileReport:
    """Bring every reviewable row, the version history and the derived statuses in line with the ledger.

    Call it after ANY load (seed, check-updates, a scheduler run, a curated reload) in the same
    transaction as the load. In one pass it

    1. computes the evidence key, fingerprint and field snapshot of every reviewable row and records
       the source snapshot each row was extracted from or anchored against;
    2. re-validates the quote anchoring of every curated row against the newest snapshot of its source;
    3. upserts ``evidence_version`` (one row per key and fingerprint ever seen) and marks which
       version is current;
    4. re-derives every effective status with ``effective_status`` and writes it, keeping the
       mechanical status in ``base_verification_status``;
    5. stores automated check flags and the risk level when ``review.checks`` is installed;
    6. audits every status transition and every approval carried forward to a new source version;
    7. records a reviewable change event, with field-level before / after values, for every row whose
       content changed since it was last seen (HIGH when a reported value changed);
    8. reports decisions whose evidence no longer exists (orphans are surfaced, never deleted).

    Idempotent: a second run with nothing loaded in between writes no row, no version, no audit event
    and no change event; only ``last_seen_at`` of the current versions moves. The session is flushed
    and expired, not committed: the caller owns the transaction.
    """
    started = time.perf_counter()
    now = utcnow()
    session.flush()
    report = ReconcileReport()
    report.retired_queue_items = retire_legacy_queue_items(session)

    snapshots = identity.latest_snapshots(session)
    records = identity.load_records(session, snapshots)
    mechanical = _mechanical_statuses(session, records)
    report.population_entries_reanchored = _reanchor_population_summaries(session, snapshots)
    check_flags = _automated_flags(session, report, run_checks)

    decisions = load_decision_facts(session)
    attached, orphans = attach_decisions(((r.evidence_type, r.evidence_key, r.natural_key, r.fingerprint) for r in records), decisions)

    versions = _load_versions(session)
    by_identity = {(v.evidence_type, v.evidence_key, v.fingerprint): v for v in versions}
    current = {(v.evidence_type, v.evidence_key): v for v in versions if v.is_current}
    # For a key that has no current version (its row disappeared, or the evidence tables were reset), the version
    # it had when it was last seen: a row that comes back is compared with that, so a change is not missed.
    last_known: dict[tuple[str, str], _Version] = {}
    for v in versions:
        key = (v.evidence_type, v.evidence_key)
        if not v.is_current and key not in current:
            held = last_known.get(key)
            if held is None or (v.last_seen_at, v.id) > (held.last_seen_at, held.id):
                last_known[key] = v

    row_updates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    key_clears: dict[str, list[dict[str, Any]]] = defaultdict(list)
    version_inserts: list[tuple[EvidenceRecord, dict[str, Any], _Version | None]] = []
    version_updates: list[dict[str, Any]] = []
    reactivated: list[tuple[EvidenceRecord, _Version, _Version | None]] = []
    demote: set[int] = set()
    changed: list[tuple[EvidenceRecord, _Version, str]] = []
    statuses: Counter[str] = Counter()
    collisions: Counter[str] = Counter()
    live_keys: set[tuple[str, str]] = set()
    outcomes: dict[tuple[str, str], StatusOutcome] = {}

    for record, anchor_result in zip(records, mechanical, strict=True):
        key = (record.evidence_type, record.evidence_key)
        live_keys.add(key)
        ref = record.snapshot_ref
        source_hash = ref.content_hash if ref else None
        outcome = effective_status(
            mechanical=anchor_result.status, decisions=attached.get(key, ()), fingerprint=record.fingerprint, structured=record.is_registry, source_content_hash=source_hash
        )
        outcomes[key] = outcome
        statuses[outcome.status] += 1
        report.open_requests += len(outcome.open_requests)
        if record.collision_level:
            collisions[record.evidence_type] += 1

        version = by_identity.get((record.evidence_type, record.evidence_key, record.fingerprint))
        previous = current.get(key) or last_known.get(key)

        own_flags = identity_flags(record)
        if check_flags is None:  # checks skipped or failed: keep what was stored for this exact content
            kept = [f for f in (version.check_flags if version else None) or [] if not str(f.get("code", "")).startswith("IDENTITY_")]
            flags = _flag_dicts([*own_flags, *kept])
        else:
            flags = _flag_dicts([*own_flags, *check_flags.get((record.evidence_type, record.row_id), ())])
        risk = _risk(flags)

        stored = record.stored
        snapshot_id = ref.id if ref else None
        if (stored.evidence_key, stored.fingerprint, stored.verification_status, stored.base_verification_status, stored.source_snapshot_id, stored.check_flags, stored.risk_level) != (
            record.evidence_key, record.fingerprint, outcome.status, anchor_result.status, snapshot_id, flags, risk
        ):
            if stored.evidence_key is not None and stored.evidence_key != record.evidence_key:
                key_clears[record.evidence_type].append({"b_id": record.row_id})
            row_updates[record.evidence_type].append({
                "b_id": record.row_id, "b_key": record.evidence_key, "b_fingerprint": record.fingerprint, "b_status": outcome.status,
                "b_base": anchor_result.status, "b_snapshot": snapshot_id, "b_flags": flags, "b_risk": risk,
            })

        index_values = {
            "natural_key": record.natural_key, "label": record.label, "study_nct": record.study_nct, "study_name": record.study_name,
            "drug_slugs": _packed(record.drug_slugs), "missing_fields": _packed(record.missing_fields), "endpoint_category": record.endpoint_category,
            "tier": record.tier, "source_type": record.source_type, "source_key": record.source_key, "collision_level": record.collision_level,
            "effective_status": outcome.status, "mechanical_status": anchor_result.status, "risk_level": risk, "check_flags": flags,
            "last_source_content_hash": source_hash, "last_source_snapshot_version": ref.version_no if ref else None,
        }
        if version is None:
            version_inserts.append((record, index_values, previous))
            report.new_versions += 1
            if previous is None:
                report.first_seen_keys += 1
        else:
            if outcome.carried_forward and version.last_source_content_hash != source_hash and outcome.decision is not None:
                report.carried_forward.append({
                    "evidence_type": record.evidence_type, "evidence_key": record.evidence_key, "fingerprint": record.fingerprint, "label": record.label,
                    "study": record.study_nct, "decision_id": outcome.decision.id, "decision_uid": outcome.decision.uid, "reviewer": outcome.decision.reviewer,
                    "from_source_hash": outcome.decision.source_content_hash, "to_source_hash": source_hash, "to_snapshot_version": ref.version_no if ref else None,
                    "basis": "re-extracted with identical content" if record.is_registry else "quote found in the new source version",
                })
            if not version.is_current and previous is not None and previous is not version:
                reactivated.append((record, version, previous))  # the content went back to an earlier version
            if not version.is_current or any(getattr(version, name) != value for name, value in index_values.items()):
                version_updates.append({"b_id": version.id, **{f"b_{name}": value for name, value in index_values.items()}, "b_search_text": record.search_text})
        if previous is not None and previous is not version:
            demote.add(previous.id)
            changed.append((record, previous, outcome.status))
        if previous is not None and previous.effective_status and previous.effective_status != outcome.status:
            report.transitions_total += 1
            entry = {
                "evidence_type": record.evidence_type, "evidence_key": record.evidence_key, "label": record.label, "study": record.study_nct,
                "from": previous.effective_status, "to": outcome.status, "reason": outcome.reason, "fingerprint_before": previous.fingerprint,
                "fingerprint_after": record.fingerprint, "decision_id": outcome.decision.id if outcome.decision else None,
            }
            audit(session, "review.status_changed", target_type=record.evidence_type, target_id=record.evidence_key, **entry)
            if len(report.transitions) < REPORT_LIST_LIMIT:
                report.transitions.append(entry)

    for key, version in current.items():
        if key not in live_keys:
            demote.add(version.id)
            report.disappeared_keys += 1

    _write_rows(session, key_clears, row_updates)
    report.rows_updated = sum(len(v) for v in row_updates.values())
    snapshot_cache = _write_versions(session, now, version_inserts, version_updates, reactivated, demote)
    _record_changes(session, report, changed, snapshot_cache, snapshots)

    for entry in report.carried_forward:
        audit(session, "review.approval_carried_forward", target_type=entry["evidence_type"], target_id=entry["evidence_key"], **entry)

    for decision in orphans:
        seen = by_identity.get((decision.evidence_type, decision.evidence_key, decision.fingerprint or ""))
        report.orphaned_decisions.append({
            "decision_id": decision.id, "uid": decision.uid, "evidence_type": decision.evidence_type, "evidence_key": decision.evidence_key,
            "fingerprint": decision.fingerprint, "decision": decision.decision, "reviewer": decision.reviewer,
            "decided_at": decision.decided_at.isoformat() if decision.decided_at else None, "label": seen.label if seen else None,
            "study": seen.study_nct if seen else None,
        })

    report.rows = len(records)
    report.by_type = dict(Counter(r.evidence_type for r in records))
    report.statuses = dict(statuses)
    report.key_collisions = dict(collisions)
    report.seconds = round(time.perf_counter() - started, 3)
    if report.changed_anything:
        audit(session, "review.reconciled", target_type="review", **report.summary())
    session.flush()
    session.expire_all()
    return report


def hold_rejections(session: Session) -> int:
    """Between the steps of one load, keep every rejected row hidden. Returns the number of rows re-hidden.

    A load rebuilds evidence rows with their mechanical status. ``reconcile`` puts the decided
    statuses back, but it must run on a complete, consistent state: half-way through a seed the
    registry rows exist while the curated arm annotations they are fingerprinted with do not, and a
    full pass there would report every such row as changed. So a multi-step load calls this after a
    step that rebuilt rows and ``reconcile`` once at the end. This pass is deliberately one-sided and
    conservative: a row whose latest decisive decision is a rejection is stored as REJECTED again
    whatever its interim fingerprint, so it cannot be read by anything analytic in between. It
    records no version, writes no audit event and never marks anything verified; approved rows simply
    show their mechanical status until the final reconcile.
    """
    session.flush()
    decisive = [d for d in load_decision_facts(session) if d.decisive]
    if not any(d.decision in REJECTING_DECISIONS for d in decisive):
        return 0
    records = identity.load_records(session)
    attached, _ = attach_decisions(((r.evidence_type, r.evidence_key, r.natural_key, r.fingerprint) for r in records), decisive)
    held: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        facts = attached.get((record.evidence_type, record.evidence_key))
        if facts and facts[-1].decision in REJECTING_DECISIONS and record.stored.verification_status != REJECTED:
            held[record.evidence_type].append({"b_id": record.row_id})
    for evidence_type, params in held.items():
        table = REVIEWABLE_MODELS[evidence_type].__table__
        session.execute(update(table).where(table.c.id == bindparam("b_id")).values(verification_status=REJECTED), params)
    session.flush()
    session.expire_all()
    return sum(len(v) for v in held.values())


def _write_rows(session: Session, key_clears: dict[str, list[dict[str, Any]]], row_updates: dict[str, list[dict[str, Any]]]) -> None:
    """The only statement in the codebase that can store HUMAN_VERIFIED or REJECTED on an evidence row.

    ``b_status`` is always the value ``effective_status`` returned. Keys that move to another row are
    cleared first so the unique index never sees two rows with one key mid-update.
    """
    for evidence_type, params in key_clears.items():
        table = REVIEWABLE_MODELS[evidence_type].__table__
        session.execute(update(table).where(table.c.id == bindparam("b_id")).values(evidence_key=None), params)
    for evidence_type, params in row_updates.items():
        table = REVIEWABLE_MODELS[evidence_type].__table__
        session.execute(
            update(table).where(table.c.id == bindparam("b_id")).values(
                evidence_key=bindparam("b_key"), fingerprint=bindparam("b_fingerprint"), verification_status=bindparam("b_status"),
                base_verification_status=bindparam("b_base"), source_snapshot_id=bindparam("b_snapshot"), check_flags=bindparam("b_flags"),
                risk_level=bindparam("b_risk"),
            ),
            params,
        )


def _fetch_snapshots(session: Session, ids: Iterable[int], cache: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    wanted = sorted({i for i in ids if i not in cache})
    for start in range(0, len(wanted), 400):
        chunk = wanted[start : start + 400]
        for version_id, snapshot in session.execute(select(EvidenceVersion.id, EvidenceVersion.snapshot).where(EvidenceVersion.id.in_(chunk))):
            cache[version_id] = snapshot or {}
    return cache


def _write_versions(
    session: Session,
    now: datetime,
    inserts: list[tuple[EvidenceRecord, dict[str, Any], _Version | None]],
    updates: list[dict[str, Any]],
    reactivated: list[tuple[EvidenceRecord, _Version, _Version | None]],
    demote: set[int],
) -> dict[int, dict[str, Any]]:
    table = EvidenceVersion.__table__
    cache: dict[int, dict[str, Any]] = {}
    _fetch_snapshots(session, [p.id for _, _, p in inserts if p is not None] + [p.id for _, _, p in reactivated], cache)
    if demote:
        session.execute(update(table).where(table.c.id == bindparam("b_id")).values(is_current=False), [{"b_id": i} for i in sorted(demote)])
    if updates:
        names = [n for n in updates[0] if n not in ("b_id",)]
        session.execute(
            update(table).where(table.c.id == bindparam("b_id")).values(is_current=True, **{n[2:]: bindparam(n) for n in names}),
            updates,
        )
    if reactivated:
        session.execute(
            update(table).where(table.c.id == bindparam("b_id")).values(supersedes_id=bindparam("b_prev"), changed_fields=bindparam("b_changed"), current_since=now),
            [
                {"b_id": version.id, "b_prev": previous.id, "b_changed": identity.diff_snapshots(cache.get(previous.id), record.snapshot)}
                for record, version, previous in reactivated
            ],
        )
    if inserts:
        rows = []
        for record, index_values, previous in inserts:
            ref = record.snapshot_ref
            rows.append({
                "evidence_type": record.evidence_type, "evidence_key": record.evidence_key, "fingerprint": record.fingerprint, "is_current": True,
                "snapshot": record.snapshot, "search_text": record.search_text, "origin": record.origin,
                "source_content_hash": ref.content_hash if ref else None, "source_snapshot_version": ref.version_no if ref else None,
                "supersedes_id": previous.id if previous is not None else None,
                "changed_fields": identity.diff_snapshots(cache.get(previous.id), record.snapshot) if previous is not None else None,
                "first_seen_at": now, "last_seen_at": now, "current_since": now, **index_values,
            })
        for start in range(0, len(rows), 2000):
            session.execute(insert(table), rows[start : start + 2000])
    session.execute(update(table).where(table.c.is_current.is_(True)).values(last_seen_at=now))
    return cache


def _record_changes(
    session: Session,
    report: ReconcileReport,
    changed: list[tuple[EvidenceRecord, _Version, str]],
    snapshot_cache: dict[int, dict[str, Any]],
    snapshots: dict[int, SnapshotRef],
) -> None:
    """Turn every changed fingerprint into a field-level, reviewable change record.

    One change event per cited source. A change of a reported value is HIGH: it used to be logged as
    a LOW "registry revision date" and overwritten in place.
    """
    if not changed:
        return
    _fetch_snapshots(session, [previous.id for _, previous, _ in changed], snapshot_cache)
    by_source: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for record, previous, status_after in changed:
        fields = identity.diff_snapshots(snapshot_cache.get(previous.id), record.snapshot)
        value_changed = identity.is_value_change(record.evidence_type, fields)
        entry = {
            "evidence_type": record.evidence_type, "evidence_key": record.evidence_key, "label": record.label, "study": record.study_nct,
            "source_key": record.source_key, "fingerprint_before": previous.fingerprint, "fingerprint_after": record.fingerprint,
            "status_before": previous.effective_status, "status_after": status_after, "value_changed": value_changed, "changed_fields": fields,
            "source_changed": previous.last_source_content_hash != (record.snapshot_ref.content_hash if record.snapshot_ref else None),
        }
        report.changed_total += 1
        report.value_changes += int(value_changed)
        if len(report.changed) < REPORT_LIST_LIMIT:
            report.changed.append(entry)
        by_source[record.source_id].append(entry)

    value_names = identity.VALUE_FIELDS
    for source_id, entries in by_source.items():
        if source_id is None:
            continue
        ref = snapshots.get(source_id)
        source = session.get(EvidenceSource, source_id)
        if source is None:
            continue
        n_values = sum(1 for e in entries if e["value_changed"])
        significance = "HIGH" if n_values else "MEDIUM"
        lines: list[dict[str, Any]] = []
        for entry in entries:
            for change in entry["changed_fields"]:
                is_value = "." not in change["field"] and change["field"] in value_names.get(entry["evidence_type"], frozenset())
                if len(lines) < CHANGE_ENTRY_LIMIT:
                    lines.append({
                        "field": f"{entry['evidence_type']}.{change['field']}", "label": f"{entry['label']} — {change['field']}",
                        "before": change["before"], "after": change["after"], "significance": "HIGH" if is_value else "MEDIUM",
                        "note": "Stored evidence changed since it was last seen. " + (
                            "A reported value differs: re-verify it and everything derived from it." if is_value else "A label, classification or supporting text differs."
                        ),
                        "evidence_type": entry["evidence_type"], "evidence_key": entry["evidence_key"],
                        "status_before": entry["status_before"], "status_after": entry["status_after"],
                    })
        example = next((e for e in entries if e["value_changed"]), entries[0])
        first = example["changed_fields"][0] if example["changed_fields"] else None
        detail = f" For example {example['label']}: {first['field']} {first['before']!r} -> {first['after']!r}." if first else ""
        summary = (
            f"{len(entries)} stored evidence row(s) citing {source.key} changed since they were last seen"
            f" ({n_values} with a changed reported value).{detail}"
        )
        source_changed = all(e["source_changed"] for e in entries)
        event = None
        if source_changed and ref is not None:
            candidate = session.scalar(
                select(ChangeEvent).where(ChangeEvent.source_id == source_id, ChangeEvent.to_snapshot_id == ref.id).order_by(ChangeEvent.id.desc()).limit(1)
            )
            if candidate is not None and candidate.review_status == ReviewStatus.OPEN.value and not any(c.get("evidence_key") for c in candidate.changes or []):
                event = candidate
        if event is not None:
            event.changes = [*(event.changes or []), *lines]
            event.summary = f"{event.summary} {summary}"
            if significance == "HIGH" or event.significance == "LOW":
                event.significance = significance
            item = session.scalar(select(ReviewQueueItem).where(ReviewQueueItem.item_type == "SOURCE_CHANGE", ReviewQueueItem.target_id == event.id))
            if item is not None:
                item.priority, item.reason = event.significance, event.summary
                item.payload = {**(item.payload or {}), "evidence_rows_changed": len(entries), "values_changed": n_values}
        else:
            earlier = None
            if ref is not None and source_changed:
                earlier = session.scalar(
                    select(SourceSnapshot.id).where(SourceSnapshot.source_id == source_id, SourceSnapshot.version_no < ref.version_no)
                    .order_by(SourceSnapshot.version_no.desc()).limit(1)
                )
            event = ChangeEvent(
                source_id=source_id, from_snapshot_id=earlier if earlier is not None else (ref.id if ref else None), to_snapshot_id=ref.id if ref else None,
                summary=summary if source_changed else summary + " The source itself has no new version: the curated or extracted record changed.",
                changes=lines, significance=significance,
            )
            session.add(event)
            session.flush()
            enqueue_review(
                session, item_type="SOURCE_CHANGE", target_type="change_event", target_id=event.id,
                title=f"Stored evidence citing {source.title[:160]} changed", reason=event.summary, priority=significance,
                payload={"source_key": source.key, "n_changes": len(lines), "evidence_rows_changed": len(entries), "values_changed": n_values, "synthetic": False},
                dedupe_key=f"evidence-change:{identity.digest([source.key, sorted((e['evidence_key'], e['fingerprint_after']) for e in entries)])[:40]}",
            )
        session.flush()
        audit(
            session, "review.evidence_changed", target_type="change_event", target_id=event.id, source_key=source.key, significance=significance,
            rows_changed=len(entries), values_changed=n_values, truncated=sum(len(e["changed_fields"]) for e in entries) > len(lines),
        )
        report.change_event_ids.append(event.id)


# --------------------------------------------------------------------------- recording a decision


class ReviewError(Exception):
    """Base class: the decision could not be recorded."""


class EvidenceNotFound(ReviewError):
    pass


class NotReconciled(ReviewError):
    pass


class FingerprintMismatch(ReviewError):
    def __init__(self, current_fingerprint: str) -> None:
        super().__init__("The evidence changed since it was opened; reload it and review the current version.")
        self.current_fingerprint = current_fingerprint


class InvalidDecision(ReviewError):
    pass


@dataclass(frozen=True)
class RecordedDecision:
    decision_id: int
    uid: str
    evidence_type: str
    evidence_key: str
    fingerprint: str
    decision: str
    status_before: str
    status_after: str
    reason: str


def record_decision(
    session: Session,
    *,
    evidence_type: str,
    row_id: int,
    decision: str,
    reviewer: str,
    rationale: str,
    attested: bool,
    expected_fingerprint: str,
    proposed_correction: dict[str, Any] | None = None,
    actor_kind: str = ActorKind.HUMAN.value,
) -> RecordedDecision:
    """Record one decision on one exact evidence version and apply its effect, without committing.

    The decision row, the new status of the evidence row (for a decisive decision) and the audit
    event are written together; the caller commits once. Raises a ``ReviewError`` subclass and writes
    nothing when the request is not acceptable.
    """
    if evidence_type not in REVIEWABLE_TYPES:
        raise EvidenceNotFound(f"Unknown evidence type '{evidence_type}'")
    if decision not in DECISION_KINDS:
        raise InvalidDecision(f"Unknown decision '{decision}'")
    for problem in (reviewer_problem(reviewer), rationale_problem(rationale)):
        if problem:
            raise InvalidDecision(problem)
    decisive_kind = decision not in REQUEST_DECISIONS
    if decisive_kind and actor_kind != ActorKind.HUMAN.value:
        raise InvalidDecision("Only a human reviewer can approve, reject or flag evidence.")
    if decisive_kind and attested is not True:
        raise InvalidDecision("Approving, rejecting or flagging evidence requires the reviewer's attestation that they checked it against the source.")
    if decision == ReviewDecisionKind.PROPOSE_CORRECTION.value and not proposed_correction:
        raise InvalidDecision("A proposed correction must name the fields to correct and quote the supporting passage.")
    if decision != ReviewDecisionKind.PROPOSE_CORRECTION.value and proposed_correction:
        raise InvalidDecision("proposed_correction is only accepted with the PROPOSE_CORRECTION decision.")

    model = REVIEWABLE_MODELS[evidence_type]
    row = session.get(model, row_id, execution_options={INCLUDE_REJECTED: True})
    if row is None:
        raise EvidenceNotFound(f"No {evidence_type} with id {row_id}")
    if not row.evidence_key or not row.fingerprint:
        raise NotReconciled("This evidence has not been keyed yet; run `incretinos review reconcile` and try again.")
    if expected_fingerprint != row.fingerprint:
        raise FingerprintMismatch(row.fingerprint)
    version = session.scalar(
        select(EvidenceVersion).where(
            EvidenceVersion.evidence_type == evidence_type, EvidenceVersion.evidence_key == row.evidence_key, EvidenceVersion.fingerprint == row.fingerprint
        )
    )
    if version is None:
        raise NotReconciled("No version record exists for this evidence; run `incretinos review reconcile` and try again.")
    if proposed_correction:
        known = set(identity.flatten(version.snapshot or {}))
        unknown = sorted(set(proposed_correction.get("changes") or {}) - known)
        if unknown:
            raise InvalidDecision(f"Unknown field(s) in the proposed correction: {', '.join(unknown)}. Known fields: {', '.join(sorted(known))}.")

    snapshot = session.get(SourceSnapshot, row.source_snapshot_id) if row.source_snapshot_id else None
    source_key = session.scalar(select(EvidenceSource.key).where(EvidenceSource.id == row.source_id)) if getattr(row, "source_id", None) else None
    source_hash = snapshot.content_hash if snapshot else None
    decided_at = utcnow()
    status_before = row.verification_status
    status_after, reason = status_before, "REQUEST_RECORDED"
    if decisive_kind:
        # The decision being recorded is the newest decisive one on the current fingerprint and source
        # version, so it settles the status on its own (rows 2 and 7 of the truth table).
        fact = DecisionFact(decision=decision, fingerprint=row.fingerprint, actor_kind=actor_kind, attested=bool(attested), decided_at=decided_at,
                            source_content_hash=source_hash)
        outcome = effective_status(
            mechanical=row.base_verification_status or PENDING, decisions=[fact], fingerprint=row.fingerprint,
            structured=getattr(row, "origin", None) == identity.REGISTRY, source_content_hash=source_hash,
        )
        status_after, reason = outcome.status, outcome.reason
    recorded = ReviewDecision(
        target_type=evidence_type, target_id=row.id, decision=decision, reviewer=" ".join(reviewer.split()), rationale=rationale.strip(), uid=str(uuid.uuid4()),
        created_at=decided_at, evidence_type=evidence_type, evidence_key=row.evidence_key, natural_key=version.natural_key, fingerprint=row.fingerprint,
        evidence_version_id=version.id, source_key=source_key, source_content_hash=source_hash, source_snapshot_version=snapshot.version_no if snapshot else None,
        proposed_correction=proposed_correction, actor_kind=actor_kind, attested=bool(attested), status_before=status_before, status_after=status_after,
    )
    session.add(recorded)
    session.flush()
    if status_after != status_before:
        _write_rows(session, {}, {evidence_type: [{
            "b_id": row.id, "b_key": row.evidence_key, "b_fingerprint": row.fingerprint, "b_status": status_after,
            "b_base": row.base_verification_status, "b_snapshot": row.source_snapshot_id, "b_flags": row.check_flags, "b_risk": row.risk_level,
        }]})
        version.effective_status = status_after
    audit(
        session, "review.evidence_decision", actor=recorded.reviewer, target_type=evidence_type, target_id=row.evidence_key, decision=decision,
        decision_id=recorded.id, decision_uid=recorded.uid, evidence_type=evidence_type, evidence_key=row.evidence_key, fingerprint=row.fingerprint,
        label=version.label, study=version.study_nct, status_before=status_before, status_after=status_after, reason=reason, source_key=source_key,
        source_content_hash=recorded.source_content_hash, attested=bool(attested), actor_kind=actor_kind, rationale=recorded.rationale,
        has_proposed_correction=bool(proposed_correction),
    )
    session.flush()
    session.expire(row)
    return RecordedDecision(recorded.id, recorded.uid, evidence_type, row.evidence_key, row.fingerprint, decision, status_before, status_after, reason)
