"""A *finding* is one reported result together with everything needed to read it correctly.

The product never hands out a bare number: every result travels with its study, population,
arm, dose, endpoint, estimand, time point, baseline definition, precision, source and
limitations. This module builds that bundle and renders the context-carrying sentence.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import (
    AE_KIND_LABEL,
    TIER_LABEL,
    EndpointCategory,
    EndpointRole,
    EstimandBasis,
    EstimandLabel,
    EvidenceTier,
    TriState,
    VerificationStatus,
)
from ..models import AdverseEvent, EvidenceSource, OutcomeResult, StatisticalAnalysis, Study

EFFICACY_CATEGORIES = {
    EndpointCategory.WEIGHT_PCT_CHANGE.value,
    EndpointCategory.WEIGHT_ABS_CHANGE.value,
    EndpointCategory.WEIGHT_RESPONDER.value,
    EndpointCategory.BMI_CHANGE.value,
    EndpointCategory.WAIST_CHANGE.value,
    EndpointCategory.HBA1C_CHANGE.value,
    EndpointCategory.HBA1C_RESPONDER.value,
    EndpointCategory.FASTING_GLUCOSE.value,
}

CATEGORY_LABEL = {
    "WEIGHT_PCT_CHANGE": "percent change in body weight",
    "WEIGHT_ABS_CHANGE": "change in body weight",
    "WEIGHT_RESPONDER": "participants reaching a weight-reduction threshold",
    "BMI_CHANGE": "change in BMI",
    "WAIST_CHANGE": "change in waist circumference",
    "HBA1C_CHANGE": "change in HbA1c",
    "HBA1C_RESPONDER": "participants reaching an HbA1c target",
    "FASTING_GLUCOSE": "change in fasting glucose",
    "BLOOD_PRESSURE": "change in blood pressure",
    "LIPIDS": "change in lipids",
    "MACE": "major adverse cardiovascular events",
    "MORTALITY": "all-cause mortality",
    "CV_COMPONENT": "cardiovascular component event",
    "KIDNEY": "kidney outcome",
    "BODY_COMPOSITION": "body composition",
    "PHYSICAL_FUNCTION": "physical function",
    "PATIENT_REPORTED": "patient-reported outcome",
    "DIABETES_ONSET": "onset of type 2 diabetes",
    "PAIN": "pain",
    "PHARMACOKINETICS": "pharmacokinetics",
    "OTHER": "other outcome",
}

LEAN_MASS_CAVEAT = (
    "Lean (fat-free) mass measured by DXA includes water, organ and connective tissue as well as muscle; "
    "a change in lean mass is not a direct measurement of skeletal muscle mass, strength or function."
)


def baseline_class(design_features: dict[str, Any] | None, baseline_definition: str | None) -> str:
    """Which baseline a change is measured from — the distinction that matters in withdrawal designs."""
    text = (baseline_definition or "").lower()
    lead_in_randomization = bool(re.search(r"randomi[sz]ation \(week\s*[1-9]|week\s*[1-9]\d*\s*\(randomi", text))
    withdrawal = bool((design_features or {}).get("randomized_withdrawal")) or lead_in_randomization
    if not withdrawal:
        return "STUDY_BASELINE"
    if re.search(r"study entry|week 0|lead-in start", text):
        return "STUDY_ENTRY_INCLUDING_LEAD_IN"
    if "randomi" in text:
        return "RANDOMIZATION_AFTER_LEAD_IN"
    return "STUDY_BASELINE"


def source_ref(source: EvidenceSource | None) -> dict[str, Any] | None:
    if source is None:
        return None
    return {
        "id": source.id,
        "key": source.key,
        "tier": source.tier,
        "tier_label": TIER_LABEL.get(EvidenceTier(source.tier), source.tier) if source.tier in EvidenceTier._value2member_map_ else source.tier,
        "title": source.title,
        "publisher": source.publisher,
        "url": source.url,
        "published_date": source.published_date,
        "retrieved_at": source.retrieved_at.isoformat() if source.retrieved_at else None,
        "version_label": source.version_label,
        "content_hash": source.content_hash,
        "stores_full_text": source.stores_full_text,
    }


@dataclass
class Finding:
    result_id: int
    origin: str
    # study
    study_id: int
    nct_id: str
    study_name: str
    phase: str | None
    study_status: str | None
    allocation: str | None
    masking: str | None
    design_features: dict[str, Any]
    conditions: list[str]
    program: str | None
    # population
    diabetes_status: str
    diabetes_basis: str | None
    population_summary: dict[str, Any]
    # arm
    arm_id: int | None
    arm_label: str
    scope: str
    drug_slug: str | None
    dose_amount: float | None
    dose_unit: str | None
    dose_text: str | None
    formulation_key: str | None
    is_placebo: bool
    is_pooled: bool
    # endpoint
    endpoint_id: int
    endpoint_title: str
    category: str
    threshold: float | None
    role: str
    multiplicity_controlled: str
    class_title: str | None
    # estimand
    estimand_label: str
    estimand_basis: str
    estimand_quote: str | None
    ice_strategies: list[dict[str, str]]
    # value
    param_type: str | None
    value: float | None
    value_low: float | None
    value_high: float | None
    unit: str | None
    ci_low: float | None
    ci_high: float | None
    ci_level: float | None
    dispersion_type: str | None
    dispersion_value: float | None
    n_analyzed: int | None
    analysis_population: str | None
    analysis_method: str | None
    baseline_definition: str | None
    timepoint_weeks: float | None
    timeframe_text: str | None
    # provenance
    tier: str
    verification_status: str
    source: dict[str, Any] | None
    source_location: str | None
    locator_quote: str | None
    missing_fields: list[str]
    notes: str | None
    labels: list[dict[str, str]] = field(default_factory=list)
    statement: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    # -- derived descriptors
    @property
    def randomized_withdrawal(self) -> bool:
        return bool(self.design_features.get("randomized_withdrawal")) or bool(
            self.baseline_definition and re.search(r"randomi[sz]ation \(week\s*[1-9]", self.baseline_definition, re.IGNORECASE)
        )

    @property
    def baseline_class(self) -> str:
        return baseline_class(self.design_features, self.baseline_definition)

    @property
    def is_body_composition_lean(self) -> bool:
        text = f"{self.endpoint_title} {self.class_title or ''}".lower()
        return self.category == EndpointCategory.BODY_COMPOSITION.value and bool(re.search(r"lean|fat[- ]free", text))


def _fmt(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "not reported"
    text = f"{value:.{digits}f}"
    if "." in text:  # trim trailing decimal zeros only — never the zeros of a whole number (80 must stay 80)
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


def format_value(f: Finding) -> str:
    unit = f.unit or ""
    pct = "%" if re.search(r"percent|^%|percentage of participants|% of participants", unit.lower()) else ""
    suffix = pct or (f" {unit}" if unit else "")
    if f.scope == "RANGE_ACROSS_ARMS" and f.value is None:
        return f"{_fmt(f.value_low)}{pct} to {_fmt(f.value_high)}{suffix} (range across arms; arm-level values not disclosed)"
    return f"{_fmt(f.value)}{suffix}"


def format_ci(f: Finding) -> str:
    if f.ci_low is not None and f.ci_high is not None:
        level = f"{_fmt(f.ci_level, 1)}% " if f.ci_level else ""
        return f"{level}CI {_fmt(f.ci_low)} to {_fmt(f.ci_high)}"
    return "confidence interval not reported in this source"


def build_labels(f: Finding) -> list[dict[str, str]]:
    """Mandatory qualifiers. The UI renders every one of these next to the number."""
    labels: list[dict[str, str]] = []

    def add(code: str, text: str, tone: str, detail: str) -> None:
        labels.append({"code": code, "text": text, "tone": tone, "detail": detail})

    tier = f.tier
    if tier == EvidenceTier.PRESS_RELEASE.value:
        add("PRESS_RELEASE", "Press release · topline", "warn", "Sponsor disclosure; not peer reviewed; statistical detail is usually incomplete.")
    elif tier == EvidenceTier.REGISTRY_RESULTS.value:
        add("REGISTRY", "Registry-posted", "info", "Sponsor-submitted to ClinicalTrials.gov; quality-controlled by NLM but not peer reviewed.")
    elif tier == EvidenceTier.PEER_REVIEWED.value:
        add("PEER_REVIEWED", "Peer-reviewed", "ok", "Reported in a peer-reviewed journal article.")
    elif tier in (EvidenceTier.REGULATORY_LABEL.value, EvidenceTier.REGULATORY_REVIEW.value):
        add("REGULATORY", "Regulator-reviewed", "ok", "Reported in FDA labeling or an FDA review document.")

    if f.estimand_label == EstimandLabel.EFFICACY.value:
        add("ESTIMAND_EFFICACY", "Efficacy estimand", "info", "Hypothetical strategy: the effect had participants remained on treatment. Depends on hypothetical treatment continuation.")
    elif f.estimand_label == EstimandLabel.TREATMENT_REGIMEN.value:
        add("ESTIMAND_TREATMENT_REGIMEN", "Treatment-regimen estimand", "info", "Treatment-policy strategy: the effect regardless of treatment discontinuation.")
    elif f.category in EFFICACY_CATEGORIES:
        add("ESTIMAND_NOT_STATED", "Estimand not stated", "warn", "The source does not say how data after treatment discontinuation were handled.")
    if f.estimand_basis == EstimandBasis.INFERRED_FROM_ANALYSIS_DESCRIPTION.value:
        add("ESTIMAND_INFERRED", "Estimand inferred", "warn", "Label inferred by rule from the registry's analysis-population text; the source does not name the estimand.")

    if f.role in (EndpointRole.EXPLORATORY.value, EndpointRole.POST_HOC.value):
        add("EXPLORATORY", "Exploratory" if f.role == EndpointRole.EXPLORATORY.value else "Post hoc", "warn", "Not a confirmatory endpoint; hypothesis-generating only.")
    if f.multiplicity_controlled == TriState.NO.value:
        add("MULTIPLICITY_UNCONTROLLED", "Not multiplicity-controlled", "warn", "Reported outside the type I error control strategy; any p-value is nominal.")
    elif f.multiplicity_controlled == TriState.NOT_STATED.value:
        add("MULTIPLICITY_NOT_STATED", "Multiplicity status not stated", "muted", "The stored source does not say whether this endpoint was within the type I error control strategy.")

    if f.ci_low is None or f.ci_high is None:
        add("CI_NOT_REPORTED", "CI not reported", "muted", "No confidence interval is available in this source; none has been estimated.")
    if f.scope == "RANGE_ACROSS_ARMS":
        add("RANGE_ONLY", "Range across arms", "warn", "Only a range across several arms was disclosed; it cannot be attributed to any single arm.")
    if f.is_pooled:
        add("POOLED_ARMS", "Pooled arms", "info", "Value is for pooled dose groups, not a single randomized arm.")
    if f.baseline_class == "RANDOMIZATION_AFTER_LEAD_IN":
        add("WITHDRAWAL_BASELINE", "Baseline = randomization after lead-in", "warn", "Change is measured from randomization following an open-label lead-in, not from study entry.")
    if f.is_body_composition_lean:
        add("LEAN_MASS", "Lean mass ≠ muscle", "warn", LEAN_MASS_CAVEAT)
    if f.phase and "PHASE2" in f.phase and "PHASE3" not in f.phase:
        add("PHASE2", "Phase 2", "info", "Dose-finding study; smaller arms and wider uncertainty than Phase 3.")
    if f.verification_status == VerificationStatus.CURATED_PENDING_REVIEW.value:
        add("PENDING_REVIEW", "Pending human review", "muted", "Curated value whose quote could not be anchored automatically to stored source text.")
    elif f.verification_status == VerificationStatus.QUOTE_ANCHORED.value:
        add("QUOTE_ANCHORED", "Quote-anchored", "ok", "The locator quote was matched verbatim against the source text.")
    elif f.verification_status == VerificationStatus.MACHINE_EXTRACTED.value:
        add("MACHINE_EXTRACTED", "Machine-extracted", "ok", "Parsed directly from the structured registry payload whose hash is stored.")
    return labels


def population_phrase(f: Finding) -> str:
    status = {
        "NO_T2D": "adults without type 2 diabetes",
        "T2D": "adults with type 2 diabetes",
        "MIXED": "adults with or without type 2 diabetes",
        "NOT_STATED": "population (diabetes status not stated in stored sources)",
    }.get(f.diabetes_status, "population not characterised")
    conditions = ", ".join(c for c in f.conditions[:3])
    return f"{status}{f' — registered conditions: {conditions}' if conditions else ''}"


def render_statement(f: Finding) -> str:
    """One sentence that cannot be quoted without its population, dose, duration and limitations."""
    phase = (f.phase or "phase not stated").replace("PHASE", "Phase ").replace("/", "/")
    endpoint = CATEGORY_LABEL.get(f.category, f.endpoint_title)
    if f.category == EndpointCategory.WEIGHT_RESPONDER.value and f.threshold is not None:
        endpoint = f"participants with ≥{_fmt(f.threshold)}% weight reduction"
    if f.category in ("BODY_COMPOSITION", "OTHER", "LIPIDS", "PATIENT_REPORTED"):
        endpoint = f.endpoint_title if not f.class_title else f"{f.endpoint_title} — {f.class_title}"
    baseline = f.baseline_definition or ("baseline" if f.category in EFFICACY_CATEGORIES else None)
    when = f"at week {_fmt(f.timepoint_weeks, 0)}" if f.timepoint_weeks is not None else "(assessment time not stated)"
    estimand = {
        EstimandLabel.EFFICACY.value: "efficacy estimand",
        EstimandLabel.TREATMENT_REGIMEN.value: "treatment-regimen estimand",
    }.get(f.estimand_label, "estimand not stated")
    if f.estimand_basis == EstimandBasis.INFERRED_FROM_ANALYSIS_DESCRIPTION.value:
        estimand += " (inferred from the analysis description)"
    n = f"n = {f.n_analyzed}" if f.n_analyzed is not None else "n not reported"
    tier = TIER_LABEL.get(EvidenceTier(f.tier), f.tier).lower() if f.tier in EvidenceTier._value2member_map_ else f.tier
    parts = [
        f"In {f.study_name} ({f.nct_id}; {phase}; {population_phrase(f)}), the {f.arm_label} group had a reported {endpoint} of {format_value(f)}",
        f"{'from ' + baseline + ' ' if baseline else ''}{when}",
        f"({estimand}; {format_ci(f)}; {n}; source: {tier})",
    ]
    sentence = " ".join(parts) + "."
    caveats = []
    if f.role in (EndpointRole.EXPLORATORY.value, EndpointRole.POST_HOC.value):
        caveats.append("This is an exploratory result.")
    if f.multiplicity_controlled == TriState.NO.value:
        caveats.append("It was not controlled for multiplicity.")
    if f.phase and "PHASE2" in f.phase and "PHASE3" not in f.phase:
        caveats.append("Phase 2 dose-finding data.")
    if f.tier == EvidenceTier.PRESS_RELEASE.value:
        caveats.append("Topline press-release disclosure, not peer reviewed.")
    if f.is_body_composition_lean:
        caveats.append(LEAN_MASS_CAVEAT)
    return sentence + (" " + " ".join(caveats) if caveats else "")


def build_finding(session: Session, result: OutcomeResult) -> Finding:
    study: Study = result.study
    endpoint = result.endpoint
    arm = result.arm
    estimand = result.estimand
    population = study.population
    finding = Finding(
        result_id=result.id,
        origin=result.origin,
        study_id=study.id,
        nct_id=study.nct_id,
        study_name=study.display_name,
        phase=study.phase,
        study_status=study.overall_status,
        allocation=study.allocation,
        masking=study.masking,
        design_features=dict(study.design_features or {}),
        conditions=list(study.conditions or []),
        program=study.program,
        diabetes_status=population.diabetes_status if population else "NOT_STATED",
        diabetes_basis=population.diabetes_basis if population else None,
        population_summary=dict(population.summary or {}) if population else {},
        arm_id=arm.id if arm else None,
        arm_label=result.arm_label,
        scope=result.scope,
        drug_slug=arm.drug_slug if arm else None,
        dose_amount=arm.dose_amount if arm else None,
        dose_unit=arm.dose_unit if arm else None,
        dose_text=arm.dose_text if arm else None,
        formulation_key=arm.formulation_key if arm else None,
        is_placebo=bool(arm.is_placebo) if arm else False,
        is_pooled=bool(arm.is_pooled) if arm else result.scope == "POOLED",
        endpoint_id=endpoint.id,
        endpoint_title=endpoint.title,
        category=endpoint.category,
        threshold=endpoint.threshold,
        role=endpoint.role,
        multiplicity_controlled=endpoint.multiplicity_controlled,
        class_title=result.class_title,
        estimand_label=estimand.label if estimand else EstimandLabel.NOT_STATED.value,
        estimand_basis=estimand.basis if estimand else EstimandBasis.NOT_DETERMINABLE.value,
        estimand_quote=estimand.quote if estimand else None,
        ice_strategies=[{"event": s.event, "strategy": s.strategy, "basis": s.basis} for s in (estimand.strategies if estimand else [])],
        param_type=result.param_type,
        value=result.value,
        value_low=result.value_low,
        value_high=result.value_high,
        unit=result.unit,
        ci_low=result.ci_low,
        ci_high=result.ci_high,
        ci_level=result.ci_level,
        dispersion_type=result.dispersion_type,
        dispersion_value=result.dispersion_value,
        n_analyzed=result.n_analyzed,
        analysis_population=result.analysis_population,
        analysis_method=result.analysis_method,
        baseline_definition=result.baseline_definition,
        timepoint_weeks=result.timepoint_weeks,
        timeframe_text=result.timeframe_text,
        tier=result.tier,
        verification_status=result.verification_status,
        source=source_ref(result.source),
        source_location=result.source_location,
        locator_quote=result.locator_quote,
        missing_fields=list(result.missing_fields or []),
        notes=result.notes,
    )
    finding.labels = build_labels(finding)
    finding.statement = render_statement(finding)
    return finding


def get_finding(session: Session, result_id: int) -> Finding | None:
    result = session.get(OutcomeResult, result_id)
    return build_finding(session, result) if result else None


def analysis_dto(a: StatisticalAnalysis) -> dict[str, Any]:
    """Between-arm analysis exactly as reported, with explicit status for every absent element."""
    labels: list[dict[str, str]] = []
    if a.hypothesis_type == "NON_INFERIORITY":
        labels.append({"code": "NON_INFERIORITY", "text": "Non-inferiority test", "tone": "warn", "detail": "A met non-inferiority margin shows the treatment is not unacceptably worse; it does not show superiority."})
    elif a.hypothesis_type == "SUPERIORITY":
        labels.append({"code": "SUPERIORITY", "text": "Superiority test", "tone": "info", "detail": "Analysis registered or reported as a superiority comparison."})
    else:
        labels.append({"code": "HYPOTHESIS_NOT_STATED", "text": "Test type not stated", "tone": "muted", "detail": "The source does not state the hypothesis tested."})
    if a.multiplicity_controlled == TriState.NO.value:
        labels.append({"code": "MULTIPLICITY_UNCONTROLLED", "text": "Not multiplicity-controlled", "tone": "warn", "detail": "Nominal result outside the type I error control strategy."})
    elif a.multiplicity_controlled == TriState.NOT_STATED.value:
        labels.append({"code": "MULTIPLICITY_NOT_STATED", "text": "Multiplicity status not stated", "tone": "muted", "detail": "Not stated in the stored source."})
    if a.endpoint.role in (EndpointRole.EXPLORATORY.value, EndpointRole.POST_HOC.value):
        labels.append({"code": "EXPLORATORY", "text": "Exploratory", "tone": "warn", "detail": "Hypothesis-generating only."})
    if a.ci_low is None or a.ci_high is None:
        labels.append({"code": "CI_NOT_REPORTED", "text": "CI not reported", "tone": "muted", "detail": "No confidence interval in this source; none has been estimated."})
    if a.tier == EvidenceTier.PRESS_RELEASE.value:
        labels.append({"code": "PRESS_RELEASE", "text": "Press release · topline", "tone": "warn", "detail": "Sponsor disclosure; not peer reviewed."})
    return {
        "id": a.id,
        "study_id": a.study_id,
        "nct_id": a.study.nct_id,
        "study_name": a.study.display_name,
        "endpoint_id": a.endpoint_id,
        "endpoint_title": a.endpoint.title,
        "category": a.endpoint.category,
        "role": a.endpoint.role,
        "estimand_label": a.estimand.label if a.estimand else EstimandLabel.NOT_STATED.value,
        "estimand_basis": a.estimand.basis if a.estimand else EstimandBasis.NOT_DETERMINABLE.value,
        "treatment_label": a.treatment_label,
        "comparator_label": a.comparator_label,
        "treatment_arm_id": a.treatment_arm_id,
        "comparator_arm_id": a.comparator_arm_id,
        "hypothesis_type": a.hypothesis_type,
        "param_type": a.param_type,
        "estimate": a.estimate,
        "unit": a.unit,
        "ci_low": a.ci_low,
        "ci_high": a.ci_high,
        "ci_level": a.ci_level,
        "ci_status": "REPORTED" if a.ci_low is not None and a.ci_high is not None else "NOT_REPORTED",
        "p_value_text": a.p_value_text,
        "p_value_status": "REPORTED" if a.p_value_text else "NOT_REPORTED",
        "method": a.method,
        "ni_margin": a.ni_margin,
        "ni_margin_text": a.ni_margin_text,
        "ni_met": a.ni_met,
        "superiority_tested": a.superiority_tested,
        "superiority_met": a.superiority_met,
        "multiplicity_controlled": a.multiplicity_controlled,
        "timepoint_weeks": a.timepoint_weeks,
        "comment": a.comment,
        "tier": a.tier,
        "verification_status": a.verification_status,
        "source": source_ref(a.source),
        "source_location": a.source_location,
        "locator_quote": a.locator_quote,
        "missing_fields": list(a.missing_fields or []),
        "origin": a.origin,
        "labels": labels,
    }


def ae_dto(ae: AdverseEvent) -> dict[str, Any]:
    """Adverse-event row with an explicit statement of what the number counts."""
    percent_calc = None
    if ae.n_affected is not None and ae.n_at_risk:
        percent_calc = round(100.0 * ae.n_affected / ae.n_at_risk, 2)
    return {
        "id": ae.id,
        "study_id": ae.study_id,
        "nct_id": ae.study.nct_id,
        "study_name": ae.study.display_name,
        "arm_id": ae.arm_id,
        "arm_label": ae.arm_label,
        "scope": ae.scope,
        "term": ae.term,
        "organ_system": ae.organ_system,
        "category": ae.category,
        "measure_kind": ae.measure_kind,
        "measure_kind_label": AE_KIND_LABEL.get(ae.measure_kind, ae.measure_kind),
        "is_serious": ae.is_serious,
        "n_affected": ae.n_affected,
        "n_at_risk": ae.n_at_risk,
        "n_events": ae.n_events,
        "percent_reported": ae.percent,
        "percent_low": ae.percent_low,
        "percent_high": ae.percent_high,
        # Calculated from reported counts; shown as a calculation, never stored as a reported value.
        "percent_calculated": percent_calc,
        "period": ae.period,
        "tier": ae.tier,
        "verification_status": ae.verification_status,
        "source": source_ref(ae.source),
        "source_location": ae.source_location,
        "locator_quote": ae.locator_quote,
        "missing_fields": list(ae.missing_fields or []),
        "notes": ae.notes,
        "origin": ae.origin,
    }


def study_brief(study: Study) -> dict[str, Any]:
    population = study.population
    return {
        "id": study.id,
        "nct_id": study.nct_id,
        "display_name": study.display_name,
        "acronym": study.acronym,
        "brief_title": study.brief_title,
        "phase": study.phase,
        "overall_status": study.overall_status,
        "has_results": study.has_results,
        "enrollment": study.enrollment,
        "enrollment_type": study.enrollment_type,
        "start_date": study.start_date,
        "primary_completion_date": study.primary_completion_date,
        "primary_completion_type": study.primary_completion_type,
        "last_update_posted": study.last_update_posted,
        "program": study.program,
        "allocation": study.allocation,
        "masking": study.masking,
        "conditions": study.conditions,
        "design_features": study.design_features,
        "diabetes_status": population.diabetes_status if population else "NOT_STATED",
        "drugs": [{"slug": link.drug.slug, "name": link.drug.name, "role": link.role} for link in study.drug_links],
    }


def results_for_study(session: Session, study_id: int) -> list[OutcomeResult]:
    return list(session.scalars(select(OutcomeResult).where(OutcomeResult.study_id == study_id).order_by(OutcomeResult.endpoint_id, OutcomeResult.id)))
