"""Read models for evidence review: the list, the per-row dossier and the summary.

Everything here is read-only. The list is served from the current rows of ``evidence_version``
(one indexed table for all seven evidence types); the dossier is built from the evidence row itself
and everything a reviewer needs to decide on it. Rejected evidence is visible here, labelled: this
is an inspection surface.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import INCLUDE_REJECTED
from ..enums import TIER_LABEL, EvidenceTier, VerificationStatus
from ..models import (
    REVIEWABLE_MODELS,
    AdverseEvent,
    AuditEvent,
    Drug,
    Endpoint,
    Estimand,
    EvidenceSource,
    EvidenceVersion,
    Formulation,
    OutcomeResult,
    ReviewDecision,
    SourceSnapshot,
    StatisticalAnalysis,
    Study,
    StudyArm,
)
from . import identity
from .anchoring import ANCHOR_BASIS_LABEL, FULL_TEXT, STRUCTURED_PAYLOAD, anchor
from .lifecycle import (
    REASON_TEXT,
    DecisionFact,
    StatusOutcome,
    attach_decisions,
    effective_status,
    load_decision_facts,
)
from .policy import (
    DECISIVE_DECISIONS,
    MECHANICAL_STATUSES,
    REVIEWABLE_TYPES,
    RISK_ORDER,
    STATUS_ATTENTION_ORDER,
    STATUS_LABEL,
    is_decisive,
    is_usable,
)

RECHECK = VerificationStatus.SOURCE_CHANGED_RECHECK_REQUIRED.value
_OPT_IN = {INCLUDE_REJECTED: True}

SORT_KEYS = ("status", "risk", "study", "last_change")
FACETS = ("evidence_type", "status", "tier", "source_type", "drug", "risk_level", "endpoint_category", "origin", "missing_field")


@dataclass(slots=True)
class IndexRow:
    version_id: int
    evidence_type: str
    evidence_key: str
    natural_key: str
    fingerprint: str
    label: str
    study_nct: str | None
    study_name: str | None
    drug_slugs: str | None
    origin: str | None
    source_key: str | None
    source_type: str | None
    tier: str | None
    endpoint_category: str | None
    missing_fields: str | None
    collision_level: int
    mechanical_status: str | None
    effective_status: str | None
    risk_level: str | None
    supersedes_id: int | None
    current_since: datetime | None
    search_text: str | None

    @property
    def drugs(self) -> list[str]:
        return [s for s in (self.drug_slugs or "").split("|") if s]

    @property
    def missing(self) -> list[str]:
        return [s for s in (self.missing_fields or "").split("|") if s]


_INDEX_COLUMNS = (
    "id", "evidence_type", "evidence_key", "natural_key", "fingerprint", "label", "study_nct", "study_name", "drug_slugs", "origin", "source_key",
    "source_type", "tier", "endpoint_category", "missing_fields", "collision_level", "mechanical_status", "effective_status", "risk_level",
    "supersedes_id", "current_since", "search_text",
)


def index_rows(session: Session) -> list[IndexRow]:
    """The current version of every reviewable row: the light columns only (no snapshot, no flags)."""
    columns = [getattr(EvidenceVersion, name) for name in _INDEX_COLUMNS]
    return [IndexRow(*row) for row in session.execute(select(*columns).where(EvidenceVersion.is_current.is_(True)))]


@dataclass(slots=True)
class ReviewState:
    """Decision-derived facts about every live evidence key."""

    attached: dict[tuple[str, str], tuple[DecisionFact, ...]]
    orphans: list[DecisionFact]
    open_requests: dict[tuple[str, str], tuple[DecisionFact, ...]]
    latest_decisive: dict[tuple[str, str], DecisionFact]
    decided_fingerprints: dict[tuple[str, str], set[str]]


def review_state(session: Session, rows: list[IndexRow]) -> ReviewState:
    decisions = load_decision_facts(session)
    attached, orphans = attach_decisions(((r.evidence_type, r.evidence_key, r.natural_key, r.fingerprint) for r in rows), decisions)
    open_requests: dict[tuple[str, str], tuple[DecisionFact, ...]] = {}
    latest: dict[tuple[str, str], DecisionFact] = {}
    decided: dict[tuple[str, str], set[str]] = {}
    for key, facts in attached.items():
        decisive = [d for d in facts if d.decisive]
        if decisive:
            latest[key] = decisive[-1]
        requests = tuple(d for d in facts if d.decision not in DECISIVE_DECISIONS and (not decisive or (d.decided_at, d.id) > (decisive[-1].decided_at, decisive[-1].id)))
        if requests:
            open_requests[key] = requests
        decided[key] = {d.fingerprint for d in facts if d.fingerprint}
    return ReviewState(attached, orphans, open_requests, latest, decided)


# --------------------------------------------------------------------------- list


@dataclass(slots=True)
class EvidenceFilters:
    evidence_type: list[str] | None = None
    status: list[str] | None = None
    drug: str | None = None
    nct: str | None = None
    source_type: str | None = None
    source_key: str | None = None
    tier: str | None = None
    endpoint_category: str | None = None
    risk_level: list[str] | None = None
    missing_field: str | None = None
    origin: str | None = None
    changed_since_last_seen: bool | None = None
    changed_since_review: bool | None = None
    has_open_request: bool | None = None
    q: str | None = None


def _changed_since_last_seen(row: IndexRow, state: ReviewState) -> bool:
    """The stored content replaced an earlier version and nobody has recorded anything on the new one yet."""
    return row.supersedes_id is not None and row.fingerprint not in state.decided_fingerprints.get((row.evidence_type, row.evidence_key), ())


def _predicates(filters: EvidenceFilters, state: ReviewState) -> dict[str, Any]:
    """One predicate per active filter, keyed by the facet it belongs to (so a facet can ignore its own filter)."""
    tests: dict[str, Any] = {}
    if filters.evidence_type:
        wanted_types = set(filters.evidence_type)
        tests["evidence_type"] = lambda r: r.evidence_type in wanted_types
    if filters.status:
        wanted_status = set(filters.status)
        tests["status"] = lambda r: r.effective_status in wanted_status
    if filters.drug:
        tests["drug"] = lambda r: f"|{filters.drug}|" in (r.drug_slugs or "")
    if filters.nct:
        nct = filters.nct.upper()
        tests["nct"] = lambda r: r.study_nct == nct
    if filters.source_type:
        tests["source_type"] = lambda r: r.source_type == filters.source_type
    if filters.source_key:
        tests["source_key"] = lambda r: r.source_key == filters.source_key
    if filters.tier:
        tests["tier"] = lambda r: r.tier == filters.tier
    if filters.endpoint_category:
        tests["endpoint_category"] = lambda r: r.endpoint_category == filters.endpoint_category
    if filters.risk_level:
        wanted_risk = set(filters.risk_level)
        tests["risk_level"] = lambda r: (r.risk_level or "NONE") in wanted_risk
    if filters.missing_field:
        tests["missing_field"] = lambda r: f"|{filters.missing_field}|" in (r.missing_fields or "")
    if filters.origin:
        tests["origin"] = lambda r: r.origin == filters.origin
    if filters.changed_since_last_seen is not None:
        tests["changed_since_last_seen"] = lambda r: _changed_since_last_seen(r, state) == filters.changed_since_last_seen
    if filters.changed_since_review is not None:
        tests["changed_since_review"] = lambda r: (r.effective_status == RECHECK) == filters.changed_since_review
    if filters.has_open_request is not None:
        tests["has_open_request"] = lambda r: ((r.evidence_type, r.evidence_key) in state.open_requests) == filters.has_open_request
    if filters.q:
        needle = filters.q.strip().lower()
        tests["q"] = lambda r: any(needle in (text or "").lower() for text in (r.label, r.study_nct, r.study_name, r.source_key, r.search_text, r.drug_slugs, r.evidence_key))
    return tests


_SINGLE_FACETS = {"evidence_type": "evidence_type", "status": "effective_status", "tier": "tier", "source_type": "source_type", "risk_level": "risk_level",
                  "endpoint_category": "endpoint_category", "origin": "origin"}
_FACET_DEFAULT = {"status": "UNKNOWN", "risk_level": "NONE"}


def _count_facet(rows: list[IndexRow], facet: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    if facet in _SINGLE_FACETS:
        attribute, default = _SINGLE_FACETS[facet], _FACET_DEFAULT.get(facet)
        for row in rows:
            value = getattr(row, attribute) or default
            if value:
                counts[value] = counts.get(value, 0) + 1
    else:
        packed = "drug_slugs" if facet == "drug" else "missing_fields"
        for row in rows:
            text = getattr(row, packed)
            if text:
                for value in text.split("|"):
                    if value:
                        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _sort_key(sort: str, state: ReviewState):
    def last_change(row: IndexRow) -> float:
        return -(row.current_since.timestamp() if row.current_since else 0.0)

    def by_status(row: IndexRow) -> tuple:
        return (STATUS_ATTENTION_ORDER.get(row.effective_status or "", 9), RISK_ORDER.get(row.risk_level or "NONE", 9), row.study_nct or "~", row.label, row.evidence_key)

    def by_risk(row: IndexRow) -> tuple:
        return (RISK_ORDER.get(row.risk_level or "NONE", 9), STATUS_ATTENTION_ORDER.get(row.effective_status or "", 9), row.study_nct or "~", row.label, row.evidence_key)

    def by_study(row: IndexRow) -> tuple:
        return (row.study_name or row.study_nct or "~", row.evidence_type, row.label, row.evidence_key)

    def by_change(row: IndexRow) -> tuple:
        return (0 if row.supersedes_id is not None else 1, last_change(row), row.label, row.evidence_key)

    return {"status": by_status, "risk": by_risk, "study": by_study, "last_change": by_change}[sort]


def _value_summary(evidence_type: str, snapshot: dict[str, Any]) -> str | None:
    """A short rendering of what the row asserts, exactly as stored (no rounding, no derivation)."""

    def num(value: Any) -> str:
        return "not reported" if value is None else f"{value:g}" if isinstance(value, int | float) and not isinstance(value, bool) else str(value)

    if evidence_type == "result":
        unit = f" {snapshot.get('unit')}" if snapshot.get("unit") else ""
        if snapshot.get("value") is None and (snapshot.get("value_low") is not None or snapshot.get("value_high") is not None):
            text = f"{num(snapshot.get('value_low'))} to {num(snapshot.get('value_high'))}{unit} (range)"
        else:
            text = f"{num(snapshot.get('value'))}{unit}"
        if snapshot.get("ci_low") is not None and snapshot.get("ci_high") is not None:
            level = f"{num(snapshot.get('ci_level'))}% " if snapshot.get("ci_level") is not None else ""
            text += f" ({level}CI {num(snapshot['ci_low'])} to {num(snapshot['ci_high'])})"
        return text
    if evidence_type == "analysis":
        text = f"{snapshot.get('param_type') or 'estimate'}: {num(snapshot.get('estimate'))}"
        if snapshot.get("ci_low") is not None and snapshot.get("ci_high") is not None:
            level = f"{num(snapshot.get('ci_level'))}% " if snapshot.get("ci_level") is not None else ""
            text += f" ({level}CI {num(snapshot['ci_low'])} to {num(snapshot['ci_high'])})"
        if snapshot.get("p_value_text"):
            text += f"; p {snapshot['p_value_text']}"
        return text
    if evidence_type == "adverse_event":
        if snapshot.get("n_affected") is not None or snapshot.get("n_at_risk") is not None:
            return f"{num(snapshot.get('n_affected'))} of {num(snapshot.get('n_at_risk'))} participants"
        if snapshot.get("percent") is not None:
            return f"{num(snapshot['percent'])}%"
        if snapshot.get("percent_low") is not None or snapshot.get("percent_high") is not None:
            return f"{num(snapshot.get('percent_low'))}% to {num(snapshot.get('percent_high'))}% (range)"
        return "counts not reported"
    if evidence_type == "claim":
        return snapshot.get("statement")
    if evidence_type == "mechanism":
        return f"{snapshot.get('target')} {str(snapshot.get('action') or '').lower()}".strip()
    if evidence_type == "indication":
        return f"{snapshot.get('status')}: {snapshot.get('text')}"
    if evidence_type == "equivalence":
        return snapshot.get("statement")
    return None


def _row_ids(session: Session, rows: list[IndexRow]) -> dict[tuple[str, str], int]:
    keys: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        keys[row.evidence_type].append(row.evidence_key)
    out: dict[tuple[str, str], int] = {}
    for evidence_type, wanted in keys.items():
        model = REVIEWABLE_MODELS[evidence_type]
        for row_id, key in session.execute(select(model.id, model.evidence_key).where(model.evidence_key.in_(wanted)).execution_options(**_OPT_IN)):
            out[(evidence_type, key)] = row_id
    return out


def _decision_brief(fact: DecisionFact | None) -> dict[str, Any] | None:
    if fact is None:
        return None
    return {"decision_id": fact.id, "decision": fact.decision, "reviewer": fact.reviewer, "decided_at": fact.decided_at, "fingerprint": fact.fingerprint}


def list_evidence(session: Session, filters: EvidenceFilters, *, sort: str = "status", descending: bool = False, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    rows = index_rows(session)
    state = review_state(session, rows)
    tests = _predicates(filters, state)
    matched = [row for row in rows if all(test(row) for test in tests.values())]
    facets: dict[str, dict[str, int]] = {}
    for facet in FACETS:
        if facet in tests:  # a facet counts every value of its own filter, with the other filters applied
            others = [test for name, test in tests.items() if name != facet]
            facets[facet] = _count_facet([row for row in rows if all(test(row) for test in others)], facet)
        else:
            facets[facet] = _count_facet(matched, facet)
    matched.sort(key=_sort_key(sort, state), reverse=descending)
    page = matched[offset : offset + limit]
    ids = _row_ids(session, page)
    extra: dict[int, tuple[dict[str, Any], list[dict[str, Any]], Any]] = {}
    if page:
        for version_id, snapshot, check_flags, first_seen_at in session.execute(
            select(EvidenceVersion.id, EvidenceVersion.snapshot, EvidenceVersion.check_flags, EvidenceVersion.first_seen_at).where(EvidenceVersion.id.in_([r.version_id for r in page]))
        ):
            extra[version_id] = (snapshot or {}, check_flags or [], first_seen_at)
    items = []
    for row in page:
        key = (row.evidence_type, row.evidence_key)
        snapshot, flags, first_seen_at = extra.get(row.version_id, ({}, [], None))
        items.append({
            "evidence_type": row.evidence_type, "id": ids.get(key), "evidence_key": row.evidence_key, "fingerprint": row.fingerprint, "label": row.label,
            "value_summary": _value_summary(row.evidence_type, snapshot), "study_nct": row.study_nct, "study_name": row.study_name,
            "drugs": row.drugs, "origin": row.origin, "source_key": row.source_key, "source_type": row.source_type, "tier": row.tier,
            "tier_label": _tier_label(row.tier), "endpoint_category": row.endpoint_category, "verification_status": row.effective_status,
            "status_label": STATUS_LABEL.get(row.effective_status or "", row.effective_status), "base_verification_status": row.mechanical_status,
            "usable": is_usable(row.effective_status), "risk_level": row.risk_level or "NONE", "n_flags": len(flags), "flag_codes": [str(f.get("code")) for f in flags],
            "missing_fields": row.missing, "changed_since_last_seen": _changed_since_last_seen(row, state), "changed_since_review": row.effective_status == RECHECK,
            "changed_at": row.current_since if row.supersedes_id is not None else None, "first_seen_at": first_seen_at,
            "open_requests": len(state.open_requests.get(key, ())), "last_decision": _decision_brief(state.latest_decisive.get(key)),
            "identity_collision": row.collision_level > 0,
        })
    return {"total": len(matched), "limit": limit, "offset": offset, "sort": sort, "descending": descending, "rows": items, "facets": facets, "all_rows": len(rows)}


def _tier_label(tier: str | None) -> str | None:
    try:
        return TIER_LABEL[EvidenceTier(tier)] if tier else None
    except ValueError:
        return tier


# --------------------------------------------------------------------------- summary


def summarize(session: Session, *, list_limit: int = 100) -> dict[str, Any]:
    rows = index_rows(session)
    state = review_state(session, rows)
    cube: Counter[tuple[str, str, str, str]] = Counter()
    by_status: Counter[str] = Counter()
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        status = row.effective_status or "UNKNOWN"
        by_status[status] += 1
        by_type[row.evidence_type][status] += 1
        for drug in row.drugs or [""]:
            cube[(status, row.evidence_type, row.tier or "", drug)] += 1
    labels = {(r.evidence_type, r.evidence_key): r for r in rows}
    open_requests = []
    for key, facts in state.open_requests.items():
        row = labels[key]
        for fact in facts:
            open_requests.append({"evidence_type": key[0], "evidence_key": key[1], "label": row.label, "study_nct": row.study_nct, "decision_id": fact.id,
                                  "decision": fact.decision, "reviewer": fact.reviewer, "decided_at": fact.decided_at, "on_current_version": fact.fingerprint == row.fingerprint})
    open_requests.sort(key=lambda r: (r["decided_at"], r["decision_id"]), reverse=True)
    orphan_labels: dict[tuple[str, str, str], tuple[str, str | None]] = {}
    if state.orphans:
        wanted = {d.evidence_key for d in state.orphans}
        for evidence_type, evidence_key, fingerprint, label, study in session.execute(
            select(EvidenceVersion.evidence_type, EvidenceVersion.evidence_key, EvidenceVersion.fingerprint, EvidenceVersion.label, EvidenceVersion.study_nct)
            .where(EvidenceVersion.evidence_key.in_(wanted))
        ):
            orphan_labels[(evidence_type, evidence_key, fingerprint)] = (label, study)
    orphans = []
    for fact in state.orphans:
        label, study = orphan_labels.get((fact.evidence_type, fact.evidence_key, fact.fingerprint or ""), (None, None))
        orphans.append({"decision_id": fact.id, "uid": fact.uid, "evidence_type": fact.evidence_type, "evidence_key": fact.evidence_key, "fingerprint": fact.fingerprint,
                        "decision": fact.decision, "reviewer": fact.reviewer, "decided_at": fact.decided_at, "label": label, "study_nct": study})
    changed_since_review = [
        {"evidence_type": r.evidence_type, "evidence_key": r.evidence_key, "label": r.label, "study_nct": r.study_nct, "changed_at": r.current_since,
         "last_decision": _decision_brief(state.latest_decisive.get((r.evidence_type, r.evidence_key)))}
        for r in rows if r.effective_status == RECHECK
    ]
    unreconciled = {
        evidence_type: session.scalar(select(func.count()).select_from(model).where(or_(model.evidence_key.is_(None), model.fingerprint.is_(None))).execution_options(**_OPT_IN)) or 0
        for evidence_type, model in REVIEWABLE_MODELS.items()
    }
    n_decisions = session.scalar(select(func.count()).select_from(ReviewDecision).where(ReviewDecision.evidence_type.is_not(None))) or 0
    return {
        "total": len(rows),
        "by_status": {status: by_status.get(status, 0) for status in STATUS_LABEL},
        "by_type": {t: dict(by_type[t]) for t in REVIEWABLE_TYPES if t in by_type},
        "counts": [{"status": s, "evidence_type": t, "tier": tier or None, "drug": drug or None, "n": n} for (s, t, tier, drug), n in sorted(cube.items())],
        "status_labels": dict(STATUS_LABEL),
        "decisions_recorded": n_decisions,
        "open_requests_total": len(open_requests),
        "open_requests": open_requests[:list_limit],
        "orphaned_decisions_total": len(orphans),
        "orphaned_decisions": orphans[:list_limit],
        "changed_since_review_total": len(changed_since_review),
        "changed_since_review": changed_since_review[:list_limit],
        "changed_since_last_seen_total": sum(1 for r in rows if _changed_since_last_seen(r, state)),
        "identity_collisions": sum(1 for r in rows if r.collision_level > 0),
        "unreconciled_rows": {t: n for t, n in unreconciled.items() if n},
    }


# --------------------------------------------------------------------------- dossier


def find_row(session: Session, evidence_type: str, *, row_id: int | None = None, evidence_key: str | None = None) -> Any | None:
    model = REVIEWABLE_MODELS.get(evidence_type)
    if model is None:
        return None
    if row_id is not None:
        return session.get(model, row_id, execution_options=_OPT_IN)
    return session.scalar(select(model).where(model.evidence_key == evidence_key).execution_options(**_OPT_IN))


def decisions_for(session: Session, evidence_type: str, evidence_key: str | None, natural_key: str | None) -> tuple[DecisionFact, ...]:
    """The decision history of one evidence row, attached exactly as ``reconcile`` attaches it."""
    if not evidence_key:
        return ()
    d = ReviewDecision
    condition = d.evidence_key == evidence_key if natural_key is None else or_(d.evidence_key == evidence_key, d.natural_key == natural_key)
    rows = session.execute(
        select(d.id, d.decision, d.fingerprint, d.actor_kind, d.attested, d.created_at, d.evidence_type, d.evidence_key, d.natural_key, d.source_content_hash, d.reviewer, d.uid)
        .where(d.evidence_type == evidence_type, condition).order_by(d.created_at, d.id)
    ).all()
    if not rows:
        return ()
    facts = [
        DecisionFact(decision=r.decision, fingerprint=r.fingerprint, actor_kind=r.actor_kind, attested=r.attested, decided_at=r.created_at, id=r.id,
                     evidence_type=r.evidence_type, evidence_key=r.evidence_key, natural_key=r.natural_key, source_content_hash=r.source_content_hash,
                     reviewer=r.reviewer, uid=r.uid)
        for r in rows
    ]
    v = EvidenceVersion
    scope = v.evidence_key == evidence_key if natural_key is None else or_(v.evidence_key == evidence_key, v.natural_key == natural_key)
    live = session.execute(select(v.evidence_type, v.evidence_key, v.natural_key, v.fingerprint).where(v.is_current.is_(True), v.evidence_type == evidence_type, scope)).all()
    attached, _ = attach_decisions([tuple(r) for r in live], facts)
    return attached.get((evidence_type, evidence_key), ())


def _source_block(session: Session, source: EvidenceSource | None, snapshot: SourceSnapshot | None) -> dict[str, Any] | None:
    if source is None:
        return None
    latest = session.scalar(select(func.max(SourceSnapshot.version_no)).where(SourceSnapshot.source_id == source.id))
    return {
        "id": source.id, "key": source.key, "title": source.title, "url": source.url, "source_type": source.source_type, "tier": source.tier,
        "tier_label": _tier_label(source.tier), "publisher": source.publisher, "published_date": source.published_date, "retrieved_at": source.retrieved_at,
        "content_hash": snapshot.content_hash if snapshot else source.content_hash, "snapshot_id": snapshot.id if snapshot else None,
        "snapshot_version": snapshot.version_no if snapshot else None, "latest_snapshot_version": latest,
        "snapshot_retrieved_at": snapshot.retrieved_at if snapshot else None, "version_label": snapshot.version_label if snapshot else source.version_label,
        "license_note": source.license_note, "stores_full_text": bool(snapshot.content is not None) if snapshot else bool(source.stores_full_text),
        "is_synthetic": bool(snapshot.is_synthetic) if snapshot else False,
    }


def _quote_context(quote: str, text: str, width: int = 240) -> str | None:
    from ..ingestion.snapshots import normalize_ws

    haystack, needle = normalize_ws(text), normalize_ws(quote)
    at = haystack.lower().find(needle.lower())
    if at < 0:
        return None
    start, end = max(0, at - width), min(len(haystack), at + len(needle) + width)
    return ("… " if start else "") + haystack[start:end] + (" …" if end < len(haystack) else "")


def _anchoring_block(row: Any, quote: str | None, locator: str | None, snapshot: SourceSnapshot | None, structured: bool) -> dict[str, Any]:
    if structured:
        return {"basis": STRUCTURED_PAYLOAD, "basis_label": ANCHOR_BASIS_LABEL[STRUCTURED_PAYLOAD], "quote": None, "locator": locator, "context": None,
                "checked_against_snapshot_version": snapshot.version_no if snapshot else None,
                "note": "Machine-extracted rows have no quote: the value was read from the stored registry JSON at the location shown."}
    result = anchor(quote, snapshot.content if snapshot else None, list(snapshot.excerpts or []) if snapshot else [])
    context = _quote_context(quote, snapshot.content) if (result.basis == FULL_TEXT and quote and snapshot and snapshot.content) else None
    return {
        "basis": result.basis, "basis_label": ANCHOR_BASIS_LABEL[result.basis], "quote": quote, "locator": locator, "context": context,
        "checked_against_snapshot_version": snapshot.version_no if snapshot else None,
        "note": "Anchoring shows that the quoted words are in the source. It does not show that the number belongs to this study, arm, endpoint, estimand or population.",
    }


_FRAGMENT_LIMIT = 6000


def _registry_block(session: Session, row: Any, snapshot: SourceSnapshot | None) -> dict[str, Any] | None:
    """The JSON at the row's registry location, read from the stored snapshot, with the labels around it."""
    location = getattr(row, "source_location", None)
    if not location or snapshot is None:
        return None
    ref = identity.SnapshotRef(snapshot.id, snapshot.source_id, snapshot.version_no, snapshot.content_hash, snapshot.content is not None)
    chain = identity.resolve_json_path(identity.parsed_snapshot(session, ref), location)
    if not chain:
        return {"location": location, "resolved": False, "fragment": None, "fragment_truncated": False, "labels": {},
                "note": "The location could not be resolved in the stored snapshot."}
    node = chain[-1][1]
    labels: dict[str, Any] = {}
    measure = next((n for _, n in chain if isinstance(n, dict) and "title" in n and ("classes" in n or "analyses" in n)), None)
    if measure is not None:
        labels.update({
            "measure_title": measure.get("title"), "measure_type": measure.get("type"), "measure_description": measure.get("description"),
            "analysis_population": measure.get("populationDescription"), "time_frame": measure.get("timeFrame"), "unit": measure.get("unitOfMeasure"),
            "param_type": measure.get("paramType"), "dispersion_type": measure.get("dispersionType"), "reporting_status": measure.get("reportingStatus"),
        })
        groups = {g.get("id"): g for g in measure.get("groups", []) if isinstance(g, dict)}
        klass = next((n for token, n in chain if isinstance(n, dict) and "categories" in n), None)
        category = next((n for token, n in chain if isinstance(n, dict) and "measurements" in n), None)
        if klass is not None:
            labels["class_title"] = klass.get("title")
            labels["class_denominators"] = klass.get("denoms")
        if category is not None:
            labels["category_title"] = category.get("title")
        if isinstance(node, dict) and node.get("groupId") in groups:
            labels["group_id"] = node["groupId"]
            labels["group_title"] = groups[node["groupId"]].get("title")
            labels["group_description"] = groups[node["groupId"]].get("description")
        if isinstance(node, dict) and "groupIds" in node:
            labels["analysis_groups"] = [{"id": g, "title": groups.get(g, {}).get("title")} for g in node.get("groupIds", [])]
            labels["analysis_group_description"] = node.get("groupDescription")
        labels["measure_denominators"] = measure.get("denoms")
    else:  # adverse-event module or participant flow
        module = next((n for token, n in chain if token in ("adverseEventsModule", "participantFlowModule")), None)
        if isinstance(module, dict):
            labels.update({"time_frame": module.get("timeFrame"), "frequency_threshold": module.get("frequencyThreshold"), "description": module.get("description")})
            groups = module.get("eventGroups") or module.get("groups") or []
            group = next((g for g in groups if isinstance(g, dict) and g.get("title") == getattr(row, "arm_label", None)), None)
            if group is not None:
                labels["group_id"], labels["group_title"], labels["group_description"] = group.get("id"), group.get("title"), group.get("description")
                if isinstance(node, dict) and "stats" in node:
                    node = {**{k: v for k, v in node.items() if k != "stats"}, "stats": [s for s in node["stats"] if s.get("groupId") == group.get("id")]}
        if isinstance(node, dict):
            labels.setdefault("term", node.get("term"))
            labels.setdefault("organ_system", node.get("organSystem"))
            labels.setdefault("period_title", node.get("title"))
    text = json.dumps(node, ensure_ascii=False)
    truncated = len(text) > _FRAGMENT_LIMIT
    return {
        "location": location, "resolved": True, "fragment": None if truncated else node, "fragment_text": text[:_FRAGMENT_LIMIT] if truncated else None,
        "fragment_truncated": truncated, "labels": {k: v for k, v in labels.items() if v is not None},
        "note": "Read from the stored snapshot of the registry record. The location is positional and can shift when the sponsor inserts an outcome measure.",
    }


def _arm_block(arm: StudyArm | None, label: str | None, role: str) -> dict[str, Any] | None:
    if arm is None and label is None:
        return None
    return {
        "role": role, "label": label, "registered_label": arm.label if arm else None, "arm_type": arm.arm_type if arm else None,
        "drug_slug": arm.drug_slug if arm else None, "dose_amount": arm.dose_amount if arm else None, "dose_unit": arm.dose_unit if arm else None,
        "dose_text": arm.dose_text if arm else None, "formulation_key": arm.formulation_key if arm else None, "is_placebo": arm.is_placebo if arm else None,
        "is_pooled": arm.is_pooled if arm else None, "n_randomized": arm.n_randomized if arm else None, "origin": arm.origin if arm else None,
        "linked": arm is not None,
    }


def _endpoint_block(endpoint: Endpoint | None) -> dict[str, Any] | None:
    if endpoint is None:
        return None
    from ..engines.findings import CATEGORY_LABEL

    return {
        "title": endpoint.title, "category": endpoint.category, "category_label": CATEGORY_LABEL.get(endpoint.category), "threshold": endpoint.threshold,
        "role": endpoint.role, "unit": endpoint.unit, "timeframe_text": endpoint.timeframe_text, "timepoint_weeks": endpoint.timepoint_weeks,
        "hierarchy_rank": endpoint.hierarchy_rank, "multiplicity_controlled": endpoint.multiplicity_controlled, "kind": endpoint.kind, "origin": endpoint.origin,
        "description": endpoint.description, "is_pooled_arms": endpoint.is_pooled_arms,
    }


def _estimand_block(estimand: Estimand | None) -> dict[str, Any] | None:
    if estimand is None:
        return None
    return {
        "label": estimand.label, "basis": estimand.basis, "description": estimand.description, "population_text": estimand.population_text, "quote": estimand.quote,
        "strategies": [{"event": s.event, "strategy": s.strategy, "basis": s.basis, "quote": s.quote} for s in estimand.strategies],
    }


def _study_block(study: Study | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if study is None:
        return None, None
    population = study.population
    study_block = {
        "nct_id": study.nct_id, "display_name": study.display_name, "brief_title": study.brief_title, "phase": study.phase, "overall_status": study.overall_status,
        "allocation": study.allocation, "masking": study.masking, "program": study.program, "design_features": study.design_features or {},
        "has_results": bool(study.has_results), "conditions": study.conditions or [],
    }
    population_block = None
    if population is not None:
        population_block = {
            "diabetes_status": population.diabetes_status, "diabetes_basis": population.diabetes_basis, "sex": population.sex, "min_age": population.min_age,
            "max_age": population.max_age, "description": population.description, "baseline_summary": population.summary or {},
        }
    return study_block, population_block


def _drug_blocks(session: Session, slugs: list[str | None], formulation_keys: list[str | None]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    wanted = [s for s in dict.fromkeys(slugs) if s]
    drugs = [
        {"slug": d.slug, "name": d.name, "code_name": d.code_name, "regulatory_status": d.regulatory_status, "sponsor": d.sponsor, "route": d.route}
        for d in (session.scalars(select(Drug).where(Drug.slug.in_(wanted))).all() if wanted else [])
    ]
    keys = [k for k in dict.fromkeys(formulation_keys) if k]
    formulations = [
        {"key": f.key, "name": f.name, "dosage_form": f.dosage_form, "route": f.route, "stage": f.stage}
        for f in (session.scalars(select(Formulation).where(Formulation.key.in_(keys))).all() if keys else [])
    ]
    return drugs, formulations


def _related(evidence_type: str, row: Any, relation: str) -> dict[str, Any]:
    source = getattr(row, "source", None)
    base = {
        "evidence_type": evidence_type, "id": row.id, "evidence_key": row.evidence_key, "relation": relation, "verification_status": row.verification_status,
        "tier": getattr(row, "tier", None), "source_key": source.key if source else None, "origin": getattr(row, "origin", None),
        "usable": is_usable(row.verification_status),
    }
    if evidence_type == "result":
        base.update({"label": f"{row.endpoint.title} — {row.arm_label}", "values": {
            "value": row.value, "value_low": row.value_low, "value_high": row.value_high, "unit": row.unit, "ci_low": row.ci_low, "ci_high": row.ci_high,
            "ci_level": row.ci_level, "n_analyzed": row.n_analyzed, "timepoint_weeks": row.timepoint_weeks,
            "estimand": row.estimand.label if row.estimand else None}})
    elif evidence_type == "analysis":
        base.update({"label": f"{row.endpoint.title} — {row.treatment_label} vs {row.comparator_label}", "values": {
            "param_type": row.param_type, "estimate": row.estimate, "unit": row.unit, "ci_low": row.ci_low, "ci_high": row.ci_high, "ci_level": row.ci_level,
            "p_value_text": row.p_value_text, "hypothesis_type": row.hypothesis_type, "method": row.method, "timepoint_weeks": row.timepoint_weeks,
            "estimand": row.estimand.label if row.estimand else None}})
    else:
        base.update({"label": f"{row.term} — {row.arm_label}", "values": {
            "n_affected": row.n_affected, "n_at_risk": row.n_at_risk, "n_events": row.n_events, "percent": row.percent, "measure_kind": row.measure_kind, "period": row.period}})
    return base


def _same_arm(a: StudyArm | None, b: StudyArm | None, a_id: int | None, b_id: int | None) -> bool:
    if a_id is not None and a_id == b_id:
        return True
    if a is None or b is None:
        return False
    return (a.drug_slug, a.dose_amount, bool(a.is_placebo), a.formulation_key) == (b.drug_slug, b.dose_amount, bool(b.is_placebo), b.formulation_key) and a.drug_slug is not None


def _related_rows(session: Session, evidence_type: str, row: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(between-arm analyses or arm-level results for the same endpoint, rows reporting the same quantity from other sources)."""
    related: list[dict[str, Any]] = []
    same: list[dict[str, Any]] = []
    if evidence_type == "result":
        analyses = session.scalars(
            select(StatisticalAnalysis).where(StatisticalAnalysis.endpoint_id == row.endpoint_id).execution_options(**_OPT_IN)
        ).all()
        for a in analyses:
            if row.arm_id is None or row.arm_id in (a.treatment_arm_id, a.comparator_arm_id) or a.treatment_arm_id is None:
                related.append(_related("analysis", a, "between-arm analysis on the same endpoint"))
        others = session.scalars(
            select(OutcomeResult).join(Endpoint, OutcomeResult.endpoint_id == Endpoint.id)
            .where(OutcomeResult.study_id == row.study_id, OutcomeResult.id != row.id, Endpoint.category == row.endpoint.category).execution_options(**_OPT_IN)
        ).all()
        for other in others:
            if other.source_id != row.source_id and other.timepoint_weeks == row.timepoint_weeks and _same_arm(row.arm, other.arm, row.arm_id, other.arm_id):
                same.append(_related("result", other, "same study, arm, endpoint category and time point; other source"))
    elif evidence_type == "analysis":
        arm_ids = [i for i in (row.treatment_arm_id, row.comparator_arm_id) if i is not None]
        if arm_ids:
            for r in session.scalars(select(OutcomeResult).where(OutcomeResult.endpoint_id == row.endpoint_id, OutcomeResult.arm_id.in_(arm_ids)).execution_options(**_OPT_IN)):
                related.append(_related("result", r, "arm-level result on the same endpoint"))
        others = session.scalars(
            select(StatisticalAnalysis).join(Endpoint, StatisticalAnalysis.endpoint_id == Endpoint.id)
            .where(StatisticalAnalysis.study_id == row.study_id, StatisticalAnalysis.id != row.id, Endpoint.category == row.endpoint.category).execution_options(**_OPT_IN)
        ).all()
        mine = {row.treatment_arm_id, row.comparator_arm_id}
        for other in others:
            if other.source_id != row.source_id and other.timepoint_weeks == row.timepoint_weeks and {other.treatment_arm_id, other.comparator_arm_id} == mine and None not in mine:
                same.append(_related("analysis", other, "same study, arms, endpoint category and time point; other source"))
    elif evidence_type == "adverse_event":
        others = session.scalars(
            select(AdverseEvent).where(AdverseEvent.study_id == row.study_id, AdverseEvent.id != row.id, AdverseEvent.measure_kind == row.measure_kind,
                                       func.lower(AdverseEvent.term) == (row.term or "").lower()).execution_options(**_OPT_IN)
        ).all()
        for other in others:
            if other.source_id != row.source_id and _same_arm(row.arm, other.arm, row.arm_id, other.arm_id):
                same.append(_related("adverse_event", other, "same study, arm, term and measure; other source"))
    return related[:60], same[:60]


_VALUE_KEYS = {
    "result": ("value", "value_text", "value_low", "value_high", "unit", "param_type", "dispersion_type", "dispersion_value", "ci_low", "ci_high", "ci_level",
               "n_analyzed", "scope", "class_title", "analysis_population", "analysis_method", "baseline_definition", "notes"),
    "analysis": ("param_type", "estimate", "estimate_text", "unit", "ci_low", "ci_high", "ci_level", "ci_sides", "p_value_text", "hypothesis_type", "method",
                 "ni_margin", "ni_margin_text", "ni_met", "superiority_tested", "superiority_met", "multiplicity_controlled", "group_description", "comment", "notes"),
    "adverse_event": ("term", "organ_system", "category", "measure_kind", "is_serious", "severity", "n_affected", "n_at_risk", "n_events", "percent",
                      "percent_low", "percent_high", "period", "scope", "notes"),
    "claim": ("key", "topic", "statement", "claim_kind"),
    "mechanism": ("target", "action", "description"),
    "indication": ("brand", "text", "status", "jurisdiction", "approval_date"),
    "equivalence": ("dose_a", "dose_b", "basis", "statement"),
}


def _decision_out(decision: ReviewDecision, row: Any) -> dict[str, Any]:
    return {
        "id": decision.id, "uid": decision.uid, "decision": decision.decision, "reviewer": decision.reviewer, "rationale": decision.rationale,
        "attested": bool(decision.attested), "actor_kind": decision.actor_kind, "created_at": decision.created_at, "fingerprint": decision.fingerprint,
        "on_current_version": decision.fingerprint == row.fingerprint, "decisive": is_decisive(decision.decision, decision.actor_kind, decision.attested),
        "source_key": decision.source_key, "source_content_hash": decision.source_content_hash, "source_snapshot_version": decision.source_snapshot_version,
        "status_before": decision.status_before, "status_after": decision.status_after, "proposed_correction": decision.proposed_correction,
        "recorded_under_other_key": decision.evidence_key != row.evidence_key, "imported": decision.imported_from is not None,
    }


def evidence_detail(session: Session, evidence_type: str, row: Any) -> dict[str, Any]:
    """Everything a reviewer needs to decide on one evidence row. Read-only."""
    model = REVIEWABLE_MODELS[evidence_type]
    assert isinstance(row, model)
    structured = getattr(row, "origin", None) == identity.REGISTRY
    source: EvidenceSource | None = row.source
    snapshot = session.get(SourceSnapshot, row.source_snapshot_id) if row.source_snapshot_id else None
    quote = getattr(row, "locator_quote", None) if hasattr(row, "locator_quote") else getattr(row, "quote", None)
    locator = getattr(row, "source_location", None) if hasattr(row, "source_location") else getattr(row, "locator", None)

    versions = session.scalars(
        select(EvidenceVersion).where(EvidenceVersion.evidence_type == evidence_type, EvidenceVersion.evidence_key == row.evidence_key).order_by(EvidenceVersion.id.desc())
    ).all() if row.evidence_key else []
    current = next((v for v in versions if v.fingerprint == row.fingerprint), None)
    snapshot_fields: dict[str, Any] = dict(current.snapshot or {}) if current else {}

    # Decision history: the same attachment rule reconcile uses (own key, plus decisions re-attached through the natural key).
    mine = decisions_for(session, evidence_type, row.evidence_key, current.natural_key if current else None)
    decisions = session.scalars(select(ReviewDecision).where(ReviewDecision.id.in_([f.id for f in mine])).order_by(ReviewDecision.created_at.desc(), ReviewDecision.id.desc())).all() if mine else []
    outcome: StatusOutcome | None = None
    if row.fingerprint:
        mechanical = row.base_verification_status if row.base_verification_status in MECHANICAL_STATUSES else VerificationStatus.PENDING_HUMAN_REVIEW.value
        outcome = effective_status(mechanical=mechanical, decisions=mine, fingerprint=row.fingerprint, structured=structured,
                                   source_content_hash=snapshot.content_hash if snapshot else None)
    decided_by = outcome.decision if outcome else None
    open_ids = {f.id for f in outcome.open_requests} if outcome else set()

    changed_since_review = None
    if decided_by is not None and decided_by.fingerprint != row.fingerprint:
        reviewed = session.scalar(select(EvidenceVersion).where(EvidenceVersion.evidence_type == evidence_type, EvidenceVersion.fingerprint == decided_by.fingerprint,
                                                                EvidenceVersion.evidence_key == decided_by.evidence_key))
        changed_since_review = {
            "decision_id": decided_by.id, "decision": decided_by.decision, "reviewer": decided_by.reviewer, "decided_at": decided_by.decided_at,
            "reviewed_fingerprint": decided_by.fingerprint, "current_fingerprint": row.fingerprint,
            "diff": identity.diff_snapshots(reviewed.snapshot if reviewed else None, snapshot_fields) if reviewed else [],
            "reviewed_version_available": reviewed is not None,
        }

    study: Study | None = getattr(row, "study", None)
    study_block, population_block = _study_block(study)
    endpoint = getattr(row, "endpoint", None)
    estimand = getattr(row, "estimand", None)
    arms: list[dict[str, Any]] = []
    slugs: list[str | None] = []
    formulation_keys: list[str | None] = []
    if evidence_type in ("result", "adverse_event"):
        arms = [b for b in [_arm_block(row.arm, row.arm_label, "arm")] if b]
    elif evidence_type == "analysis":
        arms = [b for b in [_arm_block(row.treatment_arm, row.treatment_label, "treatment"), _arm_block(row.comparator_arm, row.comparator_label, "comparator")] if b]
    for arm in arms:
        slugs.append(arm["drug_slug"])
        formulation_keys.append(arm["formulation_key"])
    drug = getattr(row, "drug", None)
    if drug is not None:
        slugs.append(drug.slug)
    if evidence_type == "equivalence":
        slugs.append(row.dose_a.formulation.drug.slug)
        formulation_keys += [row.dose_a.formulation.key, row.dose_b.formulation.key]
    if study is not None and not any(slugs):
        slugs += [link.drug.slug for link in study.drug_links if link.role != "COMPARATOR"]
    drugs, formulations = _drug_blocks(session, slugs, formulation_keys)

    related_analyses, same_quantity = _related_rows(session, evidence_type, row)
    ci = None
    if evidence_type in ("result", "analysis"):
        ci = {"low": row.ci_low, "high": row.ci_high, "level": row.ci_level, "sides": getattr(row, "ci_sides", None),
              "status": "REPORTED" if row.ci_low is not None and row.ci_high is not None else "NOT_REPORTED"}
    time_point = None
    if evidence_type in ("result", "analysis"):
        time_point = {"weeks": row.timepoint_weeks, "text": getattr(row, "timeframe_text", None) or (endpoint.timeframe_text if endpoint else None),
                      "baseline_definition": getattr(row, "baseline_definition", None)}
    elif evidence_type == "adverse_event":
        time_point = {"weeks": None, "text": row.period, "baseline_definition": None}

    audit_rows = session.scalars(
        select(AuditEvent).where(AuditEvent.target_id == row.evidence_key, AuditEvent.action.like("review.%")).order_by(AuditEvent.id.desc()).limit(60)
    ).all() if row.evidence_key else []
    flags = row.check_flags or []
    status = row.verification_status
    collision = None
    if current is not None and current.collision_level:
        collision = {"level": current.collision_level, "explanation": next((f.get("explanation") for f in flags if str(f.get("code", "")).startswith("IDENTITY_")), None)}
    return {
        "evidence_type": evidence_type, "id": row.id, "evidence_key": row.evidence_key, "natural_key": current.natural_key if current else None,
        "fingerprint": row.fingerprint, "label": current.label if current else None, "origin": getattr(row, "origin", None) or identity.CURATED,
        "reconciled": bool(row.evidence_key and row.fingerprint and current is not None),
        "status": {
            "verification_status": status, "label": STATUS_LABEL.get(status, status), "base_verification_status": row.base_verification_status,
            "base_label": STATUS_LABEL.get(row.base_verification_status or "", row.base_verification_status), "usable": is_usable(status),
            "reason": outcome.reason if outcome else None, "reason_text": REASON_TEXT.get(outcome.reason) if outcome else None,
            "derived_status": outcome.status if outcome else None,
            "decided_by": _decision_brief(decided_by), "open_requests": [_decision_out(d, row) for d in decisions if d.id in open_ids],
        },
        "source": _source_block(session, source, snapshot),
        "anchoring": _anchoring_block(row, quote, locator, snapshot, structured),
        "registry": _registry_block(session, row, snapshot) if structured else None,
        "values": {name: snapshot_fields.get(name, getattr(row, name, None)) for name in _VALUE_KEYS[evidence_type]},
        "confidence_interval": ci,
        "missing_fields": list(getattr(row, "missing_fields", None) or []),
        "study": study_block, "population": population_block, "drugs": drugs, "formulations": formulations, "arms": arms,
        "endpoint": _endpoint_block(endpoint), "time_point": time_point, "estimand": _estimand_block(estimand),
        "related_analyses": related_analyses, "same_quantity_other_sources": same_quantity,
        "check_flags": flags, "risk_level": row.risk_level or "NONE", "identity_collision": collision,
        "snapshot": snapshot_fields,
        "versions": [
            {"version_id": v.id, "fingerprint": v.fingerprint, "is_current": v.fingerprint == row.fingerprint, "first_seen_at": v.first_seen_at, "last_seen_at": v.last_seen_at,
             "current_since": v.current_since, "source_content_hash": v.source_content_hash, "source_snapshot_version": v.source_snapshot_version,
             "last_source_content_hash": v.last_source_content_hash, "last_source_snapshot_version": v.last_source_snapshot_version,
             "status_when_last_current": v.effective_status, "supersedes_version_id": v.supersedes_id, "changed_fields": v.changed_fields or [], "snapshot": v.snapshot or {}}
            for v in versions
        ],
        "changed_since_review": changed_since_review,
        "decisions": [_decision_out(d, row) for d in decisions],
        "audit": [{"id": e.id, "at": e.at, "actor": e.actor, "action": e.action, "detail": e.detail} for e in audit_rows],
    }
