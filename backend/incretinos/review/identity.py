"""Stable identity and content fingerprints for reviewable evidence.

Evidence rows are deleted and re-inserted by every load, so a primary key says nothing about which
fact a row holds. This module derives, from fields that survive a rebuild:

``natural_key``
    A SHA-256 over the fields that say WHICH quantity from WHICH source a row represents. It never
    contains an extracted value, a primary key or a list position.

``evidence_key``
    The natural key when it identifies one row. When several rows share a natural key (which is a
    data-quality finding in itself: the stored labels cannot tell the rows apart) a tie-breaker is
    added, in this order, until the key is unique:

    1. the locator passage (normalised quote and curated locator text),
    2. the content fingerprint (the rows differ only in their values),
    3. an ordinal (the rows are identical in every stored field).

    ``collision_level`` records which step was needed (0 = none). The tie-breaker is used only for
    rows that need it, so editing the quote of an unambiguous row keeps its key and its history.

``fingerprint``
    A SHA-256 over the scientifically meaningful content a reviewer signs off on. A decision applies
    to exactly one fingerprint.

``snapshot``
    The fingerprinted fields as readable JSON, explicit nulls included. ``evidence_version`` stores
    it for every version ever seen, which is what makes "what changed" answerable.

Key recipe per evidence type
----------------------------
Text is compared case-insensitively with whitespace and dash variants unified. Fields derived by
this platform's own classifiers (endpoint category, responder threshold parsed from a registry
title, the inferred estimand of a registry row, drug and dose parsed from an arm label) are left out
of keys on purpose: a classifier improvement must show up as a changed fingerprint that asks for a
recheck, not as a new identity that orphans a decision.

result (registry)
    study NCT · origin · source key · outcome title · outcome time frame · unit of measure ·
    parameter type · class / category title · results-group title
result (curated)
    study NCT · origin · source key · endpoint title · endpoint threshold · endpoint time-frame text ·
    unit · time point (weeks) · normalised arm label · scope · estimand label · class title ·
    parameter type · baseline definition
analysis (registry)
    study NCT · origin · source key · outcome title · outcome time frame · unit of measure ·
    the compared group titles (unordered) · parameter type · statistical method · hypothesis type ·
    the registry's ``groupDescription`` for the analysis (it is what separates the per-class analyses
    of one outcome measure). It is read from ``statistical_analysis.group_description``, which the
    registry adapter stores verbatim; for a row loaded before that column existed it is read from
    the stored snapshot at the row's JSON location, which yields the same text
analysis (curated)
    study NCT · origin · source key · endpoint title · endpoint threshold · endpoint time-frame text ·
    unit · time point (weeks) · treatment label · comparator label · estimand label · parameter type ·
    hypothesis type
adverse_event (registry)
    study NCT · origin · source key · results-group title · scope · term · measure kind · serious flag ·
    organ system · and, for participant-flow rows only, the period title
adverse_event (curated)
    study NCT · origin · source key · normalised arm label · scope · term · measure kind · serious flag
claim
    the curated claim key
mechanism
    drug slug · receptor target
indication
    drug slug · brand · jurisdiction · indication text
equivalence
    the two dose keys

The source key is part of the key where one quantity may legitimately be stored once per source
(results, analyses, adverse events). For claims, mechanisms, indications and equivalences the loader
keeps one row per statement, so the source is content: re-pointing a statement at another source
changes the fingerprint and forces a recheck.

Columns added by the data-integrity revision (65bce81e7f45), and where each belongs
-----------------------------------------------------------------------------------
``statistical_analysis.group_description``
    Key and fingerprint (registry analyses): the registry's own words for which analysis this is.
``statistical_analysis.group_labels``
    Fingerprint only. The compared groups in posted order. The key holds the two group titles
    unordered, so correcting which group is the treatment keeps the identity and its history, and
    changes the fingerprint: an approval of the earlier direction asks for a recheck.
``statistical_analysis.direction_basis``
    Fingerprint only. How treatment and comparator were told apart (stated by the registry, taken
    from the arm role, taken from a placebo-titled group, not stated) is this platform's own
    derivation; like every classifier output it must force a recheck when it changes, not orphan a
    decision.
``statistical_analysis.class_title``
    Fingerprint only. The outcome class is matched by the adapter from ``groupDescription`` against
    the class titles; the verbatim text it is matched from is already in the key.
``estimand.origin``
    Neither. It records which loader owns the estimand record (and may delete it on a rebuild). It
    is implied by the row's own ``origin``, which is in the key, because a curated row may link only
    to an estimand of its own source; the estimand's label and basis are in the fingerprint.
``participant_flow``
    Not reviewable: it has no evidence key, takes no decisions and is rebuilt on every ingest.

Fingerprint content per evidence type
-------------------------------------
See the ``_load_*`` functions below, which build each snapshot: values and limits, interval and
level, n, dispersion, units, estimand label and basis, baseline definition, population and method
text, time point, the curated locator, the quote, notes, source key and tier, and the full arm and
endpoint description including the platform's own classification of them (drug, dose, formulation,
endpoint category, role, threshold). The JSON path of a registry row is a list position that shifts
when the sponsor inserts an outcome measure, so it is left out of both the key and the fingerprint;
the review API reads it from the row and resolves it against the stored snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import OrderedDict, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from ..db import INCLUDE_REJECTED
from ..enums import DISCONTINUATION_KINDS, Origin
from ..models import (
    AdverseEvent,
    Dose,
    Drug,
    DrugMechanism,
    Endpoint,
    Estimand,
    EvidenceClaim,
    EvidenceSource,
    ExposureEquivalence,
    Formulation,
    Indication,
    OutcomeResult,
    SourceSnapshot,
    StatisticalAnalysis,
    Study,
    StudyArm,
    StudyDrug,
)

REGISTRY = Origin.REGISTRY.value
CURATED = Origin.CURATED.value

#: Fields whose change means the reported number itself changed (HIGH significance, see lifecycle).
VALUE_FIELDS: dict[str, frozenset[str]] = {
    "result": frozenset({"value", "value_text", "value_low", "value_high", "ci_low", "ci_high", "ci_level", "n_analyzed", "dispersion_value", "dispersion_type", "unit"}),
    "analysis": frozenset({"estimate", "estimate_text", "ci_low", "ci_high", "ci_level", "ci_sides", "p_value_text", "ni_margin", "ni_margin_text", "ni_met", "superiority_met", "unit"}),
    "adverse_event": frozenset({"n_affected", "n_at_risk", "n_events", "percent", "percent_low", "percent_high"}),
    "claim": frozenset({"statement"}),
    "mechanism": frozenset({"action"}),
    "indication": frozenset({"status", "approval_date", "text"}),
    "equivalence": frozenset({"basis", "statement"}),
}


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    """The latest stored snapshot of a source."""

    id: int
    source_id: int
    version_no: int
    content_hash: str
    has_content: bool
    retrieved_at: Any = None


@dataclass(slots=True)
class StoredState:
    """What the evidence row currently holds in the columns the lifecycle maintains."""

    evidence_key: str | None
    fingerprint: str | None
    verification_status: str | None
    base_verification_status: str | None
    source_snapshot_id: int | None
    check_flags: list[dict[str, Any]] | None
    risk_level: str | None


@dataclass(slots=True)
class EvidenceRecord:
    """One reviewable row with its derived identity. Built fresh from the database on every reconcile."""

    evidence_type: str
    row_id: int
    origin: str
    natural_key: str
    fingerprint: str
    snapshot: dict[str, Any]
    label: str
    stored: StoredState
    study_nct: str | None = None
    study_name: str | None = None
    drug_slugs: tuple[str, ...] = ()
    source_id: int | None = None
    source_key: str | None = None
    source_type: str | None = None
    tier: str | None = None
    endpoint_category: str | None = None
    missing_fields: tuple[str, ...] = ()
    quote: str | None = None
    locator: str | None = None
    evidence_key: str = ""
    collision_level: int = 0
    collision_size: int = 1
    snapshot_ref: SnapshotRef | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_registry(self) -> bool:
        return self.origin == REGISTRY

    @property
    def search_text(self) -> str | None:
        """Free text that is searchable but lives in no other indexed column: the locator quote."""
        return self.quote.lower()[:2000] if self.quote else None


# --------------------------------------------------------------------------- hashing helpers


def key_text(value: Any) -> str | None:
    """Normalise text for use in a key: case, whitespace, dash and quote variants do not matter."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = text.replace("−", "-").replace("–", "-").replace("—", "-").replace("’", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def key_number(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(float(value))


def digest(parts: Any) -> str:
    """SHA-256 of a JSON-serialisable structure, stable across processes, platforms and databases."""
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def hashed_content(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The part of a snapshot that the fingerprint covers (any key starting with ``_`` would be display-only)."""
    return {k: v for k, v in snapshot.items() if not k.startswith("_")}


def fingerprint_of(snapshot: dict[str, Any]) -> str:
    return digest(hashed_content(snapshot))


def flatten(snapshot: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """{"endpoint": {"title": x}} -> {"endpoint.title": x}; lists are kept as values."""
    out: dict[str, Any] = {}
    for key, value in snapshot.items():
        if key.startswith("_"):
            continue
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{name}."))
        else:
            out[name] = value
    return out


def diff_snapshots(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Field-level difference between two snapshots: [{"field", "before", "after"}], sorted by field."""
    old, new = flatten(before or {}), flatten(after or {})
    return [{"field": name, "before": old.get(name), "after": new.get(name)} for name in sorted(set(old) | set(new)) if old.get(name) != new.get(name)]


def leaf_field(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def is_value_change(evidence_type: str, changed_fields: Iterable[dict[str, Any]]) -> bool:
    """True when any changed field is a reported number (not a label, a note or a classification)."""
    names = VALUE_FIELDS.get(evidence_type, frozenset())
    return any("." not in c["field"] and c["field"] in names for c in changed_fields)


# --------------------------------------------------------------------------- registry JSON fragments

_PARSED_SNAPSHOTS: OrderedDict[str, Any] = OrderedDict()
_PARSED_LIMIT = 96
_PATH_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[([^\]]+)\]")


def parsed_snapshot(session: Session, ref: SnapshotRef | None) -> Any | None:
    """The stored JSON of a snapshot, parsed once per content hash (content is immutable per hash)."""
    if ref is None or not ref.has_content:
        return None
    if ref.content_hash in _PARSED_SNAPSHOTS:
        _PARSED_SNAPSHOTS.move_to_end(ref.content_hash)
        return _PARSED_SNAPSHOTS[ref.content_hash]
    content = session.scalar(select(SourceSnapshot.content).where(SourceSnapshot.id == ref.id))
    try:
        doc = json.loads(content) if content else None
    except (TypeError, ValueError):
        doc = None
    _PARSED_SNAPSHOTS[ref.content_hash] = doc
    while len(_PARSED_SNAPSHOTS) > _PARSED_LIMIT:
        _PARSED_SNAPSHOTS.popitem(last=False)
    return doc


def resolve_json_path(doc: Any, path: str | None) -> list[tuple[str, Any]]:
    """Walk a registry locator such as ``resultsSection...outcomeMeasures[0].classes[1].measurements[OG003]``.

    Returns the chain of (token, node) visited, or an empty list when the path does not resolve.
    A numeric index selects a list position; any other bracket value selects the list element whose
    ``groupId`` or ``id`` equals it.
    """
    if doc is None or not path:
        return []
    chain: list[tuple[str, Any]] = []
    node = doc
    for name, index in _PATH_TOKEN.findall(path):
        try:
            if name:
                node = node[name]
                chain.append((name, node))
            elif index.isdigit():
                node = node[int(index)]
                chain.append((f"[{index}]", node))
            else:
                node = next(item for item in node if isinstance(item, dict) and index in (item.get("groupId"), item.get("id")))
                chain.append((f"[{index}]", node))
        except (KeyError, IndexError, TypeError, StopIteration):
            return []
    return chain


# --------------------------------------------------------------------------- loading


def latest_snapshots(session: Session) -> dict[int, SnapshotRef]:
    rows = session.execute(
        select(SourceSnapshot.id, SourceSnapshot.source_id, SourceSnapshot.version_no, SourceSnapshot.content_hash, SourceSnapshot.content.is_not(None), SourceSnapshot.retrieved_at)
        .order_by(SourceSnapshot.source_id, SourceSnapshot.version_no)
    ).all()
    return {source_id: SnapshotRef(sid, source_id, version_no, content_hash, bool(has_content), retrieved_at) for sid, source_id, version_no, content_hash, has_content, retrieved_at in rows}


def _c(entity: Any, prefix: str, *names: str) -> list[Any]:
    return [getattr(entity, name).label(f"{prefix}_{name}") for name in names]


_STATE = ("id", "evidence_key", "fingerprint", "verification_status", "base_verification_status", "source_snapshot_id", "check_flags", "risk_level")


def _state(row: Any) -> StoredState:
    return StoredState(row.evidence_key, row.fingerprint, row.verification_status, row.base_verification_status, row.source_snapshot_id, row.check_flags, row.risk_level)


def _rows(session: Session, stmt: Any) -> list[Any]:
    return session.execute(stmt.execution_options(**{INCLUDE_REJECTED: True})).all()


def _endpoint_snapshot(row: Any) -> dict[str, Any]:
    return {
        "title": row.e_title,
        "category": row.e_category,
        "role": row.e_role,
        "threshold": row.e_threshold,
        "unit": row.e_unit,
        "timeframe": row.e_timeframe_text,
        "timepoint_weeks": row.e_timepoint_weeks,
        "hierarchy_rank": row.e_hierarchy_rank,
        "multiplicity_controlled": row.e_multiplicity_controlled,
    }


def _arm_snapshot(row: Any, prefix: str, label: str | None) -> dict[str, Any]:
    get = lambda name: getattr(row, f"{prefix}_{name}")  # noqa: E731
    return {
        "label": label,
        "drug": get("drug_slug"),
        "dose_amount": get("dose_amount"),
        "dose_unit": get("dose_unit"),
        "dose_text": get("dose_text"),
        "formulation": get("formulation_key"),
        "is_placebo": get("is_placebo"),
        "is_pooled": get("is_pooled"),
    }


_ARM_COLS = ("drug_slug", "dose_amount", "dose_unit", "dose_text", "formulation_key", "is_placebo", "is_pooled")
_ENDPOINT_COLS = ("title", "category", "role", "threshold", "unit", "timeframe_text", "timepoint_weeks", "hierarchy_rank", "multiplicity_controlled")


def _study_drugs(session: Session) -> dict[int, tuple[str, ...]]:
    links: dict[int, list[str]] = defaultdict(list)
    for study_id, slug in session.execute(select(StudyDrug.study_id, Drug.slug).join(Drug, StudyDrug.drug_id == Drug.id).order_by(StudyDrug.id)):
        links[study_id].append(slug)
    return {k: tuple(v) for k, v in links.items()}


def _drugs(*groups: Iterable[str | None]) -> tuple[str, ...]:
    out: list[str] = []
    for group in groups:
        for slug in group:
            if slug and slug not in out:
                out.append(slug)
    return tuple(out)


def _load_results(session: Session, study_drugs: dict[int, tuple[str, ...]]) -> list[EvidenceRecord]:
    r = OutcomeResult
    stmt = (
        select(
            *[getattr(r, n) for n in _STATE],
            r.study_id, r.origin, r.arm_label, r.scope, r.class_title, r.param_type, r.value, r.value_text, r.value_low, r.value_high, r.unit,
            r.dispersion_type, r.dispersion_value, r.ci_low, r.ci_high, r.ci_level, r.n_analyzed, r.analysis_population, r.analysis_method,
            r.baseline_definition, r.timepoint_weeks, r.timeframe_text, r.source_id, r.source_location, r.locator_quote, r.tier, r.missing_fields, r.notes,
            Study.nct_id, Study.display_name,
            *_c(Endpoint, "e", *_ENDPOINT_COLS),
            *_c(StudyArm, "a", *_ARM_COLS),
            Estimand.label.label("es_label"), Estimand.basis.label("es_basis"),
            EvidenceSource.key.label("s_key"), EvidenceSource.source_type.label("s_type"),
        )
        .join(Study, r.study_id == Study.id)
        .join(Endpoint, r.endpoint_id == Endpoint.id)
        .outerjoin(StudyArm, r.arm_id == StudyArm.id)
        .outerjoin(Estimand, r.estimand_id == Estimand.id)
        .outerjoin(EvidenceSource, r.source_id == EvidenceSource.id)
    )
    out: list[EvidenceRecord] = []
    for row in _rows(session, stmt):
        registry = row.origin == REGISTRY
        snapshot = {
            "study": row.nct_id,
            "origin": row.origin,
            "source": {"key": row.s_key, "tier": row.tier},
            "endpoint": _endpoint_snapshot(row),
            "arm": _arm_snapshot(row, "a", row.arm_label),
            "estimand": {"label": row.es_label, "basis": row.es_basis},
            "scope": row.scope,
            "class_title": row.class_title,
            "param_type": row.param_type,
            "value": row.value,
            "value_text": row.value_text,
            "value_low": row.value_low,
            "value_high": row.value_high,
            "unit": row.unit,
            "dispersion_type": row.dispersion_type,
            "dispersion_value": row.dispersion_value,
            "ci_low": row.ci_low,
            "ci_high": row.ci_high,
            "ci_level": row.ci_level,
            "n_analyzed": row.n_analyzed,
            "analysis_population": row.analysis_population,
            "analysis_method": row.analysis_method,
            "baseline_definition": row.baseline_definition,
            "timepoint_weeks": row.timepoint_weeks,
            "timeframe_text": row.timeframe_text,
            "locator": None if registry else row.source_location,
            "quote": row.locator_quote,
            "notes": row.notes,
        }
        if registry:
            natural = ["result", REGISTRY, row.nct_id, row.s_key, key_text(row.e_title), key_text(row.e_timeframe_text), key_text(row.e_unit),
                       key_text(row.param_type), key_text(row.class_title), key_text(row.arm_label)]
        else:
            natural = ["result", CURATED, row.nct_id, row.s_key, key_text(row.e_title), key_number(row.e_threshold), key_text(row.e_timeframe_text),
                       key_text(row.unit), key_number(row.timepoint_weeks), key_text(row.arm_label), row.scope, row.es_label,
                       key_text(row.class_title), key_text(row.param_type), key_text(row.baseline_definition)]
        label = " — ".join(p for p in (row.display_name, row.e_title, row.class_title, row.arm_label) if p)
        out.append(
            EvidenceRecord(
                evidence_type="result", row_id=row.id, origin=row.origin, natural_key=digest(natural), fingerprint=fingerprint_of(snapshot), snapshot=snapshot,
                label=label, stored=_state(row), study_nct=row.nct_id, study_name=row.display_name,
                drug_slugs=_drugs([row.a_drug_slug], study_drugs.get(row.study_id, ())), source_id=row.source_id, source_key=row.s_key, source_type=row.s_type,
                tier=row.tier, endpoint_category=row.e_category, missing_fields=tuple(row.missing_fields or ()), quote=row.locator_quote, locator=row.source_location,
            )
        )
    return out


def _load_analyses(session: Session, study_drugs: dict[int, tuple[str, ...]], snapshots: dict[int, SnapshotRef]) -> list[EvidenceRecord]:
    a = StatisticalAnalysis
    trt, cmp_ = aliased(StudyArm), aliased(StudyArm)
    stmt = (
        select(
            *[getattr(a, n) for n in _STATE],
            a.study_id, a.origin, a.treatment_label, a.comparator_label, a.hypothesis_type, a.param_type, a.estimate, a.estimate_text, a.unit,
            a.ci_low, a.ci_high, a.ci_level, a.ci_sides, a.p_value_text, a.method, a.ni_margin, a.ni_margin_text, a.ni_met, a.superiority_tested,
            a.superiority_met, a.multiplicity_controlled, a.timepoint_weeks, a.comment, a.source_id, a.source_location, a.locator_quote, a.tier,
            a.missing_fields, a.notes, a.group_description, a.group_labels, a.direction_basis, a.class_title,
            Study.nct_id, Study.display_name,
            *_c(Endpoint, "e", *_ENDPOINT_COLS),
            *_c(trt, "t", *_ARM_COLS),
            *_c(cmp_, "c", *_ARM_COLS),
            Estimand.label.label("es_label"), Estimand.basis.label("es_basis"),
            EvidenceSource.key.label("s_key"), EvidenceSource.source_type.label("s_type"),
        )
        .join(Study, a.study_id == Study.id)
        .join(Endpoint, a.endpoint_id == Endpoint.id)
        .outerjoin(trt, a.treatment_arm_id == trt.id)
        .outerjoin(cmp_, a.comparator_arm_id == cmp_.id)
        .outerjoin(Estimand, a.estimand_id == Estimand.id)
        .outerjoin(EvidenceSource, a.source_id == EvidenceSource.id)
    )
    out: list[EvidenceRecord] = []
    for row in _rows(session, stmt):
        registry = row.origin == REGISTRY
        # The adapter stores the registry's groupDescription verbatim. A row loaded before that column existed
        # (a database upgraded but not yet re-ingested) has it read from the stored snapshot instead: same text,
        # so the key does not move when the row is rebuilt.
        group_description = row.group_description
        if group_description is None and registry and row.source_id is not None:
            chain = resolve_json_path(parsed_snapshot(session, snapshots.get(row.source_id)), row.source_location)
            if chain and isinstance(chain[-1][1], dict):
                group_description = (chain[-1][1].get("groupDescription") or "").strip() or None
        snapshot = {
            "study": row.nct_id,
            "origin": row.origin,
            "source": {"key": row.s_key, "tier": row.tier},
            "endpoint": _endpoint_snapshot(row),
            "estimand": {"label": row.es_label, "basis": row.es_basis},
            "treatment": _arm_snapshot(row, "t", row.treatment_label),
            "comparator": _arm_snapshot(row, "c", row.comparator_label),
            "group_description": group_description,
            "group_labels": list(row.group_labels) if row.group_labels is not None else None,
            "direction_basis": row.direction_basis,
            "class_title": row.class_title,
            "hypothesis_type": row.hypothesis_type,
            "param_type": row.param_type,
            "estimate": row.estimate,
            "estimate_text": row.estimate_text,
            "unit": row.unit,
            "ci_low": row.ci_low,
            "ci_high": row.ci_high,
            "ci_level": row.ci_level,
            "ci_sides": row.ci_sides,
            "p_value_text": row.p_value_text,
            "method": row.method,
            "ni_margin": row.ni_margin,
            "ni_margin_text": row.ni_margin_text,
            "ni_met": row.ni_met,
            "superiority_tested": row.superiority_tested,
            "superiority_met": row.superiority_met,
            "multiplicity_controlled": row.multiplicity_controlled,
            "timepoint_weeks": row.timepoint_weeks,
            "comment": row.comment,
            "locator": None if registry else row.source_location,
            "quote": row.locator_quote,
            "notes": row.notes,
        }
        if registry:
            groups = sorted(g for g in (key_text(row.treatment_label), key_text(row.comparator_label)) if g)
            natural = ["analysis", REGISTRY, row.nct_id, row.s_key, key_text(row.e_title), key_text(row.e_timeframe_text), key_text(row.e_unit), groups,
                       key_text(row.param_type), key_text(row.method), row.hypothesis_type, key_text(group_description)]
        else:
            natural = ["analysis", CURATED, row.nct_id, row.s_key, key_text(row.e_title), key_number(row.e_threshold), key_text(row.e_timeframe_text),
                       key_text(row.unit), key_number(row.timepoint_weeks), key_text(row.treatment_label), key_text(row.comparator_label), row.es_label,
                       key_text(row.param_type), row.hypothesis_type]
        versus = f"{row.treatment_label} vs {row.comparator_label}" if row.comparator_label else row.treatment_label
        label = " — ".join(p for p in (row.display_name, row.e_title, group_description, versus) if p)
        out.append(
            EvidenceRecord(
                evidence_type="analysis", row_id=row.id, origin=row.origin, natural_key=digest(natural), fingerprint=fingerprint_of(snapshot), snapshot=snapshot,
                label=label, stored=_state(row), study_nct=row.nct_id, study_name=row.display_name,
                drug_slugs=_drugs([row.t_drug_slug, row.c_drug_slug], study_drugs.get(row.study_id, ())), source_id=row.source_id, source_key=row.s_key,
                source_type=row.s_type, tier=row.tier, endpoint_category=row.e_category, missing_fields=tuple(row.missing_fields or ()),
                quote=row.locator_quote, locator=row.source_location,
            )
        )
    return out


def _load_adverse_events(session: Session, study_drugs: dict[int, tuple[str, ...]]) -> list[EvidenceRecord]:
    ae = AdverseEvent
    stmt = (
        select(
            *[getattr(ae, n) for n in _STATE],
            ae.study_id, ae.origin, ae.arm_label, ae.scope, ae.term, ae.organ_system, ae.category, ae.measure_kind, ae.is_serious, ae.severity,
            ae.n_affected, ae.n_at_risk, ae.n_events, ae.percent, ae.percent_low, ae.percent_high, ae.period, ae.source_id, ae.source_location,
            ae.locator_quote, ae.tier, ae.missing_fields, ae.notes,
            Study.nct_id, Study.display_name,
            *_c(StudyArm, "a", *_ARM_COLS),
            EvidenceSource.key.label("s_key"), EvidenceSource.source_type.label("s_type"),
        )
        .join(Study, ae.study_id == Study.id)
        .outerjoin(StudyArm, ae.arm_id == StudyArm.id)
        .outerjoin(EvidenceSource, ae.source_id == EvidenceSource.id)
    )
    out: list[EvidenceRecord] = []
    for row in _rows(session, stmt):
        registry = row.origin == REGISTRY
        snapshot = {
            "study": row.nct_id,
            "origin": row.origin,
            "source": {"key": row.s_key, "tier": row.tier},
            "arm": _arm_snapshot(row, "a", row.arm_label),
            "scope": row.scope,
            "term": row.term,
            "organ_system": row.organ_system,
            "category": row.category,
            "measure_kind": row.measure_kind,
            "is_serious": row.is_serious,
            "severity": row.severity,
            "n_affected": row.n_affected,
            "n_at_risk": row.n_at_risk,
            "n_events": row.n_events,
            "percent": row.percent,
            "percent_low": row.percent_low,
            "percent_high": row.percent_high,
            "period": row.period,
            "locator": None if registry else row.source_location,
            "quote": row.locator_quote,
            "notes": row.notes,
        }
        if registry:
            period = key_text(row.period) if row.measure_kind in DISCONTINUATION_KINDS else None
            natural = ["adverse_event", REGISTRY, row.nct_id, row.s_key, key_text(row.arm_label), row.scope, key_text(row.term), row.measure_kind,
                       bool(row.is_serious), key_text(row.organ_system), period]
        else:
            natural = ["adverse_event", CURATED, row.nct_id, row.s_key, key_text(row.arm_label), row.scope, key_text(row.term), row.measure_kind, bool(row.is_serious)]
        label = " — ".join(p for p in (row.display_name, row.term, row.arm_label) if p)
        out.append(
            EvidenceRecord(
                evidence_type="adverse_event", row_id=row.id, origin=row.origin, natural_key=digest(natural), fingerprint=fingerprint_of(snapshot), snapshot=snapshot,
                label=label, stored=_state(row), study_nct=row.nct_id, study_name=row.display_name,
                drug_slugs=_drugs([row.a_drug_slug], study_drugs.get(row.study_id, ())), source_id=row.source_id, source_key=row.s_key, source_type=row.s_type,
                tier=row.tier, endpoint_category=None, missing_fields=tuple(row.missing_fields or ()), quote=row.locator_quote, locator=row.source_location,
                extra={"measure_kind": row.measure_kind, "ae_category": row.category},
            )
        )
    return out


def _load_claims(session: Session, study_drugs: dict[int, tuple[str, ...]]) -> list[EvidenceRecord]:
    c = EvidenceClaim
    stmt = (
        select(
            *[getattr(c, n) for n in _STATE],
            c.key, c.study_id, c.topic, c.statement, c.claim_kind, c.tier, c.source_id, c.locator, c.quote, c.origin,
            Drug.slug.label("d_slug"), Drug.name.label("d_name"), Study.nct_id, Study.display_name,
            EvidenceSource.key.label("s_key"), EvidenceSource.source_type.label("s_type"),
        )
        .outerjoin(Drug, c.drug_id == Drug.id)
        .outerjoin(Study, c.study_id == Study.id)
        .outerjoin(EvidenceSource, c.source_id == EvidenceSource.id)
    )
    out: list[EvidenceRecord] = []
    for row in _rows(session, stmt):
        snapshot = {
            "key": row.key,
            "origin": row.origin,
            "drug": row.d_slug,
            "study": row.nct_id,
            "topic": row.topic,
            "statement": row.statement,
            "claim_kind": row.claim_kind,
            "source": {"key": row.s_key, "tier": row.tier},
            "locator": row.locator,
            "quote": row.quote,
        }
        subject = row.display_name or row.d_name
        label = " — ".join(p for p in (subject, row.topic.replace("_", " ").title() if row.topic else None, _clip(row.statement, 160)) if p)
        out.append(
            EvidenceRecord(
                evidence_type="claim", row_id=row.id, origin=row.origin or CURATED, natural_key=digest(["claim", row.key]), fingerprint=fingerprint_of(snapshot),
                snapshot=snapshot, label=label, stored=_state(row), study_nct=row.nct_id, study_name=row.display_name,
                drug_slugs=_drugs([row.d_slug], study_drugs.get(row.study_id, ()) if row.study_id else ()), source_id=row.source_id, source_key=row.s_key,
                source_type=row.s_type, tier=row.tier, quote=row.quote, locator=row.locator,
            )
        )
    return out


def _load_mechanisms(session: Session) -> list[EvidenceRecord]:
    m = DrugMechanism
    stmt = (
        select(*[getattr(m, n) for n in _STATE], m.target, m.action, m.description, m.source_id, m.quote, Drug.slug.label("d_slug"), Drug.name.label("d_name"),
               EvidenceSource.key.label("s_key"), EvidenceSource.source_type.label("s_type"), EvidenceSource.tier.label("s_tier"))
        .join(Drug, m.drug_id == Drug.id)
        .outerjoin(EvidenceSource, m.source_id == EvidenceSource.id)
    )
    out: list[EvidenceRecord] = []
    for row in _rows(session, stmt):
        snapshot = {
            "drug": row.d_slug, "target": row.target, "action": row.action, "description": row.description,
            "source": {"key": row.s_key, "tier": row.s_tier}, "quote": row.quote,
        }
        out.append(
            EvidenceRecord(
                evidence_type="mechanism", row_id=row.id, origin=CURATED, natural_key=digest(["mechanism", row.d_slug, key_text(row.target)]),
                fingerprint=fingerprint_of(snapshot), snapshot=snapshot, label=f"{row.d_name} — mechanism: {row.target} {str(row.action).lower()}", stored=_state(row),
                drug_slugs=(row.d_slug,), source_id=row.source_id, source_key=row.s_key, source_type=row.s_type, tier=row.s_tier, quote=row.quote,
            )
        )
    return out


def _load_indications(session: Session) -> list[EvidenceRecord]:
    i = Indication
    stmt = (
        select(*[getattr(i, n) for n in _STATE], i.brand, i.text, i.status, i.jurisdiction, i.approval_date, i.source_id, i.quote,
               Drug.slug.label("d_slug"), Drug.name.label("d_name"),
               EvidenceSource.key.label("s_key"), EvidenceSource.source_type.label("s_type"), EvidenceSource.tier.label("s_tier"))
        .join(Drug, i.drug_id == Drug.id)
        .outerjoin(EvidenceSource, i.source_id == EvidenceSource.id)
    )
    out: list[EvidenceRecord] = []
    for row in _rows(session, stmt):
        snapshot = {
            "drug": row.d_slug, "brand": row.brand, "text": row.text, "status": row.status, "jurisdiction": row.jurisdiction, "approval_date": row.approval_date,
            "source": {"key": row.s_key, "tier": row.s_tier}, "quote": row.quote,
        }
        natural = ["indication", row.d_slug, key_text(row.brand), key_text(row.jurisdiction), key_text(row.text)]
        out.append(
            EvidenceRecord(
                evidence_type="indication", row_id=row.id, origin=CURATED, natural_key=digest(natural), fingerprint=fingerprint_of(snapshot), snapshot=snapshot,
                label=f"{row.d_name} — indication ({row.brand or row.jurisdiction}): {_clip(row.text, 140)}", stored=_state(row), drug_slugs=(row.d_slug,),
                source_id=row.source_id, source_key=row.s_key, source_type=row.s_type, tier=row.s_tier, quote=row.quote,
            )
        )
    return out


def _load_equivalences(session: Session) -> list[EvidenceRecord]:
    eq = ExposureEquivalence
    dose_a, dose_b, form_a, form_b = aliased(Dose), aliased(Dose), aliased(Formulation), aliased(Formulation)
    stmt = (
        select(*[getattr(eq, n) for n in _STATE], eq.basis, eq.statement, eq.source_id, eq.quote,
               dose_a.key.label("a_key"), dose_a.amount.label("a_amount"), dose_a.unit.label("a_unit"), form_a.key.label("a_formulation"),
               dose_b.key.label("b_key"), dose_b.amount.label("b_amount"), dose_b.unit.label("b_unit"), form_b.key.label("b_formulation"),
               Drug.slug.label("d_slug"), Drug.name.label("d_name"),
               EvidenceSource.key.label("s_key"), EvidenceSource.source_type.label("s_type"), EvidenceSource.tier.label("s_tier"))
        .join(dose_a, eq.dose_a_id == dose_a.id)
        .join(dose_b, eq.dose_b_id == dose_b.id)
        .join(form_a, dose_a.formulation_id == form_a.id)
        .join(form_b, dose_b.formulation_id == form_b.id)
        .join(Drug, form_a.drug_id == Drug.id)
        .outerjoin(EvidenceSource, eq.source_id == EvidenceSource.id)
    )
    out: list[EvidenceRecord] = []
    for row in _rows(session, stmt):
        snapshot = {
            "drug": row.d_slug,
            "dose_a": {"key": row.a_key, "amount": row.a_amount, "unit": row.a_unit, "formulation": row.a_formulation},
            "dose_b": {"key": row.b_key, "amount": row.b_amount, "unit": row.b_unit, "formulation": row.b_formulation},
            "basis": row.basis, "statement": row.statement, "source": {"key": row.s_key, "tier": row.s_tier}, "quote": row.quote,
        }
        out.append(
            EvidenceRecord(
                evidence_type="equivalence", row_id=row.id, origin=CURATED, natural_key=digest(["equivalence", row.a_key, row.b_key]),
                fingerprint=fingerprint_of(snapshot), snapshot=snapshot,
                label=f"{row.d_name} — exposure equivalence: {row.a_amount:g} {row.a_unit} ({row.a_formulation}) ↔ {row.b_amount:g} {row.b_unit} ({row.b_formulation})",
                stored=_state(row), drug_slugs=(row.d_slug,), source_id=row.source_id, source_key=row.s_key, source_type=row.s_type, tier=row.s_tier, quote=row.quote,
            )
        )
    return out


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def assign_keys(records: list[EvidenceRecord]) -> None:
    """Turn natural keys into unique evidence keys, recording every tie-break that was needed."""
    by_natural: dict[tuple[str, str], list[EvidenceRecord]] = defaultdict(list)
    for record in records:
        by_natural[(record.evidence_type, record.natural_key)].append(record)
    for group in by_natural.values():
        if len(group) == 1:
            group[0].evidence_key, group[0].collision_level, group[0].collision_size = group[0].natural_key, 0, 1
            continue
        by_passage: dict[str, list[EvidenceRecord]] = defaultdict(list)
        for record in group:
            locator = None if record.is_registry else key_text(record.locator)  # a registry locator is a list position
            by_passage[digest([record.natural_key, "passage", key_text(record.quote), locator])].append(record)
        for passage_key, same_passage in by_passage.items():
            if len(same_passage) == 1:
                same_passage[0].evidence_key, same_passage[0].collision_level = passage_key, 1
                continue
            by_content: dict[str, list[EvidenceRecord]] = defaultdict(list)
            for record in same_passage:
                by_content[digest([passage_key, "content", record.fingerprint])].append(record)
            for content_key, same_content in by_content.items():
                if len(same_content) == 1:
                    same_content[0].evidence_key, same_content[0].collision_level = content_key, 2
                    continue
                for ordinal, record in enumerate(sorted(same_content, key=lambda rec: rec.row_id)):
                    record.evidence_key, record.collision_level = digest([content_key, "duplicate", ordinal]), 3
        for record in group:
            record.collision_size = len(group)


def load_records(session: Session, snapshots: dict[int, SnapshotRef] | None = None) -> list[EvidenceRecord]:
    """Every reviewable row in the database (rejected ones included) with key, fingerprint and snapshot."""
    snapshots = snapshots if snapshots is not None else latest_snapshots(session)
    study_drugs = _study_drugs(session)
    records = [
        *_load_results(session, study_drugs),
        *_load_analyses(session, study_drugs, snapshots),
        *_load_adverse_events(session, study_drugs),
        *_load_claims(session, study_drugs),
        *_load_mechanisms(session),
        *_load_indications(session),
        *_load_equivalences(session),
    ]
    for record in records:
        record.snapshot_ref = snapshots.get(record.source_id) if record.source_id is not None else None
    assign_keys(records)
    return records
