"""Clinical comparability engine.

Decides whether two reported findings can be compared, and says why. The output is a
classification plus a factor-by-factor account — never a bare score — and an explicit
list of statements the evidence does and does not permit.

Classification logic (deterministic):

* DIRECT RANDOMIZED COMPARISON requires two DIFFERENT arms that were randomized against each other in
  the SAME randomization (same trial, same sub-study and period), reported IN ONE DOCUMENT on the same
  endpoint definition (measurement, responder criterion, unit), the same baseline, the same estimand and
  the same time point. The reported between-arm analysis is quoted only for a direct comparison, and only
  when it is the analysis of exactly that endpoint, estimand, time point and arm pair in that same document;
  any other stored analysis of the two arms is listed under ``other_analyses`` with the reason.
* An A − B difference is formed only between values of one endpoint definition, unit, baseline, assessment
  time (at most one week apart) and analysed population; otherwise the reason is given and no difference.
* Two records of ONE arm (a registry row beside a curated row, or two analyses of one group) are not a
  treatment comparison at all; the engine says so and computes no difference.
* Any *blocking* mismatch (endpoint definition, unit, baseline, estimand, diabetes status)
  -> NOT SUITABLE FOR EFFICACY COMPARISON.
* An essential descriptor is unknown (the estimand of an efficacy endpoint, diabetes status, what the
  endpoint measures, whether two labels are one arm, the value itself) -> INSUFFICIENT EVIDENCE.
* Different randomizations, no blocking mismatch: PARTIALLY COMPARABLE only when the endpoint,
  baseline, estimand, follow-up, diabetes status and indication match AND the population could actually
  be assessed (age, sex and body size); otherwise DESCRIPTIVE CROSS-STUDY COMPARISON. An unknown factor
  is never read as "similar".

Only a DIRECT classification can carry a between-arm analysis; no classification licenses a
superiority or significance claim that the cited analysis does not itself support.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..enums import (
    TIER_LABEL,
    ComparabilityClass,
    EndpointCategory,
    EquivalenceBasis,
    EstimandLabel,
    EvidenceTier,
    FactorAssessment,
    TriState,
)
from ..models import (
    Dose,
    Drug,
    ExposureEquivalence,
    Formulation,
    StatisticalAnalysis,
    Study,
    StudyArm,
    StudyDrug,
)
from ..review.policy import is_usable
from ..vocab import is_placebo_arm
from .findings import (
    BASELINE_CLASS_LABEL,
    EFFICACY_CATEGORIES,
    Finding,
    _fmt,
    analysis_dto,
    arm_relation,
    baseline_class,
    display_order,
    endpoint_definition,
    resolve_arm,
)

A = FactorAssessment


@dataclass
class Factor:
    key: str
    label: str
    a: str
    b: str
    assessment: str
    explanation: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class Comparison:
    classification: str
    headline: str
    same_study: bool
    factors: list[Factor]
    reasons: list[str]
    permitted_statements: list[str]
    prohibited_statements: list[str]
    requirements: list[str]
    #: Analyses of exactly this endpoint, estimand, time point and arm pair. Empty when none is stored.
    reported_analyses: list[dict[str, Any]] = field(default_factory=list)
    descriptive_difference: dict[str, Any] | None = None
    head_to_head_trials: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    #: How the two groups relate: RANDOMIZED_ARMS_OF_ONE_TRIAL, SAME_ARM_TWO_REPORTS,
    #: SAME_REGISTRATION_NOT_RANDOMIZED_AGAINST_EACH_OTHER, SAME_TRIAL_ARM_IDENTITY_UNRESOLVED or DIFFERENT_TRIALS.
    relationship: str = ""
    #: Analyses stored for the same two arms that concern a different endpoint, estimand or time point, are reported by another document, or belong to a pair that is not a direct comparison; never quoted.
    other_analyses: list[dict[str, Any]] = field(default_factory=list)
    endpoint_definitions: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["factors"] = [f.as_dict() for f in self.factors]
        return data


def _show(value: Any, unknown: str = "not reported") -> str:
    if value is None or value == "":
        return unknown
    return str(value)


def _summary_value(f: Finding, key: str) -> float | None:
    entry = f.population_summary.get(key)
    value = entry.get("value") if isinstance(entry, dict) else None
    return float(value) if isinstance(value, int | float) else None


def _numeric_factor(key: str, label: str, a: float | None, b: float | None, unit: str, minor: float, major: float, why: str) -> Factor:
    if a is None or b is None:
        return Factor(key, label, _show(None if a is None else f"{_fmt(a, 1)} {unit}"), _show(None if b is None else f"{_fmt(b, 1)} {unit}"), A.UNKNOWN.value,
                      f"Not available for both studies in the stored sources, so imbalance cannot be assessed. {why}")
    diff = abs(a - b)
    if diff >= major:
        level, text = A.MAJOR_DIFFERENCE, f"Differs by {_fmt(diff, 1)} {unit}, a clinically relevant imbalance. {why}"
    elif diff >= minor:
        level, text = A.MINOR_DIFFERENCE, f"Differs by {_fmt(diff, 1)} {unit}. {why}"
    else:
        level, text = A.MATCH, f"Similar (difference {_fmt(diff, 1)} {unit})."
    return Factor(key, label, f"{_fmt(a, 1)} {unit}", f"{_fmt(b, 1)} {unit}", level.value, text)


_SPECIAL_POPULATIONS = {
    "obstructive sleep apnea": r"sleep apnea",
    "knee osteoarthritis": r"osteoarthritis",
    "cardiovascular disease": r"cardiovascular disease|atherosclerotic",
    "heart failure": r"heart failure",
    "chronic kidney disease": r"kidney|renal",
    "MASLD/MASH": r"steato|masld|mash|nash",
}


def _special_population(f: Finding) -> set[str]:
    text = " ".join(f.conditions).lower()
    return {name for name, pattern in _SPECIAL_POPULATIONS.items() if re.search(pattern, text)}


def _control_type(f: Finding) -> str | None:
    return (f.design_features or {}).get("comparator_type")


def _is_open_label(f: Finding) -> bool | None:
    if f.masking is None:
        return None
    return f.masking.upper() in ("NONE", "OPEN_LABEL")


def _method_family(text: str | None) -> str | None:
    if not text:
        return None
    low = text.lower()
    parts = []
    if "mmrm" in low or "mixed model" in low or "mixed-model" in low:
        parts.append("MMRM")
    if "ancova" in low:
        parts.append("ANCOVA")
    if "multiple imputation" in low:
        parts.append("multiple imputation")
    if "logistic" in low:
        parts.append("logistic regression")
    if "cox" in low:
        parts.append("Cox model")
    return " + ".join(parts) or None


def documented_equivalence(session: Session, formulation_a: str | None, dose_a: float | None, formulation_b: str | None, dose_b: float | None) -> ExposureEquivalence | None:
    """Return the documented exposure equivalence between two formulation doses, if one is on record."""
    if not formulation_a or not formulation_b or dose_a is None or dose_b is None:
        return None
    rows = session.scalars(select(ExposureEquivalence)).all()
    for row in rows:
        pair = {(row.dose_a.formulation.key, row.dose_a.amount), (row.dose_b.formulation.key, row.dose_b.amount)}
        if pair == {(formulation_a, dose_a), (formulation_b, dose_b)} and row.basis != EquivalenceBasis.NOT_DOCUMENTED.value:
            return row
    return None


def harmonize_dose(session: Session, formulation_key: str, amount: float, target_formulation_key: str) -> dict[str, Any]:
    """Map a dose to another formulation ONLY through a documented equivalence.

    There is intentionally no arithmetic fallback: when no source documents the pair, the
    answer is "not documented", never a scaled milligram value.
    """
    source_dose = session.scalar(select(Dose).join(Dose.formulation).where(Dose.amount == amount, Dose.formulation.has(key=formulation_key)))
    if source_dose is None:
        return {"status": "NOT_DOCUMENTED", "equivalent": None,
                "explanation": f"No {amount:g} mg dose of formulation '{formulation_key}' is on record; no conversion is offered."}
    for row in session.scalars(select(ExposureEquivalence)).all():
        ends = [(row.dose_a, row.dose_b), (row.dose_b, row.dose_a)]
        for src, dst in ends:
            if src.id == source_dose.id and dst.formulation.key == target_formulation_key and row.basis != EquivalenceBasis.NOT_DOCUMENTED.value:
                return {
                    "status": "DOCUMENTED",
                    "basis": row.basis,
                    "equivalent": {"formulation": dst.formulation.key, "formulation_name": dst.formulation.name, "amount": dst.amount, "unit": dst.unit},
                    "statement": row.statement,
                    "quote": row.quote,
                    "verification_status": row.verification_status,
                    "source_id": row.source_id,
                    "source_key": row.source.key if row.source else None,
                    "explanation": "Documented dose pair. This is not a conversion factor and must not be applied to other doses.",
                }
    return {
        "status": "NOT_DOCUMENTED",
        "equivalent": None,
        "explanation": (
            f"No source on record documents an exposure-equivalent dose of '{target_formulation_key}' for {amount:g} mg of "
            f"'{formulation_key}'. A milligram ratio from another dose pair must not be used."
        ),
    }


def find_head_to_head_trials(session: Session, drug_a: str | None, drug_b: str | None) -> list[dict[str, Any]]:
    """Registered trials that include both drugs (investigational or comparator role)."""
    if not drug_a or not drug_b or drug_a == drug_b:
        return []
    out = []
    for study in session.scalars(select(Study)).all():
        slugs = {link.drug.slug for link in study.drug_links}
        if drug_a in slugs and drug_b in slugs:
            out.append(
                {
                    "nct_id": study.nct_id,
                    "display_name": study.display_name,
                    "phase": study.phase,
                    "overall_status": study.overall_status,
                    "has_results": study.has_results,
                    "primary_completion_date": study.primary_completion_date,
                    "primary_completion_type": study.primary_completion_type,
                }
            )
    return out


# --------------------------------------------------------------------------- reported between-arm analyses


def _analysis_baseline_class(row: StatisticalAnalysis) -> str | None:
    """Baseline class of an analysis: that of the arm values on its endpoint record, when they agree."""
    classes = {baseline_class(row.study.design_features, r.baseline_definition) for r in row.study.results if r.endpoint_id == row.endpoint_id}
    if len(classes) == 1:
        return classes.pop()
    if classes:
        return None
    return baseline_class(row.study.design_features, row.endpoint.title)


def _source_id(f: Finding) -> int | None:
    return (f.source or {}).get("id")


def _one_outcome_measure(a: Finding, b: Finding) -> bool:
    """Whether two findings are values of ONE outcome measure as its source reports it.

    That is the case when both are stored on the same endpoint record. It is also the case for two curated findings
    of one study taken from one source whose endpoint records are identical in everything that defines the
    measurement (title, category, responder criterion, class, unit and time point) and differ only in the position
    in the testing hierarchy (role, multiplicity status). Curated endpoint records are keyed on role and multiplicity
    as well, so one table of one source (for example the combination arms as the primary comparison and the
    monotherapy arms as secondary ones) is stored on two records; role and multiplicity say how a comparison was
    tested, not what was measured or from which baseline, and are assessed by their own factor.
    """
    if a.endpoint_id == b.endpoint_id:
        return True
    if a.study_id != b.study_id or a.origin != "CURATED" or b.origin != "CURATED":
        return False
    if _source_id(a) is None or _source_id(a) != _source_id(b):
        return False
    same_definition = (
        a.category == b.category
        and a.endpoint_definition.text_key == b.endpoint_definition.text_key
        and a.threshold == b.threshold
        and a.endpoint_definition.threshold == b.endpoint_definition.threshold
        and (a.unit or "").strip().lower() == (b.unit or "").strip().lower()
        and a.timepoint_weeks is not None
        and a.timepoint_weeks == b.timepoint_weeks
    )
    return same_definition


def _analysis_mismatches(row: StatisticalAnalysis, a: Finding, b: Finding) -> list[str]:
    """Why a stored analysis of these two arms is NOT the analysis of this pair of values ([] = it is)."""
    reasons: list[str] = []
    same_endpoint = row.endpoint_id == a.endpoint_id == b.endpoint_id
    # One report = the analysis and both values sit on one outcome-measure record of one source (a registry outcome
    # measure with its own analysis). A curated endpoint record is shared by several sources and estimands, so
    # sharing it proves nothing about the estimand, the baseline or the time point.
    same_report = same_endpoint and row.source_id is not None and row.source_id == _source_id(a) == _source_id(b)
    if same_endpoint:
        classes = {(r.class_title or "").strip().lower() for r in row.study.results if r.endpoint_id == row.endpoint_id}
        if (a.class_title or b.class_title) and len(classes) > 1:
            reasons.append("the outcome measure has several classes and the stored analysis does not say which one it tests")
    else:
        da, db = a.endpoint_definition, b.endpoint_definition
        dr = endpoint_definition(row.endpoint.category, row.endpoint.title, None, row.endpoint.threshold, row.endpoint.unit or row.unit)
        if not (dr.category == da.category == db.category):
            reasons.append(f"different endpoint ({row.endpoint.title})")
        elif not dr.measure or not (dr.measure == da.measure == db.measure) or not (dr.method == da.method == db.method):
            reasons.append(f"a different or unrecognised measurement ({row.endpoint.title})")
        elif not (dr.threshold == da.threshold == db.threshold) or dr.threshold_ambiguous:
            reasons.append(f"a different responder threshold ({row.endpoint.title})")
    if row.source_id is None or row.source_id != _source_id(a) or row.source_id != _source_id(b):
        # An analysis is "the analysis of this pair" only in the document that reports the two values; another document
        # may use another analysis set, data cut or handling of intercurrent events.
        reasons.append(f"reported by another source ({row.source.key if row.source else 'source not recorded'}) than the two values")
    if not same_report and not reasons:
        row_baseline = _analysis_baseline_class(row)
        if _baseline_factor(a, b).assessment != A.MATCH.value:
            # Judged exactly as the baseline factor judges it: an undescribed baseline is not assumed to be the study baseline.
            reasons.append("the two values are not established as measured from one baseline (it is not described for at least one of them), so the analysis cannot be shown to share it")
        elif row_baseline is None or not (row_baseline == a.baseline_class == b.baseline_class):
            reasons.append("a different (or undeterminable) baseline from the two values")
    row_estimand = row.estimand.label if row.estimand else EstimandLabel.NOT_STATED.value
    if not (row_estimand == a.estimand_label == b.estimand_label):
        reasons.append(f"estimand {row_estimand.replace('_', ' ').lower()} on the analysis versus {a.estimand_label.replace('_', ' ').lower()} / {b.estimand_label.replace('_', ' ').lower()} on the values")
    elif row_estimand == EstimandLabel.NOT_STATED.value and not same_report:
        reasons.append("the estimand is not stated, so the analysis cannot be shown to be the analysis of these two values")
    weeks = {a.timepoint_weeks, b.timepoint_weeks}
    if len(weeks) != 1 or None in weeks:
        reasons.append("the two values are not at one stated time point")
    elif row.timepoint_weeks is None:
        if not same_report:
            reasons.append("the analysis has no stated time point")
    elif row.timepoint_weeks != a.timepoint_weeks:
        reasons.append(f"week {_fmt(row.timepoint_weeks, 0)} on the analysis versus week {_fmt(a.timepoint_weeks, 0)} on the values")
    return reasons


def _pair_orientation(dto: dict[str, Any], treatment_key: str | None, a: Finding, b: Finding) -> dict[str, Any]:
    """Which finding is the treatment and which the comparator of a stored analysis. The estimate is never re-signed."""
    treatment_is = "A" if treatment_key == a.arm_group_key else "B"
    comparator_is = "B" if treatment_is == "A" else "A"
    established = bool(dto["direction"]["established"])
    if established:
        text = f"Reported as {dto['treatment_label']} versus {dto['comparator_label']}: finding {treatment_is} is the treatment arm and finding {comparator_is} the comparator."
        if treatment_is == "B":
            text += " The descriptive difference shown here is A − B, the opposite orientation; the stored estimate is shown as reported and is not re-signed."
    else:
        text = (f"The stored record lists {dto['treatment_label']} and {dto['comparator_label']}, but which of them is the reference is not established "
                f"({dto['direction']['basis']}) The sign of the estimate is therefore not interpreted.")
    direction = dto["direction"]
    if established:
        text += " Direction basis: " + direction["basis"]
    return {"treatment_is": treatment_is, "comparator_is": comparator_is, "direction_established": established,
            "same_orientation_as_descriptive_difference": established and treatment_is == "A", "text": text,
            # Exactly how the direction is known: stated by the source, confirmed by the arm values, both, or not established.
            "direction_status": direction["status"], "direction_basis_kind": direction.get("basis_kind"),
            "direction_stated_by": direction.get("stated_by"), "direction_confirmed_by_arm_values": bool(direction.get("confirmed_by_arm_values")),
            "direction_basis": direction["basis"]}


def _reported_analyses(session: Session, a: Finding, b: Finding) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(exact, other): analyses of exactly this pair of values, and analyses of the same two arms that are something else."""
    if a.arm_id is None or b.arm_id is None or a.arm_group_key is None or b.arm_group_key is None or a.arm_group_key == b.arm_group_key:
        return [], []
    pair = {a.arm_group_key, b.arm_group_key}
    exact: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for row in session.scalars(select(StatisticalAnalysis).where(StatisticalAnalysis.study_id == a.study_id).order_by(StatisticalAnalysis.id)):
        if row.treatment_arm is None or row.comparator_arm is None or not is_usable(row.verification_status):
            continue
        treatment_key = resolve_arm(session, row.treatment_arm).group_key
        if {treatment_key, resolve_arm(session, row.comparator_arm).group_key} != pair:
            continue
        dto = analysis_dto(row)
        mismatches = _analysis_mismatches(row, a, b)
        if mismatches:
            other.append({**dto, "mismatch": mismatches})
        else:
            exact.append({**dto, "pair_orientation": _pair_orientation(dto, treatment_key, a, b)})
    return display_order(exact), other


def _analysis_sentence(x: dict[str, Any]) -> str:
    tier = TIER_LABEL.get(EvidenceTier(x["tier"]), x["tier"]) if x["tier"] in EvidenceTier._value2member_map_ else x["tier"]
    if x["ci_low"] is None or x["ci_high"] is None:
        interval = " (CI not reported)"
    elif x["ci_level_status"] == "VALID":
        sides = {"TWO_SIDED": "two-sided ", "ONE_SIDED": "one-sided "}.get(x["ci_sides"], "")
        interval = f" ({sides}{_fmt(x['ci_level'], 2)}% CI {_fmt(x['ci_low'])} to {_fmt(x['ci_high'])})"
    else:
        interval = f" (interval {_fmt(x['ci_low'])} to {_fmt(x['ci_high'])}; its confidence level is {'not stated' if x['ci_level'] is None else 'not valid as stored'}, so nothing is inferred from it)"
    p = f", p {x['p_value_text']}" if x["p_value_text"] else ", p-value not reported in this source"
    estimate = _fmt(x["estimate"]) if x["estimate"] is not None else "estimate not reported"
    return f"{x['param_type'] or 'Estimate'} {estimate}{interval}{p} [{tier}]. {x['pair_orientation']['text']}"


# --------------------------------------------------------------------------- factors

_WORST = {A.BLOCKING.value: 0, A.UNKNOWN.value: 1, A.MAJOR_DIFFERENCE.value: 2, A.MINOR_DIFFERENCE.value: 3, A.MATCH.value: 4, A.NOT_APPLICABLE.value: 5}


def _endpoint_factors(a: Finding, b: Finding) -> list[Factor]:
    """Endpoint identity: measurement, responder criterion and unit — not the category alone."""
    da, db = a.endpoint_definition, b.endpoint_definition

    def shown(f: Finding, d: Any) -> str:
        bits = [f.endpoint_title + (f" — {f.class_title}" if f.class_title else "")]
        if d.threshold:
            bits.append("criterion " + ", ".join(d.threshold))
        return " · ".join(bits)

    ep_a, ep_b = shown(a, da), shown(b, db)
    notes: list[tuple[str, str]] = []  # (assessment, explanation)
    if a.category != b.category:
        notes.append((A.BLOCKING.value, "The findings belong to different endpoint categories."))
    elif a.endpoint_id == b.endpoint_id:
        if (a.class_title or "").strip().lower() != (b.class_title or "").strip().lower():
            notes.append((A.BLOCKING.value, "The findings are different components (classes) of one outcome measure; they do not measure the same thing."))
        else:
            notes.append((A.MATCH.value, "Both values are stored on the same outcome measure, so its definition is identical by construction."))
    else:
        if da.measure and db.measure:
            if set(da.measure) == set(db.measure):
                notes.append((A.MATCH.value, f"Same measurement ({', '.join(da.measure)})."))
            elif set(da.measure) < set(db.measure) or set(db.measure) < set(da.measure):
                notes.append((A.UNKNOWN.value, f"One title names additional components ({', '.join(sorted(set(da.measure) ^ set(db.measure)))}); whether the two definitions coincide is not established."))
            else:
                notes.append((A.BLOCKING.value, f"Different measurements within one endpoint family ({', '.join(da.measure)} versus {', '.join(db.measure)})."))
        elif da.text_key == db.text_key:
            notes.append((A.MATCH.value, "Identical endpoint titles."))
        else:
            notes.append((A.UNKNOWN.value, "The endpoint titles differ and the stored text does not establish that they measure the same thing; the category alone is not a definition."))
        if da.method and db.method and da.method != db.method:
            notes.append((A.BLOCKING.value, f"Different measurement methods ({', '.join(da.method)} versus {', '.join(db.method)})."))
        elif bool(da.method) != bool(db.method):
            notes.append((A.UNKNOWN.value, f"The measurement method ({', '.join(da.method or db.method)}) is stated for one finding only."))
        if da.threshold_ambiguous or db.threshold_ambiguous:
            notes.append((A.UNKNOWN.value, "A title lists several responder criteria without saying which one the value is for."))
        elif da.threshold != db.threshold:
            notes.append((A.BLOCKING.value, f"Different responder criteria ({', '.join(da.threshold) or 'none'} versus {', '.join(db.threshold) or 'none'})."))
        elif da.threshold:
            notes.append((A.MATCH.value, f"Same responder criterion ({', '.join(da.threshold)})."))
    level = min((n[0] for n in notes), key=lambda v: _WORST[v])
    worst_first = sorted(notes, key=lambda n: _WORST[n[0]])
    out = [Factor("endpoint", "Endpoint definition", ep_a, ep_b, level, " ".join(n[1] for n in worst_first))]

    ua, ub = da.unit_family, db.unit_family
    if ua is None or ub is None:
        out.append(Factor("unit", "Unit and scale", _show(a.unit, "not recorded"), _show(b.unit, "not recorded"), A.UNKNOWN.value, "The unit is not recorded for at least one value, so the two cannot be shown to be on one scale."))
    elif ua != ub:
        out.append(Factor("unit", "Unit and scale", _show(a.unit), _show(b.unit), A.BLOCKING.value, "The two values are on different scales; a difference between them would be meaningless and is not computed."))
    else:
        out.append(Factor("unit", "Unit and scale", _show(a.unit), _show(b.unit), A.MATCH.value, "Same unit."))
    return out


def _baseline_factor(a: Finding, b: Finding) -> Factor:
    label = "Baseline from which change is measured"
    ca, cb = a.baseline_class, b.baseline_class
    shown_a = _show(a.baseline_definition, "not described") + f" [{BASELINE_CLASS_LABEL.get(ca, ca)}]"
    shown_b = _show(b.baseline_definition, "not described") + f" [{BASELINE_CLASS_LABEL.get(cb, cb)}]"
    if ca != cb:
        return Factor("baseline", label, shown_a, shown_b, A.BLOCKING.value,
                      f"The two changes start from different baselines: {BASELINE_CLASS_LABEL.get(ca, ca)} versus {BASELINE_CLASS_LABEL.get(cb, cb)}. "
                      "A lead-in, run-in or randomized-withdrawal period changes both the starting value and who is still in the trial; such changes describe different quantities and are never differenced.")
    if a.endpoint_id == b.endpoint_id:
        return Factor("baseline", label, shown_a, shown_b, A.MATCH.value, "Both values are stored on the same outcome measure and therefore share its baseline.")
    if _one_outcome_measure(a, b) and a.baseline_definition == b.baseline_definition:
        return Factor("baseline", label, shown_a, shown_b, A.MATCH.value,
                      "Both values are reported by one source for the same outcome measure at the same time point (their stored records differ only in "
                      "the endpoint's place in the testing hierarchy) and therefore share its baseline.")
    if ca in ("LEAD_IN_DESIGN_BASELINE_UNSPECIFIED", "SUBSET_BASELINE") and a.study_id != b.study_id:
        return Factor("baseline", label, shown_a, shown_b, A.UNKNOWN.value,
                      "Both trials have a lead-in design or report a subset, but the stored sources do not say that the changes start from the same point of each design.")
    if a.baseline_definition is None or b.baseline_definition is None:
        return Factor("baseline", label, shown_a, shown_b, A.UNKNOWN.value,
                      "The baseline is not described in the stored source for at least one finding. It is not assumed to be the same.")
    return Factor("baseline", label, shown_a, shown_b, A.MATCH.value, "Change is measured from the same kind of baseline.")


_TRAJECTORY_CATEGORIES = EFFICACY_CATEGORIES | {EndpointCategory.BODY_COMPOSITION.value, EndpointCategory.BLOOD_PRESSURE.value, EndpointCategory.LIPIDS.value}
_EVENT_CATEGORIES = {EndpointCategory.MACE.value, EndpointCategory.MORTALITY.value, EndpointCategory.CV_COMPONENT.value, EndpointCategory.KIDNEY.value, EndpointCategory.DIABETES_ONSET.value}


def _duration_factor(a: Finding, b: Finding) -> Factor:
    label = "Assessment time point"
    ta, tb = a.timepoint_weeks, b.timepoint_weeks
    if ta is None or tb is None:
        return Factor("duration", label, _show(None if ta is None else f"week {_fmt(ta, 0)}"), _show(None if tb is None else f"week {_fmt(tb, 0)}"), A.UNKNOWN.value, "Assessment time not available for both findings.")
    gap = abs(ta - tb)
    shown_a, shown_b = f"week {_fmt(ta, 0)}", f"week {_fmt(tb, 0)}"
    if gap == 0:
        return Factor("duration", label, shown_a, shown_b, A.MATCH.value, "Same assessment time.")
    if gap <= 1:
        return Factor("duration", label, shown_a, shown_b, A.MINOR_DIFFERENCE.value, "Assessment times differ by one week or less.")
    if a.category in _TRAJECTORY_CATEGORIES:
        why = "Weight and weight-dependent measures were still changing over this range in these trials; a longer follow-up mechanically favours a larger change, so values at different weeks are not the same quantity."
    elif a.category in _EVENT_CATEGORIES:
        why = "The number of participants with an event accumulates with follow-up, so counts over different durations are not comparable."
    else:
        why = "Values assessed at different times are not the same quantity."
    return Factor("duration", label, shown_a, shown_b, A.MAJOR_DIFFERENCE.value, f"Assessment times differ by {_fmt(gap, 0)} weeks. {why}")


def _formulation_count(session: Session, drug_slug: str) -> int:
    return int(session.scalar(select(func.count()).select_from(Formulation).join(Drug, Formulation.drug_id == Drug.id).where(Drug.slug == drug_slug)) or 0)


def build_factors(session: Session, a: Finding, b: Finding) -> list[Factor]:
    f: list[Factor] = []
    same_study = a.study_id == b.study_id

    # 0 review status: evidence a reviewer rejected is excluded from comparisons
    if not (is_usable(a.verification_status) and is_usable(b.verification_status)):
        f.append(Factor("review_status", "Review status", a.verification_status, b.verification_status, A.BLOCKING.value,
                        "At least one finding was rejected in human review. Rejected evidence is kept for audit and excluded from comparisons."))

    # 1 study / randomization
    if same_study:
        randomized = (a.allocation or "").upper() == "RANDOMIZED"
        f.append(Factor("study", "Study", a.study_name, b.study_name, A.MATCH.value, "Both findings come from the same registered trial."))
        arm_a = session.get(StudyArm, a.arm_id) if a.arm_id is not None else None
        arm_b = session.get(StudyArm, b.arm_id) if b.arm_id is not None else None
        relation, why = arm_relation(session, arm_a, arm_b) if arm_a is not None and arm_b is not None else ("UNRESOLVED", "At least one finding is not attached to a single arm.")
        if relation == "SAME_ARM":
            f.append(Factor("randomization", "Randomized contrast", a.arm_label, b.arm_label, A.BLOCKING.value,
                            f"{why} This is not a treatment comparison: it sets two reports (or two analyses) of one randomized group side by side."))
        elif a.is_pooled or b.is_pooled or a.scope != "ARM" or b.scope != "ARM":
            f.append(Factor("randomization", "Randomized contrast", a.arm_label, b.arm_label, A.MAJOR_DIFFERENCE.value,
                            "At least one finding is a pooled group or a range across arms, not a single randomized arm."))
        elif relation == "DIFFERENT_STRATUM":
            f.append(Factor("randomization", "Randomized contrast", a.arm_label, b.arm_label, A.MAJOR_DIFFERENCE.value, why))
        elif relation == "UNRESOLVED":
            f.append(Factor("randomization", "Randomized contrast", a.arm_label, b.arm_label, A.UNKNOWN.value, why))
        elif randomized:
            f.append(Factor("randomization", "Randomized contrast", a.arm_label, b.arm_label, A.MATCH.value,
                            f"{why} The arms were allocated by randomization against each other, so baseline differences are attributable to chance."))
        else:
            f.append(Factor("randomization", "Randomized contrast", a.arm_label, b.arm_label, A.MAJOR_DIFFERENCE.value,
                            f"Registry allocation is '{_show(a.allocation, 'not stated')}': arms are not a randomized contrast."))
    else:
        f.append(Factor("study", "Study", f"{a.study_name} ({a.nct_id})", f"{b.study_name} ({b.nct_id})", A.MAJOR_DIFFERENCE.value,
                        "Findings come from different trials: there is no randomization between these groups, so differences may reflect "
                        "populations, sites, era, conduct or analysis rather than the treatments."))

    # 2 endpoint definition and unit
    f.extend(_endpoint_factors(a, b))

    # 3 baseline definition
    f.append(_baseline_factor(a, b))

    # 4 estimand
    is_efficacy = a.category in EFFICACY_CATEGORIES
    unknown = EstimandLabel.NOT_STATED.value
    if a.estimand_label != unknown and b.estimand_label != unknown:
        if a.estimand_label != b.estimand_label:
            pair = {a.estimand_label, b.estimand_label}
            text = ("One finding uses the efficacy estimand (hypothetical: had participants stayed on treatment) and the other the treatment-regimen "
                    "estimand (regardless of discontinuation). They answer different questions and are systematically different in magnitude."
                    if pair == {EstimandLabel.EFFICACY.value, EstimandLabel.TREATMENT_REGIMEN.value}
                    else "The findings are reported under different estimands: they answer different questions and are not differenced.")
            f.append(Factor("estimand", "Estimand", a.estimand_label, b.estimand_label, A.BLOCKING.value, text))
        else:
            inferred = "INFERRED" in a.estimand_basis or "INFERRED" in b.estimand_basis
            f.append(Factor("estimand", "Estimand", a.estimand_label, b.estimand_label, A.MATCH.value,
                            "Same estimand." + (" For at least one finding the label was inferred (from the registry's analysis description, or attributed by the curator without a source quote) rather than stated." if inferred else "")))
    elif is_efficacy:
        f.append(Factor("estimand", "Estimand", a.estimand_label, b.estimand_label, A.UNKNOWN.value,
                        "The estimand is not stated (and cannot be inferred) for at least one finding, so it is unknown whether both handle treatment discontinuation the same way."))
    else:
        f.append(Factor("estimand", "Estimand", a.estimand_label, b.estimand_label, A.NOT_APPLICABLE.value, "Estimand labelling not used for this endpoint type in the stored sources."))

    # 5 intercurrent events other than discontinuation
    ice_a = {(s["event"], s["strategy"]) for s in a.ice_strategies}
    ice_b = {(s["event"], s["strategy"]) for s in b.ice_strategies}
    if ice_a and ice_b:
        f.append(Factor("intercurrent_events", "Handling of discontinuation / rescue therapy", "; ".join(sorted(f"{e}: {s}" for e, s in ice_a)), "; ".join(sorted(f"{e}: {s}" for e, s in ice_b)),
                        A.MATCH.value if ice_a == ice_b else A.MINOR_DIFFERENCE.value,
                        "Same documented strategies." if ice_a == ice_b else "Documented intercurrent-event strategies differ in at least one respect."))
    else:
        f.append(Factor("intercurrent_events", "Handling of discontinuation / rescue therapy", "; ".join(sorted(f"{e}: {s}" for e, s in ice_a)) or "not documented", "; ".join(sorted(f"{e}: {s}" for e, s in ice_b)) or "not documented",
                        A.UNKNOWN.value, "Intercurrent-event strategies are not documented for both findings."))

    if not same_study:
        # 6 diabetes status
        da, db = a.diabetes_status, b.diabetes_status
        if "NOT_STATED" in (da, db):
            f.append(Factor("diabetes_status", "Diabetes status of population", da, db, A.UNKNOWN.value, "Diabetes status is not established for at least one study."))
        elif da != db:
            level = A.MAJOR_DIFFERENCE if "MIXED" in (da, db) else A.BLOCKING
            f.append(Factor("diabetes_status", "Diabetes status of population", da, db, level.value,
                            "Weight reduction with incretin-based therapies is consistently smaller in people with type 2 diabetes; results from populations with "
                            "and without diabetes are not interchangeable."))
        else:
            f.append(Factor("diabetes_status", "Diabetes status of population", da, db, A.MATCH.value, "Same diabetes status."))

        # 7 indication / special population
        sa, sb = _special_population(a), _special_population(b)
        f.append(Factor("indication", "Indication / defining comorbidity", ", ".join(sorted(sa)) or "none registered", ", ".join(sorted(sb)) or "none registered",
                        A.MATCH.value if sa == sb else A.MAJOR_DIFFERENCE.value,
                        "Same registered population focus." if sa == sb else "The trials enrolled populations defined by different comorbidities."))

        # 8 baseline characteristics
        f.append(_numeric_factor("baseline_weight", "Mean baseline body weight", _summary_value(a, "weight_mean_kg"), _summary_value(b, "weight_mean_kg"), "kg", 5, 10,
                                 "Baseline weight influences both absolute and percent change."))
        f.append(_numeric_factor("baseline_bmi", "Mean baseline BMI", _summary_value(a, "bmi_mean"), _summary_value(b, "bmi_mean"), "kg/m²", 2, 4, "Baseline BMI is a recognised effect modifier."))
        f.append(_numeric_factor("age", "Mean age", _summary_value(a, "age_mean_years"), _summary_value(b, "age_mean_years"), "years", 5, 10, "Age distribution differs between populations."))

        def female_pct(x: Finding) -> float | None:
            n, total = _summary_value(x, "female_n"), _summary_value(x, "sex_total_n")
            return 100.0 * n / total if n is not None and total else None

        f.append(_numeric_factor("sex", "Female participants (calculated from registry counts)", female_pct(a), female_pct(b), "%", 10, 20, "Sex is associated with magnitude of weight reduction in these trials."))

        # 9 comparator / control
        ca, cb = _control_type(a), _control_type(b)
        if ca is None or cb is None:
            f.append(Factor("comparator", "Control type", _show(ca, "not curated"), _show(cb, "not curated"), A.UNKNOWN.value, "Control type not established for both studies."))
        elif ca != cb:
            f.append(Factor("comparator", "Control type", ca, cb, A.MAJOR_DIFFERENCE.value, "Different control types: no common comparator through which an anchored indirect comparison could be made."))
        else:
            f.append(Factor("comparator", "Control type", ca, cb, A.MATCH.value,
                            "Same control type." + (" A common placebo control makes an anchored indirect comparison conceivable, subject to effect-modifier balance." if ca == "PLACEBO" else "")))

        # 10 masking
        oa, ob = _is_open_label(a), _is_open_label(b)
        if oa is None or ob is None:
            f.append(Factor("masking", "Masking", _show(a.masking, "not stated"), _show(b.masking, "not stated"), A.UNKNOWN.value, "Masking not stated for both studies."))
        else:
            f.append(Factor("masking", "Masking", _show(a.masking), _show(b.masking), A.MATCH.value if oa == ob else A.MAJOR_DIFFERENCE.value,
                            "Same masking approach." if oa == ob else "One trial was open-label and the other blinded; open-label designs can influence adherence, retention and reported outcomes."))

        # 11 phase
        pa, pb = a.phase or "", b.phase or ""
        f.append(Factor("phase", "Study phase", _show(a.phase, "not stated"), _show(b.phase, "not stated"), A.MATCH.value if pa == pb else A.MAJOR_DIFFERENCE.value,
                        "Same phase." if pa == pb else "Different phases: Phase 2 dose-finding arms are smaller, shorter and less precise than Phase 3 arms."))

    # 12 duration
    f.append(_duration_factor(a, b))

    # 13 dose / titration / formulation
    dose_a = f"{_fmt(a.dose_amount)} {a.dose_unit}" if a.dose_amount is not None else _show(a.dose_text, "placebo" if is_placebo_arm(a) else "not parsed from arm label")
    dose_b = f"{_fmt(b.dose_amount)} {b.dose_unit}" if b.dose_amount is not None else _show(b.dose_text, "placebo" if is_placebo_arm(b) else "not parsed from arm label")
    if a.drug_slug and a.drug_slug == b.drug_slug and not is_placebo_arm(a):
        if a.formulation_key and b.formulation_key and a.formulation_key != b.formulation_key:
            eq = documented_equivalence(session, a.formulation_key, a.dose_amount, b.formulation_key, b.dose_amount)
            if eq is not None:
                f.append(Factor("dose", "Dose and formulation", f"{dose_a} ({a.formulation_key})", f"{dose_b} ({b.formulation_key})", A.MATCH.value,
                                f"Different formulations, but this dose pair has a documented exposure equivalence ({eq.basis.replace('_', ' ').lower()})."))
            else:
                f.append(Factor("dose", "Dose and formulation", f"{dose_a} ({a.formulation_key})", f"{dose_b} ({b.formulation_key})", A.MAJOR_DIFFERENCE.value,
                                "Different formulations with no documented exposure equivalence for this dose pair; milligram amounts are not interchangeable across formulations."))
        elif not (a.formulation_key and b.formulation_key) and _formulation_count(session, a.drug_slug) > 1:
            f.append(Factor("dose", "Dose and formulation", f"{dose_a} ({_show(a.formulation_key, 'formulation not recorded')})", f"{dose_b} ({_show(b.formulation_key, 'formulation not recorded')})", A.UNKNOWN.value,
                            "This drug has more than one formulation on record and the formulation is not recorded for at least one arm; milligram amounts are not interchangeable across formulations, so the doses cannot be compared."))
        elif a.dose_amount is not None and b.dose_amount is not None and a.dose_amount != b.dose_amount:
            f.append(Factor("dose", "Dose and formulation", dose_a, dose_b, A.MINOR_DIFFERENCE.value, "Same drug at different doses."))
        else:
            f.append(Factor("dose", "Dose and formulation", dose_a, dose_b, A.MATCH.value if a.dose_amount == b.dose_amount and a.dose_amount is not None else A.UNKNOWN.value,
                            "Same drug; dose as labelled." if a.dose_amount is not None else "Dose not parsed for both arms; titration scheme not compared."))
    else:
        f.append(Factor("dose", "Dose and formulation", f"{_show(a.drug_slug, 'drug not identified')} — {dose_a}", f"{_show(b.drug_slug, 'drug not identified')} — {dose_b}", A.NOT_APPLICABLE.value,
                        "Different agents: doses are not on a common scale. Each arm's titration scheme and whether the maximum tolerated dose was reached should be checked in the protocols."))

    # 14 analysis method / missing data
    ma, mb = _method_family(a.analysis_method), _method_family(b.analysis_method)
    if ma is None or mb is None:
        f.append(Factor("analysis_method", "Analysis model / missing-data handling", _show(ma, "not described"), _show(mb, "not described"), A.UNKNOWN.value,
                        "The analysis model is not described for both findings in the stored sources."))
    else:
        f.append(Factor("analysis_method", "Analysis model / missing-data handling", ma, mb, A.MATCH.value if ma == mb else A.MINOR_DIFFERENCE.value,
                        "Same model family." if ma == mb else "Different analysis models and therefore different missing-data assumptions."))

    # 15 analysis population (including a named subset)
    sub_a, sub_b = a.endpoint_definition.subset, b.endpoint_definition.subset
    if sub_a != sub_b:
        f.append(Factor("analysis_population", "Analysis population", _show(sub_a, _show(a.analysis_population, "not described"))[:240], _show(sub_b, _show(b.analysis_population, "not described"))[:240], A.MAJOR_DIFFERENCE.value,
                        "One finding is reported for a named subset of participants and the other is not (or names a different subset); they do not describe the same analysed population."))
    else:
        f.append(Factor("analysis_population", "Analysis population", _show(a.analysis_population, "not described")[:240], _show(b.analysis_population, "not described")[:240],
                        A.UNKNOWN.value if not (a.analysis_population and b.analysis_population) else (A.MATCH.value if a.analysis_population == b.analysis_population else A.MINOR_DIFFERENCE.value),
                        "Compare the text: who was analysed and which observations were excluded."))

    # 16 endpoint hierarchy / multiplicity
    exploratory = {"EXPLORATORY", "POST_HOC"}
    if a.role in exploratory or b.role in exploratory:
        level, text = A.MAJOR_DIFFERENCE, "At least one finding is exploratory or post hoc and carries no confirmatory weight."
    elif TriState.NO.value in (a.multiplicity_controlled, b.multiplicity_controlled):
        level, text = A.MAJOR_DIFFERENCE, "At least one finding was reported outside multiplicity control."
    elif a.role != b.role:
        level, text = A.MINOR_DIFFERENCE, "The endpoint has a different position in each trial's hierarchy."
    elif TriState.NOT_STATED.value in (a.multiplicity_controlled, b.multiplicity_controlled):
        level, text = A.UNKNOWN, "Same endpoint role; multiplicity control is not stated for at least one finding in the stored source."
    else:
        level, text = A.MATCH, "Same endpoint role, both within multiplicity control."
    f.append(Factor("hierarchy", "Endpoint hierarchy and multiplicity", f"{a.role} / multiplicity {a.multiplicity_controlled}", f"{b.role} / multiplicity {b.multiplicity_controlled}", level.value, text))

    # 17 precision
    def precision(x: Finding) -> str:
        bits = [f"n = {x.n_analyzed}" if x.n_analyzed is not None else "n not reported"]
        bits.append("CI reported" if x.ci_low is not None else (f"{x.dispersion_type} {_fmt(x.dispersion_value)}" if x.dispersion_value is not None else "no CI or dispersion reported"))
        return "; ".join(bits)

    no_uncertainty = (a.ci_low is None and a.dispersion_value is None) or (b.ci_low is None and b.dispersion_value is None)
    small = any(x.n_analyzed is not None and x.n_analyzed < 100 for x in (a, b))
    f.append(Factor("precision", "Precision", precision(a), precision(b),
                    (A.MAJOR_DIFFERENCE if no_uncertainty else A.MINOR_DIFFERENCE if small else A.MATCH).value,
                    "At least one finding has no reported measure of uncertainty, so the precision of any difference cannot be judged." if no_uncertainty
                    else ("At least one arm analysed fewer than 100 participants." if small else "Both findings report sample size and a measure of uncertainty.")))

    # 18 evidence tier
    press = EvidenceTier.PRESS_RELEASE.value
    if press in (a.tier, b.tier):
        level, text = A.MAJOR_DIFFERENCE, "At least one finding is a topline press-release disclosure: not peer reviewed, statistical detail incomplete, and subject to revision on publication."
    elif a.tier != b.tier:
        level, text = A.MINOR_DIFFERENCE, "The findings come from different evidence tiers (e.g. registry-posted versus peer-reviewed)."
    else:
        level, text = A.MATCH, "Same evidence tier."
    f.append(Factor("evidence_tier", "Evidence tier", a.tier, b.tier, level.value, text))

    # 19 source document (within one trial): a direct comparison is one report of one analysis
    if same_study:
        f.append(_source_factor(a, b))
    return f


def _source_label(f: Finding) -> str:
    src = f.source or {}
    if not src:
        return "source not recorded"
    tier = src.get("tier_label") or src.get("tier") or ""
    return f"{src.get('key')} ({tier})" if tier else str(src.get("key"))


def _source_factor(a: Finding, b: Finding) -> Factor:
    label = "Source document"
    ida, idb = _source_id(a), _source_id(b)
    if ida is None or idb is None:
        return Factor("source_document", label, _source_label(a), _source_label(b), A.UNKNOWN.value,
                      "The source document is not recorded for at least one value, so the two cannot be shown to come from one report.")
    if ida == idb:
        return Factor("source_document", label, _source_label(a), _source_label(b), A.MATCH.value, "Both values are reported in the same document.")
    return Factor("source_document", label, _source_label(a), _source_label(b), A.MAJOR_DIFFERENCE.value,
                  f"Finding A is reported in {_source_label(a)} and finding B in {_source_label(b)}. No single source reports both values, so their "
                  "difference is not one any source reported: the documents may use different analysis sets, data cuts or handling of intercurrent events.")


#: Factors that must MATCH before two findings of different randomizations are called partially comparable.
PARTIAL_REQUIRED_MATCH = ("endpoint", "unit", "baseline", "diabetes_status", "indication")
#: Population descriptors that must have been assessable (age, sex, and body size by weight or BMI). Unknown is unknown.
PARTIAL_POPULATION_KEYS = ("age", "sex")
PARTIAL_BODY_SIZE_KEYS = ("baseline_weight", "baseline_bmi")


def _partial_gaps(by_key: dict[str, Factor]) -> list[str]:
    """Why a cross-randomization pair with no major difference still cannot be called partially comparable ([] = it can)."""
    gaps = [by_key[k].label for k in PARTIAL_REQUIRED_MATCH if k in by_key and by_key[k].assessment != A.MATCH.value]
    if "estimand" in by_key and by_key["estimand"].assessment not in (A.MATCH.value, A.NOT_APPLICABLE.value):
        gaps.append(by_key["estimand"].label)
    if "duration" in by_key and by_key["duration"].assessment not in (A.MATCH.value, A.MINOR_DIFFERENCE.value):
        gaps.append(by_key["duration"].label)
    gaps += [by_key[k].label for k in PARTIAL_POPULATION_KEYS if k in by_key and by_key[k].assessment == A.UNKNOWN.value]
    if all(k in by_key and by_key[k].assessment == A.UNKNOWN.value for k in PARTIAL_BODY_SIZE_KEYS):
        gaps.append("body size (neither mean baseline weight nor mean baseline BMI)")
    return gaps


def compare_findings(session: Session, a: Finding, b: Finding) -> Comparison:
    factors = build_factors(session, a, b)
    by_key = {x.key: x for x in factors}
    same_study = a.study_id == b.study_id
    blocking = [x for x in factors if x.assessment == A.BLOCKING.value]
    major = [x for x in factors if x.assessment == A.MAJOR_DIFFERENCE.value and x.key != "study"]
    unknown = [x for x in factors if x.assessment == A.UNKNOWN.value]
    minor = [x for x in factors if x.assessment == A.MINOR_DIFFERENCE.value]
    is_efficacy = a.category in EFFICACY_CATEGORIES
    # Two values of ONE reported analysis: one outcome-measure record of one source, under one estimand label.
    one_report = same_study and _one_outcome_measure(a, b) and _source_id(a) is not None and _source_id(a) == _source_id(b) and a.estimand_label == b.estimand_label

    randomization = by_key.get("randomization")
    if not same_study:
        relationship = "DIFFERENT_TRIALS"
    elif randomization is not None and randomization.assessment == A.BLOCKING.value:
        relationship = "SAME_ARM_TWO_REPORTS"
    elif randomization is not None and randomization.assessment == A.UNKNOWN.value:
        relationship = "SAME_TRIAL_ARM_IDENTITY_UNRESOLVED"
    elif randomization is not None and randomization.assessment == A.MATCH.value:
        relationship = "RANDOMIZED_ARMS_OF_ONE_TRIAL"
    else:
        relationship = "SAME_REGISTRATION_NOT_RANDOMIZED_AGAINST_EACH_OTHER"

    value_missing = (a.value is None and a.value_low is None) or (b.value is None and b.value_low is None)
    essential_keys = ["endpoint", "unit", "randomization"]
    if is_efficacy and not one_report:
        # Within one trial an unstated estimand is not disqualifying when both values come from one reported analysis.
        essential_keys.append("estimand")
    if not same_study:
        essential_keys.append("diabetes_status")
    essential_unknown = [by_key[k] for k in essential_keys if k in by_key and by_key[k].assessment == A.UNKNOWN.value]

    def matched(*keys: str) -> bool:
        return all(k in by_key and by_key[k].assessment == A.MATCH.value for k in keys)

    partial_gaps: list[str] = []
    direct_gaps: list[str] = []
    if blocking:
        cls = ComparabilityClass.NOT_SUITABLE
    elif value_missing or essential_unknown:
        cls = ComparabilityClass.INSUFFICIENT_EVIDENCE
    elif same_study:
        estimand_ok = by_key["estimand"].assessment in (A.MATCH.value, A.NOT_APPLICABLE.value) or one_report
        direct_gaps = [by_key[k].label for k in ("randomization", "endpoint", "unit", "baseline", "duration", "source_document") if not matched(k)]
        if not estimand_ok:
            direct_gaps.append(by_key["estimand"].label)
        if by_key["analysis_population"].assessment == A.MAJOR_DIFFERENCE.value:
            direct_gaps.append(by_key["analysis_population"].label)
        cls = ComparabilityClass.DESCRIPTIVE_CROSS_STUDY if direct_gaps else ComparabilityClass.DIRECT_RANDOMIZED_COMPARISON
    elif major:
        cls = ComparabilityClass.DESCRIPTIVE_CROSS_STUDY
    else:
        partial_gaps = _partial_gaps(by_key)
        cls = ComparabilityClass.DESCRIPTIVE_CROSS_STUDY if partial_gaps else ComparabilityClass.PARTIALLY_COMPARABLE

    reasons: list[str] = []
    for group in (blocking, essential_unknown if cls == ComparabilityClass.INSUFFICIENT_EVIDENCE else [], major):
        for x in group:
            line = f"{x.label}: {x.explanation}"
            if line not in reasons:
                reasons.append(line)
    if not same_study:
        reasons.insert(0 if not blocking else len(blocking), by_key["study"].explanation)
    if cls == ComparabilityClass.INSUFFICIENT_EVIDENCE and value_missing:
        reasons.insert(0, "At least one finding has no reported value.")
    if partial_gaps:
        reasons.append("Not established as similar, so the findings are not called partially comparable: " + ", ".join(g.lower() for g in partial_gaps) + ". An unknown factor is not treated as a matching one.")
    if same_study and cls == ComparabilityClass.DESCRIPTIVE_CROSS_STUDY and direct_gaps:
        reasons.append("Not a direct randomized comparison of one quantity because these do not match: " + ", ".join(g.lower() for g in direct_gaps) + ".")
    for x in minor[:4]:
        reasons.append(f"{x.label} (minor): {x.explanation}")
    if unknown and cls not in (ComparabilityClass.NOT_SUITABLE,):
        reasons.append("Not assessable from stored sources: " + ", ".join(x.label.lower() for x in unknown) + ".")

    analyses, other_analyses = _reported_analyses(session, a, b) if relationship == "RANDOMIZED_ARMS_OF_ONE_TRIAL" else ([], [])
    if analyses and cls != ComparabilityClass.DIRECT_RANDOMIZED_COMPARISON:
        # Only a direct comparison quotes a between-arm analysis. Otherwise the analysis is listed, never quoted, with the reason.
        why = "this pair of values is not a direct randomized comparison of one quantity" + (f" ({', '.join(g.lower() for g in direct_gaps)} not matched)" if direct_gaps else f" (classified {cls.value.lower()})")
        other_analyses = [{**{k: v for k, v in x.items() if k != "pair_orientation"}, "mismatch": [why]} for x in analyses] + other_analyses
        analyses = []
    head_to_head = [] if same_study else find_head_to_head_trials(session, a.drug_slug, b.drug_slug)

    # Descriptive arithmetic difference — only where a difference of the two values is meaningful at all.
    descriptive = None
    # The same measurement, on the same scale, from the same kind of baseline, at the same assessment time (one week at most
    # apart) and in the same analysed population. A baseline that is not described for one of the two values is not assumed
    # to be the same, and values at different weeks or of a named subset beside the whole population are different quantities.
    difference_keys = ("endpoint", "unit", "baseline")
    not_same = [by_key[k].label.lower() for k in difference_keys if not matched(k)]
    if by_key["duration"].assessment not in (A.MATCH.value, A.MINOR_DIFFERENCE.value):
        not_same.append(by_key["duration"].label.lower() + f" ({by_key['duration'].a} versus {by_key['duration'].b})")
    if by_key["analysis_population"].assessment == A.MAJOR_DIFFERENCE.value:
        not_same.append(by_key["analysis_population"].label.lower() + " (a named subset beside another population)")
    difference_meaningful = not not_same
    if cls in (ComparabilityClass.DIRECT_RANDOMIZED_COMPARISON, ComparabilityClass.PARTIALLY_COMPARABLE, ComparabilityClass.DESCRIPTIVE_CROSS_STUDY) and a.value is not None and b.value is not None and difference_meaningful:
        descriptive = {
            "value": round(a.value - b.value, 4),
            "unit": a.unit,
            "orientation": "A minus B",
            "minuend": f"{a.arm_label} ({a.study_name})",
            "subtrahend": f"{b.arm_label} ({b.study_name})",
            "kind": "ARITHMETIC_DIFFERENCE_OF_REPORTED_VALUES",
            "ci": None,
            "ci_status": "NOT_AVAILABLE",
            "note": (
                "Arithmetic difference of the two arm-level values (A − B), both reported in one document. It is not a model-based treatment-effect estimate and has no confidence interval."
                if cls == ComparabilityClass.DIRECT_RANDOMIZED_COMPARISON
                else "Unadjusted arithmetic difference (A − B) between two arms of one randomized trial, but the two values are not established as one randomized quantity ("
                + ", ".join(g.lower() for g in direct_gaps) + " not matched). It is descriptive only: it is not an estimate of the treatment effect, "
                "it has no confidence interval, and it must not be described as statistically significant."
                if relationship == "RANDOMIZED_ARMS_OF_ONE_TRIAL"
                else "Unadjusted arithmetic difference (A − B) between groups that were not randomized against each other. It is descriptive only: it is not an estimate of comparative efficacy, "
                "it has no confidence interval, and it must not be described as statistically significant."
            ),
        }

    elif cls in (ComparabilityClass.PARTIALLY_COMPARABLE, ComparabilityClass.DESCRIPTIVE_CROSS_STUDY) and a.value is not None and b.value is not None:
        reasons.append("No arithmetic difference is given: " + ", ".join(not_same) + " could not be established as the same for the two values.")

    a_name, b_name = f"{a.arm_label} ({a.study_name})", f"{b.arm_label} ({b.study_name})"
    permitted: list[str] = []
    prohibited: list[str] = []
    requirements: list[str] = []

    if cls == ComparabilityClass.DIRECT_RANDOMIZED_COMPARISON:
        headline = "Randomized within-trial comparison"
        if analyses:
            lead = "A between-arm analysis of this endpoint, estimand and time point is reported for this pair" if len(analyses) == 1 else f"{len(analyses)} sources report a between-arm analysis of this endpoint, estimand and time point for this pair (all are listed; the order is a display choice, not a ranking)"
            for x in analyses:
                permitted.append(f"{lead}: {_analysis_sentence(x)}")
            if any(x["hypothesis_type"] == "NON_INFERIORITY" for x in analyses):
                prohibited.append("Do not describe a met non-inferiority margin as superiority.")
            if any(not x["pair_orientation"]["direction_established"] for x in analyses):
                prohibited.append("Do not state which arm the reported estimate favours: the direction of the stored analysis is not established.")
            statuses = {x["multiplicity_controlled"] for x in analyses}
            if statuses != {TriState.YES.value}:
                permitted.append("Whether this comparison was inside the trial's multiplicity control is "
                                 + ("documented as NO — treat any p-value as nominal." if TriState.NO.value in statuses else "not stated in the stored source."))
        else:
            also = f" ({len(other_analyses)} other stored {'analysis' if len(other_analyses) == 1 else 'analyses'} of these two arms concern a different endpoint, estimand or time point, or are reported in another document, and are not quoted for this pair.)" if other_analyses else ""
            permitted.append("The arms were randomized against each other, but no between-arm analysis of this endpoint, estimand and time point is stored for this pair; "
                             f"the difference between the two arm-level values is descriptive.{also}")
            prohibited.append("Do not state that the difference is statistically significant: no supporting test is on record for this pair.")
        permitted.append(f"The comparison applies to this trial's population, estimand ({a.estimand_label.replace('_', ' ').lower()}) and time point only.")
    elif cls == ComparabilityClass.PARTIALLY_COMPARABLE:
        headline = "Similar designs, but not randomized against each other"
        agree = [x.label.lower() for x in factors if x.assessment == A.MATCH.value and x.key != "study"]
        permitted.append(f"{a_name} and {b_name} may be shown side by side. These descriptors match on the stored evidence: {', '.join(agree)}.")
        if minor:
            permitted.append("They differ to a minor degree on: " + ", ".join(x.label.lower() for x in minor) + ".")
        if unknown:
            permitted.append("Not assessable from the stored sources, and therefore not established as similar: " + ", ".join(x.label.lower() for x in unknown) + ".")
        permitted.append("The groups were not randomized against each other. Any numerical difference may be described only as an unadjusted, cross-trial observation.")
    elif cls == ComparabilityClass.DESCRIPTIVE_CROSS_STUDY:
        headline = "Side-by-side description only"
        permitted.append("The two values may be tabulated side by side with their study context and the methodological differences listed here.")
    elif cls == ComparabilityClass.NOT_SUITABLE:
        headline = "These findings should not be compared for efficacy"
        permitted.append("Each finding may be reported on its own, with its context. No difference between them should be computed or quoted.")
    else:
        headline = "Too little is documented to judge comparability"
        permitted.append("Each finding may be reported on its own. Comparability cannot be judged until the missing descriptors are obtained.")

    if relationship == "SAME_ARM_TWO_REPORTS":
        headline = "Two reports of one arm — not a treatment comparison"
        permitted = ["Each value may be reported with its own source, estimand, baseline and time point. Side by side they show how two sources or two analyses describe the same randomized group; they say nothing about a treatment effect."]
        prohibited.append("Do not describe the gap between these two values as a treatment difference or a treatment effect: both belong to the same arm.")
        requirements.append("If two sources disagree about the same quantity for one arm, that is a source conflict for human review, not a comparison of treatments.")
    elif cls != ComparabilityClass.DIRECT_RANDOMIZED_COMPARISON:
        if relationship == "SAME_REGISTRATION_NOT_RANDOMIZED_AGAINST_EACH_OTHER" and cls == ComparabilityClass.DESCRIPTIVE_CROSS_STUDY:
            headline = "Same registration, but not a randomized contrast of one quantity"
        prohibited += [
            f"Do not state or imply that {a.arm_label} is superior (or inferior) to {b.arm_label} on the basis of these two findings.",
            "Do not describe the difference between these values as statistically significant; no valid test supports it.",
            "Do not rank the treatments by these values as if the ranking were a demonstrated treatment effect.",
        ]
        requirements.append("A randomized head-to-head trial with a pre-specified estimand is the only design that removes cross-trial confounding.")
        if head_to_head:
            for trial in head_to_head:
                when = f"primary completion {trial['primary_completion_date']} ({(trial['primary_completion_type'] or 'date type not stated').lower()})" if trial["primary_completion_date"] else "primary completion date not stated"
                requirements.append(
                    f"A registered trial includes both agents: {trial['display_name']} ({trial['nct_id']}), status {trial['overall_status']}, {when}; "
                    + ("results are posted in the registry." if trial["has_results"] else "no results are posted in the registry.")
                )
        elif not same_study and a.drug_slug and b.drug_slug and a.drug_slug != b.drug_slug and not (is_placebo_arm(a) or is_placebo_arm(b)):
            requirements.append("No registered trial including both agents was found among the tracked studies (this is a statement about tracked registry records, not proof that none exists).")
        requirements += [
            "Without a head-to-head trial: individual participant data from at least one trial for a matching-adjusted or simulated-treatment comparison, with all effect modifiers measured in both.",
            "If both trials share a comparator (e.g. placebo): an anchored indirect comparison on the same estimand and time point, with its consistency assumptions stated and tested where possible.",
            "The same estimand, endpoint definition, baseline definition and assessment time in both analyses.",
        ]
    if a.is_body_composition_lean or b.is_body_composition_lean:
        prohibited.append("Do not describe a change in DXA lean or fat-free mass as loss of skeletal muscle without qualification.")

    return Comparison(
        classification=cls.value,
        headline=headline,
        same_study=same_study,
        factors=factors,
        reasons=reasons,
        permitted_statements=permitted,
        prohibited_statements=prohibited,
        requirements=requirements,
        reported_analyses=analyses,
        descriptive_difference=descriptive,
        head_to_head_trials=head_to_head,
        counts={"blocking": len(blocking), "major": len(major), "minor": len(minor), "unknown": len(unknown), "match": sum(1 for x in factors if x.assessment == A.MATCH.value)},
        relationship=relationship,
        other_analyses=other_analyses,
        endpoint_definitions={"a": a.endpoint_definition.as_dict(), "b": b.endpoint_definition.as_dict()},
    )


def link_study_drugs(session: Session, study_id: int) -> list[str]:
    return [link.drug.slug for link in session.scalars(select(StudyDrug).where(StudyDrug.study_id == study_id))]
