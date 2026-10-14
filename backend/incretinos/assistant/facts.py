"""Evidence facts: the structured record behind every evidence card.

A card handed to the claim verifier (and to a language model) is prose. Prose cannot be checked: a
number in it has no owner. An :class:`EvidenceFact` keeps what the database knows about the record
the card was rendered from - which study, arm, dose, comparator, endpoint, estimand, time point,
population, interval, hypothesis, tier and verification status the value belongs to - so that a
sentence can be bound to exactly one fact and every field of the sentence compared with it.

Nothing here is inferred from text when a column exists. A field the source does not report stays
``None`` and is listed in ``missing_fields``; it is never zero.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engines.findings import Finding, analysis_dto, build_finding, population_phrase
from ..engines.interpretation import classify_drug, interpret_analysis
from ..enums import TIER_LABEL, EvidenceTier, VerificationStatus
from ..models import Drug, EvidenceClaim, OutcomeResult, StatisticalAnalysis, Study, StudyArm
from ..review import policy
from ..vocab import DRUG_ALIASES

# Fact kinds. The first four are the kinds a benchmark case can select; the last two exist because
# the assistant also shows registry record facts and rule-based text, which a draft may cite.
ARM_RESULT = "ARM_RESULT"  # one arm's reported value
BETWEEN_ARM_ANALYSIS = "BETWEEN_ARM_ANALYSIS"  # a reported comparison of two arms
TEXT_CLAIM = "TEXT_CLAIM"  # a curated non-numeric claim
REGULATORY_STATUS = "REGULATORY_STATUS"  # a drug's regulatory status and pharmacologic class
STUDY_RECORD = "STUDY_RECORD"  # registry record facts about a study (no result)
DERIVED_TEXT = "DERIVED_TEXT"  # rule-based interpretation or "not available" statement; no stored record

STRUCTURED_KINDS = frozenset({ARM_RESULT, BETWEEN_ARM_ANALYSIS})

# Categories whose stored value is a change from a baseline: the sign of the value is its direction.
_CHANGE_CATEGORIES = frozenset({"WEIGHT_PCT_CHANGE", "WEIGHT_ABS_CHANGE", "BMI_CHANGE", "WAIST_CHANGE", "HBA1C_CHANGE", "FASTING_GLUCOSE"})

COMBINATION_COMPONENTS = {"eloralintide-tirzepatide": ("eloralintide", "tirzepatide")}

_DOSE_RE = re.compile(r"((?:\d+(?:\.\d+)?\s*(?:/|,|\bor\b|\band\b|-|\bto\b)\s*)*\d+(?:\.\d+)?)\s*-?\s*(mg/kg|mg|mcg|µg|μg)\b(?!\s*/\s*d[lL])", re.IGNORECASE)
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def expand_drugs(slug: str | None) -> list[str]:
    """Component drug slugs of an arm's drug slug (a combination arm names every component)."""
    if not slug:
        return []
    if slug in COMBINATION_COMPONENTS:
        return list(COMBINATION_COMPONENTS[slug])
    return [part for part in slug.split("+") if part]


def doses_in(label: str | None) -> list[float]:
    """Every milligram amount named in an arm label, in order ("5/10/15 mg" names three)."""
    out: list[float] = []
    for match in _DOSE_RE.finditer(label or ""):
        for number in _NUM_RE.findall(match.group(1)):
            value = float(number)
            if value not in out:
                out.append(value)
    return out


def primary_doses(label: str | None) -> list[float]:
    """Dose amounts written outside brackets. In "12 mg LY3437943 (2 mg)" the arm is the 12 mg arm; 2 mg is its starting dose."""
    return doses_in(re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", label or ""))


def arm_doses(label: str | None, drugs: list[str]) -> dict[str, list[float]]:
    """Dose amounts per drug for an arm label. With one drug every amount belongs to it; with several,
    each amount belongs to the drug named closest to it in the label."""
    label = label or ""
    if not drugs:
        return {"": doses_in(label)} if doses_in(label) else {}
    if len(drugs) == 1:
        return {drugs[0]: doses_in(label)}
    low = label.lower()
    positions: dict[str, list[int]] = {}
    for slug in drugs:
        positions[slug] = [m.start() for alias in DRUG_ALIASES.get(slug, [slug]) for m in re.finditer(re.escape(alias), low)]
    out: dict[str, list[float]] = {slug: [] for slug in drugs}
    for match in _DOSE_RE.finditer(label):
        owner, best = None, None
        for slug, where in positions.items():
            for pos in where:
                distance = abs(pos - match.start())
                if best is None or distance < best:
                    owner, best = slug, distance
        for number in _NUM_RE.findall(match.group(1)):
            target = out.setdefault(owner if owner is not None else "", [])
            if float(number) not in target:
                target.append(float(number))
    return out


def unit_class(unit: str | None, param: str | None = None) -> str | None:
    """Normalise the many spellings of a unit to the class a sentence can be compared with."""
    u = (unit or "").lower().strip()
    p = (param or "").lower()
    if "ratio" in p or u == "ratio":
        return "RATIO"
    if re.search(r"participants|patients", u):
        return "PCT_PARTICIPANTS" if re.search(r"%|percent|proportion", u) else "COUNT_PARTICIPANTS"
    if "hba1c" in u:
        return "PCT_HBA1C"
    if "percentage point" in u:
        return "PCT_POINTS"
    if "kg/m" in u or "kilograms per met" in u or "kilogram per square" in u:
        return "KG_M2"
    if re.search(r"\bkg\b|kilogram", u):
        return "KG"
    if "mmhg" in u.replace(" ", "") or "millimet" in u and "mercury" in u:
        return "MMHG"
    if "mg/dl" in u or "per deciliter" in u or "/deciliter" in u:
        return "MG_DL"
    if re.search(r"\bcm\b|centimet", u):
        return "CM"
    if "%" in u or "percent" in u:
        return "PCT"
    return "OTHER" if u else None


def param_family(param: str | None, kind: str) -> str | None:
    """HR | OR | RR | DIFFERENCE for a between-arm estimate; ARM_VALUE for an arm-level value."""
    if kind == ARM_RESULT:
        return "ARM_VALUE"
    p = (param or "").lower()
    if "hazard" in p:
        return "HR"
    if "odds" in p:
        return "OR"
    if re.search(r"risk ratio|relative risk|rate ratio", p):
        return "RR"
    if "ratio" in p:
        return "RATIO"
    if "difference" in p:
        return "DIFFERENCE"
    return None


def weakest_status(statuses: list[str]) -> str | None:
    """The status that says least about a set of records. Human-verified only if every one is."""
    canonical = [policy.canonical_status(s) for s in statuses if s]
    if not canonical:
        return None
    order = [
        VerificationStatus.REJECTED.value,
        VerificationStatus.SOURCE_CHANGED_RECHECK_REQUIRED.value,
        VerificationStatus.PENDING_HUMAN_REVIEW.value,
        VerificationStatus.MACHINE_EXTRACTED.value,
        VerificationStatus.QUOTE_ANCHORED.value,
        VerificationStatus.HUMAN_VERIFIED.value,
    ]
    return min(canonical, key=lambda s: order.index(s) if s in order else -1)


def verification_summary(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """What may be said about how far the records behind a block or card have been checked."""
    statuses = [policy.canonical_status(r.get("verification_status")) for r in records if r.get("verification_status")]
    if not statuses:
        return None
    distinct = list(dict.fromkeys(statuses))
    human = all(policy.is_human_verified(s) for s in statuses)
    labels = [policy.STATUS_LABEL.get(s, s) for s in distinct]
    weakest = weakest_status(statuses)
    return {
        "statuses": distinct,
        "labels": labels,
        "weakest": weakest,
        "label": labels[0] if len(labels) == 1 else "Mixed: " + ", ".join(labels),
        "human_verified": human,
        "needs_attention": any(s in policy.ATTENTION_STATUSES for s in statuses),
        "records": len(statuses),
    }


@dataclass
class EvidenceFact:
    fact_id: str
    kind: str
    text: str  # the card text this fact was rendered as (what a model sees)
    ref_type: str | None = None  # result | analysis | claim | drug | study
    ref_id: int | None = None
    source_id: int | None = None
    source_ids: list[int] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)  # {type, id, verification_status} behind this fact
    # who
    drugs: list[str] = field(default_factory=list)
    arm_label: str | None = None
    arm_doses: dict[str, list[float]] = field(default_factory=dict)
    arm_primary_doses: list[float] = field(default_factory=list)
    comparator_primary_doses: list[float] = field(default_factory=list)
    is_placebo: bool = False
    is_pooled: bool = False
    scope: str | None = None
    comparator_label: str | None = None
    comparator_drugs: list[str] = field(default_factory=list)
    comparator_doses: dict[str, list[float]] = field(default_factory=dict)
    # where
    study_nct: str | None = None
    study_name: str | None = None
    study_acronym: str | None = None
    phase: str | None = None
    diabetes_status: str | None = None
    conditions: list[str] = field(default_factory=list)
    # what
    endpoint_category: str | None = None
    endpoint_title: str | None = None
    endpoint_threshold: float | None = None
    endpoint_role: str | None = None
    multiplicity_controlled: str | None = None
    param_type: str | None = None
    param_family: str | None = None
    direction: str | None = None  # DECREASE | INCREASE | NO_CHANGE, or None when the value is not a change or ratio
    value: float | None = None
    signed_change: float | None = None  # the value as a signed change (a "% reduction" of 20.9 is -20.9)
    value_low: float | None = None
    value_high: float | None = None
    unit: str | None = None
    unit_class: str | None = None
    timepoint_weeks: float | None = None
    baseline_definition: str | None = None
    estimand_label: str | None = None
    estimand_basis: str | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    ci_level: float | None = None
    n: int | None = None
    dispersion_type: str | None = None
    dispersion_value: float | None = None
    # inference
    hypothesis_type: str | None = None
    ni_margin: float | None = None
    ni_met: str | None = None
    superiority_tested: str | None = None
    superiority_met: str | None = None
    p_value_text: str | None = None
    # provenance
    tier: str | None = None
    verification_status: str | None = None  # weakest status among the records behind this fact
    verification_statuses: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    regulatory_status: str | None = None
    context: str = ""  # further wording a faithful sentence may use: titles, labels, population, tier label

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public(self) -> dict[str, Any]:
        """Compact view for API responses: enough to see what a marker points at."""
        keys = (
            "fact_id", "kind", "ref_type", "ref_id", "source_id", "drugs", "arm_label", "comparator_label", "study_nct", "study_name", "endpoint_category",
            "endpoint_title", "endpoint_role", "multiplicity_controlled", "direction", "value", "value_low", "value_high", "unit", "timepoint_weeks",
            "diabetes_status", "estimand_label", "estimand_basis", "ci_low", "ci_high", "ci_level", "n", "hypothesis_type", "ni_margin", "ni_met",
            "superiority_tested", "superiority_met", "p_value_text", "tier", "verification_status", "missing_fields", "regulatory_status",
        )
        return {key: getattr(self, key) for key in keys}

    @property
    def is_structured(self) -> bool:
        return self.kind in STRUCTURED_KINDS

    @property
    def all_human_verified(self) -> bool:
        return bool(self.verification_statuses) and all(policy.is_human_verified(s) for s in self.verification_statuses)

    @property
    def usable(self) -> bool:
        return all(policy.is_usable(s) for s in self.verification_statuses)


def _tier_label(tier: str | None) -> str:
    if tier and tier in EvidenceTier._value2member_map_:
        return TIER_LABEL[EvidenceTier(tier)]
    return tier or ""


def _status_label(status: str | None) -> str:
    return policy.STATUS_LABEL.get(policy.canonical_status(status) or "", "")


def _direction_of_change(category: str, title: str, unit: str | None, value: float | None) -> tuple[str | None, float | None]:
    """(direction, signed change) for an arm-level value, or (None, None) when it is not a change."""
    if value is None:
        return None, None
    low_title, low_unit = (title or "").lower(), (unit or "").lower()
    if re.search(r"participants|patients", low_unit) or re.search(r"(percentage|proportion|number) of (participants|patients)|^\s*(proportion|share|fraction|number) of\b", low_title):
        return None, None
    as_reduction = "reduction" in low_unit or bool(re.search(r"\bweight loss\b|\breduction\b", low_title)) and "change" not in low_title
    if as_reduction:
        signed = -value
    elif category in _CHANGE_CATEGORIES or "change" in low_title or "change" in low_unit:
        signed = value
    else:
        return None, None
    return ("DECREASE" if signed < 0 else "INCREASE" if signed > 0 else "NO_CHANGE"), signed


def fact_from_finding(f: Finding, verification_status: str | None = None) -> EvidenceFact:
    drugs = expand_drugs(f.drug_slug)
    status = verification_status or f.verification_status
    direction, signed = _direction_of_change(f.category, f.endpoint_title, f.unit, f.value)
    context = " ".join(
        str(part)
        for part in (
            f.endpoint_title, f.class_title, f.arm_label, population_phrase(f), " ".join(f.conditions), _tier_label(f.tier), _status_label(status),
            f.baseline_definition, f.analysis_population, f.param_type, f.program, (f.phase or "").replace("PHASE", "Phase "), f.timeframe_text,
            (f.source or {}).get("publisher"), f.dispersion_type,
        )
        if part
    )
    return EvidenceFact(
        fact_id=f"result:{f.result_id}",
        kind=ARM_RESULT,
        text=f.statement,
        ref_type="result",
        ref_id=f.result_id,
        source_id=(f.source or {}).get("id"),
        source_ids=[(f.source or {}).get("id")] if (f.source or {}).get("id") else [],
        records=[{"type": "result", "id": f.result_id, "verification_status": policy.canonical_status(status)}],
        drugs=drugs,
        arm_label=f.arm_label,
        arm_doses=arm_doses(f.arm_label, drugs),
        arm_primary_doses=primary_doses(f.arm_label),
        is_placebo=f.is_placebo,
        is_pooled=f.is_pooled,
        scope=f.scope,
        study_nct=f.nct_id,
        study_name=f.study_name,
        phase=f.phase,
        diabetes_status=f.diabetes_status,
        conditions=list(f.conditions),
        endpoint_category=f.category,
        endpoint_title=f.endpoint_title if not f.class_title else f"{f.endpoint_title} - {f.class_title}",
        endpoint_threshold=f.threshold,
        endpoint_role=f.role,
        multiplicity_controlled=f.multiplicity_controlled,
        param_type=f.param_type,
        param_family="ARM_VALUE",
        direction=direction,
        value=f.value,
        signed_change=signed,
        value_low=f.value_low,
        value_high=f.value_high,
        unit=f.unit,
        unit_class=unit_class(f.unit, None),
        timepoint_weeks=f.timepoint_weeks,
        baseline_definition=f.baseline_definition,
        estimand_label=f.estimand_label,
        estimand_basis=f.estimand_basis,
        ci_low=f.ci_low,
        ci_high=f.ci_high,
        ci_level=f.ci_level,
        n=f.n_analyzed,
        dispersion_type=f.dispersion_type,
        dispersion_value=f.dispersion_value,
        tier=f.tier,
        verification_status=policy.canonical_status(status),
        verification_statuses=[policy.canonical_status(status)] if status else [],
        missing_fields=list(f.missing_fields),
        context=context,
    )


def fact_from_result(session: Session, result: OutcomeResult) -> EvidenceFact:
    return fact_from_finding(build_finding(session, result))


def _arm_side(arm: StudyArm | None, label: str | None) -> tuple[list[str], dict[str, list[float]]]:
    drugs = expand_drugs(arm.drug_slug) if arm is not None else []
    return drugs, arm_doses(label or (arm.label if arm is not None else ""), drugs)


def render_analysis_text(dto: dict[str, Any], with_labels: bool = True) -> str:
    reading = interpret_analysis(dto)
    text = f"{dto['study_name']} — {dto['endpoint_title']}: {reading['observed']}"
    if with_labels and dto["labels"]:
        text += f" [{'; '.join(label['text'] for label in dto['labels'])}]"
    return text


def fact_from_analysis(analysis: StatisticalAnalysis, text: str | None = None) -> EvidenceFact:
    dto = analysis_dto(analysis)
    study: Study = analysis.study
    population = study.population
    t_drugs, t_doses = _arm_side(analysis.treatment_arm, analysis.treatment_label)
    c_drugs, c_doses = _arm_side(analysis.comparator_arm, analysis.comparator_label)
    family = param_family(dto["param_type"], BETWEEN_ARM_ANALYSIS)
    uclass = unit_class(dto["unit"], dto["param_type"])
    estimate = dto["estimate"]
    direction = None
    if estimate is not None:
        null = 1.0 if uclass == "RATIO" else 0.0
        direction = "DECREASE" if estimate < null else "INCREASE" if estimate > null else "NO_CHANGE"
    status = policy.canonical_status(dto["verification_status"])
    diabetes = population.diabetes_status if population else "NOT_STATED"
    population_text = {"NO_T2D": "adults without type 2 diabetes", "T2D": "adults with type 2 diabetes", "MIXED": "adults with or without type 2 diabetes"}.get(diabetes, "")
    context = " ".join(
        str(part)
        for part in (
            dto["endpoint_title"], dto["treatment_label"], dto["comparator_label"], dto["param_type"], dto["method"], dto["ni_margin_text"], dto["comment"],
            population_text, " ".join(study.conditions or []), _tier_label(dto["tier"]), _status_label(status), study.nct_id, study.display_name,
            (study.phase or "").replace("PHASE", "Phase "), study.program, "between-arm comparison treatment comparator estimate",
            analysis.source.publisher if analysis.source is not None else None,
        )
        if part
    )
    return EvidenceFact(
        fact_id=f"analysis:{analysis.id}",
        kind=BETWEEN_ARM_ANALYSIS,
        text=text or render_analysis_text(dto),
        ref_type="analysis",
        ref_id=analysis.id,
        source_id=analysis.source_id,
        source_ids=[analysis.source_id] if analysis.source_id else [],
        records=[{"type": "analysis", "id": analysis.id, "verification_status": status}],
        drugs=t_drugs,
        arm_label=dto["treatment_label"],
        arm_doses=t_doses,
        arm_primary_doses=primary_doses(dto["treatment_label"]),
        comparator_primary_doses=primary_doses(dto["comparator_label"]),
        is_placebo=bool(analysis.treatment_arm.is_placebo) if analysis.treatment_arm is not None else False,
        is_pooled=bool(analysis.treatment_arm.is_pooled) if analysis.treatment_arm is not None else False,
        comparator_label=dto["comparator_label"],
        comparator_drugs=c_drugs,
        comparator_doses=c_doses,
        study_nct=study.nct_id,
        study_name=study.display_name,
        study_acronym=study.acronym,
        phase=study.phase,
        diabetes_status=diabetes,
        conditions=list(study.conditions or []),
        endpoint_category=dto["category"],
        endpoint_title=dto["endpoint_title"],
        endpoint_threshold=analysis.endpoint.threshold,
        endpoint_role=dto["role"],
        multiplicity_controlled=dto["multiplicity_controlled"],
        param_type=dto["param_type"],
        param_family=family,
        direction=direction,
        value=estimate,
        signed_change=estimate if uclass != "RATIO" else None,
        unit=dto["unit"],
        unit_class=uclass,
        timepoint_weeks=dto["timepoint_weeks"],
        estimand_label=dto["estimand_label"],
        estimand_basis=dto["estimand_basis"],
        ci_low=dto["ci_low"],
        ci_high=dto["ci_high"],
        ci_level=dto["ci_level"],
        hypothesis_type=dto["hypothesis_type"],
        ni_margin=dto["ni_margin"],
        ni_met=dto["ni_met"],
        superiority_tested=dto["superiority_tested"],
        superiority_met=dto["superiority_met"],
        p_value_text=dto["p_value_text"],
        tier=dto["tier"],
        verification_status=status,
        verification_statuses=[status] if status else [],
        missing_fields=list(dto["missing_fields"]),
        context=context,
    )


def claim_text(claim: EvidenceClaim) -> str:
    subject = claim.drug.name if claim.drug else (claim.study.display_name if claim.study else "General")
    return f"{subject}: {claim.statement}"


def fact_from_claim(claim: EvidenceClaim, text: str | None = None) -> EvidenceFact:
    status = policy.canonical_status(claim.verification_status)
    study = claim.study
    return EvidenceFact(
        fact_id=f"claim:{claim.id}",
        kind=TEXT_CLAIM,
        text=text or claim_text(claim),
        ref_type="claim",
        ref_id=claim.id,
        source_id=claim.source_id,
        source_ids=[claim.source_id] if claim.source_id else [],
        records=[{"type": "claim", "id": claim.id, "verification_status": status}],
        drugs=expand_drugs(claim.drug.slug) if claim.drug else [],
        study_nct=study.nct_id if study else None,
        study_name=study.display_name if study else None,
        study_acronym=study.acronym if study else None,
        phase=study.phase if study else None,
        tier=claim.tier,
        verification_status=status,
        verification_statuses=[status] if status else [],
        context=" ".join(
            part
            for part in (claim.drug.name if claim.drug else "", study.display_name if study else "", study.nct_id if study else "", _tier_label(claim.tier), _status_label(status),
                         (claim.source.publisher or "") if claim.source is not None else "")
            if part
        ),
    )


def usable_mechanisms(drug: Drug) -> list[Any]:
    """Mechanism rows a reviewer has not rejected."""
    return [m for m in drug.mechanisms if policy.is_usable(m.verification_status)]


def drug_profile_text(drug: Drug) -> str:
    cls = classify_drug([(m.target, m.action) for m in usable_mechanisms(drug)])
    return (
        f"{drug.name} — pharmacologic class: {cls['class_label']} (derived from sourced receptor actions). "
        f"Regulatory status: {drug.regulatory_status.lower()} as of {drug.regulatory_as_of}. {drug.regulatory_summary}"
    )


def fact_from_drug(session: Session, drug: Drug, text: str | None = None) -> EvidenceFact:
    """A drug's regulatory status and pharmacologic class, with the status of every record behind it."""
    mechanisms = usable_mechanisms(drug)
    records = [{"type": "drug_mechanism", "id": m.id, "verification_status": policy.canonical_status(m.verification_status)} for m in mechanisms]
    status_claim = session.scalar(select(EvidenceClaim).where(EvidenceClaim.key == f"{drug.slug}:regulatory-status"))
    if status_claim is not None:
        records.insert(0, {"type": "claim", "id": status_claim.id, "verification_status": policy.canonical_status(status_claim.verification_status)})
    statuses = [r["verification_status"] for r in records if r["verification_status"]]
    source_ids = list(dict.fromkeys(s for s in [drug.regulatory_source_id, *(m.source_id for m in mechanisms)] if s))
    tier = drug.regulatory_source.tier if drug.regulatory_source is not None else (status_claim.tier if status_claim is not None else None)
    return EvidenceFact(
        fact_id=f"drug:{drug.slug}",
        kind=REGULATORY_STATUS,
        text=text or drug_profile_text(drug),
        ref_type="drug",
        ref_id=drug.id,
        source_id=drug.regulatory_source_id,
        source_ids=source_ids,
        records=records,
        drugs=expand_drugs(drug.slug),
        tier=tier,
        verification_status=weakest_status(statuses),
        verification_statuses=statuses,
        regulatory_status=drug.regulatory_status,
        context=" ".join(part for part in (drug.name, drug.code_name or "", " ".join(drug.brand_names or []), _tier_label(tier), " ".join(_status_label(s) for s in dict.fromkeys(statuses))) if part),
    )


def fact_from_study(study: Study, text: str) -> EvidenceFact:
    """Registry record facts about a study. The registry record itself has no review status."""
    population = study.population
    return EvidenceFact(
        fact_id=f"study:{study.nct_id}",
        kind=STUDY_RECORD,
        text=text,
        ref_type="study",
        ref_id=study.id,
        source_id=study.source_id,
        source_ids=[study.source_id] if study.source_id else [],
        records=[{"type": "study", "id": study.id, "verification_status": None}],
        drugs=[slug for link in study.drug_links for slug in expand_drugs(link.drug.slug)],
        study_nct=study.nct_id,
        study_name=study.display_name,
        study_acronym=study.acronym,
        phase=study.phase,
        diabetes_status=population.diabetes_status if population else None,
        conditions=list(study.conditions or []),
        tier=EvidenceTier.REGISTRY_PROTOCOL.value,
        context=" ".join(part for part in (study.display_name, study.nct_id, (study.phase or "").replace("PHASE", "Phase "), " ".join(study.conditions or [])) if part),
    )


def fact_from_text(fact_id: str, text: str, source_ids: list[int] | None = None) -> EvidenceFact:
    """A card with no stored record behind it (rule-based reading, "not available" statement)."""
    return EvidenceFact(fact_id=fact_id, kind=DERIVED_TEXT, text=text, source_ids=list(source_ids or []), source_id=(source_ids or [None])[0])
