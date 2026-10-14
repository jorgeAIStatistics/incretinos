"""Source snapshots: content hashing, versioning, change detection, review queue, audit."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import DataClassification
from ..models import AuditEvent, ChangeEvent, EvidenceSource, ReviewQueueItem, SourceSnapshot, utcnow


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_json(obj: Any) -> str:
    """Stable serialization so semantically identical payloads hash identically."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def normalize_ws(text: str) -> str:
    """Collapse whitespace and unify typographic variants so quote matching is robust."""
    text = text.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    text = text.replace("−", "-").replace("–", "-").replace("—", "-")
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    text = text.replace("·", ".")
    # Table rows are quoted with "|" between cells; cell borders are not part of the source text.
    text = text.replace("|", " ")
    return re.sub(r"\s+", " ", text).strip()


def quote_in_text(quote: str, text: str) -> bool:
    return bool(quote) and normalize_ws(quote).lower() in normalize_ws(text).lower()


def audit(session: Session, action: str, *, actor: str = "system", target_type: str | None = None, target_id: Any = None, **detail: Any) -> AuditEvent:
    event = AuditEvent(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=None if target_id is None else str(target_id),
        detail=detail,
    )
    session.add(event)
    return event


#: Queue statuses in which an item is still waiting for a person. An item in any other status is closed.
OPEN_QUEUE_STATUSES = ("OPEN", "IN_REVIEW")


def enqueue_review(
    session: Session,
    *,
    item_type: str,
    target_type: str,
    target_id: int | None,
    title: str,
    reason: str,
    priority: str = "MEDIUM",
    payload: dict | None = None,
    dedupe_key: str | None = None,
) -> ReviewQueueItem | None:
    """Open a review-queue item unless the same thing is already waiting for a reviewer.

    The queue is for things that are not themselves evidence rows: source changes, reconciliation
    problems, ingestion failures. (A pending evidence row is reviewed directly through the evidence
    review API and never gets a queue item.)

    ``dedupe_key`` suppresses a duplicate only while the earlier item is still open. Once that item
    has been resolved or retired, the same condition arising again opens a new item: the closed one
    keeps its decisions and gives up its key (it is renamed ``<key>#closed:<id>``). A resolved item
    therefore never hides a later occurrence.
    """
    if dedupe_key:
        existing = session.scalar(select(ReviewQueueItem).where(ReviewQueueItem.dedupe_key == dedupe_key))
        if existing is not None:
            if existing.status in OPEN_QUEUE_STATUSES:
                return None
            existing.dedupe_key = f"{dedupe_key[:170]}#closed:{existing.id}"
            session.flush()
    item = ReviewQueueItem(
        item_type=item_type,
        target_type=target_type,
        target_id=target_id,
        title=title,
        reason=reason,
        priority=priority,
        payload=payload or {},
        dedupe_key=dedupe_key,
    )
    session.add(item)
    return item


@dataclass
class SnapshotOutcome:
    source: EvidenceSource
    snapshot: SourceSnapshot
    is_new_source: bool
    changed: bool  # True when content differs from the previously stored snapshot
    previous: SourceSnapshot | None


def record_snapshot(
    session: Session,
    *,
    key: str,
    source_type: str,
    tier: str,
    title: str,
    content_hash: str,
    content: str | None,
    url: str | None = None,
    publisher: str | None = None,
    published_date: str | None = None,
    version_label: str | None = None,
    license_note: str | None = None,
    content_type: str = "application/json",
    excerpts: list[str] | None = None,
    external_ids: dict | None = None,
    retrieved_at: datetime | None = None,
    byte_size: int | None = None,
    is_synthetic: bool = False,
    classification: str = DataClassification.PUBLIC.value,
) -> SnapshotOutcome:
    """Insert or update a source and append a snapshot only when its content hash changed.

    Duplicate detection is by ``key`` (one source per external identifier) and by
    content hash (re-fetching unchanged content adds no new snapshot).
    """
    if classification != DataClassification.PUBLIC.value:
        raise PermissionError("This build ingests public sources only; restricted data must not be stored.")
    retrieved_at = retrieved_at or utcnow()
    source = session.scalar(select(EvidenceSource).where(EvidenceSource.key == key))
    is_new = source is None
    if source is None:
        source = EvidenceSource(key=key, source_type=source_type, tier=tier, title=title)
        session.add(source)
    source.title = title
    source.tier = tier
    source.url = url or source.url
    source.publisher = publisher or source.publisher
    source.published_date = published_date or source.published_date
    source.license_note = license_note or source.license_note
    source.stores_full_text = content is not None
    source.external_ids = {**(source.external_ids or {}), **(external_ids or {})}
    session.flush()

    previous = session.scalar(
        select(SourceSnapshot).where(SourceSnapshot.source_id == source.id).order_by(SourceSnapshot.version_no.desc()).limit(1)
    )
    if previous is not None and previous.content_hash == content_hash:
        # Unchanged: refresh the "last checked" time only.
        source.retrieved_at = retrieved_at
        if excerpts:
            merged = list(dict.fromkeys([*(previous.excerpts or []), *excerpts]))
            previous.excerpts = merged
        return SnapshotOutcome(source, previous, is_new, False, previous)

    snapshot = SourceSnapshot(
        source_id=source.id,
        version_no=(previous.version_no + 1) if previous else 1,
        content_hash=content_hash,
        retrieved_at=retrieved_at,
        version_label=version_label,
        content_type=content_type,
        content=content,
        excerpts=excerpts or [],
        byte_size=byte_size if byte_size is not None else (len(content.encode("utf-8")) if content else None),
        is_synthetic=is_synthetic,
    )
    session.add(snapshot)
    source.content_hash = content_hash
    source.version_label = version_label
    source.retrieved_at = retrieved_at
    session.flush()
    audit(
        session,
        "source.snapshot_recorded",
        target_type="evidence_source",
        target_id=source.id,
        key=key,
        version_no=snapshot.version_no,
        content_hash=content_hash,
        synthetic=is_synthetic,
    )
    return SnapshotOutcome(source, snapshot, is_new, previous is not None, previous)


def record_change_event(
    session: Session,
    outcome: SnapshotOutcome,
    *,
    changes: list[dict[str, Any]],
    summary: str,
    significance: str,
    title: str,
    is_synthetic: bool = False,
) -> ChangeEvent:
    event = ChangeEvent(
        source_id=outcome.source.id,
        from_snapshot_id=outcome.previous.id if outcome.previous else None,
        to_snapshot_id=outcome.snapshot.id,
        summary=summary,
        changes=changes,
        significance=significance,
        is_synthetic=is_synthetic,
    )
    session.add(event)
    session.flush()
    enqueue_review(
        session,
        item_type="SOURCE_CHANGE",
        target_type="change_event",
        target_id=event.id,
        title=title,
        reason=summary,
        priority=significance,
        payload={"source_key": outcome.source.key, "n_changes": len(changes), "synthetic": is_synthetic},
        dedupe_key=f"change:{outcome.source.key}:{outcome.snapshot.content_hash[:16]}",
    )
    audit(
        session,
        "source.change_detected",
        target_type="change_event",
        target_id=event.id,
        key=outcome.source.key,
        significance=significance,
        n_changes=len(changes),
        synthetic=is_synthetic,
    )
    return event


def latest_snapshot(session: Session, source_id: int) -> SourceSnapshot | None:
    return session.scalar(
        select(SourceSnapshot).where(SourceSnapshot.source_id == source_id).order_by(SourceSnapshot.version_no.desc()).limit(1)
    )


def source_text_for_anchoring(session: Session, source: EvidenceSource) -> tuple[str | None, list[str]]:
    """Return (full_text_or_None, excerpts) available for quote anchoring."""
    snap = latest_snapshot(session, source.id)
    if snap is None:
        return None, []
    return snap.content, list(snap.excerpts or [])
