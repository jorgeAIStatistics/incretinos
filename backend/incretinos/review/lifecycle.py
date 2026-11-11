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
load is still in progress; it can only write REJECTED (or the exclusion hold) and only where such a
decision exists. The ORM refuses any other assignment (``models.HumanStatusWriteError``), validation
rule V16 re-derives the status of every such row from the ledger, and ``reconcile`` audits every
stored status it has to repair (``review.status_repaired``, with the stored and the derived value).

A rejection outlives a change of content
----------------------------------------
A row whose latest decisive decision is a rejection made on another version is shown as
SOURCE_CHANGED_RECHECK_REQUIRED *and* held out of every analytic surface (``review_hold``) until a
person decides on the current version. Whatever changed (a note, a classification, a value), nothing
a reviewer excluded comes back by itself.

Decisions follow a relabel
--------------------------
A decision is attached to its evidence key, then through the natural key (see ``attach_decisions``),
and finally, when neither exists any more, through a *content anchor* (``identity.content_anchor``:
same study, origin, source, quote / locator and reported values). A sponsor retitling an outcome
measure or a group, or a curator rewording a label, changes the key without changing the measurement;
the decision is carried to the row that now holds the measurement, which then needs a recheck (and is
held when the decision was a rejection). The link is stored on that row's version
(``evidence_version.inherited_decision_ids``) and kept for as long as the key lives. A decision that
matches nothing is an orphan: listed in the review summary and raised as a review-queue item, never
deleted and never silently dropped.

Concurrency
-----------
Every writer of decisions or derived statuses (``record_decision``, ``reconcile``, ledger import)
first takes the review lock (``lock_review_state``): a table lock on PostgreSQL, the database write
lock on SQLite. The decision history is read, and the decision is dated, only after that, so the
status written always agrees with the full history.
"""

from __future__ import annotations

import importlib
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, false, insert, or_, select, text, update
from sqlalchemy.orm import Session

from ..db import INCLUDE_REJECTED, INCLUDE_UNREVIEWED_FULLTEXT
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
from .anchoring import ANCHOR_CACHE, FULLTEXT_EXTRACTION, STRUCTURED_PAYLOAD, AnchorResult, anchor
from .identity import EvidenceRecord, SnapshotRef
from .policy import (
    DECISION_KINDS,
    DECISIVE_DECISIONS,
    HUMAN_DECISION_STATUSES,
    IDENTITY_BASES,
    IDENTITY_SELF_DECLARED,
    MECHANICAL_STATUSES,
    REJECTING_DECISIONS,
    REQUEST_DECISIONS,
    REVIEWABLE_TYPES,
    CheckFlag,
    canonical_status,
    is_decisive,
    is_usable,
    normalise_reviewer,
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
    CHANGED_SINCE_REJECTION: "A reviewer rejected an earlier version; the stored content has changed since and must be looked at again. "
                             "It stays excluded from every comparison, summary and report until a reviewer decides on the current version.",
}

#: ``review_decision.ledger_match`` of an imported decision that matched no known evidence version and carried no
#: verifiable content: kept as history, reported as orphaned, never applied.
LEDGER_UNMATCHED = "UNMATCHED"

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
    applicable: bool = True  # False for an imported decision that matched no known version (never applied)

    @property
    def decisive(self) -> bool:
        return is_decisive(self.decision, self.actor_kind, self.attested)

    @property
    def order(self) -> tuple[datetime, int]:
        return (self.decided_at, self.id)


@dataclass(frozen=True, slots=True)
class StatusOutcome:
    status: str
    reason: str
    decision: DecisionFact | None = None  # the decisive decision the status rests on
    open_requests: tuple[DecisionFact, ...] = ()
    hold: bool = False  # excluded from analytic use although the status is not REJECTED (a rejection of another version)

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
                                                                                                        + hold (still excluded from use)
      9  REQUEST_INFO / PROPOSE_CORRECTION, whenever made                                            -> no effect on the status; listed in ``open_requests``
                                                                                                        while newer than the latest decisive decision
     10  APPROVE / REJECT / FLAG by a non-human actor, or without attestation                        -> no effect at all (row 1 applies if nothing else exists)

    Row 4 is what "quote still anchored" means for a curated row: its quote is found in the current
    stored text or verified excerpts of the source, i.e. ``mechanical`` is QUOTE_ANCHORED. A curated
    row that a reviewer approved although it could not be anchored mechanically keeps its approval
    only while the source version the reviewer saw is the current one (row 2); once the source
    changes there is nothing mechanical to carry the approval forward (row 5).

    A rejected row stays rejected whatever happens to the source (row 7). A change of the stored content
    itself (row 8) asks for a recheck but does NOT end the exclusion: the row is held out of every analytic
    surface (``hold``) until a person records a decisive decision on the current version. Only a later
    decision ends a rejection.
    """
    mechanical = canonical_status(mechanical) or PENDING
    if mechanical not in MECHANICAL_STATUSES:
        raise ValueError(f"'{mechanical}' is not a mechanical status")
    ordered = sorted((d for d in decisions if d.applicable), key=lambda d: d.order)
    latest: DecisionFact | None = None
    for candidate in ordered:
        if candidate.decisive:
            latest = candidate
    requests = tuple(d for d in ordered if d.decision in REQUEST_DECISIONS and (latest is None or d.order > latest.order))
    if latest is None:
        return StatusOutcome(mechanical, NO_DECISION, None, requests)
    on_current = latest.fingerprint == fingerprint
    if latest.decision in REJECTING_DECISIONS:
        if on_current:
            return StatusOutcome(REJECTED, REJECTED_IN_REVIEW, latest, requests)
        return StatusOutcome(RECHECK, CHANGED_SINCE_REJECTION, latest, requests, hold=True)
    if not on_current:
        return StatusOutcome(RECHECK, CHANGED_SINCE_APPROVAL, latest, requests)
    if latest.source_content_hash == source_content_hash:
        return StatusOutcome(HUMAN_VERIFIED, APPROVED, latest, requests)
    if structured or mechanical == QUOTE_ANCHORED:
        return StatusOutcome(HUMAN_VERIFIED, APPROVAL_CARRIED_FORWARD, latest, requests)
    return StatusOutcome(RECHECK, SUPPORT_NOT_FOUND, latest, requests)


def attach_decisions(
    rows: Iterable[tuple[str, str, str, str]],
    decisions: Sequence[DecisionFact],
    inherited: dict[tuple[str, str], Iterable[int]] | None = None,
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

    ``inherited`` maps a live row to the ids of detached decisions carried to it through the content anchor
    (``anchor_matches``; stored on the row's version by ``reconcile``). A decision already attached by key or
    natural key is never inherited as well.

    Detached decisions that no row takes are orphans: reported, never deleted. An imported decision that is
    not ``applicable`` (it matched no known version) is always an orphan.
    """
    rows = list(rows)
    live = {(evidence_type, key) for evidence_type, key, _, _ in rows}
    own: dict[tuple[str, str], list[DecisionFact]] = defaultdict(list)
    detached: dict[tuple[str, str | None], list[DecisionFact]] = defaultdict(list)
    inapplicable: list[DecisionFact] = []
    for decision in decisions:
        if not decision.applicable:
            inapplicable.append(decision)
        elif (decision.evidence_type, decision.evidence_key) in live:
            own[(decision.evidence_type, decision.evidence_key)].append(decision)
        else:
            detached[(decision.evidence_type, decision.natural_key)].append(decision)
    taken: set[int] = set()
    by_row: dict[tuple[str, str], list[DecisionFact]] = {}
    for evidence_type, key, natural_key, fingerprint in rows:
        mine = own.get((evidence_type, key), [])
        loose = detached.get((evidence_type, natural_key)) if natural_key else None
        through_natural: list[DecisionFact] = []
        if loose:
            same_content = [d for d in loose if d.fingerprint == fingerprint]
            through_natural = same_content if (mine or same_content) else list(loose)
            taken.update(id(d) for d in through_natural)
        by_row[(evidence_type, key)] = [*mine, *through_natural]
    if inherited:
        loose_by_id = {d.id: d for group in detached.values() for d in group if id(d) not in taken}
        carried: set[int] = set()
        for row_key, ids in inherited.items():
            if row_key not in by_row:
                continue
            extra = [loose_by_id[i] for i in ids if i in loose_by_id]
            by_row[row_key] += extra
            carried.update(id(d) for d in extra)
        taken |= carried
    attached = {k: tuple(sorted(v, key=lambda d: d.order)) for k, v in by_row.items() if v}
    orphans = [d for group in detached.values() for d in group if id(d) not in taken] + inapplicable
    return attached, sorted(orphans, key=lambda d: d.order)


@dataclass(frozen=True, slots=True)
class AnchorMatch:
    """A detached decision carried to the live row that now holds the same measurement."""

    decision: DecisionFact
    evidence_type: str
    evidence_key: str  # the live row it is carried to
    distance: int  # fields that differ between the reviewed version and the live row (labels, classifications)
    ambiguous: bool  # several live rows were equally close; the decision is carried to each of them


def anchor_matches(
    orphans: Sequence[DecisionFact],
    reviewed_snapshots: dict[int, list[dict[str, Any]]],
    live: Iterable[tuple[str, str, dict[str, Any]]],
) -> list[AnchorMatch]:
    """Match detached decisions to live rows through the content anchor. Pure.

    ``reviewed_snapshots`` gives, per decision id, the snapshot(s) of the content that decision is known to apply
    to (the version it was made on, and the latest version it was carried to before). ``live`` is (evidence type,
    evidence key, snapshot) of every live row. A row is a candidate when its anchor equals the anchor of one of
    those snapshots; among candidates the ones closest to the reviewed content (fewest differing fields) are
    chosen. Several equally close rows all receive the decision (``ambiguous``): carrying a rejection to one row
    too many asks a person to look; carrying it to none would put the rejected value back into use.
    """
    index: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for evidence_type, key, snapshot in live:
        anchor_value = identity.content_anchor(evidence_type, snapshot)
        if anchor_value is not None:
            index[(evidence_type, anchor_value)].append((key, snapshot))
    out: list[AnchorMatch] = []
    for decision in orphans:
        if not decision.applicable:
            continue
        best: dict[str, int] = {}
        for reviewed in reviewed_snapshots.get(decision.id, ()):
            anchor_value = identity.content_anchor(decision.evidence_type, reviewed)
            if anchor_value is None:
                continue
            for key, snapshot in index.get((decision.evidence_type, anchor_value), ()):
                distance = identity.snapshot_distance(reviewed, snapshot)
                if key not in best or distance < best[key]:
                    best[key] = distance
        if not best:
            continue
        closest = min(best.values())
        chosen = sorted(key for key, distance in best.items() if distance == closest)
        for key in chosen:
            out.append(AnchorMatch(decision, decision.evidence_type, key, closest, len(chosen) > 1))
    return out


# --------------------------------------------------------------------------- reading the ledger


def _utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes, PostgreSQL aware ones; everything is stored in UTC and compared as such."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


_FACT_COLUMNS = (
    ReviewDecision.id, ReviewDecision.decision, ReviewDecision.fingerprint, ReviewDecision.actor_kind, ReviewDecision.attested, ReviewDecision.created_at,
    ReviewDecision.evidence_type, ReviewDecision.evidence_key, ReviewDecision.natural_key, ReviewDecision.source_content_hash, ReviewDecision.reviewer,
    ReviewDecision.uid, ReviewDecision.ledger_match,
)


def _fact(r: Any) -> DecisionFact:
    return DecisionFact(decision=r.decision, fingerprint=r.fingerprint, actor_kind=r.actor_kind, attested=r.attested, decided_at=_utc(r.created_at), id=r.id,
                        evidence_type=r.evidence_type, evidence_key=r.evidence_key, natural_key=r.natural_key, source_content_hash=r.source_content_hash,
                        reviewer=r.reviewer, uid=r.uid, applicable=r.ledger_match != LEDGER_UNMATCHED)


def load_decision_facts(session: Session) -> list[DecisionFact]:
    d = ReviewDecision
    rows = session.execute(select(*_FACT_COLUMNS).where(d.evidence_type.is_not(None), d.evidence_key.is_not(None)).order_by(d.created_at, d.id)).all()
    return sorted((_fact(r) for r in rows), key=lambda f: f.order)


def current_inherited(session: Session) -> dict[tuple[str, str], list[int]]:
    """Decisions carried to live rows through the content anchor, as stored by the last reconcile."""
    v = EvidenceVersion
    return {
        (t, k): list(ids) for t, k, ids in session.execute(
            select(v.evidence_type, v.evidence_key, v.inherited_decision_ids).where(v.is_current.is_(True), v.inherited_decision_ids.is_not(None))
        ) if ids
    }


def decisions_for(session: Session, evidence_type: str, evidence_key: str | None, natural_key: str | None) -> tuple[DecisionFact, ...]:
    """The decision history of one evidence row, attached exactly as ``reconcile`` attaches it."""
    if not evidence_key:
        return ()
    d, v = ReviewDecision, EvidenceVersion
    inherited_ids: list[int] = []
    for ids in session.scalars(select(v.inherited_decision_ids).where(v.is_current.is_(True), v.evidence_type == evidence_type, v.evidence_key == evidence_key)):
        inherited_ids += list(ids or [])
    condition = d.evidence_key == evidence_key if natural_key is None else or_(d.evidence_key == evidence_key, d.natural_key == natural_key)
    if inherited_ids:
        condition = or_(condition, d.id.in_(inherited_ids))
    rows = session.execute(select(*_FACT_COLUMNS).where(d.evidence_type == evidence_type, condition).order_by(d.created_at, d.id)).all()
    if not rows:
        return ()
    facts = [_fact(r) for r in rows]
    scope = v.evidence_key == evidence_key if natural_key is None else or_(v.evidence_key == evidence_key, v.natural_key == natural_key)
    live = session.execute(select(v.evidence_type, v.evidence_key, v.natural_key, v.fingerprint).where(v.is_current.is_(True), v.evidence_type == evidence_type, scope)).all()
    attached, _ = attach_decisions([tuple(r) for r in live], facts, {(evidence_type, evidence_key): inherited_ids} if inherited_ids else None)
    return attached.get((evidence_type, evidence_key), ())


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
    anchored_decisions: list[dict[str, Any]] = field(default_factory=list)
    held_rows: int = 0
    exclusions_changed: int = 0
    repairs_total: int = 0  # stored human statuses / holds the ledger does not support (written outside the lifecycle)
    decisions_reapplied: int = 0  # stored mechanical statuses where the ledger yields a human one (in-place reloads, or tampering)
    repairs: list[dict[str, Any]] = field(default_factory=list)
    orphan_queue_items: int = 0
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
                    or self.retired_queue_items or self.population_entries_reanchored or self.anchored_decisions or self.repairs_total
                    or self.decisions_reapplied or self.orphan_queue_items)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def summary(self) -> dict[str, Any]:
        """The counts, without the per-row lists (for audit events and ingest reports)."""
        return {
            "rows": self.rows, "statuses": self.statuses, "new_versions": self.new_versions, "first_seen_keys": self.first_seen_keys,
            "rows_updated": self.rows_updated, "changed": self.changed_total, "value_changes": self.value_changes, "transitions": self.transitions_total,
            "carried_forward": len(self.carried_forward), "orphaned_decisions": len(self.orphaned_decisions), "open_requests": self.open_requests,
            "orphaned_rejections": sum(1 for o in self.orphaned_decisions if o.get("decision") in REJECTING_DECISIONS),
            "decisions_carried_by_anchor": len(self.anchored_decisions), "held_rows": self.held_rows, "exclusions_changed": self.exclusions_changed,
            "status_repairs": self.repairs_total, "decisions_reapplied": self.decisions_reapplied, "orphan_queue_items": self.orphan_queue_items,
            "disappeared_keys": self.disappeared_keys, "key_collisions": self.key_collisions, "change_events": self.change_event_ids,
            "retired_queue_items": self.retired_queue_items, "checks": self.checks, "seconds": self.seconds,
        }


#: Summary counters that add up over the steps of one load (``aggregate_summaries``).
_SUMMED = ("new_versions", "first_seen_keys", "rows_updated", "changed", "value_changes", "transitions", "carried_forward", "decisions_carried_by_anchor",
           "exclusions_changed", "status_repairs", "decisions_reapplied", "orphan_queue_items", "disappeared_keys", "retired_queue_items", "seconds")


def aggregate_summaries(steps: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """One summary for a load that reconciled after several steps (check-updates: registry, then labels).

    Counters of what happened (new versions, re-keyed rows, transitions, ...) are summed over the steps; counters of
    the resulting state (rows, statuses, orphans, open requests, held rows) are those of the last step. Each
    step's own summary is kept under ``steps``.
    """
    if not steps:
        return {}
    last = steps[-1][1]
    total = dict(last)
    for name in _SUMMED:
        values = [summary.get(name) for _, summary in steps if isinstance(summary.get(name), int | float)]
        if values:
            total[name] = round(sum(values), 3) if name == "seconds" else sum(values)
    total["change_events"] = [e for _, summary in steps for e in summary.get("change_events") or []]
    total["steps"] = [{"step": step, **summary} for step, summary in steps]
    return total


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
    supersedes_id: int | None
    review_hold: bool | None
    inherited_decision_ids: list[int] | None


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
    # A value read from a full-text document by a parser is never mechanically verified, whatever its quote: finding the
    # quoted cells in the stored text would only show the parser read its own input. It waits for a person.
    parsed = AnchorResult(PENDING, FULLTEXT_EXTRACTION)
    needed = sorted({r.snapshot_ref.id for r in records if not r.is_registry and r.origin != identity.FULLTEXT and r.snapshot_ref is not None and r.quote})
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
        elif record.origin == identity.FULLTEXT:
            out.append(parsed)
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


def _checks_module() -> Any | None:
    """``review.checks`` when it is installed (it is optional: reconcile never depends on it)."""
    module = f"{__package__}.checks"
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name != module:
            raise
        return None


def _automated_flags(session: Session, report: ReconcileReport, run_checks: bool) -> dict[tuple[str, int], list[CheckFlag]] | None:
    """Flags from ``review.checks.evaluate_all`` when that module is installed; None when they are unavailable.

    The result has an entry for every record the checks evaluated, an empty list included. A record without an
    entry was not evaluated (the checks run as analytic code and do not see rejected rows, so a rejected sibling
    cannot keep a duplicate flag alive on the row that remains); reconcile keeps what is stored for it.
    """
    if not run_checks:
        report.checks = "SKIPPED"
        return None
    checks = _checks_module()
    if checks is None:
        report.checks = "NOT_INSTALLED"
        return {}
    evaluate = getattr(checks, "evaluate_all", None)
    if evaluate is None:
        report.checks = "NOT_INSTALLED"
        return {}
    # The checks run as analytic code (rejected rows hidden), but they must see full-text rows nobody has approved yet:
    # flagging those rows is what they are for.
    previous = session.info.get(INCLUDE_UNREVIEWED_FULLTEXT)
    session.info[INCLUDE_UNREVIEWED_FULLTEXT] = True
    try:
        with session.begin_nested():
            found = evaluate(session) or {}
    except Exception as exc:  # a failing check must not block a load; it is reported and audited instead
        report.checks, report.checks_error = "FAILED", f"{type(exc).__name__}: {exc}"[:500]
        audit(session, "review.checks_failed", target_type="review", error=report.checks_error)
        return None
    finally:
        if previous is None:
            session.info.pop(INCLUDE_UNREVIEWED_FULLTEXT, None)
        else:
            session.info[INCLUDE_UNREVIEWED_FULLTEXT] = previous
    report.checks = "RAN"
    return found


CHANGE_FLAG = "CHANGED_SINCE_LAST_INGESTION"


def _superseded_version_id(version: _Version | None, previous: _Version | None) -> int | None:
    """The version whose content the row's present content replaced, if it replaced any.

    ``previous`` is the version the key had when it was last seen and ``version`` the stored version with the
    row's present fingerprint, if one exists. When they differ, the present content is replacing ``previous`` in
    this very pass; when they are the same version, what it replaced earlier is on record in ``supersedes_id``.
    """
    if previous is not None and previous is not version:
        return previous.id
    return version.supersedes_id if version is not None else None


def _change_flags(session: Session, report: ReconcileReport, superseded: dict[tuple[str, str], int], records: list[EvidenceRecord]) -> dict[tuple[str, str], CheckFlag]:
    """The "changed since the last ingestion" flag of every row whose content replaced an earlier version.

    The previous field values come from the superseded version's snapshot in ``evidence_version`` and the present
    ones from the row's own snapshot, both flattened the way the version history diffs them, so the flag, the
    stored ``changed_fields`` and the change event describe one and the same difference. The flag is derived from
    stored history on every pass (which keeps reconcile idempotent) and stays for as long as that version is the
    current one. It is produced by ``review.checks.change_flag``; without that module there is no such flag.
    """
    if not superseded or report.checks != "RAN":
        return {}
    build = getattr(_checks_module(), "change_flag", None)
    if build is None:
        return {}
    earlier = _fetch_snapshots(session, superseded.values(), {})
    out: dict[tuple[str, str], CheckFlag] = {}
    for record in records:
        key = (record.evidence_type, record.evidence_key)
        if key not in superseded:
            continue
        flag = build(identity.flatten(record.snapshot), identity.flatten(earlier.get(superseded[key]) or {}))
        if flag is not None:
            out[key] = flag
    return out


def _flag_dicts(flags: Iterable[Any]) -> list[dict[str, Any]]:
    out = [f.as_dict() if hasattr(f, "as_dict") else dict(f) for f in flags]
    return sorted(out, key=lambda f: ({"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(str(f.get("severity")), 9), str(f.get("code"))))


def _risk(flags: list[dict[str, Any]]) -> str:
    return risk_level([CheckFlag(str(f.get("code")), str(f.get("severity")), "") for f in flags])


def lock_review_state(session: Session) -> None:
    """Serialise every writer of decisions and derived statuses for the rest of the transaction.

    PostgreSQL: a SHARE ROW EXCLUSIVE lock on ``review_decision`` (it conflicts with itself and with the row
    inserts of another decision, and leaves plain reads alone). SQLite: a no-op write, which makes the driver open
    the transaction and take the database's write lock now rather than at the first real write, so the history
    read afterwards cannot change before this transaction commits. Idempotent within a transaction.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        session.execute(text("LOCK TABLE review_decision IN SHARE ROW EXCLUSIVE MODE"))
    else:
        session.execute(update(ReviewDecision.__table__).where(false()).values(decision=ReviewDecision.__table__.c.decision))
    session.expire_all()


def _reviewed_snapshots(session: Session, orphans: Sequence[DecisionFact], versions: list[_Version]) -> dict[int, list[dict[str, Any]]]:
    """Per orphaned decision: the snapshot it was made on, and the snapshot of the last version it was carried to."""
    wanted = {(d.evidence_type, d.evidence_key, d.fingerprint or "") for d in orphans if d.applicable}
    ids = {v.id for v in versions if (v.evidence_type, v.evidence_key, v.fingerprint) in wanted}
    carriers: dict[int, _Version] = {}
    orphan_ids = {d.id for d in orphans}
    for v in versions:
        for decision_id in v.inherited_decision_ids or ():
            if decision_id in orphan_ids:
                held = carriers.get(decision_id)
                if held is None or (v.last_seen_at or datetime.min.replace(tzinfo=UTC), v.id) > (held.last_seen_at or datetime.min.replace(tzinfo=UTC), held.id):
                    carriers[decision_id] = v
    ids |= {v.id for v in carriers.values()}
    cache = _fetch_snapshots(session, ids, {})
    by_identity = {(v.evidence_type, v.evidence_key, v.fingerprint): v.id for v in versions if v.id in cache}
    out: dict[int, list[dict[str, Any]]] = {}
    for d in orphans:
        snaps = []
        own = by_identity.get((d.evidence_type, d.evidence_key, d.fingerprint or ""))
        if own is not None and cache.get(own):
            snaps.append(cache[own])
        carrier = carriers.get(d.id)
        if carrier is not None and cache.get(carrier.id):
            snaps.append(cache[carrier.id])
        if snaps:
            out[d.id] = snaps
    return out


def _attach_with_anchors(
    session: Session, records: list[EvidenceRecord], decisions: list[DecisionFact], versions: list[_Version]
) -> tuple[dict[tuple[str, str], tuple[DecisionFact, ...]], list[DecisionFact], dict[tuple[str, str], list[int]], list[AnchorMatch]]:
    """Attach decisions by key, natural key and content anchor. Returns (attached, orphans, inherited ids per row, new anchor matches).

    A row keeps the decisions it inherited before (from the version its key had when last seen): once carried
    to a key, a decision stays with that key for as long as the key lives, whatever happens to its content.
    """
    rows = [(r.evidence_type, r.evidence_key, r.natural_key, r.fingerprint) for r in records]
    current = {(v.evidence_type, v.evidence_key): v for v in versions if v.is_current}
    latest: dict[tuple[str, str], _Version] = {}
    for v in versions:
        key = (v.evidence_type, v.evidence_key)
        held = latest.get(key)
        if held is None or (v.last_seen_at or datetime.min.replace(tzinfo=UTC), v.id) > (held.last_seen_at or datetime.min.replace(tzinfo=UTC), held.id):
            latest[key] = v
    prior: dict[tuple[str, str], list[int]] = {}
    for record in records:
        key = (record.evidence_type, record.evidence_key)
        version = current.get(key) or latest.get(key)
        if version is not None and version.inherited_decision_ids:
            prior[key] = list(version.inherited_decision_ids)
    attached, orphans = attach_decisions(rows, decisions, prior)
    matches: list[AnchorMatch] = []
    if any(d.applicable for d in orphans):
        reviewed = _reviewed_snapshots(session, orphans, versions)
        if reviewed:
            matches = anchor_matches(orphans, reviewed, ((r.evidence_type, r.evidence_key, r.snapshot) for r in records))
    if not matches:
        return attached, orphans, {k: sorted(set(v) & {d.id for d in attached.get(k, ())}) for k, v in prior.items()}, []
    inherited = {k: list(v) for k, v in prior.items()}
    for match in matches:
        inherited.setdefault((match.evidence_type, match.evidence_key), []).append(match.decision.id)
    attached, orphans = attach_decisions(rows, decisions, inherited)
    kept = {k: sorted(set(v) & {d.id for d in attached.get(k, ())}) for k, v in inherited.items()}
    return attached, orphans, kept, matches


ORPHAN_QUEUE_TYPE = "ORPHANED_DECISION"


def _orphan_dedupe_key(decision: DecisionFact) -> str:
    return f"orphaned-decision:{decision.uid or decision.id}"


def _sync_orphan_queue(session: Session, orphans: list[dict[str, Any]], orphan_facts: list[DecisionFact]) -> int:
    """Raise one review-queue item per orphaned decision (once), and retire items whose decision applies again.

    A rejection that no longer reaches any row is the dangerous case: the value it excluded may be back under
    another identity. Such items are HIGH priority.
    """
    existing = {
        item.dedupe_key: item for item in session.scalars(select(ReviewQueueItem).where(ReviewQueueItem.item_type == ORPHAN_QUEUE_TYPE))
        if item.dedupe_key
    }
    raised = 0
    wanted: set[str] = set()
    for entry, fact in zip(orphans, orphan_facts, strict=True):
        key = _orphan_dedupe_key(fact)
        wanted.add(key)
        if key in existing:
            continue
        rejecting = fact.decision in REJECTING_DECISIONS and fact.decisive
        priority = "HIGH" if rejecting else ("MEDIUM" if fact.decision in DECISIVE_DECISIONS else "LOW")
        what = entry.get("label") or f"{fact.evidence_type} {fact.evidence_key[:12]}"
        reason = (
            f"A {fact.decision} decision by {fact.reviewer} on {what} no longer applies to any stored evidence: its evidence key is gone and no "
            "current row holds the same measurement (same study, source, quote and values)."
            + (" The value it excluded may be in use again under another identity: find it and decide on it." if rejecting else "")
            + ("" if fact.applicable else " It was imported from a ledger without content that matches any known evidence version.")
        )
        enqueue_review(session, item_type=ORPHAN_QUEUE_TYPE, target_type="review_decision", target_id=fact.id, title=f"Orphaned review decision: {what}"[:300],
                       reason=reason, priority=priority, payload={k: v for k, v in entry.items() if k != "decided_at"} | {"decided_at": entry.get("decided_at")},
                       dedupe_key=key)
        raised += 1
    for key, item in existing.items():
        if key not in wanted and item.status in (ReviewStatus.OPEN.value, ReviewStatus.IN_REVIEW.value):
            item.status = ReviewStatus.RETIRED.value
            audit(session, "review.orphan_resolved", target_type="review_queue_item", target_id=item.id, decision_id=item.target_id,
                  note="The decision applies to stored evidence again (its key came back or a matching row was found).")
            raised += 1
    return raised


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
    session.flush()
    lock_review_state(session)
    now = utcnow()
    report = ReconcileReport()
    report.retired_queue_items = retire_legacy_queue_items(session)

    snapshots = identity.latest_snapshots(session)
    records = identity.load_records(session, snapshots)
    mechanical = _mechanical_statuses(session, records)
    report.population_entries_reanchored = _reanchor_population_summaries(session, snapshots)
    check_flags = _automated_flags(session, report, run_checks)

    decisions = load_decision_facts(session)
    versions = _load_versions(session)
    attached, orphans, inherited, matches = _attach_with_anchors(session, records, decisions, versions)
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

    # Rows whose present content replaced an earlier version, and the flag that says what changed.
    superseded: dict[tuple[str, str], int] = {}
    for record in records:
        key = (record.evidence_type, record.evidence_key)
        earlier_id = _superseded_version_id(by_identity.get((record.evidence_type, record.evidence_key, record.fingerprint)), current.get(key) or last_known.get(key))
        if earlier_id is not None:
            superseded[key] = earlier_id
    change_flags = _change_flags(session, report, superseded, records) if check_flags is not None else {}

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

    for record, anchor_result in zip(records, mechanical, strict=True):
        key = (record.evidence_type, record.evidence_key)
        live_keys.add(key)
        ref = record.snapshot_ref
        source_hash = ref.content_hash if ref else None
        outcome = effective_status(
            mechanical=anchor_result.status, decisions=attached.get(key, ()), fingerprint=record.fingerprint, structured=record.is_registry, source_content_hash=source_hash
        )
        statuses[outcome.status] += 1
        report.held_rows += int(outcome.hold)
        report.open_requests += len(outcome.open_requests)
        if record.collision_level:
            collisions[record.evidence_type] += 1

        version = by_identity.get((record.evidence_type, record.evidence_key, record.fingerprint))
        previous = current.get(key) or last_known.get(key)

        own_flags = identity_flags(record)
        evaluated = None if check_flags is None else check_flags.get((record.evidence_type, record.row_id))
        if evaluated is None:
            # Checks skipped or failed, or this row was not evaluated (a rejected row is hidden from them):
            # keep what was stored for this exact content.
            kept = [f for f in (version.check_flags if version else None) or [] if not str(f.get("code", "")).startswith("IDENTITY_")]
            if check_flags is not None:  # the checks ran: the change flag is derived afresh from the version history
                kept = [f for f in kept if f.get("code") != CHANGE_FLAG]
                kept += [change_flags[key]] if key in change_flags else []
            flags = _flag_dicts([*own_flags, *kept])
        else:
            flags = _flag_dicts([*own_flags, *evaluated, *([change_flags[key]] if key in change_flags else [])])
        risk = _risk(flags)

        stored = record.stored
        snapshot_id = ref.id if ref else None
        if (stored.evidence_key, stored.fingerprint, stored.verification_status, stored.base_verification_status, stored.source_snapshot_id, stored.check_flags, stored.risk_level,
                stored.review_hold) != (record.evidence_key, record.fingerprint, outcome.status, anchor_result.status, snapshot_id, flags, risk, outcome.hold):
            if stored.evidence_key is not None and stored.evidence_key != record.evidence_key:
                key_clears[record.evidence_type].append({"b_id": record.row_id})
            row_updates[record.evidence_type].append({
                "b_id": record.row_id, "b_key": record.evidence_key, "b_fingerprint": record.fingerprint, "b_status": outcome.status,
                "b_base": anchor_result.status, "b_snapshot": snapshot_id, "b_flags": flags, "b_risk": risk, "b_hold": outcome.hold,
            })
            # A row this lifecycle already keyed, with unchanged content, whose stored status or hold is not what its decision history yields
            # and was not what it yielded at the last pass either. Restored here, and audited with both values so the repair never erases
            # the evidence of what was stored. UNSUPPORTED_HUMAN_STATUS (a human status or a hold that no decision supports, or one that
            # contradicts the ledger) can only have been written outside the lifecycle: raw SQL, a bulk update, a concurrent writer.
            # DECISION_REAPPLIED is a mechanical status where the ledger says otherwise, which is also what a loader that rewrites a row
            # in place (curated claims) leaves behind.
            if (stored.evidence_key == record.evidence_key and stored.fingerprint == record.fingerprint
                    and (canonical_status(stored.verification_status) != outcome.status or stored.review_hold != outcome.hold)
                    and previous is not None and previous.effective_status == outcome.status and bool(previous.review_hold) == outcome.hold):
                unsupported = stored.verification_status in HUMAN_DECISION_STATUSES or stored.review_hold
                repair = {
                    "evidence_type": record.evidence_type, "evidence_key": record.evidence_key, "row_id": record.row_id, "label": record.label,
                    "kind": "UNSUPPORTED_HUMAN_STATUS" if unsupported else "DECISION_REAPPLIED",
                    "stored_status": stored.verification_status, "stored_hold": stored.review_hold, "derived_status": outcome.status, "derived_hold": outcome.hold,
                    "reason": outcome.reason, "decision_id": outcome.decision.id if outcome.decision else None,
                }
                report.repairs_total += int(unsupported)
                report.decisions_reapplied += int(not unsupported)
                audit(session, "review.status_repaired", target_type=record.evidence_type, target_id=record.evidence_key,
                      before={"verification_status": stored.verification_status, "review_hold": stored.review_hold},
                      after={"verification_status": outcome.status, "review_hold": outcome.hold}, **{k: v for k, v in repair.items() if k not in ("evidence_type", "evidence_key")})
                if len(report.repairs) < REPORT_LIST_LIMIT:
                    report.repairs.append(repair)

        index_values = {
            "natural_key": record.natural_key, "label": record.label, "study_nct": record.study_nct, "study_name": record.study_name,
            "drug_slugs": _packed(record.drug_slugs), "missing_fields": _packed(record.missing_fields), "endpoint_category": record.endpoint_category,
            "tier": record.tier, "source_type": record.source_type, "source_key": record.source_key, "collision_level": record.collision_level,
            "effective_status": outcome.status, "mechanical_status": anchor_result.status, "risk_level": risk, "check_flags": flags,
            "last_source_content_hash": source_hash, "last_source_snapshot_version": ref.version_no if ref else None,
            "review_hold": outcome.hold, "inherited_decision_ids": inherited.get(key) or None,
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
        if previous is not None and previous.effective_status and is_usable(previous.effective_status, previous.review_hold) != is_usable(outcome.status, outcome.hold):
            report.exclusions_changed += 1
        if previous is not None and previous.effective_status and (previous.effective_status != outcome.status or bool(previous.review_hold) != outcome.hold):
            report.transitions_total += 1
            entry = {
                "evidence_type": record.evidence_type, "evidence_key": record.evidence_key, "label": record.label, "study": record.study_nct,
                "from": previous.effective_status, "to": outcome.status, "reason": outcome.reason, "fingerprint_before": previous.fingerprint,
                "fingerprint_after": record.fingerprint, "decision_id": outcome.decision.id if outcome.decision else None,
                "hold_before": bool(previous.review_hold), "hold_after": outcome.hold,
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

    # Decisions carried to another key through the content anchor in this pass (new links only: a link kept from an earlier pass
    # is not reported again). Each is audited with the key it was made on and the key it now applies to.
    for match in matches:
        target = (match.evidence_type, match.evidence_key)
        before = current.get(target) or last_known.get(target)
        if before is not None and match.decision.id in (before.inherited_decision_ids or ()):
            continue
        origin_version = by_identity.get((match.decision.evidence_type, match.decision.evidence_key, match.decision.fingerprint or ""))
        record = next((r for r in records if (r.evidence_type, r.evidence_key) == target), None)
        entry = {
            "decision_id": match.decision.id, "decision_uid": match.decision.uid, "decision": match.decision.decision, "reviewer": match.decision.reviewer,
            "evidence_type": match.evidence_type, "from_evidence_key": match.decision.evidence_key, "to_evidence_key": match.evidence_key,
            "from_label": origin_version.label if origin_version else None, "to_label": record.label if record else None,
            "study": record.study_nct if record else None, "fields_differing": match.distance, "ambiguous": match.ambiguous,
            "excluded": match.decision.decision in REJECTING_DECISIONS,
        }
        report.anchored_decisions.append(entry)
        if entry["excluded"]:
            report.exclusions_changed += 1
        audit(session, "review.decision_carried_by_anchor", target_type=match.evidence_type, target_id=match.evidence_key, **entry)

    for decision in orphans:
        seen = by_identity.get((decision.evidence_type, decision.evidence_key, decision.fingerprint or ""))
        report.orphaned_decisions.append({
            "decision_id": decision.id, "uid": decision.uid, "evidence_type": decision.evidence_type, "evidence_key": decision.evidence_key,
            "fingerprint": decision.fingerprint, "decision": decision.decision, "reviewer": decision.reviewer,
            "decided_at": decision.decided_at.isoformat() if decision.decided_at else None, "label": seen.label if seen else None,
            "study": seen.study_nct if seen else None, "applicable": decision.applicable,
        })
    report.orphan_queue_items = _sync_orphan_queue(session, report.orphaned_decisions, orphans)

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
    # The same attachment as reconcile, content anchor included: a rejected value that a relabel moved to a new key is hidden too.
    attached, _, _, _ = _attach_with_anchors(session, records, decisive, _load_versions(session))
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
    """Store the derived state of evidence rows: the one statement that can write HUMAN_VERIFIED.

    ``b_status`` and ``b_hold`` are always the values ``effective_status`` returned (``hold_rejections`` is
    the only other writer of a human status or a hold, and it can only write REJECTED or set the hold). Keys that move to another row
    are cleared first so the unique index never sees two rows with one key mid-update.
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
                risk_level=bindparam("b_risk"), review_hold=bindparam("b_hold"),
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
        # A reviewable change record is raised when a reported value changed or a reviewed row lost its status.
        # A change of label or classification alone (for example after the platform's own classifier was
        # improved) stays in the version history, the reconcile report and one audit event: raising a queue
        # item per source for it would bury the changes that matter.
        material = [e for e in entries if e["value_changed"] or e["status_before"] != e["status_after"]]
        if not material:
            audit(session, "review.evidence_changed", target_type="evidence_source", target_id=source_id, source_key=source.key, significance="LOW",
                  rows_changed=len(entries), values_changed=0, change_event=None)
            continue
        other = len(entries) - len(material)
        entries = material
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
            + (f" {other} further row(s) changed in label or classification only." if other else "")
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
            rows_changed=len(entries) + other, values_changed=n_values, change_event=event.id, truncated=sum(len(e["changed_fields"]) for e in entries) > len(lines),
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
    hold_before: bool = False
    hold_after: bool = False
    identity_basis: str = IDENTITY_SELF_DECLARED
    gaps_refreshed: int | None = None


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
    identity_basis: str = IDENTITY_SELF_DECLARED,
) -> RecordedDecision:
    """Record one decision on one exact evidence version and apply its effect, without committing.

    The decision row, the new status of the evidence row (for a decisive decision), the gaps that depend on
    the row's usability and the audit event are written together; the caller commits once. Raises a
    ``ReviewError`` subclass and writes nothing when the request is not acceptable.

    Ordering and concurrency: the review lock is taken first (``lock_review_state``); the row, its decision
    history and the clock are read after it. The decision is dated no earlier than the latest decision already
    in the row's history (if the clock or an imported decision says otherwise, the date is moved forward by
    the smallest step and the adjustment is audited), so the decision just recorded is always the row's latest.
    The status written is then derived from the full history, this decision included, exactly as reconcile
    derives it.
    """
    if evidence_type not in REVIEWABLE_TYPES:
        raise EvidenceNotFound(f"Unknown evidence type '{evidence_type}'")
    if decision not in DECISION_KINDS:
        raise InvalidDecision(f"Unknown decision '{decision}'")
    if identity_basis not in IDENTITY_BASES:
        raise InvalidDecision(f"Unknown identity basis '{identity_basis}'")
    if not isinstance(attested, bool):
        raise InvalidDecision("The attestation must be the boolean true or false.")
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
    if proposed_correction:
        problem = correction_problem(proposed_correction)
        if problem:
            raise InvalidDecision(problem)

    model = REVIEWABLE_MODELS[evidence_type]
    lock_review_state(session)
    table = model.__table__
    if session.get_bind().dialect.name == "postgresql":
        session.execute(select(table.c.id).where(table.c.id == row_id).with_for_update())
    row = session.get(model, row_id, execution_options={INCLUDE_REJECTED: True}, populate_existing=True)
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
    history = decisions_for(session, evidence_type, row.evidence_key, version.natural_key)
    clock = utcnow()
    decided_at = clock
    latest_known = max((d.decided_at for d in history), default=None)
    if latest_known is not None and latest_known >= decided_at:
        decided_at = latest_known + timedelta(microseconds=1)
    status_before, hold_before = row.verification_status, bool(row.review_hold)
    mechanical = row.base_verification_status if row.base_verification_status in MECHANICAL_STATUSES else PENDING
    structured = getattr(row, "origin", None) == identity.REGISTRY
    # Derived exactly as reconcile derives it: the row's whole decision history, read under the lock, plus this decision.
    fact = DecisionFact(decision=decision, fingerprint=row.fingerprint, actor_kind=actor_kind, attested=attested, decided_at=decided_at, id=2**62,
                        evidence_type=evidence_type, evidence_key=row.evidence_key, natural_key=version.natural_key, source_content_hash=source_hash)
    outcome = effective_status(mechanical=mechanical, decisions=[*history, fact], fingerprint=row.fingerprint, structured=structured, source_content_hash=source_hash)
    status_after, hold_after = outcome.status, outcome.hold
    reason = outcome.reason if decisive_kind else "REQUEST_RECORDED"
    recorded = ReviewDecision(
        target_type=evidence_type, target_id=row.id, decision=decision, reviewer=normalise_reviewer(reviewer), rationale=rationale.strip(), uid=str(uuid.uuid4()),
        created_at=decided_at, evidence_type=evidence_type, evidence_key=row.evidence_key, natural_key=version.natural_key, fingerprint=row.fingerprint,
        evidence_version_id=version.id, source_key=source_key, source_content_hash=source_hash, source_snapshot_version=snapshot.version_no if snapshot else None,
        proposed_correction=proposed_correction, actor_kind=actor_kind, attested=attested, status_before=status_before, status_after=status_after,
        identity_basis=identity_basis,
    )
    session.add(recorded)
    session.flush()
    # The stored history, re-read with the new row in it, must yield the same status: the decision is the latest one.
    check = effective_status(mechanical=mechanical, decisions=decisions_for(session, evidence_type, row.evidence_key, version.natural_key),
                             fingerprint=row.fingerprint, structured=structured, source_content_hash=source_hash)
    if (check.status, check.hold) != (status_after, hold_after):  # pragma: no cover - guarded by the lock and the dating rule above
        raise ReviewError("The decision history changed while the decision was being recorded; nothing was written. Try again.")
    if (status_after, hold_after) != (status_before, hold_before):
        _write_rows(session, {}, {evidence_type: [{
            "b_id": row.id, "b_key": row.evidence_key, "b_fingerprint": row.fingerprint, "b_status": status_after, "b_hold": hold_after,
            "b_base": row.base_verification_status, "b_snapshot": row.source_snapshot_id, "b_flags": row.check_flags, "b_risk": row.risk_level,
        }]})
        session.execute(update(EvidenceVersion.__table__).where(EvidenceVersion.__table__.c.id == version.id).values(effective_status=status_after, review_hold=hold_after))
    usability_changed = is_usable(status_before, hold_before) != is_usable(status_after, hold_after)
    audit(
        session, "review.evidence_decision", actor=recorded.reviewer, target_type=evidence_type, target_id=row.evidence_key, decision=decision,
        decision_id=recorded.id, decision_uid=recorded.uid, evidence_type=evidence_type, evidence_key=row.evidence_key, fingerprint=row.fingerprint,
        label=version.label, study=version.study_nct, status_before=status_before, status_after=status_after, hold_before=hold_before, hold_after=hold_after,
        reason=reason, source_key=source_key, source_content_hash=recorded.source_content_hash, attested=attested, actor_kind=actor_kind,
        identity_basis=identity_basis, rationale=recorded.rationale, has_proposed_correction=bool(proposed_correction),
        **({"clock_adjusted_from": clock.isoformat(), "dated": decided_at.isoformat()} if decided_at != clock else {}),
    )
    session.flush()
    session.expire_all()
    gaps = refresh_dependent_gaps(session) if usability_changed else None
    return RecordedDecision(recorded.id, recorded.uid, evidence_type, row.evidence_key, row.fingerprint, decision, status_before, status_after, reason,
                            hold_before, hold_after, identity_basis, gaps)


def refresh_dependent_gaps(session: Session) -> int:
    """Recompute the rule-detected evidence gaps in the current transaction (they quote evidence values).

    Called whenever a decision or a reconcile changes whether some row may be used, so no gap keeps describing
    a value a reviewer excluded (or misses one that was readmitted). Reviewer triage on gaps is preserved by the
    gap engine. Returns the number of gaps detected.
    """
    from ..engines.gaps import refresh_gaps_after_review

    return refresh_gaps_after_review(session)


_CORRECTION_TEXT_LIMIT = 2000
_CORRECTION_FIELD_LIMIT = 200


def correction_problem(proposed: dict[str, Any]) -> str | None:
    """Why a proposed correction cannot be stored, or None. JSON-safe values only: finite numbers, bounded text."""
    import math

    changes = proposed.get("changes")
    if not isinstance(changes, dict) or not changes or len(changes) > 40:
        return "A proposed correction must name between 1 and 40 fields."
    for name, value in changes.items():
        if not isinstance(name, str) or not name or len(name) > _CORRECTION_FIELD_LIMIT:
            return "A corrected field name must be text of at most 200 characters."
        if isinstance(value, float) and not math.isfinite(value):
            return f"The proposed value of {name} is not a finite number."
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53:
            return f"The proposed value of {name} is out of range."
        if isinstance(value, str) and len(value) > _CORRECTION_TEXT_LIMIT:
            return f"The proposed value of {name} is too long ({_CORRECTION_TEXT_LIMIT} characters at most)."
        if value is not None and not isinstance(value, str | int | float | bool):
            return f"The proposed value of {name} must be text, a number, true/false or null."
    for name, limit in (("supporting_quote", 4000), ("note", 2000)):
        value = proposed.get(name)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            return f"The {name.replace('_', ' ')} must be text of at most {limit} characters."
    return None
