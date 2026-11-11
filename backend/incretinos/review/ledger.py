"""Export and import of the review ledger: every evidence decision with the version it was made on.

The ledger is how human-authored review state is backed up, and how it moves between databases
(SQLite to PostgreSQL, one deployment to another). It is plain JSON. A decision is identified by its
``uid``; the evidence it applies to is identified by evidence key and fingerprint, never by a primary
key, so the same file can be imported into a database whose rows have different ids.

Import is validated as a whole before anything is written and is idempotent: decisions already
present (same ``uid``) are skipped. After the import ``reconcile`` re-derives every status from the
ledger. Every imported decision must say it is attested and made by a human reviewer, carry a
timezone-aware date that is not in the future, and hold only JSON-safe values (no NaN or Infinity).

What an imported decision applies to
    * MATCHED_VERSION: this database already holds the exact version (type, key, fingerprint) it was made on;
    * SNAPSHOT_VERIFIED: the ledger carries the reviewed content and it hashes to the decision's fingerprint;
      the version is recorded (not current) so the reviewed content can be diffed against what is stored now;
    * UNMATCHED: neither. The decision is kept as history, marked ``ledger_match = UNMATCHED``, reported as an
      orphan with a review-queue item, and never applied to any row.
    A ledger entry whose snapshot does not hash to its fingerprint is malformed and refuses the whole file.

Dates are normalised to UTC before they are stored (SQLite keeps no offset), and "the latest decision wins"
compares those UTC instants.

Trust model, plainly: the file is not signed. The per-record checksum is an unkeyed hash that anyone can
recompute; it detects accidental damage, not forgery. The attestation flag and the reviewer name in a ledger
are statements of whoever wrote the file. Importing a ledger is therefore an operator action with the same
authority as direct database access, and every imported decision stays labelled as imported (``imported_from``,
the file's sha256) with how its reviewer identity was established (``identity_basis``) wherever it is shown.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from ..enums import ActorKind, ReviewDecisionKind
from ..ingestion.snapshots import audit
from ..models import EvidenceVersion, ReviewDecision, utcnow
from . import identity
from .lifecycle import (
    LEDGER_UNMATCHED,
    ReconcileReport,
    correction_problem,
    lock_review_state,
    reconcile,
    refresh_dependent_gaps,
)
from .policy import (
    DECISION_KINDS,
    DECISIVE_DECISIONS,
    IDENTITY_BASES,
    IDENTITY_IMPORTED_UNKNOWN,
    REVIEWABLE_TYPES,
    normalise_reviewer,
    rationale_problem,
    reviewer_problem,
)

LEDGER_MATCHED = "MATCHED_VERSION"
LEDGER_SNAPSHOT_VERIFIED = "SNAPSHOT_VERIFIED"

FORMAT = "incretinos-review-ledger"
VERSION = 1
_HEX64 = r"^[0-9a-f]{64}$"


class LedgerError(ValueError):
    """The ledger file is not acceptable; nothing was imported."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""))
        self.problems = problems


class LedgerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uid: str = Field(min_length=8, max_length=36)
    evidence_type: str
    evidence_key: str = Field(pattern=_HEX64)
    natural_key: str | None = Field(None, pattern=_HEX64)
    fingerprint: str = Field(pattern=_HEX64)
    decision: str
    reviewer: str
    rationale: str
    attested: bool
    actor_kind: str
    created_at: datetime
    source_key: str | None = None
    source_content_hash: str | None = None
    source_snapshot_version: int | None = None
    proposed_correction: dict[str, Any] | None = None
    status_before: str | None = None
    status_after: str | None = None
    label: str | None = None
    study_nct: str | None = None
    snapshot: dict[str, Any] | None = None
    identity_basis: str | None = None
    imported_from: str | None = None
    checksum: str = Field(pattern=_HEX64)


class Ledger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["incretinos-review-ledger"]
    version: Literal[1]
    exported_at: datetime
    n_decisions: int
    decisions: list[LedgerDecision]


@dataclass
class ImportReport:
    file_sha256: str
    in_file: int = 0
    imported: int = 0
    already_present: int = 0
    versions_created: int = 0
    matched: dict[str, int] = field(default_factory=dict)
    gaps_refreshed: int | None = None
    reconcile: ReconcileReport | None = None
    uids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_sha256": self.file_sha256, "in_file": self.in_file, "imported": self.imported, "already_present": self.already_present,
            "versions_created": self.versions_created, "matched": self.matched, "gaps_refreshed": self.gaps_refreshed,
            "review": self.reconcile.summary() if self.reconcile else None,
        }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite returns naive datetimes; everything is stored in UTC
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _checksum(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "checksum"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def export_ledger(session: Session) -> dict[str, Any]:
    """Every evidence decision, oldest first, with the field snapshot of the version it was made on. Read-only."""
    rows = session.execute(
        select(ReviewDecision, EvidenceVersion)
        .outerjoin(EvidenceVersion, ReviewDecision.evidence_version_id == EvidenceVersion.id)
        .where(ReviewDecision.evidence_type.is_not(None), ReviewDecision.evidence_key.is_not(None))
        .order_by(ReviewDecision.created_at, ReviewDecision.id)
    ).all()
    decisions = []
    for decision, version in rows:
        record = {
            "uid": decision.uid or f"local-{decision.id}",
            "evidence_type": decision.evidence_type,
            "evidence_key": decision.evidence_key,
            "natural_key": decision.natural_key,
            "fingerprint": decision.fingerprint,
            "decision": decision.decision,
            "reviewer": decision.reviewer,
            "rationale": decision.rationale,
            "attested": bool(decision.attested),
            "actor_kind": decision.actor_kind or ActorKind.HUMAN.value,
            "created_at": _iso(decision.created_at),
            "source_key": decision.source_key,
            "source_content_hash": decision.source_content_hash,
            "source_snapshot_version": decision.source_snapshot_version,
            "proposed_correction": decision.proposed_correction,
            "status_before": decision.status_before,
            "status_after": decision.status_after,
            "label": version.label if version is not None else None,
            "study_nct": version.study_nct if version is not None else None,
            "snapshot": version.snapshot if version is not None else None,
            "identity_basis": decision.identity_basis,
            "imported_from": decision.imported_from,
        }
        record["checksum"] = _checksum(record)
        decisions.append(record)
    return {"format": FORMAT, "version": VERSION, "exported_at": _iso(utcnow()), "n_decisions": len(decisions), "decisions": decisions}


def write_ledger(session: Session, path: Path) -> int:
    data = export_ledger(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False) + "\n", encoding="utf-8")
    return data["n_decisions"]


def validate_ledger(data: Any) -> Ledger:
    """Parse and check a ledger document. Raises ``LedgerError`` listing every problem found."""
    try:
        ledger = Ledger.model_validate(data)
    except ValidationError as exc:
        raise LedgerError([f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]) from exc
    problems: list[str] = []
    if ledger.n_decisions != len(ledger.decisions):
        problems.append(f"n_decisions says {ledger.n_decisions} but the file holds {len(ledger.decisions)} decision(s)")
    seen: set[str] = set()
    raw_decisions = data["decisions"]
    latest_acceptable = utcnow() + timedelta(minutes=5)
    for index, (decision, raw) in enumerate(zip(ledger.decisions, raw_decisions, strict=True)):
        where = f"decision {index + 1} ({decision.uid})"
        if decision.uid in seen:
            problems.append(f"{where}: duplicate uid")
        seen.add(decision.uid)
        if _checksum(raw) != decision.checksum:
            problems.append(f"{where}: checksum does not match the record (the file was altered or damaged)")
        if decision.evidence_type not in REVIEWABLE_TYPES:
            problems.append(f"{where}: unknown evidence type '{decision.evidence_type}'")
        if decision.decision not in DECISION_KINDS:
            problems.append(f"{where}: unknown decision '{decision.decision}'")
        if decision.actor_kind not in {k.value for k in ActorKind}:
            problems.append(f"{where}: unknown actor kind '{decision.actor_kind}'")
        if decision.decision in DECISIVE_DECISIONS and (decision.actor_kind != ActorKind.HUMAN.value or not decision.attested):
            problems.append(f"{where}: {decision.decision} must be an attested decision by a human reviewer")
        if decision.decision == ReviewDecisionKind.PROPOSE_CORRECTION.value and not decision.proposed_correction:
            problems.append(f"{where}: PROPOSE_CORRECTION without a proposed correction")
        if decision.proposed_correction is not None:
            problem = correction_problem(decision.proposed_correction)
            if problem:
                problems.append(f"{where}: {problem}")
        if decision.identity_basis is not None and decision.identity_basis not in IDENTITY_BASES:
            problems.append(f"{where}: unknown identity basis '{decision.identity_basis}'")
        if decision.snapshot is not None and identity.fingerprint_of(decision.snapshot) != decision.fingerprint:
            problems.append(f"{where}: the stored snapshot does not hash to the decision's fingerprint")
        if decision.created_at.tzinfo is None or decision.created_at.utcoffset() is None:
            problems.append(f"{where}: the date {decision.created_at.isoformat()} has no UTC offset; the order of decisions depends on it")
        elif decision.created_at.astimezone(UTC) > latest_acceptable:
            problems.append(f"{where}: dated in the future ({decision.created_at.isoformat()}); the latest decision wins, so dates must be real")
        for problem in (reviewer_problem(decision.reviewer), rationale_problem(decision.rationale)):
            if problem:
                problems.append(f"{where}: {problem}")
    if problems:
        raise LedgerError(problems)
    return ledger


def import_ledger(session: Session, data: Any, *, file_sha256: str = "") -> ImportReport:
    """Add the ledger's decisions to this database and re-derive every status. Does not commit."""
    ledger = validate_ledger(data)
    lock_review_state(session)
    report = ImportReport(file_sha256=file_sha256, in_file=len(ledger.decisions))
    present = {uid for (uid,) in session.execute(select(ReviewDecision.uid).where(ReviewDecision.uid.is_not(None)))}
    for decision in ledger.decisions:
        if decision.uid in present:
            report.already_present += 1
            continue
        version_id = session.scalar(
            select(EvidenceVersion.id).where(
                EvidenceVersion.evidence_type == decision.evidence_type, EvidenceVersion.evidence_key == decision.evidence_key,
                EvidenceVersion.fingerprint == decision.fingerprint,
            )
        )
        created_at = decision.created_at.astimezone(UTC)
        match = LEDGER_MATCHED
        if version_id is None and decision.snapshot is None:
            # Nothing says what this decision was made on: kept as history, never applied (see the module docstring).
            match = LEDGER_UNMATCHED
        elif version_id is None:
            # The reviewed version is unknown here (fresh database, or the content has changed since), but the ledger carries
            # it and it hashes to the fingerprint (checked by validate_ledger): keep it so a diff against the current content is possible.
            match = LEDGER_SNAPSHOT_VERIFIED
            snapshot = decision.snapshot
            version_id = session.execute(
                insert(EvidenceVersion.__table__).values(
                    evidence_type=decision.evidence_type, evidence_key=decision.evidence_key, natural_key=decision.natural_key or decision.evidence_key,
                    fingerprint=decision.fingerprint, is_current=False, snapshot=snapshot, label=decision.label or f"{decision.evidence_type} {decision.evidence_key[:12]}",
                    study_nct=decision.study_nct, source_key=decision.source_key, source_content_hash=decision.source_content_hash,
                    source_snapshot_version=decision.source_snapshot_version, last_source_content_hash=decision.source_content_hash,
                    last_source_snapshot_version=decision.source_snapshot_version, collision_level=0, first_seen_at=created_at,
                    last_seen_at=created_at,
                )
            ).inserted_primary_key[0]
            report.versions_created += 1
        report.matched[match] = report.matched.get(match, 0) + 1
        session.add(
            ReviewDecision(
                target_type=decision.evidence_type, target_id=None, decision=decision.decision, reviewer=normalise_reviewer(decision.reviewer), rationale=decision.rationale,
                created_at=created_at, uid=decision.uid, identity_basis=decision.identity_basis or IDENTITY_IMPORTED_UNKNOWN, ledger_match=match, evidence_type=decision.evidence_type, evidence_key=decision.evidence_key,
                natural_key=decision.natural_key or decision.evidence_key, fingerprint=decision.fingerprint, evidence_version_id=version_id,
                source_key=decision.source_key, source_content_hash=decision.source_content_hash, source_snapshot_version=decision.source_snapshot_version,
                proposed_correction=decision.proposed_correction, actor_kind=decision.actor_kind, attested=decision.attested,
                status_before=decision.status_before, status_after=decision.status_after, imported_from=(file_sha256 or "ledger")[:80],
            )
        )
        present.add(decision.uid)
        report.imported += 1
        report.uids.append(decision.uid)
    session.flush()
    if report.imported:
        audit(session, "review.ledger_imported", target_type="review", target_id=file_sha256[:16] or None, file_sha256=file_sha256,
              imported=report.imported, already_present=report.already_present, versions_created=report.versions_created, matched=report.matched)
    report.reconcile = reconcile(session)
    if report.reconcile.exclusions_changed:
        report.gaps_refreshed = refresh_dependent_gaps(session)
    return report


def read_ledger_file(path: Path) -> tuple[Any, str]:
    raw = path.read_bytes()

    def refuse(constant: str) -> Any:
        raise ValueError(f"non-standard JSON constant {constant}")

    try:
        return json.loads(raw.decode("utf-8"), parse_constant=refuse), hashlib.sha256(raw).hexdigest()
    except (UnicodeDecodeError, ValueError) as exc:
        raise LedgerError([f"{path} is not a JSON document: {exc}"]) from exc
