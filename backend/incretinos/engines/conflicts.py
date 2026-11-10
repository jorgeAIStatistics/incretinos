"""Source conflict engine: where public sources report the same quantity, do they agree, and if not, why not?

Rows from different sources are grouped when they purport to report one quantity:

* the same study;
* the same randomized group, by the arm identity rules of ``findings.resolve_arm`` (a documented exposure
  equivalence identifies a label's tablet dosage with the trial's capsule arm; arm labels are never matched as text);
  for a between-arm analysis the same pair of groups in the same orientation;
* the same endpoint as reported: category, measured concept, responder threshold and unit
  (``findings.endpoint_definition``); for an adverse event the same term, measure kind and seriousness table;
* the same time point.

Everything else that defines a quantity — estimand, baseline (randomization or study entry), analysis set, the
endpoint's subset or method, the adverse-event observation period, the effect measure and orientation of a
contrast, the formulation a source names — is compared between every two rows of different sources, field by
field, and each pair is classified:

``CONSISTENT``                the values agree at the precision the sources printed (the reported text, never the float);
``EXPLAINABLE_DIFFERENCE``    the values differ and a stated difference in one of those details explains it;
``INSUFFICIENT_INFORMATION``  the values differ and a detail needed to tell whether they are the same quantity is not stored;
``TRUE_CONFLICT``             every stored detail that defines the quantity is the same and the values still differ beyond precision.

A group takes the most action-requiring class among its pairs (true conflict, then insufficient information, then
explainable difference, then consistent) and keeps every pair's own class. Evidence tier is shown as context and is
never read by the classification: no source wins because of what kind of source it is. Nothing here changes, rounds
or replaces a stored value, and rows rejected or held in review never reach this module (``incretinos.db``).
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from itertools import combinations
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ..db import session_memo
from ..enums import TIER_LABEL, EstimandLabel, EvidenceTier
from ..models import AdverseEvent, Formulation, OutcomeResult, StatisticalAnalysis, Study, StudyArm
from ..review.policy import STATUS_LABEL, is_usable
from ..vocab import is_placebo_arm, parse_single_dose
from .findings import (
    BASELINE_CLASS_LABEL,
    CATEGORY_LABEL,
    EndpointDefinition,
    _documented_equivalent,
    baseline_class,
    display_order,
    effect_scale,
    endpoint_definition,
    resolve_arm,
    source_ref,
    unit_family,
)

TRUE_CONFLICT = "TRUE_CONFLICT"
INSUFFICIENT = "INSUFFICIENT_INFORMATION"
EXPLAINABLE = "EXPLAINABLE_DIFFERENCE"
CONSISTENT = "CONSISTENT"
#: Most action-requiring first. A group carries the first class any of its pairs has.
CLASSES: tuple[str, ...] = (TRUE_CONFLICT, INSUFFICIENT, EXPLAINABLE, CONSISTENT)
CLASS_LABEL = {
    TRUE_CONFLICT: "True conflict",
    INSUFFICIENT: "Insufficient information",
    EXPLAINABLE: "Explainable difference",
    CONSISTENT: "Consistent",
}
CLASS_DEFINITION = {
    TRUE_CONFLICT: "The sources report the same quantity — same study, randomized group, endpoint, time point, estimand, baseline and, where stated, "
                   "analysis set — and the values differ by more than the precision the sources printed.",
    INSUFFICIENT: "The values differ, and a detail needed to tell whether they are the same quantity is not in the stored records "
                  "(for example, one source does not name its estimand). The missing details are listed.",
    EXPLAINABLE: "The values differ, and the stored records state a difference that explains it: estimand, baseline, analysis set, "
                 "follow-up or endpoint definition. This is not an error in either source.",
    CONSISTENT: "The values agree at the precision the sources printed.",
}
EVIDENCE_TYPES: tuple[str, ...] = ("result", "analysis", "adverse_event")
EVIDENCE_TYPE_LABEL = {"result": "Arm-level result", "analysis": "Between-arm analysis", "adverse_event": "Adverse event"}
METHOD = (
    "Rows are grouped when they report the same study, randomized group (arm identity after documented formulation equivalence), endpoint "
    "(category, measured concept, responder threshold, unit) and time point; for an analysis the same pair of groups in the same orientation; for an "
    "adverse event the same term, measure kind and seriousness table. Every two rows of different sources are then compared field by field. Agreement "
    "is judged at the precision each source printed: values agree when the more precise one rounds to the less precise one (the same digits when both "
    "print the same number of decimals). Evidence tier is shown as context only and is never used to classify or to choose a value."
)
TIER_CONTEXT_NOTE = (
    "Context only. The classification above does not read the evidence tier, and no value is preferred because of the kind of source that reports it. "
    "Kinds of source are listed from regulator-reviewed to sponsor topline so a reader can see who reported what."
)
_TIER_RANK = {tier.value: i for i, tier in enumerate(EvidenceTier)}

# Dimension status vocabulary.
SAME = "SAME"  # stated by both rows and equal
DIFFERENT = "DIFFERENT"  # stated by both rows and different
UNKNOWN = "UNKNOWN"  # not determinable from the stored records, and needed to call the rows the same quantity
UNCONFIRMED = "UNCONFIRMED"  # not stated by one or both rows; not needed to call the rows the same quantity
NOTED = "NOTED"  # a difference in how the rows describe the quantity that cannot change the value (recorded, never an explanation)


# --------------------------------------------------------------------------- reported numbers and their precision

_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")


def _decimal_text(text: str | None) -> str | None:
    """The plain number in a reported text ('−12.4', '12.4 %', '62·1') or None when the text is not one number."""
    if text is None:
        return None
    t = text.strip().replace("−", "-").replace("–", "-").replace("·", ".").replace(",", "")
    t = re.sub(r"\s*%$", "", t).strip()
    return t if _NUMBER.fullmatch(t) else None


def reported_number(value: float | None, text: str | None, quote: str | None = None) -> tuple[Decimal | None, int | None, str]:
    """(number as printed, decimals printed, basis) for a stored value.

    The precision is read from the source's own text: ``value_text`` / ``estimate_text`` when it denotes the stored
    number, else the one way the number is printed in the locator quote. Only when neither exists is the stored
    number's shortest representation used, and the basis says so (a printed trailing zero may then be lost).
    """
    if value is None:
        return None, None, "NOT_REPORTED"
    plain = _decimal_text(text)
    if plain is not None and abs(float(plain) - value) < 1e-9:
        return Decimal(plain), _places(plain), "REPORTED_TEXT"
    if quote:
        printed = {m for m in re.findall(r"(?<![\d.·])\d+(?:[.·]\d+)?(?![\d·]|\.\d)", quote) if abs(float(m.replace("·", ".")) - abs(value)) < 1e-9}
        if len(printed) == 1:
            plain = printed.pop().replace("·", ".")
            return Decimal(("-" if value < 0 else "") + plain), _places(plain), "LOCATOR_QUOTE"
    stored = repr(float(value))
    if "e" in stored or "E" in stored:
        return Decimal(str(value)), None, "STORED_NUMBER"
    return Decimal(stored), _places(stored), "STORED_NUMBER"


def _places(text: str) -> int:
    return len(text.split(".")[1]) if "." in text else 0


def agree_at_precision(a: Decimal, pa: int | None, b: Decimal, pb: int | None) -> bool:
    """True when two printed numbers can be the same value: the more precise one rounds to the less precise one.

    Printed with the same number of decimals, two numbers agree only when they are equal. A tie (−12.35 against
    −12.4 or −12.3) is accepted either way, since sources round ties differently.
    """
    if pa is None or pb is None:
        return a == b
    if pa == pb:
        return a == b
    fine, coarse, places = (a, b, pb) if pa > pb else (b, a, pa)
    return abs(fine - coarse) <= Decimal(5) * Decimal(10) ** (-(places + 1))


def _half_unit(places: int | None) -> Decimal:
    return Decimal(5) * Decimal(10) ** (-((places or 0) + 1))


def _fmt_num(d: Decimal | None) -> str:
    if d is None:
        return "not reported"
    return format(d, "f").replace("-", "−")


# --------------------------------------------------------------------------- reading the stored rows

_ANALYSIS_SETS: tuple[tuple[str, str], ...] = (
    ("SUBSET", r"participants with prediabetes|\bextension subset\b|\bsub-?study\b|\bsub-?group\b|\bsubset\b"),
    ("mITT", r"modified intent(?:ion)?[- ]to[- ]treat|\bmitt\b"),
    ("ITT", r"intent(?:ion)?[- ]to[- ]treat|\bitt\b"),
    ("FAS", r"full analysis set|\bfas\b"),
    ("EAS", r"efficacy analysis set|\beas\b"),
    ("PP", r"per[- ]protocol"),
    ("SAFETY", r"safety (?:population|analysis set)"),
    ("COMPLETERS", r"\bcompleters?\b"),
)
ANALYSIS_SET_LABEL = {
    "SUBSET": "a named subset of participants", "mITT": "modified intention-to-treat population", "ITT": "intention-to-treat population",
    "FAS": "full analysis set", "EAS": "efficacy analysis set", "PP": "per-protocol set", "SAFETY": "safety population", "COMPLETERS": "completers",
}
_FIELD_LABEL = {
    "analysis_population": "analysis population (as stored)", "estimand.population_text": "estimand population", "estimand.quote": "estimand quote",
    "notes": "curator's note on the row", "analysis_method": "analysis method (as stored)",
}


def analysis_set(fields: Sequence[tuple[str, str | None]]) -> tuple[str | None, str | None, str | None]:
    """(class, field it was read from, the matching passage) from the first stored text that names an analysis set."""
    for name, text in fields:
        if not text:
            continue
        for code, pattern in _ANALYSIS_SETS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                start, end = max(0, match.start() - 50), min(len(text), match.end() + 50)
                return code, name, ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")
    return None, None, None


_MISSING_DATA: tuple[tuple[str, str], ...] = (
    ("modified multiple imputation", r"modified multiple imputation"),
    ("retrieved-dropout multiple imputation", r"retrieved[- ]drop[- ]?outs?"),
    ("jump-to-reference imputation", r"jump[- ]to[- ]reference"),
    ("multiple imputation", r"multiple imputation|imputed data"),
    ("mixed model for repeated measures", r"\bmmrm\b|mixed[- ]models? (?:for )?repeated measures"),
    ("last observation carried forward", r"\blocf\b|last observation carried forward"),
)


def missing_data_handling(fields: Sequence[tuple[str, str | None]]) -> tuple[str | None, str | None, str | None]:
    """(method, field, passage) of the first stored text that names how missing data were handled."""
    for name, text in fields:
        if not text:
            continue
        for method, pattern in _MISSING_DATA:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                start, end = max(0, match.start() - 40), min(len(text), match.end() + 40)
                return method, name, ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")
    return None, None, None


_EFFECT_MEASURES: tuple[tuple[str, str], ...] = (
    ("hazard ratio", r"hazard"),
    ("odds ratio", r"\bodds\b"),
    ("ratio of geometric means", r"geometric"),
    ("risk ratio", r"relative risk|risk ratio|rate ratio"),
    ("risk difference", r"risk difference|difference in (?:proportion|percentage)"),
    ("mean difference", r"mean difference|ls ?mean|least[- ]squares|treatment difference|\betd\b|mean change difference"),
)


def effect_measure(param_type: str | None) -> str | None:
    text = (param_type or "").lower()
    return next((name for name, pattern in _EFFECT_MEASURES if re.search(pattern, text)), None)


def _period_weeks(text: str | None) -> float | None:
    """The end of an adverse-event observation period in weeks, as stated ('Baseline up to Week 74' -> 74; '72 weeks' -> 72)."""
    if not text:
        return None
    found = re.findall(r"(\d+(?:\.\d+)?)\s*weeks?\b", text, re.IGNORECASE) + re.findall(r"\bweek\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    values = sorted({float(f) for f in found})
    return values[-1] if values else None


def _reported_formulation(row_origin: str, arm_label: str, arm: StudyArm | None) -> tuple[str | None, str]:
    """(formulation key, basis) the row's own source names for its arm.

    A curated source names the formulation in its arm description (the Foundayo label's "17.2 mg ... label dosage");
    the rule that reads it is the curation loader's, so the two never diverge. Otherwise the arm's stored formulation.
    """
    if row_origin == "CURATED" and arm_label:
        # One rule for reading a formulation from a source's arm label: the curation loader's (imported here, not at
        # module level, because the loader depends on the ingestion layer).
        from ..curation.loader import _curated_arm_content

        key = _curated_arm_content(arm_label).get("formulation_key")
        if key:
            return key, "named in the source's arm description"
    if arm is not None and arm.formulation_key:
        return arm.formulation_key, "formulation stored on the arm"
    return None, "not stated"


def _formulation_names(session: Session) -> dict[str, str]:
    return {f.key: f.name for f in session.scalars(select(Formulation))}


def _arm_natural(session: Session, arm: StudyArm) -> tuple[str, StudyArm, str, bool]:
    """(natural identity, arm that identity stands for, explanation, resolved) of an arm row: never a row id."""
    identity = resolve_arm(session, arm)
    target = arm
    if identity.registered_arm_id is not None and identity.registered_arm_id != arm.id:
        target = next((p for p in arm.study.arms if p.id == identity.registered_arm_id), arm)
    prefix = "arm" if identity.resolved else ("pooled" if identity.kind == "POOLED" else "label")
    return f"{prefix}:{target.norm_label}#{identity.stratum}", target, identity.basis, identity.resolved


def _legacy_arm_key(session: Session, arm: StudyArm) -> str | None:
    """The arm part of the gap keys used before this engine ('placebo', 'orforglipron|36'), so reviewer triage stays attached."""
    if is_placebo_arm(arm):
        return "placebo"
    if arm.dose_amount is None or not arm.drug_slug:
        return None
    dose = arm.dose_amount
    for eq_from, eq_amount, _eq_to, eq_to_amount in session_memo(session, "documented_equivalents_raw", _equivalences_a_side):
        if eq_from == arm.formulation_key and eq_amount == arm.dose_amount:
            dose = eq_to_amount
    return f"{arm.drug_slug}|{dose:g}"


def _equivalences_a_side(session: Session) -> tuple[tuple[str, float, str, float], ...]:
    """(formulation b, amount b, formulation a, amount a) of every documented equivalence: b-side doses named by their a-side dose."""
    from ..models import Dose, ExposureEquivalence

    doses = (joinedload(ExposureEquivalence.dose_a).joinedload(Dose.formulation), joinedload(ExposureEquivalence.dose_b).joinedload(Dose.formulation))
    return tuple(
        (row.dose_b.formulation.key, row.dose_b.amount, row.dose_a.formulation.key, row.dose_a.amount)
        for row in session.scalars(select(ExposureEquivalence).options(*doses).order_by(ExposureEquivalence.id))
        if row.basis != "NOT_DOCUMENTED"
    )


def _unit_key(category: str, unit: str | None) -> str | None:
    family = unit_family(unit)
    # One quantity, two spellings of its unit: '%' under a responder endpoint is a share of participants, and under an
    # HbA1c endpoint it is HbA1c percentage points. Nothing else is folded together.
    if family == "PERCENT" and category in ("WEIGHT_RESPONDER", "HBA1C_RESPONDER"):
        return "PERCENT_OF_PARTICIPANTS"
    if family == "PERCENT" and category in ("HBA1C_CHANGE",):
        return "HBA1C_PERCENT"
    return family


def _endpoint_key(definition: EndpointDefinition, category: str, unit: str | None) -> str:
    return f"{category}|{','.join(definition.measure)}|{','.join(definition.threshold)}|{_unit_key(category, unit) or 'unit not stated'}"


def _weeks_text(weeks: float | None) -> str:
    return "time point not stated" if weeks is None else f"week {weeks:g}"


@dataclass
class Row:
    """One stored row as the conflict engine reads it: the fields that define the quantity and the value as printed."""

    evidence_type: str
    id: int
    evidence_key: str | None
    study_id: int
    nct_id: str
    origin: str
    source_id: int | None
    source: dict[str, Any] | None
    tier: str
    verification_status: str
    arm_label: str
    arm_group: str
    arm_group_basis: str
    arm_resolved: bool
    arm_natural: str
    legacy_arm_key: str | None
    formulation: str | None
    formulation_basis: str
    dose_reported: str | None
    endpoint_title: str
    category: str
    class_title: str | None
    definition: EndpointDefinition | None
    unit: str | None
    value: float | None
    number: Decimal | None
    places: int | None
    precision_basis: str
    ci_low: float | None = None
    ci_high: float | None = None
    ci_level: float | None = None
    n: int | None = None
    n_label: str = "n analysed"
    estimand_label: str | None = None
    estimand_basis: str | None = None
    estimand_quote: str | None = None
    analysis_set: str | None = None
    analysis_set_field: str | None = None
    analysis_set_text: str | None = None
    analysis_population: str | None = None
    missing_data: str | None = None
    missing_data_field: str | None = None
    missing_data_text: str | None = None
    timepoint_weeks: float | None = None
    timeframe_text: str | None = None
    baseline_definition: str | None = None
    baseline_class: str | None = None
    locator_quote: str | None = None
    source_location: str | None = None
    notes: str | None = None
    # adverse events
    term: str | None = None
    measure_kind: str | None = None
    is_serious: bool = False
    n_affected: int | None = None
    n_at_risk: int | None = None
    percent: float | None = None
    period: str | None = None
    period_weeks: float | None = None
    serious_scope: str | None = None
    # analyses
    param_type: str | None = None
    effect_measure: str | None = None
    effect_scale: str | None = None
    direction_basis: str | None = None
    p_value_text: str | None = None
    comparator_label: str | None = None
    natural: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.evidence_type}:{self.id}"

    @property
    def who(self) -> str:
        title = (self.source or {}).get("title") or (self.source or {}).get("key") or f"source #{self.source_id}"
        tier = TIER_LABEL.get(EvidenceTier(self.tier), self.tier) if self.tier in EvidenceTier._value2member_map_ else self.tier
        return f"{tier} ({title[:90]}{'…' if len(title) > 90 else ''})"

    @property
    def short_who(self) -> str:
        return TIER_LABEL.get(EvidenceTier(self.tier), self.tier).split(" (")[0] if self.tier in EvidenceTier._value2member_map_ else self.tier

    @property
    def printed(self) -> str:
        if self.evidence_type == "adverse_event" and self.number is None and self.n_affected is not None:
            return f"{self.n_affected} of {self.n_at_risk}" if self.n_at_risk else f"{self.n_affected}"
        return _fmt_num(self.number)

    def as_dict(self, formulation_names: dict[str, str]) -> dict[str, Any]:
        tier_label = TIER_LABEL.get(EvidenceTier(self.tier), self.tier) if self.tier in EvidenceTier._value2member_map_ else self.tier
        calc = None
        if self.evidence_type == "adverse_event" and self.n_affected is not None and self.n_at_risk:
            calc = float(round(Fraction(100 * self.n_affected, self.n_at_risk), 2))
        return {
            "evidence_type": self.evidence_type, "id": self.id, "evidence_key": self.evidence_key,
            "review_href": f"/review?type={self.evidence_type}&key={self.evidence_key}" if self.evidence_key else None,
            "origin": self.origin, "source": self.source, "tier": self.tier, "tier_label": tier_label,
            "verification_status": self.verification_status, "status_label": STATUS_LABEL.get(self.verification_status, self.verification_status),
            "arm_label": self.arm_label, "arm_group": self.arm_group, "arm_group_basis": self.arm_group_basis, "arm_group_resolved": self.arm_resolved,
            "formulation": self.formulation, "formulation_name": formulation_names.get(self.formulation or "", self.formulation),
            "formulation_basis": self.formulation_basis, "dose_reported": self.dose_reported,
            "endpoint_title": self.endpoint_title, "category": self.category, "class_title": self.class_title,
            "value": self.value, "value_printed": self.printed, "decimals": self.places, "precision_basis": self.precision_basis,
            "unit": self.unit, "ci_low": self.ci_low, "ci_high": self.ci_high, "ci_level": self.ci_level, "n": self.n, "n_label": self.n_label,
            "estimand_label": self.estimand_label, "estimand_basis": self.estimand_basis, "estimand_quote": self.estimand_quote,
            "analysis_set": self.analysis_set, "analysis_set_label": ANALYSIS_SET_LABEL.get(self.analysis_set or ""), "analysis_set_field": self.analysis_set_field,
            "analysis_set_text": self.analysis_set_text, "analysis_population": self.analysis_population,
            "missing_data_handling": self.missing_data, "missing_data_field": self.missing_data_field, "missing_data_text": self.missing_data_text,
            "timepoint_weeks": self.timepoint_weeks, "timeframe_text": self.timeframe_text,
            "baseline_definition": self.baseline_definition, "baseline_class": self.baseline_class,
            "baseline_label": BASELINE_CLASS_LABEL.get(self.baseline_class or "") if self.baseline_class else None,
            "locator_quote": self.locator_quote, "source_location": self.source_location, "notes": self.notes,
            "term": self.term, "measure_kind": self.measure_kind, "is_serious": self.is_serious, "n_affected": self.n_affected, "n_at_risk": self.n_at_risk,
            "percent_reported": self.percent, "percent_calculated": calc, "period": self.period,
            "param_type": self.param_type, "effect_measure": self.effect_measure, "direction_basis": self.direction_basis, "p_value_text": self.p_value_text,
            "comparator_label": self.comparator_label,
        }


def _source_fields(row: Any) -> dict[str, Any]:
    return {"source_id": row.source_id, "source": source_ref(row.source), "tier": row.tier, "verification_status": row.verification_status}


def _result_row(session: Session, study: Study, r: OutcomeResult) -> Row | None:
    if r.value is None or r.scope != "ARM" or r.arm is None:
        return None
    natural, target, basis, resolved = _arm_natural(session, r.arm)
    definition = endpoint_definition(r.endpoint.category, r.endpoint.title, r.class_title, r.endpoint.threshold, r.unit)
    number, places, precision = reported_number(r.value, r.value_text, r.locator_quote)
    estimand = r.estimand
    aset, aset_field, aset_text = analysis_set([
        ("analysis_population", r.analysis_population), ("estimand.population_text", estimand.population_text if estimand else None),
        ("estimand.quote", estimand.quote if estimand else None), ("notes", r.notes)])
    md, md_field, md_text = missing_data_handling([("analysis_method", r.analysis_method), ("estimand.quote", estimand.quote if estimand else None), ("notes", r.notes)])
    formulation, f_basis = _reported_formulation(r.origin, r.arm_label, r.arm)
    dose = parse_single_dose(r.arm_label) if r.origin == "CURATED" else None
    row = Row(
        evidence_type="result", id=r.id, evidence_key=r.evidence_key, study_id=study.id, nct_id=study.nct_id, origin=r.origin, **_source_fields(r),
        arm_label=r.arm_label, arm_group=target.label, arm_group_basis=basis, arm_resolved=resolved, arm_natural=natural, legacy_arm_key=_legacy_arm_key(session, target),
        formulation=formulation, formulation_basis=f_basis, dose_reported=f"{dose[0]:g} {dose[1]}" if dose else None,
        endpoint_title=r.endpoint.title, category=r.endpoint.category, class_title=r.class_title, definition=definition, unit=r.unit,
        value=r.value, number=number, places=places, precision_basis=precision, ci_low=r.ci_low, ci_high=r.ci_high, ci_level=r.ci_level, n=r.n_analyzed,
        estimand_label=estimand.label if estimand else EstimandLabel.NOT_STATED.value, estimand_basis=estimand.basis if estimand else None,
        estimand_quote=estimand.quote if estimand else None, analysis_set=aset, analysis_set_field=aset_field, analysis_set_text=aset_text,
        analysis_population=r.analysis_population, missing_data=md, missing_data_field=md_field, missing_data_text=md_text,
        timepoint_weeks=r.timepoint_weeks, timeframe_text=r.timeframe_text,
        baseline_definition=r.baseline_definition, baseline_class=baseline_class(study.design_features, r.baseline_definition),
        locator_quote=r.locator_quote, source_location=r.source_location, notes=r.notes,
    )
    weeks = "" if r.timepoint_weeks is None else f"{r.timepoint_weeks:g}"
    row.natural = f"result|{study.nct_id}|{natural}|{_endpoint_key(definition, r.endpoint.category, r.unit)}|{weeks}"
    return row


def _analysis_row(session: Session, study: Study, a: StatisticalAnalysis) -> Row | None:
    if a.estimate is None or a.treatment_arm is None or a.comparator_arm is None:
        return None
    t_natural, t_target, t_basis, t_resolved = _arm_natural(session, a.treatment_arm)
    c_natural, c_target, c_basis, c_resolved = _arm_natural(session, a.comparator_arm)
    definition = endpoint_definition(a.endpoint.category, a.endpoint.title, a.class_title, a.endpoint.threshold, a.unit)
    number, places, precision = reported_number(a.estimate, a.estimate_text, a.locator_quote)
    estimand = a.estimand
    aset, aset_field, aset_text = analysis_set([
        ("estimand.population_text", estimand.population_text if estimand else None), ("estimand.quote", estimand.quote if estimand else None), ("notes", a.notes)])
    md, md_field, md_text = missing_data_handling([("analysis_method", a.method), ("estimand.quote", estimand.quote if estimand else None), ("notes", a.notes)])
    formulation, f_basis = _reported_formulation(a.origin, a.treatment_label or "", a.treatment_arm)
    measure = effect_measure(a.param_type)
    row = Row(
        evidence_type="analysis", id=a.id, evidence_key=a.evidence_key, study_id=study.id, nct_id=study.nct_id, origin=a.origin, **_source_fields(a),
        arm_label=f"{a.treatment_label} versus {a.comparator_label}", arm_group=f"{t_target.label} versus {c_target.label}",
        arm_group_basis=" ".join(dict.fromkeys(b for b in (t_basis, c_basis))), arm_resolved=t_resolved and c_resolved,
        arm_natural=f"{t_natural}>{c_natural}", legacy_arm_key=None, formulation=formulation, formulation_basis=f_basis, dose_reported=None,
        endpoint_title=a.endpoint.title, category=a.endpoint.category, class_title=a.class_title, definition=definition, unit=a.unit,
        value=a.estimate, number=number, places=places, precision_basis=precision, ci_low=a.ci_low, ci_high=a.ci_high, ci_level=a.ci_level, n=None,
        estimand_label=estimand.label if estimand else EstimandLabel.NOT_STATED.value, estimand_basis=estimand.basis if estimand else None,
        estimand_quote=estimand.quote if estimand else None, analysis_set=aset, analysis_set_field=aset_field, analysis_set_text=aset_text,
        missing_data=md, missing_data_field=md_field, missing_data_text=md_text,
        timepoint_weeks=a.timepoint_weeks, baseline_class=baseline_class(study.design_features, None),
        locator_quote=a.locator_quote, source_location=a.source_location, notes=a.notes,
        param_type=a.param_type, effect_measure=measure, effect_scale=effect_scale(a.param_type, a.unit)["scale"], direction_basis=a.direction_basis,
        p_value_text=a.p_value_text, comparator_label=a.comparator_label,
    )
    weeks = "" if a.timepoint_weeks is None else f"{a.timepoint_weeks:g}"
    row.natural = f"analysis|{study.nct_id}|{row.arm_natural}|{_endpoint_key(definition, a.endpoint.category, a.unit)}|{weeks}"
    return row


def _ae_row(session: Session, study: Study, ae: AdverseEvent, serious_on_record: dict[tuple[str, str], int | None]) -> Row | None:
    if ae.scope != "ARM" or ae.arm is None or (ae.percent is None and ae.n_affected is None):
        return None
    natural, target, basis, resolved = _arm_natural(session, ae.arm)
    if ae.n_affected is not None:
        number, places, precision = None, 0, "COUNT"
    else:
        number, places, precision = reported_number(ae.percent, None, ae.locator_quote)
    formulation, f_basis = _reported_formulation(ae.origin, ae.arm_label, ae.arm)
    if ae.is_serious:
        scope = "SERIOUS"
    elif ae.origin == "REGISTRY":
        scope = "REGISTRY_NON_SERIOUS_TABLE"
    else:
        scope = "ALL_EVENTS"
    row = Row(
        evidence_type="adverse_event", id=ae.id, evidence_key=ae.evidence_key, study_id=study.id, nct_id=study.nct_id, origin=ae.origin, **_source_fields(ae),
        arm_label=ae.arm_label, arm_group=target.label, arm_group_basis=basis, arm_resolved=resolved, arm_natural=natural, legacy_arm_key=None,
        formulation=formulation, formulation_basis=f_basis, dose_reported=None, endpoint_title=ae.term, category=ae.category, class_title=None,
        definition=None, unit="% of participants" if ae.n_affected is None else "participants", value=ae.percent if ae.n_affected is None else float(ae.n_affected),
        number=number, places=places, precision_basis=precision, n=ae.n_at_risk, n_label="n at risk",
        locator_quote=ae.locator_quote, source_location=ae.source_location, notes=ae.notes,
        term=ae.term, measure_kind=ae.measure_kind, is_serious=bool(ae.is_serious), n_affected=ae.n_affected, n_at_risk=ae.n_at_risk, percent=ae.percent,
        period=ae.period, period_weeks=_period_weeks(ae.period), serious_scope=scope,
    )
    row.extra["serious_on_record"] = serious_on_record.get((natural, ae.term.lower()), "absent") if scope == "REGISTRY_NON_SERIOUS_TABLE" else None
    row.natural = f"adverse_event|{study.nct_id}|{natural}|{ae.term.strip().lower()}|{ae.measure_kind}|{'serious' if ae.is_serious else 'any'}"
    return row


# --------------------------------------------------------------------------- comparing two rows


def _dim(dimension: str, label: str, status: str, a: str | None, b: str | None, *, explains: bool = False, decisive: bool = False, note: str = "",
         a_field: str | None = None, b_field: str | None = None) -> dict[str, Any]:
    return {"dimension": dimension, "label": label, "status": status, "a": a, "b": b, "a_field": a_field, "b_field": b_field,
            "explains": explains and status == DIFFERENT, "decisive": decisive, "note": note}


_ESTIMAND_TEXT = {"EFFICACY": "efficacy estimand", "TREATMENT_REGIMEN": "treatment-regimen estimand", "OTHER": "another named estimand"}
_BASIS_TEXT = {
    "STATED_BY_SOURCE": "named by the source", "INFERRED_FROM_ANALYSIS_DESCRIPTION": "inferred by rule from the registry's analysis description",
    "INFERRED_BY_CURATOR": "the curator's attribution (no checked quote names it)", "NOT_DETERMINABLE": "not determinable",
}


def _estimand_text(row: Row) -> str:
    if row.estimand_label in (None, EstimandLabel.NOT_STATED.value):
        return "estimand not named"
    return f"{_ESTIMAND_TEXT.get(row.estimand_label, row.estimand_label)} ({_BASIS_TEXT.get(row.estimand_basis or '', row.estimand_basis or 'basis not recorded')})"


def _compare_estimand(a: Row, b: Row) -> dict[str, Any]:
    known_a = a.estimand_label not in (None, EstimandLabel.NOT_STATED.value)
    known_b = b.estimand_label not in (None, EstimandLabel.NOT_STATED.value)
    if known_a and known_b:
        if a.estimand_label == b.estimand_label:
            return _dim("estimand", "Estimand", SAME, _estimand_text(a), _estimand_text(b), decisive=True, a_field="estimand.label", b_field="estimand.label")
        return _dim("estimand", "Estimand", DIFFERENT, _estimand_text(a), _estimand_text(b), explains=True, decisive=True, a_field="estimand.label", b_field="estimand.label",
                    note="An efficacy estimand (effect had participants stayed on treatment) and a treatment-regimen estimand (effect regardless of discontinuation) "
                         "are different quantities; the efficacy value is usually the larger effect.")
    return _dim("estimand", "Estimand", UNKNOWN, _estimand_text(a), _estimand_text(b), decisive=True, a_field="estimand.label", b_field="estimand.label",
                note="Without the estimand of both values it cannot be told whether they estimate the same quantity.")


_SPECIFIC_BASELINES = {"STUDY_BASELINE", "RANDOMIZATION_AFTER_LEAD_IN", "STUDY_ENTRY_INCLUDING_LEAD_IN", "SUBSET_BASELINE"}


def _compare_baseline(a: Row, b: Row) -> dict[str, Any]:
    ta = f"{BASELINE_CLASS_LABEL.get(a.baseline_class or '', a.baseline_class)}" + (f" — stored as '{a.baseline_definition}'" if a.baseline_definition else "")
    tb = f"{BASELINE_CLASS_LABEL.get(b.baseline_class or '', b.baseline_class)}" + (f" — stored as '{b.baseline_definition}'" if b.baseline_definition else "")
    if a.baseline_class in _SPECIFIC_BASELINES and b.baseline_class in _SPECIFIC_BASELINES:
        if a.baseline_class == b.baseline_class:
            return _dim("baseline", "Baseline", SAME, ta, tb, decisive=True, a_field="baseline_definition", b_field="baseline_definition")
        return _dim("baseline", "Baseline", DIFFERENT, ta, tb, explains=True, decisive=True, a_field="baseline_definition", b_field="baseline_definition",
                    note="A change measured from randomization after a lead-in and a change measured from study entry cover different periods of treatment.")
    return _dim("baseline", "Baseline", UNKNOWN, ta, tb, decisive=True, a_field="baseline_definition", b_field="baseline_definition",
                note="The trial has a lead-in before randomization and at least one record does not say whether its change is measured from study entry or from randomization.")


def _set_text(row: Row) -> str:
    parts = []
    if row.analysis_set:
        parts.append(f"{ANALYSIS_SET_LABEL[row.analysis_set]} (from the {_FIELD_LABEL.get(row.analysis_set_field or '', row.analysis_set_field)}: “{row.analysis_set_text}”)")
    else:
        parts.append("analysis set not named in the stored record")
    parts.append(f"{row.n_label} {row.n}" if row.n is not None else f"{row.n_label} not reported")
    return "; ".join(parts)


def _compare_analysis_set(a: Row, b: Row) -> dict[str, Any]:
    ta, tb = _set_text(a), _set_text(b)
    fields = {"a_field": a.analysis_set_field or "analysis_population", "b_field": b.analysis_set_field or "analysis_population"}
    label = "Analysis set" if a.evidence_type != "adverse_event" else "Participants at risk"
    if a.analysis_set and b.analysis_set and a.analysis_set != b.analysis_set:
        return _dim("analysis_set", label, DIFFERENT, ta, tb, explains=True, **fields, note="The values come from different sets of participants.")
    n_differ = a.n is not None and b.n is not None and a.n != b.n
    if n_differ:
        return _dim("analysis_set", label, UNKNOWN, ta, tb, decisive=True, **fields,
                    note=f"The numbers of participants differ ({a.n} and {b.n}), so the values may come from different analysis sets or data cuts; "
                         "the stored records do not say which set each value uses.")
    if a.analysis_set and b.analysis_set:
        return _dim("analysis_set", label, SAME, ta, tb, **fields)
    return _dim("analysis_set", label, UNCONFIRMED, ta, tb, **fields,
                note="Not named by both sources" + (f"; the same number of participants ({a.n}) is stored for both." if a.n is not None and a.n == b.n else "."))


def _md_text(row: Row) -> str:
    if not row.missing_data:
        return "not stated in the stored record"
    return f"{row.missing_data} (from the {_FIELD_LABEL.get(row.missing_data_field or '', row.missing_data_field)}: “{row.missing_data_text}”)"


def _compare_missing_data(a: Row, b: Row) -> dict[str, Any]:
    ta, tb = _md_text(a), _md_text(b)
    fields = {"a_field": a.missing_data_field or "analysis_method", "b_field": b.missing_data_field or "analysis_method"}
    if a.missing_data and b.missing_data:
        if a.missing_data == b.missing_data:
            return _dim("missing_data", "Missing-data handling", SAME, ta, tb, **fields)
        return _dim("missing_data", "Missing-data handling", DIFFERENT, ta, tb, explains=True, **fields,
                    note="Different handling of missing data (different estimators) gives different estimates, even of the same estimand.")
    return _dim("missing_data", "Missing-data handling", UNCONFIRMED, ta, tb, **fields, note="Not stated by both sources.")


def _compare_definition(a: Row, b: Row) -> dict[str, Any]:
    da, db = a.definition, b.definition
    ta = f"'{a.endpoint_title}'" + (f" — class '{a.class_title}'" if a.class_title else "")
    tb = f"'{b.endpoint_title}'" + (f" — class '{b.class_title}'" if b.class_title else "")
    if da is None or db is None:
        return _dim("endpoint_definition", "Endpoint definition", SAME, ta, tb)
    if da.threshold_ambiguous or db.threshold_ambiguous:
        return _dim("endpoint_definition", "Endpoint definition", UNKNOWN, ta, tb, decisive=True, a_field="endpoint.title", b_field="endpoint.title",
                    note="An endpoint title lists several responder thresholds and no class says which one the value belongs to.")
    if (da.subset or None) != (db.subset or None):
        return _dim("endpoint_definition", "Endpoint definition", DIFFERENT, ta, tb, explains=True, a_field="endpoint.title", b_field="endpoint.title",
                    note=f"One endpoint is measured in a named subset ({da.subset or db.subset}).")
    if da.method and db.method and da.method != db.method:
        return _dim("endpoint_definition", "Endpoint definition", DIFFERENT, ta, tb, explains=True, a_field="endpoint.title", b_field="endpoint.title",
                    note=f"Different measurement methods ({', '.join(da.method)} versus {', '.join(db.method)}).")
    return _dim("endpoint_definition", "Endpoint definition", SAME, ta, tb, note="Same category, measured concept, responder threshold and unit.")


def _compare_formulation(session: Session, a: Row, b: Row, names: dict[str, str]) -> dict[str, Any] | None:
    if not a.formulation or not b.formulation:
        return None
    ta = f"{names.get(a.formulation, a.formulation)}" + (f", {a.dose_reported}" if a.dose_reported else "") + f" ({a.formulation_basis})"
    tb = f"{names.get(b.formulation, b.formulation)}" + (f", {b.dose_reported}" if b.dose_reported else "") + f" ({b.formulation_basis})"
    if a.formulation == b.formulation:
        return _dim("formulation", "Formulation named", SAME, ta, tb)
    documented = None
    if a.dose_reported or b.dose_reported:
        named, other = (a, b) if a.dose_reported else (b, a)
        dose = parse_single_dose(named.arm_label)
        if dose:
            documented = _documented_equivalent(session, named.formulation or "", dose[0], other.formulation or "")
    note = (f"Both rows are stored on the randomized group '{a.arm_group}'. "
            + (f"The doses named are a documented exposure-equivalent pair (the equivalent dose is {documented:g} mg). " if documented is not None else "")
            + "The participants and the treatment they received are the same, so naming the dose in another formulation cannot by itself make the "
              "reported values differ; this is recorded, not used as an explanation.")
    return _dim("formulation", "Formulation named", NOTED, ta, tb, note=note)


def _compare_follow_up(a: Row, b: Row) -> dict[str, Any]:
    ta, tb = a.period or "observation period not stated", b.period or "observation period not stated"
    if a.period_weeks is not None and b.period_weeks is not None:
        if a.period_weeks == b.period_weeks:
            return _dim("follow_up", "Observation period", SAME, ta, tb, decisive=True, a_field="period", b_field="period")
        return _dim("follow_up", "Observation period", DIFFERENT, ta, tb, explains=True, decisive=True, a_field="period", b_field="period",
                    note=f"Events counted over {a.period_weeks:g} and {b.period_weeks:g} weeks: a longer window can only add participants with an event.")
    return _dim("follow_up", "Observation period", UNKNOWN, ta, tb, decisive=True, a_field="period", b_field="period",
                note="At least one record does not state over which period its events were counted.")


_SCOPE_TEXT = {"SERIOUS": "serious events only", "REGISTRY_NON_SERIOUS_TABLE": "the registry's table of non-serious events",
               "ALL_EVENTS": "all events of the term (seriousness not restricted)"}


def _compare_scope(a: Row, b: Row) -> dict[str, Any]:
    ta, tb = _SCOPE_TEXT.get(a.serious_scope or "", ""), _SCOPE_TEXT.get(b.serious_scope or "", "")
    if a.serious_scope == b.serious_scope:
        return _dim("endpoint_definition", "Events counted", SAME, ta, tb)
    registry = a if a.serious_scope == "REGISTRY_NON_SERIOUS_TABLE" else b
    serious = registry.extra.get("serious_on_record")
    if serious == 0:
        return _dim("endpoint_definition", "Events counted", SAME, ta, tb,
                    note="The registry's serious-event table reports no participant with a serious event of this term in this group, so both count the same participants.")
    if isinstance(serious, int) and serious > 0:
        return _dim("endpoint_definition", "Events counted", DIFFERENT, ta, tb, explains=True,
                    note=f"The registry's serious-event table reports {serious} participant(s) with a serious event of this term in this group; they are "
                         "outside the registry's non-serious count and inside a count of all events.")
    return _dim("endpoint_definition", "Events counted", UNCONFIRMED, ta, tb,
                note="No serious-event row for this term and group is on record, so whether the two counts cover the same participants is not confirmed.")


def _compare_contrast(a: Row, b: Row) -> list[dict[str, Any]]:
    dims = []
    ta, tb = f"{a.param_type or 'parameter not stated'}", f"{b.param_type or 'parameter not stated'}"
    if a.effect_scale == "NOT_ESTABLISHED" or b.effect_scale == "NOT_ESTABLISHED":
        dims.append(_dim("effect_measure", "Effect measure", UNKNOWN, ta, tb, decisive=True, a_field="param_type", b_field="param_type",
                         note="At least one record does not establish whether its estimate is a difference or a ratio."))
    elif a.effect_scale != b.effect_scale or (a.effect_measure and b.effect_measure and a.effect_measure != b.effect_measure):
        dims.append(_dim("effect_measure", "Effect measure", DIFFERENT, ta, tb, explains=True, decisive=True, a_field="param_type", b_field="param_type",
                         note="Different effect measures are different quantities."))
    else:
        dims.append(_dim("effect_measure", "Effect measure", SAME, ta, tb, decisive=True, a_field="param_type", b_field="param_type"))
    ta, tb = f"{a.arm_label} (direction: {a.direction_basis or 'not recorded'})", f"{b.arm_label} (direction: {b.direction_basis or 'not recorded'})"
    if "NOT_STATED" in (a.direction_basis, b.direction_basis):
        dims.append(_dim("orientation", "Orientation", UNKNOWN, ta, tb, decisive=True, a_field="direction_basis", b_field="direction_basis",
                         note="One source does not state which group is the reference, so the two estimates may be the same contrast taken in opposite directions."))
    else:
        dims.append(_dim("orientation", "Orientation", SAME, ta, tb, decisive=True, a_field="direction_basis", b_field="direction_basis"))
    return dims


@dataclass
class ValueCheck:
    agree: bool
    detail: str
    difference: str | None
    one_unit_apart: bool = False


def _compare_values(a: Row, b: Row) -> ValueCheck:
    if a.evidence_type == "adverse_event" and (a.n_affected is not None or b.n_affected is not None):
        return _compare_ae_values(a, b)
    assert a.number is not None and b.number is not None
    ok = agree_at_precision(a.number, a.places, b.number, b.places)
    diff = abs(a.number - b.number)
    pa, pb = a.places, b.places
    if ok:
        if a.number == b.number:
            detail = f"Both print {_fmt_num(a.number)}."
        else:
            fine, coarse = (a, b) if (pa or 0) > (pb or 0) else (b, a)
            detail = f"{_fmt_num(fine.number)} ({fine.short_who}) rounds to {_fmt_num(coarse.number)} ({coarse.short_who}) at the {coarse.places} decimal place(s) printed."
        return ValueCheck(True, detail, _fmt_num(diff))
    one_unit = pa == pb and pa is not None and diff == Decimal(10) ** (-pa)
    if pa == pb:
        detail = f"{_fmt_num(a.number)} and {_fmt_num(b.number)} are both printed to {pa} decimal place(s) and are not the same number (difference {_fmt_num(diff)})."
    else:
        fine, coarse = (a, b) if (pa or 0) > (pb or 0) else (b, a)
        detail = (f"{_fmt_num(fine.number)} does not round to {_fmt_num(coarse.number)} at the {coarse.places} decimal place(s) printed "
                  f"(difference {_fmt_num(diff)}; rounding allows up to {_fmt_num(_half_unit(coarse.places))}).")
    basis = {r.precision_basis for r in (a, b)}
    if "STORED_NUMBER" in basis:
        detail += " Precision of at least one value was read from the stored number, because the record keeps no printed text of it."
    return ValueCheck(False, detail, _fmt_num(diff), one_unit)


def _compare_ae_values(a: Row, b: Row) -> ValueCheck:
    if a.n_affected is not None and b.n_affected is not None:
        if a.n_affected == b.n_affected:
            return ValueCheck(True, f"Both report {a.n_affected} participants with the event.", "0")
        return ValueCheck(False, f"{a.n_affected} and {b.n_affected} participants with the event are reported (counts are exact).", str(abs(a.n_affected - b.n_affected)))
    counted, percented = (a, b) if a.n_affected is not None else (b, a)
    if not counted.n_at_risk:
        return ValueCheck(False, "One source gives a count without its denominator and the other a percentage, so the two cannot be compared.", None)
    if percented.number is None:
        return ValueCheck(False, "Neither a count nor a percentage is comparable.", None)
    calculated = Fraction(100 * counted.n_affected, counted.n_at_risk)  # type: ignore[operator]
    reported = Fraction(str(percented.number))
    half = Fraction(str(_half_unit(percented.places)))
    shown = float(round(calculated, (percented.places or 0) + 2))
    if abs(calculated - reported) <= half:
        return ValueCheck(True, f"{counted.n_affected} of {counted.n_at_risk} is {shown:g}% (calculated here from the reported counts), which rounds to the "
                                f"{_fmt_num(percented.number)}% printed by the {percented.short_who}.", None)
    return ValueCheck(False, f"{counted.n_affected} of {counted.n_at_risk} is {shown:g}% (calculated here from the reported counts); the {percented.short_who} prints "
                             f"{_fmt_num(percented.number)}%, which that calculation does not round to.", f"{abs(float(calculated - reported)):.2f}")


def compare(session: Session, a: Row, b: Row, names: dict[str, str]) -> dict[str, Any]:
    """Classify two rows of different sources that purport to report one quantity."""
    dims: list[dict[str, Any]] = []
    if a.evidence_type in ("result", "analysis"):
        dims += [_compare_estimand(a, b), _compare_baseline(a, b), _compare_analysis_set(a, b), _compare_missing_data(a, b), _compare_definition(a, b)]
        if a.evidence_type == "analysis":
            dims += _compare_contrast(a, b)
        dims.append(_dim("time_point", "Time point", SAME, _weeks_text(a.timepoint_weeks), _weeks_text(b.timepoint_weeks),
                         note="" if a.timepoint_weeks is not None else "Neither source states a time point (for example an event-driven analysis over the whole follow-up)."))
    else:
        dims += [_compare_follow_up(a, b), _compare_analysis_set(a, b), _compare_scope(a, b)]
    formulation = _compare_formulation(session, a, b, names)
    if formulation is not None:
        dims.append(formulation)
    values = _compare_values(a, b)
    explaining = [d for d in dims if d["explains"]]
    unknown = [d for d in dims if d["status"] == UNKNOWN]
    if values.agree:
        cls = CONSISTENT
    elif explaining:
        cls = EXPLAINABLE
    elif unknown:
        cls = INSUFFICIENT
    else:
        cls = TRUE_CONFLICT
    return {
        "a": a.ref, "b": b.ref, "a_evidence_key": a.evidence_key, "b_evidence_key": b.evidence_key, "class": cls, "class_label": CLASS_LABEL[cls],
        "values": {"a": a.printed, "b": b.printed, "agree": values.agree, "difference": values.difference, "detail": values.detail},
        "dimensions": dims, "explanation": _explain(cls, a, b, values, explaining, unknown),
        "possible_reasons": _reasons(cls, a, b, values, dims), "missing_details": _missing(a, b, dims), "next_actions": _actions(cls, a, b, values, dims),
    }


def _explain(cls: str, a: Row, b: Row, values: ValueCheck, explaining: list[dict[str, Any]], unknown: list[dict[str, Any]]) -> str:
    pair = f"{a.short_who} {a.printed} and {b.short_who} {b.printed}"
    if cls == CONSISTENT:
        return f"{pair} agree at the reported precision. {values.detail}"
    if cls == EXPLAINABLE:
        named = "; ".join(f"{d['label'].lower()}: {d['a']} versus {d['b']}" for d in explaining)
        return (f"{pair} differ, and the stored records state why: {named}. These are different quantities, not a contradiction; "
                "neither value is an error because of this difference.")
    if cls == INSUFFICIENT:
        named = "; ".join(f"{d['label'].lower()} ({d['a']} versus {d['b']})" for d in unknown)
        return (f"{pair} differ beyond reporting precision, but the stored records cannot show whether they are the same quantity: {named}. "
                "Until that is known the difference is neither an explained difference nor a conflict.")
    return (f"{pair} disagree. Every stored detail that defines the quantity is the same — study, randomized group, endpoint, time point, estimand "
            f"({_ESTIMAND_TEXT.get(a.estimand_label or '', 'not applicable') if a.evidence_type != 'adverse_event' else 'not applicable'}), baseline and, where "
            f"stated, the analysis set — and the values differ beyond the precision printed. {values.detail}")


def _reasons(cls: str, a: Row, b: Row, values: ValueCheck, dims: list[dict[str, Any]]) -> list[str]:
    if cls == CONSISTENT:
        return []
    if cls == EXPLAINABLE:
        return [d["note"] for d in dims if d["explains"] and d["note"]]
    reasons: list[str] = []
    if cls == INSUFFICIENT:
        for d in dims:
            if d["status"] == UNKNOWN and d["dimension"] == "estimand":
                reasons.append("The values may be under different estimands (an efficacy and a treatment-regimen estimand give different values from the same trial).")
            elif d["status"] == UNKNOWN and d["dimension"] == "analysis_set":
                reasons.append("The values may come from different analysis sets or data cuts.")
            elif d["status"] == UNKNOWN and d["dimension"] == "baseline":
                reasons.append("The values may be measured from different baselines (study entry or randomization).")
            elif d["status"] == UNKNOWN and d["dimension"] == "follow_up":
                reasons.append("The events may be counted over different observation periods.")
            elif d["status"] == UNKNOWN and d["dimension"] == "orientation":
                reasons.append("The estimates may be the same contrast taken in opposite directions.")
            elif d["status"] == UNKNOWN:
                reasons.append(d["note"])
    if values.one_unit_apart:
        reasons.append("The values differ by one unit in the last printed digit: different rounding or truncation of the same estimate (or rounding of an already "
                       "rounded figure) would produce this, and the stored records cannot rule it out.")
    reasons += ["A different data cut, database lock or handling of missing data (for example another imputation) behind one of the values.",
                "A transcription or extraction error in one stored record: each value should be checked against its locator quote and source table."]
    for d in dims:
        if d["dimension"] == "missing_data" and d["status"] == UNCONFIRMED and (a.missing_data or b.missing_data):
            stated = a if a.missing_data else b
            reasons.append(f"{stated.short_who} states its handling of missing data ({stated.missing_data}); the other source does not, so a different imputation cannot be ruled out.")
    if any(d["dimension"] == "analysis_set" and d["status"] == UNCONFIRMED for d in dims):
        reasons.append("Different analysis populations although neither record names one.")
    return list(dict.fromkeys(reasons))


def _missing(a: Row, b: Row, dims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in dims:
        if d["status"] not in (UNKNOWN, UNCONFIRMED):
            continue
        for row, text in ((a, d["a"]), (b, d["b"])):
            if d["dimension"] == "estimand" and row.estimand_label in (None, EstimandLabel.NOT_STATED.value):
                out.append({"evidence_key": row.evidence_key, "source": row.who, "field": "estimand", "detail": "The estimand of this value is not named in the stored record."})
            elif d["dimension"] == "baseline" and row.baseline_class not in _SPECIFIC_BASELINES:
                out.append({"evidence_key": row.evidence_key, "source": row.who, "field": "baseline_definition", "detail": f"Baseline not specified: {text}."})
            elif d["dimension"] == "missing_data" and not row.missing_data and (a.missing_data or b.missing_data):
                out.append({"evidence_key": row.evidence_key, "source": row.who, "field": "analysis_method", "detail": "How missing data were handled is not stated in the stored record."})
            elif d["dimension"] == "analysis_set" and not row.analysis_set:
                out.append({"evidence_key": row.evidence_key, "source": row.who, "field": "analysis_population", "detail": "The analysis set of this value is not named in the stored record."})
            elif d["dimension"] == "follow_up" and row.period_weeks is None:
                out.append({"evidence_key": row.evidence_key, "source": row.who, "field": "period", "detail": "The observation period is not stated."})
            elif d["dimension"] == "orientation" and row.direction_basis == "NOT_STATED":
                out.append({"evidence_key": row.evidence_key, "source": row.who, "field": "direction_basis", "detail": "The reference group of the contrast is not stated."})
            elif d["dimension"] == "endpoint_definition" and d["status"] == UNKNOWN and row.definition is not None and row.definition.threshold_ambiguous:
                out.append({"evidence_key": row.evidence_key, "source": row.who, "field": "class_title", "detail": "Which responder threshold this value belongs to is not stored."})
            elif d["dimension"] == "effect_measure" and row.effect_scale == "NOT_ESTABLISHED":
                out.append({"evidence_key": row.evidence_key, "source": row.who, "field": "param_type", "detail": "Whether the estimate is a difference or a ratio is not stated."})
            elif d["dimension"] == "endpoint_definition" and d["status"] == UNCONFIRMED and row.serious_scope == "REGISTRY_NON_SERIOUS_TABLE":
                out.append({"evidence_key": row.evidence_key, "source": row.who, "field": "is_serious", "detail": "No serious-event row for this term and group is on record."})
    for row in (a, b):
        if row.estimand_basis == "INFERRED_BY_CURATOR" and any(d["dimension"] == "estimand" and d["status"] == SAME for d in dims):
            out.append({"evidence_key": row.evidence_key, "source": row.who, "field": "estimand.basis",
                        "detail": "The estimand is the curator's attribution: no checked quote from this source names it."})
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for m in out:
        unique.setdefault((m["evidence_key"], m["field"]), m)
    return list(unique.values())


def _source_noun(row: Row) -> str:
    return {"REGULATORY_LABEL": "the label", "REGULATORY_REVIEW": "the FDA review", "PEER_REVIEWED": "the publication", "REGISTRY_RESULTS": "the registry record",
            "PRESS_RELEASE": "the press release"}.get(row.tier, "the source")


def _actions(cls: str, a: Row, b: Row, values: ValueCheck, dims: list[dict[str, Any]]) -> list[str]:
    if cls == CONSISTENT:
        return []
    actions: list[str] = []
    for d in dims:
        if d["status"] != UNKNOWN:
            continue
        for row in (a, b):
            noun = _source_noun(row)
            if d["dimension"] == "estimand" and row.estimand_label in (None, EstimandLabel.NOT_STATED.value):
                actions.append(f"Confirm the estimand of {noun} value ({row.printed}) in the source: the methods or table footnotes say how data after treatment discontinuation were handled.")
            elif d["dimension"] == "baseline" and row.baseline_class not in _SPECIFIC_BASELINES:
                actions.append(f"Confirm whether {noun} value ({row.printed}) is measured from study entry or from randomization.")
            elif d["dimension"] == "analysis_set" and not row.analysis_set:
                actions.append(f"Check {noun}'s analysis population and number analysed for this value ({row.printed}).")
            elif d["dimension"] == "follow_up" and row.period_weeks is None:
                actions.append(f"Confirm the period over which {noun} counted these events.")
            elif d["dimension"] == "orientation" and row.direction_basis == "NOT_STATED":
                actions.append(f"Confirm which group is the reference in {noun}'s contrast.")
            elif d["dimension"] in ("effect_measure", "endpoint_definition"):
                actions.append(f"Confirm the exact endpoint definition and effect measure of {noun} value ({row.printed}).")
    if cls == TRUE_CONFLICT:
        for d in dims:
            if d["dimension"] == "missing_data" and d["status"] == UNCONFIRMED and (a.missing_data or b.missing_data):
                silent = b if a.missing_data else a
                actions.append(f"Check how {_source_noun(silent)} handled missing data for this value ({silent.printed}).")
            if d["dimension"] == "analysis_set" and d["status"] == UNCONFIRMED:
                for row in (a, b):
                    if not row.analysis_set:
                        actions.append(f"Check {_source_noun(row)}'s analysis population for this value ({row.printed}).")
        actions.append(f"Check both values against their sources (locator quote and source table): {a.short_who} {a.printed} and {b.short_who} {b.printed}.")
        for row in (a, b):
            if row.tier == "REGULATORY_LABEL":
                actions.append("Check the label's analysis population, imputation method and data cut in the label table and its footnotes.")
        actions.append("If both are transcribed correctly, record the discrepancy in each row's review and cite both values with their sources; do not choose one by the kind of source.")
    if cls == EXPLAINABLE:
        actions.append("No correction is needed. Cite each value with the detail that distinguishes it (" + ", ".join(d["label"].lower() for d in dims if d["explains"]) + "), "
                       "and compare only values that share it.")
    return list(dict.fromkeys(actions))


# --------------------------------------------------------------------------- groups


@dataclass
class ConflictGroup:
    group_id: str
    natural_key: str
    evidence_type: str
    study: dict[str, Any]
    drugs: list[str]
    category: str
    rows: list[Row]
    comparisons: list[dict[str, Any]]
    names: dict[str, str]

    @property
    def cls(self) -> str:
        present = {c["class"] for c in self.comparisons}
        return next(c for c in CLASSES if c in present)

    @property
    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(CLASSES, 0)
        for c in self.comparisons:
            counts[c["class"]] += 1
        return counts

    @property
    def sources(self) -> int:
        return len({r.source_id for r in self.rows})

    def title(self) -> dict[str, Any]:
        first = self.rows[0]
        if self.evidence_type == "adverse_event":
            what = f"{first.term} ({'serious events' if first.is_serious else (first.measure_kind or '').replace('_', ' ').lower()})"
            when = None
        else:
            what = CATEGORY_LABEL.get(self.category, first.endpoint_title)
            if first.definition and first.definition.threshold:
                what += f" ({', '.join(first.definition.threshold)})"
            when = _weeks_text(first.timepoint_weeks)
        return {"quantity": what, "arm": first.arm_group, "time_point": when, "endpoint_titles": sorted({r.endpoint_title for r in self.rows})}

    def summary(self) -> dict[str, Any]:
        t = self.title()
        values = [{"evidence_key": r.evidence_key, "value_printed": r.printed, "unit": r.unit, "tier": r.tier,
                   "estimand_label": r.estimand_label, "verification_status": r.verification_status} for r in self.rows]
        return {
            "group_id": self.group_id, "evidence_type": self.evidence_type, "evidence_type_label": EVIDENCE_TYPE_LABEL[self.evidence_type],
            "class": self.cls, "class_label": CLASS_LABEL[self.cls], "pair_counts": self.counts,
            "study": self.study, "drugs": self.drugs, "category": self.category, "category_label": CATEGORY_LABEL.get(self.category, self.category.replace("_", " ").lower()),
            "quantity": t["quantity"], "arm": t["arm"], "time_point": t["time_point"], "n_rows": len(self.rows), "n_sources": self.sources,
            "values": values, "explanation": self.explanation(),
        }

    def explanation(self) -> str:
        counts = self.counts
        parts = [f"{self.sources} sources report this quantity ({len(self.rows)} stored rows; {len(self.comparisons)} comparisons between sources)."]
        if counts[TRUE_CONFLICT]:
            parts.append(f"{counts[TRUE_CONFLICT]} {'pair disagrees' if counts[TRUE_CONFLICT] == 1 else 'pairs disagree'} although every stored detail that defines the quantity is the same.")
        if counts[INSUFFICIENT]:
            parts.append(f"{counts[INSUFFICIENT]} {'pair differs' if counts[INSUFFICIENT] == 1 else 'pairs differ'} and cannot be reconciled with the stored details.")
        if counts[EXPLAINABLE]:
            dims = sorted({d["label"].lower() for c in self.comparisons if c["class"] == EXPLAINABLE for d in c["dimensions"] if d["explains"]})
            parts.append(f"{counts[EXPLAINABLE]} {'pair differs' if counts[EXPLAINABLE] == 1 else 'pairs differ'} for a stated reason ({', '.join(dims)}); that is not a contradiction.")
        if counts[CONSISTENT]:
            parts.append(f"{counts[CONSISTENT]} {'pair agrees' if counts[CONSISTENT] == 1 else 'pairs agree'} at the reported precision.")
        return " ".join(parts)

    def hierarchy(self) -> list[dict[str, Any]]:
        tiers: dict[str, list[Row]] = defaultdict(list)
        for r in self.rows:
            tiers[r.tier].append(r)
        return [{"tier": tier, "tier_label": TIER_LABEL.get(EvidenceTier(tier), tier) if tier in EvidenceTier._value2member_map_ else tier,
                 "evidence_keys": [r.evidence_key for r in rows], "values": [r.printed for r in rows]}
                for tier, rows in sorted(tiers.items(), key=lambda kv: _TIER_RANK.get(kv[0], 99))]

    def detail(self) -> dict[str, Any]:
        # What needs doing comes from the pairs that need it: true conflicts and unreconciled pairs first; only when there
        # are none, the explained differences (so "no correction is needed" is never mixed with "check both values").
        open_classes = {TRUE_CONFLICT, INSUFFICIENT} & {c["class"] for c in self.comparisons}
        relevant = open_classes or ({EXPLAINABLE} & {c["class"] for c in self.comparisons})

        def union(name: str) -> list[Any]:
            seen: dict[str, Any] = {}
            for c in self.comparisons:
                if c["class"] in relevant:
                    for item in c[name]:
                        seen.setdefault(repr(item), item)
            return list(seen.values())

        return {
            **self.summary(), "natural_key": self.natural_key, "method": METHOD, "class_definitions": CLASS_DEFINITION,
            "rows": [r.as_dict(self.names) for r in self.rows], "comparisons": self.comparisons,
            "possible_reasons": union("possible_reasons"), "missing_details": union("missing_details"), "next_actions": union("next_actions"),
            "source_hierarchy": {"note": TIER_CONTEXT_NOTE, "tiers": self.hierarchy()},
            "endpoint_titles": self.title()["endpoint_titles"],
        }


def group_id_for(nct_id: str, natural_key: str) -> str:
    """Stable identifier of a group: the study and a hash of the natural key (never row ids)."""
    return f"{nct_id}-{hashlib.sha256(natural_key.encode('utf-8')).hexdigest()[:16]}"


GROUP_ID_PATTERN = r"^NCT\d{8}-[0-9a-f]{16}$"


def _study_drugs(study: Study) -> list[str]:
    return sorted({link.drug.slug for link in study.drug_links if link.role != "COMPARATOR"})


def _serious_on_record(session: Session, study: Study, terms: set[str]) -> dict[tuple[str, str], int | None]:
    """(arm identity, term) -> participants with a serious event of the term, from the registry's serious-event table."""
    if not terms:
        return {}
    rows = session.scalars(
        select(AdverseEvent).options(joinedload(AdverseEvent.arm)).where(AdverseEvent.study_id == study.id, AdverseEvent.is_serious.is_(True),
                                                                          AdverseEvent.origin == "REGISTRY", func.lower(AdverseEvent.term).in_(sorted(terms)))
    ).all()
    out: dict[tuple[str, str], int | None] = {}
    for ae in rows:
        if ae.arm is not None and ae.scope == "ARM":
            out[(_arm_natural(session, ae.arm)[0], ae.term.lower())] = ae.n_affected
    return out


def study_groups(session: Session, study: Study, evidence_types: Iterable[str] = EVIDENCE_TYPES) -> list[ConflictGroup]:
    """Every multi-source group of one study, classified."""
    wanted = set(evidence_types)
    rows: list[Row] = []
    if "result" in wanted:
        results = session.scalars(select(OutcomeResult).options(joinedload(OutcomeResult.arm), joinedload(OutcomeResult.endpoint), joinedload(OutcomeResult.estimand),
                                                                joinedload(OutcomeResult.source)).where(OutcomeResult.study_id == study.id).order_by(OutcomeResult.id)).unique().all()
        rows += [r for r in (_result_row(session, study, x) for x in results) if r is not None]
    if "analysis" in wanted:
        analyses = session.scalars(select(StatisticalAnalysis).options(joinedload(StatisticalAnalysis.treatment_arm), joinedload(StatisticalAnalysis.comparator_arm),
                                                                       joinedload(StatisticalAnalysis.endpoint), joinedload(StatisticalAnalysis.estimand),
                                                                       joinedload(StatisticalAnalysis.source)).where(StatisticalAnalysis.study_id == study.id)
                                   .order_by(StatisticalAnalysis.id)).unique().all()
        rows += [r for r in (_analysis_row(session, study, x) for x in analyses) if r is not None]
    if "adverse_event" in wanted:
        # Only terms reported by at least two sources can form a group: the registry's thousands of single-source rows are never loaded.
        multi = session.execute(
            select(func.lower(AdverseEvent.term), AdverseEvent.measure_kind).where(AdverseEvent.study_id == study.id)
            .group_by(func.lower(AdverseEvent.term), AdverseEvent.measure_kind).having(func.count(func.distinct(AdverseEvent.source_id)) > 1)
        ).all()
        if multi:
            terms = {t for t, _ in multi}
            kinds = {(t, k) for t, k in multi}
            events = session.scalars(select(AdverseEvent).options(joinedload(AdverseEvent.arm), joinedload(AdverseEvent.source))
                                     .where(AdverseEvent.study_id == study.id, func.lower(AdverseEvent.term).in_(sorted(terms))).order_by(AdverseEvent.id)).unique().all()
            serious = _serious_on_record(session, study, terms)
            rows += [r for r in (_ae_row(session, study, x, serious) for x in events if (x.term.lower(), x.measure_kind) in kinds) if r is not None]
    by_key: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        if row.source_id is not None:
            by_key[row.natural].append(row)
    names = session_memo(session, "formulation_names", _formulation_names)
    study_block = {"id": study.id, "nct_id": study.nct_id, "display_name": study.display_name, "program": study.program}
    drugs = _study_drugs(study)
    groups = []
    for natural, members in by_key.items():
        if len({r.source_id for r in members}) < 2:
            continue
        ordered = _order(members)
        comparisons = [compare(session, a, b, names) for a, b in combinations(ordered, 2) if a.source_id != b.source_id]
        if not comparisons:
            continue
        groups.append(ConflictGroup(group_id=group_id_for(study.nct_id, natural), natural_key=natural, evidence_type=ordered[0].evidence_type, study=study_block,
                                    drugs=drugs, category=ordered[0].category, rows=ordered,
                                    comparisons=comparisons, names=names))
    return sorted(groups, key=lambda g: (EVIDENCE_TYPES.index(g.evidence_type), g.category, g.title()["arm"], g.rows[0].timepoint_weeks or 0, g.group_id))


def _order(rows: list[Row]) -> list[Row]:
    """Rows in the shared display order (completeness of reporting, then storage order); tier is never read."""
    kind = "adverse_event" if rows[0].evidence_type == "adverse_event" else "result"
    payload = [{"id": r.id, "verification_status": r.verification_status, "ci_low": r.ci_low, "ci_high": r.ci_high, "dispersion_value": None,
                "n_analyzed": r.n, "n_affected": r.n_affected, "n_at_risk": r.n_at_risk, "percent": r.percent, "row": r} for r in rows]
    return [p["row"] for p in display_order(payload, kind)]


def all_groups(session: Session, study_ids: Iterable[int] | None = None) -> list[ConflictGroup]:
    """Every multi-source group in the database (or in the given studies). Rows rejected or held in review are never read."""
    query = select(Study).order_by(Study.nct_id)
    if study_ids is not None:
        query = query.where(Study.id.in_(list(study_ids)))
    groups: list[ConflictGroup] = []
    for study in session.scalars(query):
        groups += study_groups(session, study)
    return groups


def find_group(session: Session, group_id: str) -> ConflictGroup | None:
    nct = group_id.split("-", 1)[0]
    study = session.scalar(select(Study).where(Study.nct_id == nct))
    if study is None:
        return None
    return next((g for g in study_groups(session, study) if g.group_id == group_id), None)


def conflict_for_row(session: Session, evidence_type: str, row: Any) -> dict[str, Any] | None:
    """The conflict classification of the group one evidence row belongs to, from that row's point of view (review workbench)."""
    if evidence_type not in EVIDENCE_TYPES or getattr(row, "study_id", None) is None:
        return None
    if not is_usable(row.verification_status, getattr(row, "review_hold", False)):
        return {"group_id": None, "class": None, "class_label": None, "href": None, "explanation": "This row is excluded from analytic use after a review decision, so it takes part in no "
                "source-conflict classification.", "comparisons": []}
    study = session.get(Study, row.study_id)
    if study is None:
        return None
    ref = f"{evidence_type}:{row.id}"
    for group in study_groups(session, study, (evidence_type,)):
        if any(r.ref == ref for r in group.rows):
            mine = []
            for c in group.comparisons:
                if ref not in (c["a"], c["b"]):
                    continue
                other_ref, other_key = (c["b"], c["b_evidence_key"]) if c["a"] == ref else (c["a"], c["a_evidence_key"])
                other = next(r for r in group.rows if r.ref == other_ref)
                mine.append({"evidence_type": other.evidence_type, "id": other.id, "evidence_key": other_key, "tier": other.tier, "value_printed": other.printed,
                             "class": c["class"], "class_label": c["class_label"], "explanation": c["explanation"],
                             "explains": [d["label"] for d in c["dimensions"] if d["explains"]], "missing": [m["detail"] for m in c["missing_details"]]})
            own = [c["class"] for c in mine]
            own_cls = next((k for k in CLASSES if k in own), None)
            return {"group_id": group.group_id, "class": own_cls, "class_label": CLASS_LABEL.get(own_cls or ""), "group_class": group.cls,
                    "group_class_label": CLASS_LABEL[group.cls], "href": f"/conflicts?group={group.group_id}", "explanation": group.explanation(), "comparisons": mine}
    return None


# --------------------------------------------------------------------------- what the gap tracker needs


#: The categories the gap rule covered before this engine, whose true-conflict gaps keep their earlier keys.
LEGACY_KEY_CATEGORIES = frozenset({"WEIGHT_PCT_CHANGE", "HBA1C_CHANGE"})


@dataclass
class ConflictIssue:
    """One open question for the gap tracker: a true conflict (per estimand) or an unreconciled difference in a group."""

    key_suffix: str
    cls: str
    group: ConflictGroup
    comparisons: list[dict[str, Any]]


def gap_issues(group: ConflictGroup) -> list[ConflictIssue]:
    """True conflicts and insufficient-information pairs become gaps; explainable differences and agreements never do.

    The key of a true conflict among arm-level results keeps the form used before this engine
    (``category:arm:estimand:weeks``), so a reviewer's triage of such a gap stays attached to it.
    """
    issues: list[ConflictIssue] = []
    rows = {r.ref: r for r in group.rows}
    by_estimand: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in group.comparisons:
        if c["class"] == TRUE_CONFLICT:
            by_estimand[rows[c["a"]].estimand_label or "NA"].append(c)
    for estimand, comparisons in sorted(by_estimand.items()):
        first = rows[comparisons[0]["a"]]
        if group.evidence_type == "result" and first.legacy_arm_key and group.category in LEGACY_KEY_CATEGORIES:
            suffix = f"{group.category}:{first.legacy_arm_key}:{estimand}:{first.timepoint_weeks}"
        else:
            suffix = f"{group.group_id.split('-', 1)[1]}:{estimand}"
        issues.append(ConflictIssue(suffix, TRUE_CONFLICT, group, comparisons))
    unreconciled = [c for c in group.comparisons if c["class"] == INSUFFICIENT]
    if unreconciled:
        issues.append(ConflictIssue(f"{group.group_id.split('-', 1)[1]}:unreconciled", INSUFFICIENT, group, unreconciled))
    return issues


def issue_rows(issue: ConflictIssue) -> list[Row]:
    refs = {ref for c in issue.comparisons for ref in (c["a"], c["b"])}
    return [r for r in issue.group.rows if r.ref in refs]


def group_counts(groups: Sequence[ConflictGroup]) -> dict[str, int]:
    counts = dict.fromkeys(CLASSES, 0)
    for g in groups:
        counts[g.cls] += 1
    return counts


__all__ = [
    "CLASSES", "CLASS_DEFINITION", "CLASS_LABEL", "CONSISTENT", "EVIDENCE_TYPES", "EVIDENCE_TYPE_LABEL", "EXPLAINABLE", "GROUP_ID_PATTERN", "INSUFFICIENT",
    "METHOD", "TRUE_CONFLICT", "ConflictGroup", "agree_at_precision", "all_groups", "conflict_for_row", "find_group", "gap_issues", "group_counts", "reported_number",
    "study_groups",
]
