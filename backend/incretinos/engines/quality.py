"""Evidence quality: separate, countable dimensions of the stored evidence, each with its rule and its records.

What this module answers, one dimension at a time and never as one number:

* review status         how far each stored record has been checked, and what is waiting for a person
* completeness          which reported quantities the source left out (interval, n, estimand, unit, method ...)
* source authority      what kind of source the evidence comes from (context for reading it, not a quality grade)
* reliability signals   what the automated checks flag, and how the conflict engine classifies the places where several
                        public sources report the same quantity (``incretinos.engines.conflicts``)
* precision             whether an interval was reported, and how wide reported intervals are (descriptive)
* freshness             when each source was retrieved, whether it changed, and whether it is due a refresh

Rules this module keeps:

* Every count is computed from stored rows when it is asked for. Nothing is hard-coded, cached or estimated.
* There is no combined score, and no dimension is folded into another. A high completeness count says that the public
  record reports a result fully; it never says that the drug works or is safe.
* Rejected rows and rows under a review hold are excluded from every analytic count by the session rule in
  ``incretinos.db`` (the loaders below use ordinary ORM selects). The review-status dimension is the one place they are
  counted, through the explicit opt-in ``rejected_visible``.
* Nothing here writes. In particular nothing becomes HUMAN_VERIFIED: that status exists only as the result of a
  recorded human decision (``incretinos.review.lifecycle``).
* Every metric can list exactly the records it counted (``records``), and says which page of the product shows the same
  set with its own filters, when one does (``links``).
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import rejected_visible
from ..enums import TIER_LABEL, EvidenceTier, ReviewStatus, TriState, VerificationStatus
from ..models import (
    AdverseEvent,
    ChangeEvent,
    Drug,
    DrugMechanism,
    Endpoint,
    Estimand,
    EvidenceClaim,
    EvidenceSource,
    EvidenceVersion,
    ExposureEquivalence,
    Indication,
    LabelRecord,
    OutcomeResult,
    ReviewQueueItem,
    SourceSnapshot,
    StatisticalAnalysis,
    Study,
    StudyArm,
    StudyDrug,
)
from ..review import queries as review_queries
from ..review.checks import CHECKS
from ..review.policy import STATUS_LABEL, canonical_status
from . import conflicts as conflict_engine
from .findings import CATEGORY_LABEL

# --------------------------------------------------------------------------- vocabulary

DIMENSIONS: dict[str, dict[str, str]] = {
    "review_status": {
        "label": "Review status",
        "question": "How far has each stored record been checked, and what is waiting for a person?",
        "caveat": "Machine-extracted and quote-anchored are automatic checks, not verification. Only a recorded human decision verifies a record. "
                  "Rejected rows and rows on review hold are counted here, and only here.",
    },
    "completeness": {
        "label": "Completeness",
        "question": "Which reported quantities did the source leave out?",
        "caveat": "Completeness describes how fully the public record reports a result. A complete record does not mean the drug works or is safe, "
                  "and a missing interval is often how the source reports (registry arm-level means, for example), not an extraction error.",
    },
    "authority": {
        "label": "Source authority",
        "question": "What kind of source does the evidence come from?",
        "caveat": "Context for reading a number, not a grade of the number: a press release can be accurate and a label can be out of date.",
    },
    "reliability": {
        "label": "Methodological reliability signals",
        "question": "What do the automated checks flag, and where do public sources disagree?",
        "caveat": "A flag explains a risk of misattribution or an internal inconsistency; it does not say the value is wrong, and no flag does not say it is right.",
    },
    "precision": {
        "label": "Precision",
        "question": "Was an interval reported, and how wide are the reported intervals?",
        "caveat": "Descriptive only. Interval widths depend on sample size, scale and variability; they are compared only within one endpoint, "
                  "parameter, unit and confidence level, and they are not a quality grade.",
    },
    "freshness": {
        "label": "Freshness",
        "question": "When was each source retrieved, has it changed, and is it due a refresh?",
        "caveat": "A refresh rule is an operating policy for this deployment, not a property of the source.",
    },
}

#: Breakdown dimensions, the label shown for each, and the query parameter that selects one value of it.
BREAKDOWNS: dict[str, str] = {
    "drug": "Molecule",
    "study": "Trial",
    "tier": "Evidence tier",
    "source_type": "Source type",
    "endpoint_category": "Endpoint category",
}

SOURCE_TYPE_LABEL = {
    "CTGOV": "ClinicalTrials.gov",
    "PUBMED": "PubMed",
    "OPENFDA_LABEL": "FDA labeling",
    "OPENFDA_FAERS": "FAERS (openFDA)",
    "OPENFDA_LABEL_SEARCH": "FDA label database check",
    "WEB": "Web (release, review, pipeline)",
    "GUIDANCE": "Guidance",
}

EVIDENCE_TYPE_LABEL = {
    "result": "Results",
    "analysis": "Between-arm analyses",
    "adverse_event": "Adverse-event rows",
    "claim": "Claims",
    "mechanism": "Mechanism statements",
    "indication": "Indications",
    "equivalence": "Exposure equivalences",
}

#: Freshness rule: a source is due a refresh when its last retrieval is older than this many days. Overridden per source
#: type by the setting INCRETINOS_QUALITY_REFRESH_DAYS ("CTGOV=14,PUBMED=730").
DEFAULT_REFRESH_DAYS: dict[str, int] = {
    "CTGOV": 30,  # sponsors update registry records continually
    "OPENFDA_LABEL": 90,  # labeling revisions
    "OPENFDA_LABEL_SEARCH": 90,
    "OPENFDA_FAERS": 90,  # FAERS is published quarterly
    "WEB": 180,  # press releases, review documents and pipeline pages
    "PUBMED": 365,  # article records rarely change after indexing
    "GUIDANCE": 365,
}
FALLBACK_REFRESH_DAYS = 180
REFRESH_SETTING = "INCRETINOS_QUALITY_REFRESH_DAYS"
_RULE_PART = re.compile(r"^\s*([A-Z][A-Z0-9_]{0,39})\s*=\s*(\d{1,5})\s*$")

#: Ages of the last retrieval, in days, as (bucket id, label, upper bound inclusive or None).
AGE_BUCKETS: tuple[tuple[str, str, float | None], ...] = (
    ("0-30", "30 days or less", 30),
    ("31-90", "31 to 90 days", 90),
    ("91-365", "91 to 365 days", 365),
    ("over-365", "More than 365 days", None),
)
AGE_NOT_RECORDED = "not-recorded"

#: Label sections that carry safety information.
LABEL_SAFETY_SECTIONS = ("boxed_warning", "warnings_and_cautions", "adverse_reactions")
#: Smallest number of reported intervals in one comparable group for its width distribution to be shown.
MIN_WIDTH_GROUP = 3

ALL_EVIDENCE, EVIDENCE, SOURCES, DRUGS, ORPHANS, CHANGES, QUEUE, CONFLICTS = (
    "evidence_all", "evidence", "sources", "drugs", "orphaned_decisions", "change_events", "queue_items", "source_conflicts")
UNIVERSE_LABEL = {
    ALL_EVIDENCE: "Every stored reviewable record, rejected and held rows included",
    EVIDENCE: "Stored records in analytic use (rejected and held rows excluded)",
    SOURCES: "Evidence sources",
    DRUGS: "Tracked molecules",
    ORPHANS: "Recorded decisions whose evidence no longer exists",
    CHANGES: "Detected source changes",
    QUEUE: "Review-queue items",
    CONFLICTS: "Groups of rows from several public sources reporting the same quantity (classified by the conflict engine)",
}
_ALL_DIMS = tuple(BREAKDOWNS)
UNIVERSE_DIMS: dict[str, tuple[str, ...]] = {
    ALL_EVIDENCE: _ALL_DIMS,
    EVIDENCE: _ALL_DIMS,
    SOURCES: ("study", "tier", "source_type"),
    DRUGS: ("drug",),
    ORPHANS: (),
    CHANGES: ("study", "tier", "source_type"),
    QUEUE: (),
    CONFLICTS: ("drug", "study"),
}

REVIEW_PAGE_FILTERS = {"drug": "drug", "study": "nct", "tier": "tier", "source_type": "source_type", "endpoint_category": "category"}
REVIEW_API_FILTERS = {"drug": "drug", "study": "nct", "tier": "tier", "source_type": "source_type", "endpoint_category": "endpoint_category"}


class QualityQueryError(ValueError):
    """A metric, filter or value that does not exist or does not apply."""


# --------------------------------------------------------------------------- items


@dataclass(slots=True)
class Item:
    """One countable thing: an evidence row, a source, a molecule, a decision, a change event, a queue item or a conflict."""

    kind: str
    key: str
    label: str
    groups: dict[str, tuple[str, ...]]
    f: dict[str, Any]
    links: list[dict[str, str]]
    evidence_type: str | None = None

    def in_group(self, dim: str, value: str) -> bool:
        return value in self.groups.get(dim, ())


@dataclass(slots=True)
class Facts:
    """Everything the metrics count, read once per request."""

    as_of: datetime
    refresh_days: dict[str, int]
    universes: dict[str, list[Item]]
    labels: dict[str, dict[str, str]]
    width_groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    press_release_pairs: set[tuple[str, str]] = field(default_factory=set)


def _groups(drugs: Iterable[str] | None = None, study: str | None = None, tier: str | None = None, source_type: str | None = None,
            category: str | None = None) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    slugs = tuple(dict.fromkeys(s for s in (drugs or ()) if s))
    if slugs:
        out["drug"] = slugs
    for dim, value in (("study", study), ("tier", tier), ("source_type", source_type), ("endpoint_category", category)):
        if value:
            out[dim] = (value,)
    return out


def _blank(text: str | None) -> bool:
    return text is None or not str(text).strip()


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def parse_refresh_rule(text: str | None) -> dict[str, int]:
    """``"CTGOV=14, PUBMED=730"`` -> ``{"CTGOV": 14, "PUBMED": 730}``. Raises ValueError on anything else."""
    rule: dict[str, int] = {}
    for part in (text or "").split(","):
        if not part.strip():
            continue
        match = _RULE_PART.match(part)
        if not match or int(match.group(2)) < 1:
            raise ValueError(f"{REFRESH_SETTING}: {part.strip()!r} is not SOURCE_TYPE=days (a whole number of days, at least 1)")
        rule[match.group(1)] = int(match.group(2))
    return rule


def refresh_days_for(source_type: str | None, rule: dict[str, int]) -> int:
    return rule.get(source_type or "", FALLBACK_REFRESH_DAYS)


# --------------------------------------------------------------------------- loading


_EVIDENCE_MODELS = {
    "result": OutcomeResult,
    "analysis": StatisticalAnalysis,
    "adverse_event": AdverseEvent,
    "claim": EvidenceClaim,
    "mechanism": DrugMechanism,
    "indication": Indication,
    "equivalence": ExposureEquivalence,
}


def _evidence_links(evidence_type: str, evidence_key: str | None, row_id: int, study: str | None) -> list[dict[str, str]]:
    target = {"type": evidence_type, "key": evidence_key} if evidence_key else {"type": evidence_type, "id": str(row_id)}
    links = [{"label": "Open the record in Evidence Review", "href": f"/review?{urlencode(target)}"}]
    if study:
        links.append({"label": f"Trial {study}", "href": f"/trials/{study}"})
    return links


class _Fallback:
    """Grouping attributes for a row that has no current version in the review index (it has not been reconciled)."""

    def __init__(self, session: Session) -> None:
        self.study = {sid: (nct, name) for sid, nct, name in session.execute(select(Study.id, Study.nct_id, Study.display_name))}
        self.source = {sid: (stype, tier) for sid, stype, tier in session.execute(select(EvidenceSource.id, EvidenceSource.source_type, EvidenceSource.tier))}
        self.category = dict(session.execute(select(Endpoint.id, Endpoint.category)).all())
        drugs: dict[int, list[str]] = defaultdict(list)
        for study_id, slug in session.execute(select(StudyDrug.study_id, Drug.slug).join(Drug, Drug.id == StudyDrug.drug_id).order_by(StudyDrug.id)):
            drugs[study_id].append(slug)
        self.study_drugs = drugs
        self.drug_slug = dict(session.execute(select(Drug.id, Drug.slug)).all())

    def attrs(self, row: Any) -> dict[str, Any]:
        mapping = row._mapping
        nct, name = self.study.get(mapping.get("study_id"), (None, None))
        stype, stier = self.source.get(mapping.get("source_id"), (None, None))
        drugs = [*(s for s in (mapping.get("arm_drug"),) if s), *self.study_drugs.get(mapping.get("study_id"), [])]
        if mapping.get("drug_id"):
            drugs.insert(0, self.drug_slug.get(mapping.get("drug_id")))
        return {"study_nct": nct, "study_name": name, "drugs": [d for d in drugs if d], "tier": mapping.get("tier") or stier, "source_type": stype,
                "endpoint_category": self.category.get(mapping.get("endpoint_id")), "label": None}


def _index(session: Session) -> dict[tuple[str, str], Any]:
    return {(row.evidence_type, row.evidence_key): row for row in review_queries.index_rows(session)}


def _attrs(index: dict[tuple[str, str], Any], fallback: _Fallback, evidence_type: str, row: Any) -> dict[str, Any]:
    entry = index.get((evidence_type, row.evidence_key)) if row.evidence_key else None
    if entry is None:
        return fallback.attrs(row)
    return {"study_nct": entry.study_nct, "study_name": entry.study_name, "drugs": entry.drugs, "tier": entry.tier, "source_type": entry.source_type,
            "endpoint_category": entry.endpoint_category, "label": entry.label}


def _evidence_statement(model: Any, evidence_type: str, *, analytic: bool):
    """The columns read for one evidence type. Analytic reads add what the completeness, precision and reliability rules use."""
    base = [model.id, model.evidence_key, model.verification_status, model.review_hold]
    links: list[Any] = []
    if hasattr(model, "study_id"):
        base.append(model.study_id)
    if hasattr(model, "source_id"):
        base.append(model.source_id)
    if hasattr(model, "drug_id"):
        base.append(model.drug_id)
    if hasattr(model, "tier"):
        base.append(model.tier)
    if hasattr(model, "endpoint_id"):
        base.append(model.endpoint_id)
    if not analytic:
        return select(*base).order_by(model.id)
    base += [model.risk_level, model.check_flags]
    if evidence_type == "result":
        base += [model.ci_low, model.ci_high, model.ci_level, model.n_analyzed, model.unit, model.param_type, model.value, model.arm_label,
                 Estimand.label.label("estimand_label"), Estimand.basis.label("estimand_basis"), StudyArm.drug_slug.label("arm_drug")]
        links = [(Estimand, model.estimand_id == Estimand.id), (StudyArm, model.arm_id == StudyArm.id)]
    elif evidence_type == "analysis":
        base += [model.ci_low, model.ci_high, model.ci_level, model.unit, model.param_type, model.estimate, model.method, model.multiplicity_controlled,
                 Estimand.label.label("estimand_label"), Estimand.basis.label("estimand_basis")]
        links = [(Estimand, model.estimand_id == Estimand.id)]
    elif evidence_type == "adverse_event":
        base += [model.n_at_risk, model.measure_kind, model.term, model.arm_label, StudyArm.drug_slug.label("arm_drug")]
        links = [(StudyArm, model.arm_id == StudyArm.id)]
    stmt = select(*base)
    for target, onclause in links:
        stmt = stmt.outerjoin(target, onclause)
    return stmt.order_by(model.id)


def _status_facts(row: Any) -> dict[str, Any]:
    return {"status": canonical_status(row.verification_status), "hold": bool(row.review_hold)}


def _load_evidence(session: Session, index: dict, fallback: _Fallback, *, analytic: bool, open_requests: set[tuple[str, str]] | None = None) -> list[Item]:
    items: list[Item] = []
    for evidence_type, model in _EVIDENCE_MODELS.items():
        for row in session.execute(_evidence_statement(model, evidence_type, analytic=analytic)):
            attrs = _attrs(index, fallback, evidence_type, row)
            facts = _status_facts(row)
            label = attrs["label"] or f"{EVIDENCE_TYPE_LABEL[evidence_type]} #{row.id}"
            if analytic:
                m = row._mapping
                facts.update({"risk": (m["risk_level"] or "NONE"), "flags": [f for f in (m["check_flags"] or []) if isinstance(f, dict)]})
                for name in ("ci_low", "ci_high", "ci_level", "n_analyzed", "unit", "param_type", "value", "estimate", "method", "multiplicity_controlled",
                             "estimand_label", "estimand_basis", "n_at_risk", "measure_kind", "arm_drug", "term", "arm_label"):
                    if name in m:
                        facts[name] = m[name]
            else:
                facts["open_request"] = open_requests is not None and (evidence_type, row.evidence_key) in open_requests
            facts.update({"study_nct": attrs["study_nct"], "study_name": attrs["study_name"], "drugs": list(attrs["drugs"]), "tier": attrs["tier"],
                          "source_type": attrs["source_type"], "endpoint_category": attrs["endpoint_category"], "evidence_key": row.evidence_key, "id": row.id})
            items.append(Item(
                kind="evidence", key=f"{evidence_type}:{row.evidence_key or row.id}", label=label, evidence_type=evidence_type,
                groups=_groups(attrs["drugs"], attrs["study_nct"], attrs["tier"], attrs["source_type"], attrs["endpoint_category"]),
                f=facts, links=_evidence_links(evidence_type, row.evidence_key, row.id, attrs["study_nct"]),
            ))
    return items


def _load_sources(session: Session, as_of: datetime, rule: dict[str, int]) -> list[Item]:
    hashes: dict[int, set[str]] = defaultdict(set)
    snapshots: Counter[int] = Counter()
    for source_id, content_hash in session.execute(select(SourceSnapshot.source_id, SourceSnapshot.content_hash)):
        hashes[source_id].add(content_hash)
        snapshots[source_id] += 1
    changes = Counter(dict(session.execute(select(ChangeEvent.source_id, func.count()).group_by(ChangeEvent.source_id)).all()))
    studies = dict(session.execute(select(Study.source_id, Study.nct_id).where(Study.source_id.is_not(None))).all())
    items = []
    for s in session.execute(select(EvidenceSource.id, EvidenceSource.key, EvidenceSource.title, EvidenceSource.source_type, EvidenceSource.tier,
                                    EvidenceSource.retrieved_at).order_by(EvidenceSource.id)):
        retrieved = _aware(s.retrieved_at)
        age = max(0.0, (as_of - retrieved).total_seconds() / 86400) if retrieved else None
        days = refresh_days_for(s.source_type, rule)
        nct = studies.get(s.id)
        items.append(Item(
            kind="source", key=f"source:{s.key}", label=s.title,
            groups=_groups(None, nct, s.tier, s.source_type),
            f={"id": s.id, "source_key": s.key, "source_type": s.source_type, "tier": s.tier, "retrieved_at": retrieved, "age_days": age,
               "age_bucket": _age_bucket(age), "refresh_days": days, "refresh_due": age is None or age > days, "snapshots": snapshots.get(s.id, 0),
               "distinct_hashes": len(hashes.get(s.id, ())), "change_events": changes.get(s.id, 0), "study_nct": nct},
            links=[{"label": "Open the source in Sources & Audit", "href": f"/sources?id={s.id}"}],
        ))
    return items


def _age_bucket(age: float | None) -> str:
    if age is None:
        return AGE_NOT_RECORDED
    for bucket, _, upper in AGE_BUCKETS:
        if upper is None or age <= upper:
            return bucket
    return AGE_BUCKETS[-1][0]


def _load_drugs(session: Session, evidence: list[Item]) -> list[Item]:
    """Safety evidence on record per molecule, from analytic adverse-event rows and stored FDA labeling."""
    with_denominator: Counter[str] = Counter()
    ae_rows: Counter[str] = Counter()
    for item in evidence:
        if item.evidence_type != "adverse_event" or item.f.get("measure_kind") != "PARTICIPANTS_WITH_EVENT" or not item.f.get("arm_drug"):
            continue
        ae_rows[item.f["arm_drug"]] += 1
        if item.f.get("n_at_risk") is not None:
            with_denominator[item.f["arm_drug"]] += 1
    labels: dict[int, list[str]] = defaultdict(list)
    for drug_id, brand, sections in session.execute(select(LabelRecord.drug_id, LabelRecord.brand, LabelRecord.sections).where(LabelRecord.drug_id.is_not(None)).order_by(LabelRecord.id)):
        present = [name for name in LABEL_SAFETY_SECTIONS if isinstance(sections, dict) and not _blank(_section_text(sections.get(name)))]
        if present:
            labels[drug_id].append(brand)
    items = []
    for d in session.execute(select(Drug.id, Drug.slug, Drug.name, Drug.tier_group, Drug.regulatory_status).order_by(Drug.name, Drug.slug)):
        has_trial = with_denominator.get(d.slug, 0) > 0
        has_label = bool(labels.get(d.id))
        items.append(Item(
            kind="drug", key=f"drug:{d.slug}", label=d.name, groups={"drug": (d.slug,)},
            f={"slug": d.slug, "tier_group": d.tier_group, "regulatory_status": d.regulatory_status, "ae_rows": ae_rows.get(d.slug, 0),
               "ae_rows_with_denominator": with_denominator.get(d.slug, 0), "label_brands": labels.get(d.id, []),
               "has_trial_ae_denominators": has_trial, "has_label_safety": has_label},
            links=[{"label": "Safety Intelligence", "href": f"/safety?{urlencode({'drug': d.slug})}"},
                   {"label": "Molecule Explorer", "href": f"/molecules?{urlencode({'drug': d.slug})}"}],
        ))
    return items


def _section_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(v) for v in value if v)
    if isinstance(value, dict):
        return " ".join(str(v) for v in value.values() if v)
    return None if value is None else str(value)


def source_conflicts(session: Session) -> list[Item]:
    """Every multi-source group the conflict engine classifies: the one integration point for the conflict dimension.

    One Item per group of rows from different public sources that purport to report the same quantity
    (``incretinos.engines.conflicts.all_groups``), grouped by molecule (the trial's drugs that are not only comparators,
    as /conflicts filters them) and by trial, carrying the group's class and linking to the group on /conflicts. Rows
    rejected or held in review, and full-text rows no reviewer has approved, take part in no group (``incretinos.db``).
    """
    items = []
    for group in conflict_engine.all_groups(session):
        title = group.title()
        where = ", ".join(p for p in (title["arm"], title["time_point"]) if p)
        label = f"{group.study['display_name'] or group.study['nct_id']}: {title['quantity']}" + (f" ({where})" if where else "")
        items.append(Item(kind="conflict", key=f"conflict:{group.group_id}", label=label, groups=_groups(group.drugs, group.study["nct_id"]),
                          f={"group_id": group.group_id, "class": group.cls, "class_label": conflict_engine.CLASS_LABEL[group.cls],
                             "evidence_type": group.evidence_type, "category": group.category, "n_rows": len(group.rows), "n_sources": group.sources,
                             "study_nct": group.study["nct_id"], "study_name": group.study["display_name"], "explanation": group.explanation()},
                          links=[{"label": "Open the group in Source Conflicts", "href": f"/conflicts?{urlencode({'group': group.group_id})}"}]))
    return items


def _load_review_items(session: Session) -> tuple[list[Item], list[Item], list[Item], set[tuple[str, str]]]:
    rows = review_queries.index_rows(session)
    state = review_queries.review_state(session, rows)
    labels = {}
    if state.orphans:
        wanted = {d.evidence_key for d in state.orphans}
        for etype, key, fingerprint, label in session.execute(select(EvidenceVersion.evidence_type, EvidenceVersion.evidence_key, EvidenceVersion.fingerprint,
                                                                     EvidenceVersion.label).where(EvidenceVersion.evidence_key.in_(wanted))):
            labels[(etype, key, fingerprint)] = label
    orphans = [Item(kind="decision", key=f"decision:{d.id}", label=labels.get((d.evidence_type, d.evidence_key, d.fingerprint or "")) or f"{d.evidence_type} {d.evidence_key[:12]}",
                    groups={}, f={"id": d.id, "decision": d.decision, "reviewer": d.reviewer, "decided_at": d.decided_at, "evidence_type": d.evidence_type},
                    links=[{"label": "Orphaned decisions in the Evidence Review summary", "href": "/review#review-summary"}])
               for d in state.orphans]
    studies = dict(session.execute(select(Study.source_id, Study.nct_id).where(Study.source_id.is_not(None))).all())
    changes = [Item(kind="change_event", key=f"change:{c.id}", label=c.summary, groups=_groups(None, studies.get(c.source_id), c.tier, c.source_type),
                    f={"id": c.id, "review_status": c.review_status, "significance": c.significance, "is_synthetic": bool(c.is_synthetic), "source_key": c.key,
                       "detected_at": _aware(c.detected_at)},
                    links=[{"label": "Changes and review queue", "href": "/sources?tab=changes"}])
               for c in session.execute(select(ChangeEvent.id, ChangeEvent.summary, ChangeEvent.review_status, ChangeEvent.significance, ChangeEvent.is_synthetic,
                                               ChangeEvent.detected_at, ChangeEvent.source_id, EvidenceSource.key, EvidenceSource.tier, EvidenceSource.source_type)
                                        .join(EvidenceSource, EvidenceSource.id == ChangeEvent.source_id).order_by(ChangeEvent.id))]
    queue = [Item(kind="queue_item", key=f"queue:{q.id}", label=q.title, groups={}, f={"id": q.id, "status": q.status, "item_type": q.item_type, "priority": q.priority},
                  links=[{"label": "Changes and review queue", "href": "/sources?tab=changes"}])
             for q in session.execute(select(ReviewQueueItem.id, ReviewQueueItem.title, ReviewQueueItem.status, ReviewQueueItem.item_type, ReviewQueueItem.priority)
                                      .order_by(ReviewQueueItem.id))]
    return orphans, changes, queue, set(state.open_requests)


def collect(session: Session, *, as_of: datetime | None = None, refresh_days: dict[str, int] | None = None) -> Facts:
    """Read every universe the metrics count. Analytic reads first, through the session rule; then the opt-in block."""
    as_of = _aware(as_of) or datetime.now(UTC)
    rule = {**DEFAULT_REFRESH_DAYS, **(refresh_days or {})}
    index = _index(session)
    fallback = _Fallback(session)
    evidence = _load_evidence(session, index, fallback, analytic=True)
    sources = _load_sources(session, as_of, rule)
    drugs = _load_drugs(session, evidence)
    conflicts = source_conflicts(session)
    orphans, changes, queue, open_requests = _load_review_items(session)
    # The one place rejected and held rows are read: the review-status dimension counts them.
    with rejected_visible(session):
        everything = _load_evidence(session, index, fallback, analytic=False, open_requests=open_requests)
    labels = _labels(session, evidence + everything, sources)
    facts = Facts(as_of=as_of, refresh_days=rule, universes={ALL_EVIDENCE: everything, EVIDENCE: evidence, SOURCES: sources, DRUGS: drugs, ORPHANS: orphans,
                                                             CHANGES: changes, QUEUE: queue, CONFLICTS: conflicts}, labels=labels)
    facts.width_groups = _width_groups(evidence)
    facts.press_release_pairs = _press_release_only_keys(evidence)
    return facts


def _labels(session: Session, evidence: list[Item], sources: list[Item]) -> dict[str, dict[str, str]]:
    drugs = dict(session.execute(select(Drug.slug, Drug.name)).all())
    for item in evidence:
        for slug in item.groups.get("drug", ()):
            # An arm drug stored on an arm (placebo, a combination of two molecules) that is not a tracked molecule.
            drugs.setdefault(slug, f"{slug} (arm drug as stored; not a tracked molecule)")
    studies = {}
    for nct, name in session.execute(select(Study.nct_id, Study.display_name)):
        studies[nct] = f"{name} ({nct})" if name and name != nct else nct
    for item in evidence:
        nct = item.f.get("study_nct")
        if nct and nct not in studies:
            studies[nct] = f"{item.f.get('study_name')} ({nct})" if item.f.get("study_name") else nct
    tiers = {}
    for tier in {*(t for i in evidence for t in i.groups.get("tier", ())), *(t for s in sources for t in s.groups.get("tier", ()))}:
        try:
            tiers[tier] = TIER_LABEL[EvidenceTier(tier)]
        except ValueError:
            tiers[tier] = tier
    types = {t: SOURCE_TYPE_LABEL.get(t, t) for t in {*(t for i in evidence for t in i.groups.get("source_type", ())), *(t for s in sources for t in s.groups.get("source_type", ()))}}
    categories = {c: _sentence(CATEGORY_LABEL.get(c, c)) for c in {t for i in evidence for t in i.groups.get("endpoint_category", ())}}
    return {"drug": drugs, "study": studies, "tier": tiers, "source_type": types, "endpoint_category": categories}


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


# --------------------------------------------------------------------------- precision: comparable interval groups


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _width(item: Item) -> float | None:
    low, high = item.f.get("ci_low"), item.f.get("ci_high")
    if low is None or high is None:
        return None
    return float(high) - float(low)


def width_group_key(item: Item) -> str | None:
    """Intervals are comparable only within one evidence type, endpoint category, parameter, unit and confidence level."""
    if item.evidence_type not in ("result", "analysis") or _width(item) is None:
        return None
    level = item.f.get("ci_level")
    parts = [item.evidence_type, item.f.get("endpoint_category") or "", _norm(item.f.get("param_type")), _norm(item.f.get("unit")),
             "" if level is None else f"{float(level):g}"]
    return "~".join(parts)


def quartiles(values: list[float]) -> tuple[float, float, float]:
    """First quartile, median and third quartile by linear interpolation between order statistics (the "inclusive"
    method, numpy's default), so each can be reproduced by hand from the listed widths."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0], ordered[0], ordered[0]
    q1, q2, q3 = statistics.quantiles(ordered, n=4, method="inclusive")
    return q1, q2, q3


def _width_groups(evidence: list[Item]) -> dict[str, dict[str, Any]]:
    members: dict[str, list[float]] = defaultdict(list)
    sample: dict[str, Item] = {}
    for item in evidence:
        key = width_group_key(item)
        if key is None:
            continue
        width = _width(item)
        if width is None or width < 0:  # reversed limits are a check flag (INTERVAL_LIMITS_REVERSED), not a width
            continue
        members[key].append(width)
        sample.setdefault(key, item)
    out: dict[str, dict[str, Any]] = {}
    for key, widths in members.items():
        ordered = sorted(widths)
        item = sample[key]
        q1, median, q3 = quartiles(ordered)
        out[key] = {
            "key": key, "evidence_type": item.evidence_type, "endpoint_category": item.f.get("endpoint_category"), "param_type": item.f.get("param_type"),
            "unit": item.f.get("unit"), "ci_level": item.f.get("ci_level"), "n": len(ordered), "min": ordered[0], "max": ordered[-1],
            "p25": q1, "median": median, "p75": q3, "shown": len(ordered) >= MIN_WIDTH_GROUP,
        }
    return out


# --------------------------------------------------------------------------- metrics


Test = Callable[[Item, str | None], bool]


@dataclass(frozen=True)
class Metric:
    id: str
    dimension: str
    label: str
    rule: str
    universe: str
    noun: str
    base: Callable[[Item], bool]
    test: Test
    denominator: str
    param: str | None = None
    #: Exact equivalent on another page: (page path, page params, page filter names, API path, API params, API filter names, API total field).
    page: tuple[str, dict[str, str], dict[str, str], str, dict[str, str], dict[str, str], str] | None = None
    #: A related page that is not an exact filter of the same records.
    context: tuple[str, str] | None = None
    #: Text shown beside each listed record.
    note: Callable[[Item, str | None], str | None] | None = None


def _is(evidence_type: str) -> Callable[[Item], bool]:
    return lambda item: item.evidence_type == evidence_type


def _every(_: Item) -> bool:
    return True


def _review_page(params: dict[str, str], api: dict[str, str]):
    return ("/review", params, REVIEW_PAGE_FILTERS, "/api/review/evidence", api, REVIEW_API_FILTERS, "total")


def _status_metric(status: str, label: str, rule: str) -> Metric:
    return Metric(f"status_{status.lower()}", "review_status", label, rule, ALL_EVIDENCE, "records", _every,
                  lambda i, _: i.f["status"] == status, "every stored reviewable record", page=_review_page({"status": status}, {"status": status}))


_KNOWN_STATUSES = (VerificationStatus.MACHINE_EXTRACTED.value, VerificationStatus.QUOTE_ANCHORED.value, VerificationStatus.PENDING_HUMAN_REVIEW.value,
                   VerificationStatus.HUMAN_VERIFIED.value, VerificationStatus.REJECTED.value, VerificationStatus.SOURCE_CHANGED_RECHECK_REQUIRED.value)


def _estimand_stated(item: Item) -> bool:
    return item.f.get("estimand_label") not in (None, "NOT_STATED")


def _flag_note(item: Item, code: str | None) -> str | None:
    for flag in item.f.get("flags", []):
        if flag.get("code") == code:
            return str(flag.get("explanation") or "")
    return None


def _risk_note(item: Item, _: str | None) -> str | None:
    codes = sorted({str(f.get("code")) for f in item.f.get("flags", [])})
    return ", ".join(codes) or None


def _interval_note(item: Item, _: str | None) -> str | None:
    low, high, level = item.f.get("ci_low"), item.f.get("ci_high"), item.f.get("ci_level")
    if low is None and high is None:
        return "No interval stored."
    shown = f"{'' if level is None else f'{level:g}% '}interval {'not stored' if low is None else f'{low:g}'} to {'not stored' if high is None else f'{high:g}'}"
    width = _width(item)
    return shown + (f"; width {width:g}" if width is not None else "")


def _estimand_note(item: Item, _: str | None) -> str | None:
    return f"Estimand {item.f.get('estimand_label') or 'not linked'}; basis {item.f.get('estimand_basis') or 'none recorded'}."


def _source_note(item: Item, _: str | None) -> str | None:
    retrieved = item.f.get("retrieved_at")
    when = f"retrieved {retrieved.date().isoformat()} ({item.f['age_days']:.0f} days before the count)" if retrieved else "retrieval date not recorded"
    return (f"{when}; refresh rule {item.f['refresh_days']} days for {item.f.get('source_type')}; {item.f['snapshots']} snapshot(s), "
            f"{item.f['distinct_hashes']} distinct content hash(es), {item.f['change_events']} change event(s).")


def _drug_note(item: Item, _: str | None) -> str | None:
    trial = (f"{item.f['ae_rows_with_denominator']} of {item.f['ae_rows']} adverse-event rows on its own arms carry a denominator"
             if item.f["ae_rows"] else "no adverse-event row on an arm of this molecule")
    label = f"FDA labeling with safety sections: {', '.join(item.f['label_brands'])}" if item.f["label_brands"] else "no stored FDA labeling with safety sections"
    return f"{trial}; {label}; regulatory status {item.f['regulatory_status']}."


def _decision_note(item: Item, _: str | None) -> str | None:
    return f"{item.f['decision']} by {item.f['reviewer']} on {item.f['decided_at']:%Y-%m-%d} ({item.f['evidence_type']})."


def _change_note(item: Item, _: str | None) -> str | None:
    return f"{item.f['significance']} significance; {item.f['review_status']}" + ("; synthetic demonstration change, not a real registry change" if item.f["is_synthetic"] else "")


def _queue_note(item: Item, _: str | None) -> str | None:
    return f"{item.f['item_type']}; priority {item.f['priority']}; {item.f['status']}"


def _conflict_note(item: Item, _: str | None) -> str | None:
    kind = conflict_engine.EVIDENCE_TYPE_LABEL.get(item.f["evidence_type"], item.f["evidence_type"])
    return f"{item.f['class_label']}; {kind.lower()}; {item.f['n_sources']} sources, {item.f['n_rows']} rows; trial {item.f['study_nct']}."


def _conflict_metric(metric_id: str, cls: str, label: str, rule: str) -> Metric:
    """A count of multi-source groups of one class, with /conflicts filtered to the same class as its exact equivalent."""
    filters = {"drug": "drug", "study": "study"}
    return Metric(metric_id, "reliability", label, rule, CONFLICTS, "groups", _every, lambda i, _, c=cls: i.f["class"] == c, "multi-source groups",
                  page=("/conflicts", {"class": cls}, filters, "/api/conflicts", {"class": cls}, filters, "total"), note=_conflict_note)


def _tier_note(item: Item, _: str | None) -> str | None:
    return f"{item.f.get('tier')} from {item.f.get('source_type')}"


def _press_release_only_keys(evidence: list[Item]) -> set[tuple[str, str]]:
    """(trial, endpoint category) pairs whose analytic results come only from press releases."""
    tiers: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in evidence:
        if item.evidence_type == "result" and item.f.get("study_nct") and item.f.get("endpoint_category"):
            tiers[(item.f["study_nct"], item.f["endpoint_category"])].add(item.f.get("tier") or "")
    return {key for key, found in tiers.items() if found == {EvidenceTier.PRESS_RELEASE.value}}


def _metrics() -> list[Metric]:
    result, analysis, ae = _is("result"), _is("analysis"), _is("adverse_event")
    m: list[Metric] = [
        # ---- review status (rejected and held rows included)
        Metric("review_rows", "review_status", "Stored reviewable records", "Every stored result, analysis, adverse-event row, claim, mechanism statement, indication "
               "and exposure equivalence, whatever its status, rejected and held rows included.", ALL_EVIDENCE, "records", _every, lambda i, _: True,
               "every stored reviewable record", page=_review_page({}, {})),
        _status_metric("MACHINE_EXTRACTED", "Machine-extracted", "Verification status MACHINE_EXTRACTED: parsed by code from a structured registry record whose snapshot hash is stored. Nobody has checked it."),
        _status_metric("QUOTE_ANCHORED", "Quote-anchored", "Verification status QUOTE_ANCHORED: the locator quote was found word for word in the source and contains the stored numbers. "
                       "This does not show that the number belongs to this arm, endpoint or estimand."),
        _status_metric("PENDING_HUMAN_REVIEW", "Pending human review", "Verification status PENDING_HUMAN_REVIEW: no automatic support could be established; waiting for a reviewer."),
        _status_metric("HUMAN_VERIFIED", "Human-verified", "Verification status HUMAN_VERIFIED: an authorized reviewer approved this exact version in a recorded decision. Code never sets it."),
        _status_metric("REJECTED", "Rejected in review", "Verification status REJECTED: a reviewer rejected this exact version. Kept for audit, excluded from every analytic count on this page."),
        _status_metric("SOURCE_CHANGED_RECHECK_REQUIRED", "Changed since review, recheck required",
                       "Verification status SOURCE_CHANGED_RECHECK_REQUIRED: a decision exists, but the record or its source changed afterwards."),
        Metric("status_other", "review_status", "Any other stored status", "A stored status that is none of the six above (counted so the statuses add up to the total).",
               ALL_EVIDENCE, "records", _every, lambda i, _: i.f["status"] not in _KNOWN_STATUSES, "every stored reviewable record"),
        Metric("review_hold", "review_status", "On review hold", "review_hold is set: a reviewer rejected an earlier version, the content changed since, and nobody has "
               "decided on the current version. Excluded from analytic use like a rejected row. These rows are also counted under their status above.",
               ALL_EVIDENCE, "records", _every, lambda i, _: bool(i.f["hold"]), "every stored reviewable record"),
        Metric("open_requests", "review_status", "Records with an open review request", "At least one REQUEST_INFO or PROPOSE_CORRECTION decision recorded after the "
               "record's latest decisive decision (or with no decisive decision).", ALL_EVIDENCE, "records", _every, lambda i, _: bool(i.f.get("open_request")),
               "every stored reviewable record", page=_review_page({"changed": "request"}, {"has_open_request": "true"})),
        Metric("orphaned_decisions", "review_status", "Orphaned decisions", "Recorded evidence decisions that match no current record (its evidence key and natural key "
               "no longer exist). A rejection among them may mean an excluded value is in use again under another identity.", ORPHANS, "decisions", _every,
               lambda i, _: True, "recorded decisions whose evidence no longer exists",
               page=("/review#review-summary", {}, {}, "/api/review/summary", {}, {}, "orphaned_decisions_total"), note=_decision_note),
        Metric("change_events_open", "review_status", "Source changes awaiting investigation", "Detected source changes whose review status is OPEN.", CHANGES, "change events",
               _every, lambda i, _: i.f["review_status"] == ReviewStatus.OPEN.value, "every detected source change",
               context=("Changes and review queue", "/sources?tab=changes"), note=_change_note),
        Metric("queue_open", "review_status", "Review-queue items awaiting a decision", "Review-queue items (source changes, reconciliation problems) whose status is OPEN.",
               QUEUE, "queue items", _every, lambda i, _: i.f["status"] == ReviewStatus.OPEN.value, "every review-queue item",
               page=("/sources?tab=changes", {}, {}, "/api/review-queue", {"status": "OPEN"}, {}, "total"), note=_queue_note),
        Metric("queue_in_review", "review_status", "Review-queue items waiting for information", "Review-queue items whose status is IN_REVIEW (a reviewer asked for more information).",
               QUEUE, "queue items", _every, lambda i, _: i.f["status"] == ReviewStatus.IN_REVIEW.value, "every review-queue item",
               page=("/sources?tab=changes", {}, {}, "/api/review-queue", {"status": "IN_REVIEW"}, {}, "total"), note=_queue_note),
        # ---- completeness (analytic rows only)
        Metric("result_no_ci", "completeness", "Results without a confidence interval", "Result rows with ci_low or ci_high empty (no complete interval stored).",
               EVIDENCE, "results", result, lambda i, _: i.f.get("ci_low") is None or i.f.get("ci_high") is None, "results in analytic use", note=_interval_note),
        Metric("result_no_n", "completeness", "Results without n analysed", "Result rows with n_analyzed empty.", EVIDENCE, "results", result,
               lambda i, _: i.f.get("n_analyzed") is None, "results in analytic use"),
        Metric("result_no_unit", "completeness", "Results without a unit", "Result rows whose unit is empty.", EVIDENCE, "results", result,
               lambda i, _: _blank(i.f.get("unit")), "results in analytic use"),
        Metric("result_estimand_not_stated", "completeness", "Results with no estimand stated", "Result rows linked to no estimand, or to one labelled NOT_STATED.",
               EVIDENCE, "results", result, lambda i, _: not _estimand_stated(i), "results in analytic use", note=_estimand_note),
        Metric("result_estimand_inferred", "completeness", "Results with an inferred estimand", "Result rows whose estimand is named but was inferred (basis "
               "INFERRED_FROM_ANALYSIS_DESCRIPTION or INFERRED_BY_CURATOR), not stated by the source.", EVIDENCE, "results", result,
               lambda i, _: _estimand_stated(i) and str(i.f.get("estimand_basis") or "").startswith("INFERRED"), "results in analytic use", note=_estimand_note),
        Metric("result_estimand_stated", "completeness", "Results with an estimand stated by the source", "Result rows whose estimand is named and whose basis is "
               "STATED_BY_SOURCE (a quote from the cited source names it).", EVIDENCE, "results", result,
               lambda i, _: _estimand_stated(i) and i.f.get("estimand_basis") == "STATED_BY_SOURCE", "results in analytic use", note=_estimand_note),
        Metric("analysis_no_method", "completeness", "Analyses without a method", "Between-arm analysis rows whose method is empty.", EVIDENCE, "analyses", analysis,
               lambda i, _: _blank(i.f.get("method")), "analyses in analytic use"),
        Metric("analysis_multiplicity_not_stated", "completeness", "Analyses with multiplicity control not stated", "Between-arm analysis rows whose multiplicity_controlled "
               "is NOT_STATED.", EVIDENCE, "analyses", analysis, lambda i, _: i.f.get("multiplicity_controlled") in (None, TriState.NOT_STATED.value),
               "analyses in analytic use"),
        Metric("analysis_no_method_or_multiplicity", "completeness", "Analyses without method or multiplicity status", "Between-arm analysis rows with an empty method, "
               "multiplicity control NOT_STATED, or both.", EVIDENCE, "analyses", analysis,
               lambda i, _: _blank(i.f.get("method")) or i.f.get("multiplicity_controlled") in (None, TriState.NOT_STATED.value), "analyses in analytic use"),
        Metric("analysis_estimand_not_stated", "completeness", "Analyses with no estimand stated", "Between-arm analysis rows linked to no estimand, or to one labelled NOT_STATED.",
               EVIDENCE, "analyses", analysis, lambda i, _: not _estimand_stated(i), "analyses in analytic use", note=_estimand_note),
        Metric("ae_no_denominator", "completeness", "Adverse-event rows without a denominator", "Adverse-event rows with n_at_risk empty (no number of participants at risk).",
               EVIDENCE, "adverse-event rows", ae, lambda i, _: i.f.get("n_at_risk") is None, "adverse-event rows in analytic use"),
        Metric("drug_no_trial_ae_denominators", "completeness", "Molecules without a trial adverse-event table with denominators",
               "No adverse-event row of kind PARTICIPANTS_WITH_EVENT, in analytic use, on a trial arm whose stored drug is this molecule alone, carries n_at_risk.",
               DRUGS, "molecules", _every, lambda i, _: not i.f["has_trial_ae_denominators"], "tracked molecules", note=_drug_note),
        Metric("drug_no_label_safety", "completeness", "Molecules without FDA label safety sections",
               f"No stored FDA label for the molecule has a non-empty {', '.join(LABEL_SAFETY_SECTIONS)} section. Expected for a molecule that is not approved.",
               DRUGS, "molecules", _every, lambda i, _: not i.f["has_label_safety"], "tracked molecules", note=_drug_note),
        Metric("drug_incomplete_safety", "completeness", "Molecules with incomplete safety evidence", "Either of the two rules above: no trial adverse-event table with "
               "denominators, or no FDA label safety sections. Each molecule's record says which is missing.", DRUGS, "molecules", _every,
               lambda i, _: not (i.f["has_trial_ae_denominators"] and i.f["has_label_safety"]), "tracked molecules", note=_drug_note),
        # ---- source authority (context)
        Metric("authority_rows", "authority", "Records in analytic use", "Every stored reviewable record in analytic use, by the evidence tier and source type of its source.",
               EVIDENCE, "records", _every, lambda i, _: True, "records in analytic use", note=_tier_note),
        Metric("authority_sources", "authority", "Evidence sources", "Every stored evidence source, by tier and source type.", SOURCES, "sources", _every, lambda i, _: True,
               "evidence sources", page=("/sources", {}, {"source_type": "type"}, "/api/sources", {}, {"source_type": "source_type", "tier": "tier"}, "total"), note=_source_note),
        Metric("press_release_only", "authority", "Press-release-only results", "Result rows from a press release for a trial and endpoint category that no other kind "
               "of source in analytic use reports (no registry, publication, label or review result for the same trial and endpoint category).", EVIDENCE, "results",
               result, lambda i, v: False, "results in analytic use", note=_tier_note),  # test replaced in _bind with the computed pairs
        # ---- reliability signals
        Metric("risk_high", "reliability", "Records with a high-risk check flag", "Records in analytic use whose highest automated check severity is HIGH.", EVIDENCE,
               "records", _every, lambda i, _: i.f.get("risk") == "HIGH", "records in analytic use", note=_risk_note),
        Metric("risk_medium", "reliability", "Records with a medium-risk check flag", "Highest automated check severity MEDIUM.", EVIDENCE, "records", _every,
               lambda i, _: i.f.get("risk") == "MEDIUM", "records in analytic use", note=_risk_note),
        Metric("risk_low", "reliability", "Records with a low-risk check flag", "Highest automated check severity LOW.", EVIDENCE, "records", _every,
               lambda i, _: i.f.get("risk") == "LOW", "records in analytic use", note=_risk_note),
        Metric("risk_none", "reliability", "Records with no check flag", "No automated check flagged the record. This is not a confirmation that it is right.", EVIDENCE,
               "records", _every, lambda i, _: i.f.get("risk") in (None, "NONE"), "records in analytic use"),
        Metric("flag_code", "reliability", "Records carrying one check code", "Records in analytic use that carry the named automated check code (review/checks.py).",
               EVIDENCE, "records", _every, lambda i, code: any(f.get("code") == code for f in i.f.get("flags", [])), "records in analytic use", param="code", note=_flag_note),
        _conflict_metric("source_conflicts", conflict_engine.TRUE_CONFLICT, "Source conflicts: true conflicts",
                         "Groups in which two public sources report the same quantity (same trial, randomized group, endpoint, time point, estimand and "
                         "baseline) and the values differ by more than the precision the sources printed (engines/conflicts.py). Each is a question for "
                         "reconciliation, not an error identified in either source."),
        _conflict_metric("source_conflicts_insufficient", conflict_engine.INSUFFICIENT, "Source differences that cannot be reconciled from the stored records",
                         "Groups whose values differ and in which a detail needed to tell whether the sources report the same quantity (for example the "
                         "estimand) is not stored."),
        _conflict_metric("source_conflicts_explainable", conflict_engine.EXPLAINABLE, "Source differences with a stated explanation",
                         "Groups whose values differ for a reason the stored records state (estimand, baseline, analysis set, follow-up or endpoint "
                         "definition). Not a contradiction."),
        _conflict_metric("source_conflicts_consistent", conflict_engine.CONSISTENT, "Sources that agree",
                         "Groups whose values agree at the precision the sources printed."),
        # ---- precision
        Metric("result_with_interval", "precision", "Results with a reported interval", "Result rows with both ci_low and ci_high stored.", EVIDENCE, "results", result,
               lambda i, _: i.f.get("ci_low") is not None and i.f.get("ci_high") is not None, "results in analytic use", note=_interval_note),
        Metric("analysis_with_interval", "precision", "Analyses with a reported interval", "Between-arm analysis rows with both ci_low and ci_high stored.", EVIDENCE,
               "analyses", analysis, lambda i, _: i.f.get("ci_low") is not None and i.f.get("ci_high") is not None, "analyses in analytic use", note=_interval_note),
        Metric("interval_width_group", "precision", "Intervals in one comparable group", "Rows with a reported interval in one group of the same evidence type, endpoint "
               "category, parameter, unit and confidence level (the group named by its key). Reversed limits are left out.", EVIDENCE, "records", _every,
               lambda i, key: width_group_key(i) == key and (_width(i) or 0) >= 0, "records in analytic use", param="group", note=_interval_note),
        # ---- freshness
        Metric("sources_by_age", "freshness", "Sources by age of the last retrieval", "Sources whose last retrieval falls in the named age bucket (days before the count).",
               SOURCES, "sources", _every, lambda i, bucket: i.f["age_bucket"] == bucket, "evidence sources", param="bucket", note=_source_note),
        Metric("sources_changed", "freshness", "Sources whose content changed", "Sources with more than one distinct stored snapshot hash, or at least one detected change event.",
               SOURCES, "sources", _every, lambda i, _: i.f["distinct_hashes"] > 1 or i.f["change_events"] > 0, "evidence sources", note=_source_note),
        Metric("sources_refresh_due", "freshness", "Sources due a refresh", "Sources whose last retrieval is older than the refresh rule for their source type, or "
               "whose retrieval date is not recorded.", SOURCES, "sources", _every, lambda i, _: bool(i.f["refresh_due"]), "evidence sources", note=_source_note),
    ]
    return m


METRICS: dict[str, Metric] = {metric.id: metric for metric in _metrics()}
PARAM_VALUES_HINT = {"code": "an automated check code", "group": "an interval group key", "bucket": "an age bucket"}


def _bound(metric: Metric, facts: Facts) -> Metric:
    """Metrics whose test depends on the whole universe get it here."""
    if metric.id == "press_release_only":
        pairs = facts.press_release_pairs

        def test(item: Item, _: str | None) -> bool:
            return item.f.get("tier") == EvidenceTier.PRESS_RELEASE.value and (item.f.get("study_nct"), item.f.get("endpoint_category")) in pairs

        return Metric(**{**metric.__dict__, "test": test})
    return metric


# --------------------------------------------------------------------------- counting


def _check_value(metric: Metric, value: str | None) -> None:
    if metric.param and not value:
        raise QualityQueryError(f"The metric {metric.id} counts one {metric.param} at a time: give value=<{PARAM_VALUES_HINT[metric.param]}>.")
    if not metric.param and value:
        raise QualityQueryError(f"The metric {metric.id} takes no value.")


def _check_filters(metric: Metric, filters: dict[str, str]) -> None:
    allowed = UNIVERSE_DIMS[metric.universe]
    for dim in filters:
        if dim not in BREAKDOWNS:
            raise QualityQueryError(f"Unknown breakdown {dim!r}; use one of {', '.join(BREAKDOWNS)}.")
        if dim not in allowed:
            raise QualityQueryError(f"The metric {metric.id} counts {UNIVERSE_LABEL[metric.universe].lower()}, which have no {BREAKDOWNS[dim].lower()}.")


def _matching(metric: Metric, facts: Facts, value: str | None, filters: dict[str, str]) -> tuple[list[Item], list[Item]]:
    """(denominator items, counted items) for one metric under the filters."""
    bound = _bound(metric, facts)
    base = [i for i in facts.universes[metric.universe] if bound.base(i) and all(i.in_group(d, v) for d, v in filters.items())]
    return base, [i for i in base if bound.test(i, value)]


def links(metric: Metric, value: str | None = None, filters: dict[str, str] | None = None) -> dict[str, Any]:
    """Where to see the counted records: always this page's record list; also another page when it filters to exactly the same set."""
    filters = filters or {}
    query = {"metric": metric.id, **({"value": value} if value else {}), **filters}
    out: dict[str, Any] = {
        "records": {"href": f"/quality/records?{urlencode(query)}", "api": {"path": "/api/quality/records", "params": query, "total_field": "total"}},
        "page": None,
        "context": {"label": metric.context[0], "href": metric.context[1]} if metric.context else None,
    }
    if metric.page and not value:
        path, params, page_filters, api_path, api_params, api_filters, total_field = metric.page
        if all(d in page_filters and d in api_filters for d in filters):
            page_query = {**params, **{page_filters[d]: v for d, v in filters.items()}}
            api_query = {**api_params, **{api_filters[d]: v for d, v in filters.items()}}
            base, _, anchor = path.partition("#")
            base_path, _, fixed = base.partition("?")
            fixed_params = dict(p.split("=", 1) for p in fixed.split("&") if p)
            href = base_path + (f"?{urlencode({**fixed_params, **page_query})}" if fixed_params or page_query else "") + (f"#{anchor}" if anchor else "")
            out["page"] = {"href": href, "api": {"path": api_path, "params": api_query, "total_field": total_field}}
    out["href"] = out["page"]["href"] if out["page"] else out["records"]["href"]
    return out


def _metric_out(metric: Metric, facts: Facts, filters: dict[str, str], value: str | None = None, *, with_types: bool = False) -> dict[str, Any]:
    base, counted = _matching(metric, facts, value, filters)
    out = {
        "id": metric.id, "dimension": metric.dimension, "label": metric.label, "rule": metric.rule, "noun": metric.noun, "universe": metric.universe,
        "universe_label": UNIVERSE_LABEL[metric.universe], "denominator_label": metric.denominator, "count": len(counted), "denominator": len(base),
        "share": (len(counted) / len(base)) if base else None, "param": metric.param, "breakdowns": list(UNIVERSE_DIMS[metric.universe]),
        "includes_excluded_rows": metric.universe == ALL_EVIDENCE, "links": links(metric, value, filters),
    }
    if with_types and metric.universe in (ALL_EVIDENCE, EVIDENCE):
        by_type: dict[str, list[int]] = {}
        for t in EVIDENCE_TYPE_LABEL:
            typed_base = sum(1 for i in base if i.evidence_type == t)
            if typed_base:
                by_type[t] = [sum(1 for i in counted if i.evidence_type == t), typed_base]
        out["by_type"] = by_type
    return out


def _distribution(metric: Metric, facts: Facts, dim: str, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
    filters = filters or {}
    _, counted = _matching(metric, facts, None, filters)
    counts: Counter[str] = Counter(v for i in counted for v in i.groups.get(dim, ()))
    missing = sum(1 for i in counted if not i.groups.get(dim))
    rows = [{"value": v, "label": facts.labels.get(dim, {}).get(v, v), "count": n, "links": links(metric, None, {**filters, dim: v})}
            for v, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    if missing:
        rows.append({"value": None, "label": f"No {BREAKDOWNS[dim].lower()} recorded", "count": missing, "links": None})
    return rows


def _param_distribution(metric: Metric, facts: Facts, values: Iterable[tuple[str, dict[str, Any]]], filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
    filters = filters or {}
    out = []
    for value, extra in values:
        _, counted = _matching(metric, facts, value, filters)
        out.append({"value": value, **extra, "count": len(counted), "links": links(metric, value, filters)})
    return out


def _flag_distribution(facts: Facts, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Records carrying each automated check code (one pass; a record with a code twice is counted once for it)."""
    filters = filters or {}
    metric = METRICS["flag_code"]
    counts: Counter[str] = Counter()
    for item in facts.universes[EVIDENCE]:
        if all(item.in_group(d, v) for d, v in filters.items()):
            counts.update({str(f.get("code")) for f in item.f.get("flags", [])})
    codes = list(CHECKS) + sorted(c for c in counts if c not in CHECKS)
    return [{"value": code, "title": CHECKS.get(code, {}).get("title", code), "severity": CHECKS.get(code, {}).get("severity"),
             "help": CHECKS.get(code, {}).get("help"), "known": code in CHECKS, "count": counts.get(code, 0), "links": links(metric, code, filters)}
            for code in codes]


def _age_values() -> list[tuple[str, dict[str, Any]]]:
    return [*((bucket, {"label": label}) for bucket, label, _ in AGE_BUCKETS), (AGE_NOT_RECORDED, {"label": "Retrieval date not recorded"})]


def _refresh_rule(facts: Facts) -> dict[str, Any]:
    present = sorted({i.f.get("source_type") or "" for i in facts.universes[SOURCES]} | set(facts.refresh_days))
    return {
        "rule": "A source is due a refresh when its last retrieval is more than the number of days below for its source type before the time of the count, "
                "or when its retrieval date is not recorded.",
        "days": [{"source_type": t, "label": SOURCE_TYPE_LABEL.get(t, t), "days": refresh_days_for(t, facts.refresh_days),
                  "default": DEFAULT_REFRESH_DAYS.get(t, FALLBACK_REFRESH_DAYS)} for t in present if t],
        "fallback_days": FALLBACK_REFRESH_DAYS,
        "setting": REFRESH_SETTING,
    }


def _width_out(facts: Facts) -> dict[str, Any]:
    metric = METRICS["interval_width_group"]
    groups = sorted(facts.width_groups.values(), key=lambda g: (-g["n"], g["key"]))
    shown = [g for g in groups if g["shown"]]
    labels = facts.labels.get("endpoint_category", {})
    return {
        "note": "Descriptive. Widths are ci_high minus ci_low as stored. They are compared only inside one group (same evidence type, endpoint category, "
                f"parameter, unit and confidence level), and a group is shown only when it holds at least {MIN_WIDTH_GROUP} intervals. "
                "A narrow interval is not a better result; it usually reflects a larger trial or a less variable measure. For a ratio (odds, hazard "
                "or risk ratio) the width is taken on the ratio scale as stored.",
        "min_group_size": MIN_WIDTH_GROUP,
        "groups": [{**g, "endpoint_label": labels.get(g["endpoint_category"] or "", g["endpoint_category"]), "links": links(metric, g["key"])} for g in shown],
        "groups_too_small": len(groups) - len(shown),
        "intervals_in_small_groups": sum(g["n"] for g in groups if not g["shown"]),
    }


def overview(facts: Facts, by: str = "drug") -> dict[str, Any]:
    """Every metric with its true total and links, the distributions, and one breakdown."""
    if by not in BREAKDOWNS:
        raise QualityQueryError(f"Unknown breakdown {by!r}; use one of {', '.join(BREAKDOWNS)}.")
    metrics = [_metric_out(m, facts, {}, with_types=True) for m in METRICS.values() if not m.param]
    rows, sources = METRICS["authority_rows"], METRICS["authority_sources"]
    distributions = {
        "records_by_tier": _distribution(rows, facts, "tier"),
        "records_by_source_type": _distribution(rows, facts, "source_type"),
        "sources_by_tier": _distribution(sources, facts, "tier"),
        "sources_by_source_type": _distribution(sources, facts, "source_type"),
        "flag_codes": _flag_distribution(facts),
        "retrieval_age": _param_distribution(METRICS["sources_by_age"], facts, _age_values()),
    }
    return {
        "as_of": facts.as_of.isoformat(),
        "dimensions": [{"id": k, **v} for k, v in DIMENSIONS.items()],
        "breakdown_dimensions": [{"id": k, "label": v} for k, v in BREAKDOWNS.items()],
        "metrics": metrics,
        "distributions": distributions,
        "interval_widths": _width_out(facts),
        "refresh_rule": _refresh_rule(facts),
        "safety_rule": {"label_sections": list(LABEL_SAFETY_SECTIONS), "measure_kind": "PARTICIPANTS_WITH_EVENT"},
        "breakdown": breakdown(facts, by),
        "notes": [
            "Every count is computed from the stored records when the page is loaded. There is no combined quality score: each dimension is reported on its own.",
            "A high completeness count says the public record reports a result fully. It does not mean the drug works or is safe.",
            "Rejected rows and rows on review hold are excluded from every count except the review-status dimension, where they are counted.",
        ],
    }


def breakdown(facts: Facts, by: str) -> dict[str, Any]:
    """Each applicable metric counted within each value of one breakdown dimension. Cells are [count, denominator]."""
    if by not in BREAKDOWNS:
        raise QualityQueryError(f"Unknown breakdown {by!r}; use one of {', '.join(BREAKDOWNS)}.")
    applicable = [m for m in METRICS.values() if not m.param and by in UNIVERSE_DIMS[m.universe]]
    values: set[str] = set()
    for m in applicable:
        values.update(v for i in facts.universes[m.universe] for v in i.groups.get(by, ()))
    cells: dict[str, dict[str, list[int]]] = {v: {} for v in values}
    for m in applicable:
        bound = _bound(m, facts)
        base_counts: Counter[str] = Counter()
        hit_counts: Counter[str] = Counter()
        for item in facts.universes[m.universe]:
            if not bound.base(item):
                continue
            hit = bound.test(item, None)
            for v in item.groups.get(by, ()):
                base_counts[v] += 1
                if hit:
                    hit_counts[v] += 1
        for v in values:
            cells[v][m.id] = [hit_counts.get(v, 0), base_counts.get(v, 0)]
    labels = facts.labels.get(by, {})
    groups = [{"value": v, "label": labels.get(v, v), "counts": cells[v]} for v in sorted(values, key=lambda v: (labels.get(v, v).lower(), v))]
    return {"by": by, "label": BREAKDOWNS[by], "metrics": [m.id for m in applicable],
            "not_applicable": [m.id for m in METRICS.values() if not m.param and by not in UNIVERSE_DIMS[m.universe]], "groups": groups,
            "filter_param": by}


def _record_out(item: Item, metric: Metric, value: str | None) -> dict[str, Any]:
    f = item.f
    status = f.get("status")
    return {
        "key": item.key, "kind": item.kind, "label": item.label, "evidence_type": item.evidence_type,
        "evidence_type_label": EVIDENCE_TYPE_LABEL.get(item.evidence_type or "") if item.evidence_type else None,
        "evidence_key": f.get("evidence_key"), "id": f.get("id"), "study_nct": f.get("study_nct"), "study_name": f.get("study_name"),
        "drugs": list(item.groups.get("drug", ())), "tier": f.get("tier") if item.kind in ("evidence", "source") else None,
        "source_type": f.get("source_type") if item.kind in ("evidence", "source") else None, "endpoint_category": f.get("endpoint_category"),
        "verification_status": status, "status_label": STATUS_LABEL.get(status or "", status) if status else None, "review_hold": f.get("hold"),
        "risk_level": f.get("risk"), "note": metric.note(item, value) if metric.note else None, "links": item.links,
    }


def check_records_query(metric_id: str, value: str | None, filters: dict[str, str]) -> Metric:
    """The metric named, after checking that the value and filters apply to it (cheap: nothing is read)."""
    metric = METRICS.get(metric_id)
    if metric is None:
        raise QualityQueryError(f"Unknown metric {metric_id!r}.")
    _check_value(metric, value)
    _check_filters(metric, filters)
    return metric


def records(facts: Facts, metric_id: str, *, value: str | None = None, filters: dict[str, str] | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Exactly the records a metric counted, in a stable order, with the true total."""
    filters = {k: v for k, v in (filters or {}).items() if v}
    metric = check_records_query(metric_id, value, filters)
    base, counted = _matching(metric, facts, value, filters)
    order = {t: n for n, t in enumerate(EVIDENCE_TYPE_LABEL)}
    counted.sort(key=lambda i: (i.f.get("study_nct") or "~", order.get(i.evidence_type or "", 9), i.label.lower(), i.key))
    page = counted[offset : offset + limit]
    return {
        "total": len(counted), "limit": limit, "offset": offset, "rows": [_record_out(i, metric, value) for i in page],
        "denominator": len(base), "metric": _metric_out(metric, facts, filters, value),
        "value": value, "value_label": _value_label(metric, value, facts), "filters": filters,
        "filter_labels": {d: facts.labels.get(d, {}).get(v, v) for d, v in filters.items()},
        "dimension_label": DIMENSIONS[metric.dimension]["label"], "dimension_caveat": DIMENSIONS[metric.dimension]["caveat"], "as_of": facts.as_of.isoformat(),
    }


def _value_label(metric: Metric, value: str | None, facts: Facts) -> str | None:
    if not value:
        return None
    if metric.param == "code":
        return CHECKS.get(value, {}).get("title", value)
    if metric.param == "bucket":
        return dict((b, label) for b, label, _ in AGE_BUCKETS).get(value, "Retrieval date not recorded" if value == AGE_NOT_RECORDED else value)
    if metric.param == "group":
        group = facts.width_groups.get(value)
        if group:
            category = facts.labels.get("endpoint_category", {}).get(group["endpoint_category"] or "", group["endpoint_category"])
            level = "level not stated" if group["ci_level"] is None else f"{group['ci_level']:g}% interval"
            kind = EVIDENCE_TYPE_LABEL.get(group["evidence_type"], group["evidence_type"])
            return f"{kind}: {category}; {group['param_type'] or 'parameter not stated'}; {group['unit'] or 'no unit'}; {level}"
    return value


# --------------------------------------------------------------------------- per molecule and per trial


ASSESSMENT_SECTIONS: dict[str, tuple[str, ...]] = {
    "completeness": ("result_no_ci", "result_no_n", "result_no_unit", "result_estimand_not_stated", "result_estimand_inferred", "result_estimand_stated",
                     "analysis_no_method", "analysis_multiplicity_not_stated", "analysis_estimand_not_stated", "ae_no_denominator", "drug_no_trial_ae_denominators",
                     "drug_no_label_safety"),
    "authority": ("authority_rows", "press_release_only"),
    "reliability": ("risk_high", "risk_medium", "risk_low", "risk_none", "source_conflicts", "source_conflicts_insufficient", "source_conflicts_explainable",
                    "source_conflicts_consistent"),
    "precision": ("result_with_interval", "analysis_with_interval"),
    "review_status": ("review_rows", "status_machine_extracted", "status_quote_anchored", "status_pending_human_review", "status_human_verified", "status_rejected",
                      "status_source_changed_recheck_required", "status_other", "review_hold", "open_requests"),
    "freshness": ("authority_sources", "sources_refresh_due", "sources_changed"),
}


def assessment(facts: Facts, dim: str, value: str) -> dict[str, Any]:
    """One molecule or one trial, dimension by dimension: each rule and the counts behind it, and no combined score."""
    if dim not in ("drug", "study"):
        raise QualityQueryError("An assessment is made for one molecule (drug) or one trial (study).")
    filters = {dim: value}
    known = {v for items in facts.universes.values() for i in items for v in i.groups.get(dim, ())}
    if value not in known:
        raise LookupError(f"No stored record belongs to the {BREAKDOWNS[dim].lower()} {value!r}.")
    sections = []
    for section, ids in ASSESSMENT_SECTIONS.items():
        rows = []
        for metric_id in ids:
            metric = METRICS[metric_id]
            if dim not in UNIVERSE_DIMS[metric.universe]:
                continue
            out = _metric_out(metric, facts, filters)
            if out["denominator"] == 0 and metric.universe == DRUGS:
                continue
            rows.append(out)
        extra: dict[str, Any] = {}
        if section == "authority":
            extra["by_tier"] = _distribution(METRICS["authority_rows"], facts, "tier", filters)
            extra["by_source_type"] = _distribution(METRICS["authority_rows"], facts, "source_type", filters)
        if section == "reliability":
            extra["flag_codes"] = [d for d in _flag_distribution(facts, filters) if d["count"]]
        not_attributed = []
        if section == "freshness" and dim == "drug":
            not_attributed.append("Sources are not attributed to a molecule, so freshness is reported per trial and per source type only.")
        sections.append({"id": section, **DIMENSIONS[section], "metrics": rows, **extra, "not_applicable": not_attributed})
    return {
        "by": dim, "value": value, "label": facts.labels.get(dim, {}).get(value, value), "as_of": facts.as_of.isoformat(), "sections": sections,
        "notes": [
            "Each dimension is shown on its own, with the rule used and the counts behind it. There is no combined score and none should be computed from these counts.",
            "Completeness describes how fully the public record reports the evidence. A complete record does not mean the drug works or is safe.",
            "Records are attributed to a molecule when the molecule is the arm's drug or one of the trial's drugs (comparators included), "
            "as in Evidence Review.",
        ],
    }
