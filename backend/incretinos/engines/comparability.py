"""Clinical comparability engine.

Decides whether two reported findings can be compared, and says why. The output is a
classification plus a factor-by-factor account — never a bare score — and an explicit
list of statements the evidence does and does not permit.

Classification logic (deterministic):

* Same study, concurrently randomized arms, same endpoint / estimand / time point
  -> DIRECT RANDOMIZED COMPARISON (with the reported between-arm analysis if one exists).
* Any *blocking* mismatch (endpoint, baseline definition, estimand, diabetes status)
  -> NOT SUITABLE FOR EFFICACY COMPARISON.
* An essential descriptor is unknown (estimand for an efficacy endpoint, diabetes status,
  the value itself) -> INSUFFICIENT EVIDENCE.
* Different studies, no blocking mismatch: no major difference -> PARTIALLY COMPARABLE;
  one or more major differences -> DESCRIPTIVE CROSS-STUDY COMPARISON.

A cross-study classification never licenses a superiority or significance claim.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ComparabilityClass, EquivalenceBasis, EstimandLabel, EvidenceTier, FactorAssessment, TriState
from ..models import Dose, ExposureEquivalence, StatisticalAnalysis, Study, StudyDrug
from .findings import EFFICACY_CATEGORIES, Finding, _fmt, analysis_dto

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
    reported_analyses: list[dict[str, Any]] = field(default_factory=list)
    descriptive_difference: dict[str, Any] | None = None
    head_to_head_trials: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

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


def _reported_analyses(session: Session, a: Finding, b: Finding) -> list[dict[str, Any]]:
    if a.arm_id is None or b.arm_id is None:
        return []
    rows = session.scalars(select(StatisticalAnalysis).where(StatisticalAnalysis.study_id == a.study_id)).all()
    arms = {a.arm_id, b.arm_id}
    out = []
    for row in rows:
        if {row.treatment_arm_id, row.comparator_arm_id} != arms:
            continue
        same_endpoint = row.endpoint_id in (a.endpoint_id, b.endpoint_id) or (
            row.endpoint.category == a.category and (row.timepoint_weeks == a.timepoint_weeks or row.timepoint_weeks is None)
        )
        if same_endpoint:
            out.append(analysis_dto(row))
    return out


def build_factors(session: Session, a: Finding, b: Finding) -> list[Factor]:
    f: list[Factor] = []
    same_study = a.study_id == b.study_id

    # 1 study / randomization
    if same_study:
        randomized = (a.allocation or "").upper() == "RANDOMIZED"
        f.append(Factor("study", "Study", a.study_name, b.study_name, A.MATCH.value, "Both findings come from the same trial."))
        if a.arm_id is not None and a.arm_id == b.arm_id:
            f.append(Factor("randomization", "Randomized contrast", a.arm_label, b.arm_label, A.BLOCKING.value,
                            "Both findings are from the same arm: this contrasts two analyses of one group, not two treatments."))
        elif a.is_pooled or b.is_pooled or a.scope != "ARM" or b.scope != "ARM":
            f.append(Factor("randomization", "Randomized contrast", a.arm_label, b.arm_label, A.MAJOR_DIFFERENCE.value,
                            "At least one finding is a pooled group or a range across arms, not a single randomized arm."))
        elif randomized:
            f.append(Factor("randomization", "Randomized contrast", a.arm_label, b.arm_label, A.MATCH.value,
                            "Arms were allocated by randomization within one trial, so baseline differences are attributable to chance."))
        else:
            f.append(Factor("randomization", "Randomized contrast", a.arm_label, b.arm_label, A.MAJOR_DIFFERENCE.value,
                            f"Registry allocation is '{_show(a.allocation, 'not stated')}': arms are not a randomized contrast."))
    else:
        f.append(Factor("study", "Study", f"{a.study_name} ({a.nct_id})", f"{b.study_name} ({b.nct_id})", A.MAJOR_DIFFERENCE.value,
                        "Findings come from different trials: there is no randomization between these groups, so differences may reflect "
                        "populations, sites, era, conduct or analysis rather than the treatments."))

    # 2 endpoint
    same_category = a.category == b.category and (a.threshold == b.threshold)
    ep_a = a.endpoint_title + (f" [≥{_fmt(a.threshold)}%]" if a.threshold is not None else "")
    ep_b = b.endpoint_title + (f" [≥{_fmt(b.threshold)}%]" if b.threshold is not None else "")
    if not same_category:
        f.append(Factor("endpoint", "Endpoint definition", ep_a, ep_b, A.BLOCKING.value, "The findings measure different endpoints (or different responder thresholds)."))
    elif (a.class_title or "") != (b.class_title or "") and a.category in ("BODY_COMPOSITION", "LIPIDS", "OTHER", "PATIENT_REPORTED"):
        f.append(Factor("endpoint", "Endpoint definition", f"{ep_a} — {a.class_title}", f"{ep_b} — {b.class_title}", A.BLOCKING.value, "Different measures within the same endpoint family."))
    else:
        f.append(Factor("endpoint", "Endpoint definition", ep_a, ep_b, A.MATCH.value, "Same endpoint category."))

    # 3 baseline definition
    if a.baseline_class != b.baseline_class:
        f.append(Factor("baseline", "Baseline from which change is measured", _show(a.baseline_definition, a.baseline_class), _show(b.baseline_definition, b.baseline_class), A.BLOCKING.value,
                        "One change is measured from randomization after an open-label lead-in (randomized-withdrawal design); the other from study baseline. "
                        "They describe different quantities."))
    else:
        f.append(Factor("baseline", "Baseline from which change is measured", _show(a.baseline_definition, "study baseline"), _show(b.baseline_definition, "study baseline"), A.MATCH.value,
                        "Change is measured from the same kind of baseline."))

    # 4 estimand
    is_efficacy = a.category in EFFICACY_CATEGORIES
    unknown = EstimandLabel.NOT_STATED.value
    if a.estimand_label != unknown and b.estimand_label != unknown:
        if a.estimand_label != b.estimand_label:
            f.append(Factor("estimand", "Estimand", a.estimand_label, b.estimand_label, A.BLOCKING.value,
                            "One finding uses the efficacy estimand (hypothetical: had participants stayed on treatment) and the other the treatment-regimen "
                            "estimand (regardless of discontinuation). They answer different questions and are systematically different in magnitude."))
        else:
            inferred = "INFERRED" in a.estimand_basis or "INFERRED" in b.estimand_basis
            f.append(Factor("estimand", "Estimand", a.estimand_label, b.estimand_label, A.MATCH.value,
                            "Same estimand." + (" For at least one finding the label was inferred from the registry's analysis description rather than stated." if inferred else "")))
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
    ta, tb = a.timepoint_weeks, b.timepoint_weeks
    if ta is None or tb is None:
        f.append(Factor("duration", "Assessment time point", _show(None if ta is None else f"week {_fmt(ta, 0)}"), _show(None if tb is None else f"week {_fmt(tb, 0)}"), A.UNKNOWN.value, "Assessment time not available for both findings."))
    else:
        rel = abs(ta - tb) / max(ta, tb) if max(ta, tb) else 0
        level = A.MATCH if rel == 0 else A.MINOR_DIFFERENCE if rel <= 0.10 else A.MAJOR_DIFFERENCE
        text = "Same assessment time." if rel == 0 else (
            f"Assessment times differ by {_fmt(abs(ta - tb), 0)} weeks. " + ("Weight trajectories had not necessarily plateaued; a longer study mechanically favours larger change." if level == A.MAJOR_DIFFERENCE else "Small difference in follow-up."))
        f.append(Factor("duration", "Assessment time point", f"week {_fmt(ta, 0)}", f"week {_fmt(tb, 0)}", level.value, text))

    # 13 dose / titration / formulation
    dose_a = f"{_fmt(a.dose_amount)} {a.dose_unit}" if a.dose_amount is not None else _show(a.dose_text, "placebo" if a.is_placebo else "not parsed from arm label")
    dose_b = f"{_fmt(b.dose_amount)} {b.dose_unit}" if b.dose_amount is not None else _show(b.dose_text, "placebo" if b.is_placebo else "not parsed from arm label")
    if a.drug_slug and a.drug_slug == b.drug_slug and not a.is_placebo:
        if a.formulation_key and b.formulation_key and a.formulation_key != b.formulation_key:
            eq = documented_equivalence(session, a.formulation_key, a.dose_amount, b.formulation_key, b.dose_amount)
            if eq is not None:
                f.append(Factor("dose", "Dose and formulation", f"{dose_a} ({a.formulation_key})", f"{dose_b} ({b.formulation_key})", A.MATCH.value,
                                f"Different formulations, but this dose pair has a documented exposure equivalence ({eq.basis.replace('_', ' ').lower()})."))
            else:
                f.append(Factor("dose", "Dose and formulation", f"{dose_a} ({a.formulation_key})", f"{dose_b} ({b.formulation_key})", A.MAJOR_DIFFERENCE.value,
                                "Different formulations with no documented exposure equivalence for this dose pair; milligram amounts are not interchangeable across formulations."))
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

    # 15 analysis population
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
    return f


def compare_findings(session: Session, a: Finding, b: Finding) -> Comparison:
    factors = build_factors(session, a, b)
    by_key = {x.key: x for x in factors}
    same_study = a.study_id == b.study_id
    blocking = [x for x in factors if x.assessment == A.BLOCKING.value]
    major = [x for x in factors if x.assessment == A.MAJOR_DIFFERENCE.value and x.key != "study"]
    unknown = [x for x in factors if x.assessment == A.UNKNOWN.value]
    minor = [x for x in factors if x.assessment == A.MINOR_DIFFERENCE.value]
    is_efficacy = a.category in EFFICACY_CATEGORIES

    value_missing = (a.value is None and a.value_low is None) or (b.value is None and b.value_low is None)
    essential_unknown = [by_key[k] for k in ("estimand", "diabetes_status") if k in by_key and by_key[k].assessment == A.UNKNOWN.value]
    if not is_efficacy:
        essential_unknown = [x for x in essential_unknown if x.key != "estimand"]
    if same_study:
        essential_unknown = [x for x in essential_unknown if x.key != "diabetes_status"]
        # Within one trial an unstated estimand is not disqualifying when both values come from the same analysis.
        if a.endpoint_id == b.endpoint_id:
            essential_unknown = []

    if blocking:
        cls = ComparabilityClass.NOT_SUITABLE
    elif value_missing or essential_unknown:
        cls = ComparabilityClass.INSUFFICIENT_EVIDENCE
    elif same_study and by_key["randomization"].assessment == A.MATCH.value and by_key["duration"].assessment == A.MATCH.value:
        cls = ComparabilityClass.DIRECT_RANDOMIZED_COMPARISON
    elif same_study:
        cls = ComparabilityClass.DESCRIPTIVE_CROSS_STUDY if major else ComparabilityClass.PARTIALLY_COMPARABLE
    elif major:
        cls = ComparabilityClass.DESCRIPTIVE_CROSS_STUDY
    else:
        cls = ComparabilityClass.PARTIALLY_COMPARABLE

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
    for x in minor[:4]:
        reasons.append(f"{x.label} (minor): {x.explanation}")
    if unknown and cls not in (ComparabilityClass.NOT_SUITABLE,):
        reasons.append("Not assessable from stored sources: " + ", ".join(x.label.lower() for x in unknown) + ".")

    analyses = _reported_analyses(session, a, b) if same_study else []
    head_to_head = [] if same_study else find_head_to_head_trials(session, a.drug_slug, b.drug_slug)

    # Descriptive arithmetic difference — only where a difference of the two values is meaningful at all.
    descriptive = None
    if cls in (ComparabilityClass.DIRECT_RANDOMIZED_COMPARISON, ComparabilityClass.PARTIALLY_COMPARABLE, ComparabilityClass.DESCRIPTIVE_CROSS_STUDY) and a.value is not None and b.value is not None:
        descriptive = {
            "value": round(a.value - b.value, 4),
            "unit": a.unit,
            "kind": "ARITHMETIC_DIFFERENCE_OF_REPORTED_VALUES",
            "ci": None,
            "ci_status": "NOT_AVAILABLE",
            "note": (
                "Arithmetic difference of the two arm-level values. It is not a model-based treatment-effect estimate and has no confidence interval."
                if cls == ComparabilityClass.DIRECT_RANDOMIZED_COMPARISON
                else "Unadjusted arithmetic difference between arms of different trials. It is descriptive only: it is not an estimate of comparative efficacy, "
                "it has no confidence interval, and it must not be described as statistically significant."
            ),
        }

    a_name, b_name = f"{a.arm_label} ({a.study_name})", f"{b.arm_label} ({b.study_name})"
    permitted: list[str] = []
    prohibited: list[str] = []
    requirements: list[str] = []

    if cls == ComparabilityClass.DIRECT_RANDOMIZED_COMPARISON:
        headline = "Randomized within-trial comparison"
        formal = [x for x in analyses if x["estimate"] is not None]
        if formal:
            best = formal[0]
            permitted.append(f"A between-arm analysis is reported for this pair: {best['param_type']} {_fmt(best['estimate'])}"
                             + (f" ({_fmt(best['ci_level'], 1)}% CI {_fmt(best['ci_low'])} to {_fmt(best['ci_high'])})" if best["ci_low"] is not None else " (CI not reported)")
                             + (f", p {best['p_value_text']}" if best["p_value_text"] else ", p-value not reported in this source") + ".")
            if best["hypothesis_type"] == "NON_INFERIORITY":
                prohibited.append("Do not describe a met non-inferiority margin as superiority.")
            if best["multiplicity_controlled"] != TriState.YES.value:
                permitted.append("Whether this comparison was inside the trial's multiplicity control is "
                                 + ("not stated in the stored source." if best["multiplicity_controlled"] == TriState.NOT_STATED.value else "documented as NO — treat any p-value as nominal."))
        else:
            permitted.append("The arms were randomized against each other, but no between-arm analysis for this pair is stored; the difference between the two arm-level values is descriptive.")
            prohibited.append("Do not state that the difference is statistically significant: no supporting test is on record for this pair.")
        permitted.append(f"The comparison applies to this trial's population, estimand ({a.estimand_label.replace('_', ' ').lower()}) and time point only.")
    elif cls == ComparabilityClass.PARTIALLY_COMPARABLE:
        headline = "Similar designs, but not randomized against each other"
        permitted.append(f"The two findings were obtained with similar endpoint definitions, estimands, populations and follow-up, and may be shown side by side: {a_name} versus {b_name}.")
        permitted.append("Any numerical difference may be described only as an unadjusted, cross-trial observation.")
    elif cls == ComparabilityClass.DESCRIPTIVE_CROSS_STUDY:
        headline = "Side-by-side description only"
        permitted.append("The two values may be tabulated side by side with their study context and the methodological differences listed here.")
    elif cls == ComparabilityClass.NOT_SUITABLE:
        headline = "These findings should not be compared for efficacy"
        permitted.append("Each finding may be reported on its own, with its context. No difference between them should be computed or quoted.")
    else:
        headline = "Too little is documented to judge comparability"
        permitted.append("Each finding may be reported on its own. Comparability cannot be judged until the missing descriptors are obtained.")

    if cls != ComparabilityClass.DIRECT_RANDOMIZED_COMPARISON:
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
        elif a.drug_slug and b.drug_slug and a.drug_slug != b.drug_slug and not (a.is_placebo or b.is_placebo):
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
    )


def link_study_drugs(session: Session, study_id: int) -> list[str]:
    return [link.drug.slug for link in session.scalars(select(StudyDrug).where(StudyDrug.study_id == study_id))]
