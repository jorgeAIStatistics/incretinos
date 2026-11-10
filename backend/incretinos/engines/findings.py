"""A *finding* is one reported result together with everything needed to read it correctly.

The product never hands out a bare number: every result travels with its study, population,
arm, dose, endpoint, estimand, time point, baseline definition, precision, source and
limitations. This module builds that bundle and renders the context-carrying sentence.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..db import session_memo
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
from ..models import (
    AdverseEvent,
    Dose,
    EvidenceSource,
    ExposureEquivalence,
    OutcomeResult,
    StatisticalAnalysis,
    Study,
    StudyArm,
)
from ..review.policy import is_usable
from ..vocab import ARM_ROLE_LABEL, DirectionBasis, is_placebo_arm
from .stats import StatsInputError, ci_from_se

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


#: Curated design features that mean participants were treated, selected or stabilised before the randomization
#: from which (or across which) a change is measured.
LEAD_IN_FEATURE_KEYS = ("randomized_withdrawal", "lead_in", "run_in", "maintenance_after_incretin")

BASELINE_CLASS_LABEL = {
    "STUDY_BASELINE": "study baseline (no lead-in, run-in or withdrawal design on record)",
    "RANDOMIZATION_AFTER_LEAD_IN": "randomization after a lead-in period",
    "STUDY_ENTRY_INCLUDING_LEAD_IN": "study entry, including the lead-in period",
    "LEAD_IN_DESIGN_BASELINE_UNSPECIFIED": "baseline of a trial with a lead-in before randomization (study entry or randomization not specified)",
    "SUBSET_BASELINE": "baseline of a named subset of participants",
}


def has_lead_in_design(design_features: dict[str, Any] | None) -> bool:
    return any((design_features or {}).get(key) for key in LEAD_IN_FEATURE_KEYS)


def baseline_class(design_features: dict[str, Any] | None, baseline_definition: str | None) -> str:
    """Which baseline a change is measured from.

    A change from baseline is only the same quantity in two results when it starts from the same kind of
    baseline. Five kinds are distinguished:

    * ``STUDY_BASELINE`` — the trial has no lead-in, run-in, withdrawal or maintenance design on record.
    * ``RANDOMIZATION_AFTER_LEAD_IN`` — measured from a randomization that followed a lead-in period.
    * ``STUDY_ENTRY_INCLUDING_LEAD_IN`` — measured from study entry, so the change includes the lead-in.
    * ``LEAD_IN_DESIGN_BASELINE_UNSPECIFIED`` — the trial had a lead-in (curated design feature) but the
      stored source only says "baseline". Whether that is study entry or randomization, it is not the
      baseline of a trial without a lead-in, and it is never treated as one.
    * ``SUBSET_BASELINE`` — the baseline of a named subset (for example an extension subset).
    """
    text = (baseline_definition or "").lower()
    if re.search(r"\b(extension|subset|sub-?group|sub-?study)\b", text):
        return "SUBSET_BASELINE"
    post_lead_in_randomization = bool(re.search(r"randomi[sz]ation \(week\s*[1-9]|week\s*[1-9]\d*\s*\(randomi", text))
    names_lead_in = bool(re.search(r"lead[- ]in|run[- ]in|withdrawal", text))
    if not (has_lead_in_design(design_features) or post_lead_in_randomization or names_lead_in):
        return "STUDY_BASELINE"
    if re.search(r"study entry|lead[- ]in start|start of (the )?(lead|run)[- ]in", text):
        return "STUDY_ENTRY_INCLUDING_LEAD_IN"
    if "randomi" in text:
        return "RANDOMIZATION_AFTER_LEAD_IN"
    # "Week 0" is study entry only where the weeks are counted from the start of the lead-in, i.e. in a
    # randomized-withdrawal design. After a lead-in that precedes week 0 it could equally be the randomization.
    if re.search(r"week 0\b", text) and (design_features or {}).get("randomized_withdrawal"):
        return "STUDY_ENTRY_INCLUDING_LEAD_IN"
    return "LEAD_IN_DESIGN_BASELINE_UNSPECIFIED"


# --------------------------------------------------------------------------- arm identity
#
# One randomized group can be stored as several ``StudyArm`` rows: the registry lists it once as an arm
# group and sometimes again as a results group, and every curated label that does not normalise to a
# registry label creates another row. One registration can also hold groups that were never randomized
# against each other: separately randomized sub-studies, a lead-in or extension period, crossover
# sequences. Nothing in the schema says which rows are which, so the rules below derive it from the
# stored descriptors, and say "not resolved" whenever those descriptors cannot decide.

_STRATUM_MARKERS: tuple[tuple[str, str], ...] = (
    (r"(?:_|\(\s*)([a-z]{2,5}\d{1,2})(?![a-z0-9])\s*\)?", "SUBSTUDY {0}"),  # Tirzepatide MTD_GPI1 · Eloralintide (YSA1); not a compound code such as (LY3298176)
    (r"\bpart\s+([a-z])\b", "PART {0}"),
    (r"\bcohort\s+(\d+[a-z]?)\b", "COHORT {0}"),
    (r"\bsequence\s+(\d+)\b", "SEQUENCE {0}"),
    (r"\bperiod\s+(\d+)\b", "PERIOD {0}"),
    (r"\b(first|second|third)\s+treatment\b", "{0} TREATMENT"),
    (r"\blead[- ]in\b", "LEAD-IN"),
    (r"\brun[- ]in\b", "RUN-IN"),
    (r"\bopen[- ]label\b", "OPEN-LABEL"),
    (r"\bextension\b", "EXTENSION"),
)


@lru_cache(maxsize=4096)
def arm_stratum(label: str) -> str:
    """Sub-study, period or sequence markers carried by an arm label ('' = the main randomization).

    Two arms belong to the same randomization only when their markers are identical. A label such as
    'retatrutide 4 mg to MTD' or 'Dose 1 to Placebo' names a switch sequence and is marked as such.
    """
    low = label.lower()
    markers: set[str] = set()
    for pattern, name in _STRATUM_MARKERS:
        for match in re.finditer(pattern, low):
            markers.add(name.format(*(g.upper() for g in match.groups())))
    # "X to placebo", "X to MTD", "Dose 1 to … Dose 2": one treatment followed by another. Not "titrated to 15 mg",
    # "up to 36 mg" or "placebo to match".
    outside_brackets = re.sub(r"\([^)]*\)", " ", low)
    if re.search(r"(?<!titrated)(?<!escalated)(?<!increased)(?<!\bup)(?<!equivalent)\s+to\s+(?:\w+\s+){0,2}?(?:placebo|mtd|maximum tolerated)\b|\bdose \d+\s+to\s+(?:\w+\s+)?dose \d+", outside_brackets):
        markers.add("SWITCH SEQUENCE")
    # A lead-in, run-in or extension period is open-label by construction in these designs: 'open-label tirzepatide
    # lead-in' is the same period as 'Tirzepatide (lead-in)', not a further sub-study.
    if markers & {"LEAD-IN", "RUN-IN", "EXTENSION"}:
        markers.discard("OPEN-LABEL")
    return " + ".join(sorted(markers))


@lru_cache(maxsize=4096)
def arm_dose_tokens(label: str) -> tuple[float, ...]:
    """Every dose-like number in an arm label ('3/6/9 mg' -> 3, 6, 9), ignoring ordinals such as 'Dose 2'."""
    low = re.sub(r"\b(dose level|dose|level|cohort|sequence|period|part)\s+\d+[a-z]?\b", " ", label.lower())
    low = re.sub(r"(?:_|\(\s*)[a-z]{2,5}\d{1,2}(?![a-z0-9])\s*\)?", " ", low)
    numbers = re.findall(r"(?<![a-z0-9.])\d+(?:\.\d+)?(?![a-z0-9]|\.\d)", low)
    return tuple(sorted(float(n) for n in numbers))


RESULTS_GROUP = "RESULTS_GROUP"


def _placebo_like(arm: StudyArm) -> bool:
    return is_placebo_arm(arm) or (arm.arm_type or "").upper() == "PLACEBO_COMPARATOR"


def _is_registered_group(arm: StudyArm) -> bool:
    """A registry arm group (the registry lists each randomized group once), as opposed to a reporting label."""
    return arm.origin == "REGISTRY" and (arm.arm_type or "") not in ("RESULTS_GROUP", "CURATED") and not arm.is_pooled


def _equivalence_table(session: Session) -> tuple[tuple[str, float, str, float], ...]:
    """Every documented exposure equivalence as (formulation, amount, other formulation, other amount), both ways round."""
    doses = (joinedload(ExposureEquivalence.dose_a).joinedload(Dose.formulation), joinedload(ExposureEquivalence.dose_b).joinedload(Dose.formulation))
    table: list[tuple[str, float, str, float]] = []
    for row in session.scalars(select(ExposureEquivalence).options(*doses).order_by(ExposureEquivalence.id)):
        if row.basis == "NOT_DOCUMENTED":
            continue
        for src, dst in ((row.dose_a, row.dose_b), (row.dose_b, row.dose_a)):
            table.append((src.formulation.key, src.amount, dst.formulation.key, dst.amount))
    return tuple(table)


def _documented_equivalent(session: Session, formulation_a: str, dose_a: float, formulation_b: str) -> float | None:
    """The documented dose of ``formulation_b`` equivalent to ``dose_a`` of ``formulation_a`` (never calculated).

    Arm identity asks this for every arm row of every finding, so the handful of equivalence rows are read once
    per transaction (``db.session_memo``) instead of once per finding. A rejected equivalence is not among them.
    """
    for source_formulation, source_amount, target_formulation, target_amount in session_memo(session, "documented_equivalents", _equivalence_table):
        if source_formulation == formulation_a and source_amount == dose_a and target_formulation == formulation_b:
            return target_amount
    return None


def _dose_relation(session: Session, a: StudyArm, b: StudyArm) -> str:
    """SAME, DIFFERENT or UNKNOWN for two arms of the same drug, using documented equivalences across formulations."""
    if a.dose_amount is None or b.dose_amount is None:
        return "UNKNOWN"
    if a.formulation_key and b.formulation_key and a.formulation_key != b.formulation_key:
        equivalent = _documented_equivalent(session, a.formulation_key, a.dose_amount, b.formulation_key)
        if equivalent is None:
            return "UNKNOWN"
        return "SAME" if equivalent == b.dose_amount else "DIFFERENT"
    return "SAME" if a.dose_amount == b.dose_amount else "DIFFERENT"


def _same_treatment(a: StudyArm, b: StudyArm) -> bool | None:
    """True / False when the two rows name the same / a different treatment; None when that cannot be told."""
    pa, pb = _placebo_like(a), _placebo_like(b)
    if pa or pb:
        return pa and pb
    if not a.drug_slug or not b.drug_slug:
        return None
    return a.drug_slug == b.drug_slug


@dataclass(frozen=True)
class ArmIdentity:
    arm_id: int
    group_key: str  # equal keys = one randomized group, as far as the stored records show
    resolved: bool  # the row is, or maps to exactly one, registered arm group
    registered_arm_id: int | None
    stratum: str
    kind: str  # REGISTERED_GROUP | MAPPED_TO_REGISTERED_GROUP | POOLED | UNMAPPED_LABEL
    basis: str


def resolve_arm(session: Session, arm: StudyArm) -> ArmIdentity:
    """Canonical identity of an arm row: which registered arm group it is, when the stored descriptors decide that.

    A registry arm group is its own identity. A results-group or curated row maps to a registered group of
    the same randomization stratum when exactly one is identified by an identical dose description (or by a
    documented exposure equivalence across formulations), or when the trial registers only one group of that
    treatment. Otherwise the row stays unmapped: it is then neither assumed to be a separate arm nor merged.
    """
    stratum = arm_stratum(arm.label)
    if arm.is_pooled:
        return ArmIdentity(arm.id, f"pooled:{arm.id}", False, None, stratum, "POOLED", "Pooled group, not a single randomized arm.")
    if _is_registered_group(arm):
        return ArmIdentity(arm.id, f"arm:{arm.id}", True, arm.id, stratum, "REGISTERED_GROUP", "Arm group as registered.")
    registered = [p for p in arm.study.arms if p.id != arm.id and _is_registered_group(p) and arm_stratum(p.label) == stratum and _same_treatment(arm, p)]
    compatible = [p for p in registered if _placebo_like(arm) or _dose_relation(session, arm, p) != "DIFFERENT"]
    tokens = arm_dose_tokens(arm.label)

    def identified(p: StudyArm) -> bool:
        if _placebo_like(arm):
            return True
        if arm.formulation_key and p.formulation_key and arm.formulation_key != p.formulation_key:
            return _dose_relation(session, arm, p) == "SAME"  # documented exposure equivalence
        return bool(tokens) and tokens == arm_dose_tokens(p.label)

    strong = [p for p in compatible if identified(p)]
    target = None
    basis = ""
    if len(strong) == 1:
        target = strong[0]
        basis = f"Same group as the registered arm '{target.label}' (same treatment and dose description)."
        if _placebo_like(arm):
            basis = f"Same group as the registered arm '{target.label}': the only placebo group registered in this randomization."
        if arm.formulation_key and target.formulation_key and arm.formulation_key != target.formulation_key:
            basis = f"Same group as the registered arm '{target.label}': the doses are a documented exposure-equivalent pair of two formulations."
    elif len(registered) == 1 and len(compatible) == 1:
        target = compatible[0]
        basis = f"Same group as the registered arm '{target.label}': the trial registers only one group of this treatment."
    if target is not None:
        return ArmIdentity(arm.id, f"arm:{target.id}", True, target.id, stratum, "MAPPED_TO_REGISTERED_GROUP", basis)
    reason = "no registered arm group of this treatment is on record" if not registered else f"{len(compatible) or len(registered)} registered arm groups could be this one"
    return ArmIdentity(arm.id, f"label:{arm.id}", False, None, stratum, "UNMAPPED_LABEL", f"Reporting label not mapped to a registered arm group ({reason}).")


def arm_relation(session: Session, a: StudyArm | None, b: StudyArm | None) -> tuple[str, str]:
    """How two arm rows of ONE study relate: (code, explanation).

    * ``SAME_ARM`` — the same row, or two rows of one randomized group (two reports of one arm).
    * ``DIFFERENT_STRATUM`` — different sub-studies, periods or sequences: never randomized against each other.
    * ``DISTINCT_ARMS`` — different groups of the same randomization.
    * ``UNRESOLVED`` — the stored descriptors cannot tell whether these are two arms or one.
    """
    if a is None or b is None:
        return "UNRESOLVED", "At least one finding is not attached to a single arm."
    if a.id == b.id:
        return "SAME_ARM", "Both findings are stored on the same arm."
    ia, ib = resolve_arm(session, a), resolve_arm(session, b)
    if ia.group_key == ib.group_key:
        bases = list(dict.fromkeys(i.basis for i in (ia, ib) if i.kind != "REGISTERED_GROUP"))
        return "SAME_ARM", f"'{a.label}' and '{b.label}' are two records of one randomized group. {' '.join(bases)}"
    if ia.stratum != ib.stratum:
        return "DIFFERENT_STRATUM", (
            f"The arm labels carry different sub-study or period markers ('{ia.stratum or 'main randomization'}' versus '{ib.stratum or 'main randomization'}'). "
            "Groups of different sub-studies, periods or sequences of one registration were not randomized against each other."
        )
    if ia.resolved and ib.resolved:
        return "DISTINCT_ARMS", "Two different registered arm groups of the same randomization."
    if not ia.resolved and not ib.resolved and RESULTS_GROUP in ((a.arm_type or ""), (b.arm_type or "")):
        # The registry lists each randomized group once as an arm group, and its results groups normally map onto them. Results
        # groups that map onto none are the registry's own sign that its reporting groups are not its arms: in crossover or
        # sequential-cohort designs they are successive dose periods of the same participants, not parallel randomized arms.
        return "UNRESOLVED", (
            f"Neither '{a.label}' nor '{b.label}' maps to a registered arm group, and the registry reports at least one of them as a results group, "
            "so the stored records do not establish that they are two arms randomized against each other (they may, for example, be successive "
            "dose periods of the same participants)."
        )
    same = _same_treatment(a, b)
    if same is False:
        return "DISTINCT_ARMS", "Different treatments in the same randomization."
    if same and not _placebo_like(a) and _dose_relation(session, a, b) == "DIFFERENT":
        return "DISTINCT_ARMS", "The same drug at different doses in the same randomization."
    return "UNRESOLVED", (
        f"The stored arm records do not establish whether '{a.label}' and '{b.label}' are two different randomized arms or two labels for the same arm "
        "(same treatment; dose or arm mapping not determinable)."
    )


# --------------------------------------------------------------------------- endpoint definition
#
# An endpoint category ("blood pressure", "lipids", "body composition") is a family, not a definition.
# Two results measure the same thing only when the measurement, the responder criterion, the unit and the
# baseline agree. The definition is read from the stored title, class title, threshold and unit; it never
# changes a stored value.

_UNIT_FAMILIES: tuple[tuple[str, str], ...] = (
    (r"percent(age)? of participants|% of participants|% of patients|percent of patients", "PERCENT_OF_PARTICIPANTS"),
    (r"^participants$|number of participants", "PARTICIPANT_COUNT"),
    (r"hba1c", "HBA1C_PERCENT"),
    (r"%\s*\.?\s*min", "PERCENT_MIN_PER_HOUR"),
    (r"percent(age)? change|^%$|^percent$|% reduction|percentage points", "PERCENT"),
    (r"kg/m|per (square )?met|per met(er|re) squared?", "KG_PER_M2"),
    (r"mg/kg", "MG_PER_KG"),
    (r"kilogram|\bkg\b", "KG"),
    (r"centimet|\bcms?\b", "CM"),
    (r"mm\s?hg|of mercury", "MMHG"),
    (r"mg/dl|milligrams?\s*(per|/)\s*decilit", "MG_PER_DL"),
    (r"mg/l\b|milligrams?\s*(per|/)\s*lit", "MG_PER_L"),
    (r"ng\s?\*\s?h|nanograms?\s?\*\s?hours?", "NG_H_PER_ML"),
    (r"ng/ml|nanograms?\s*(per|/)\s*millilit", "NG_PER_ML"),
    (r"t[- ]?score", "T_SCORE"),
    (r"score on a scale|units on a scale", "SCALE_SCORE"),
    (r"events per hour", "EVENTS_PER_HOUR"),
    (r"episodes/participant", "EPISODES_PER_PARTICIPANT_YEAR"),
    (r"iu/day|units per day", "IU_PER_DAY"),
    (r"^weeks?$", "WEEKS"),
    (r"1\.73", "ML_PER_MIN_PER_1_73M2"),
    (r"^ratio$", "RATIO"),
)


@lru_cache(maxsize=1024)
def unit_family(unit: str | None) -> str | None:
    """Normalised unit: spelling variants of one unit collapse, different units never do. None = not recorded."""
    if unit is None or not unit.strip():
        return None
    low = unit.strip().lower()
    for pattern, family in _UNIT_FAMILIES:
        if re.search(pattern, low):
            return family
    return "OTHER:" + re.sub(r"[^a-z0-9%/*.]+", " ", low).strip()


# (concept, pattern). Order matters only for readability; every matching concept is kept.
_MEASURE_CONCEPTS: tuple[tuple[str, str], ...] = (
    ("systolic blood pressure", r"systolic|\bsbp\b"),
    ("diastolic blood pressure", r"diastolic|\bdbp\b"),
    ("VLDL cholesterol", r"very[- ]low[- ]density|\bvldl\b"),
    ("non-HDL cholesterol", r"non[- ]?hdl|non[- ]?high[- ]density"),
    ("LDL cholesterol", r"(?<!very )(?<!very-)low[- ]density|(?<!v)\bldl\b|\bldl-c\b"),
    ("HDL cholesterol", r"(?<!non )(?<!non-)high[- ]density|(?<!non-)(?<!non )\bhdl\b"),
    ("total cholesterol", r"total (serum )?cholesterol"),
    ("triglycerides", r"triglycerid"),
    ("free fatty acids", r"free fatty|\bffas?\b"),
    ("visceral adipose tissue", r"visceral|\bvat\b"),
    ("subcutaneous adipose tissue", r"subcutaneous adipose|\bsat\b"),
    ("trunk fat", r"trunk fat"),
    ("appendicular lean mass", r"appendicular"),
    ("lean mass", r"\blean\b|fat[- ]free"),
    ("fat loss index", r"fat loss index|\bfli\b|weight loss due to fat"),
    ("fat mass", r"fat mass|body fat\b"),
    ("Cmax", r"\bcmax|maximum (observed )?concentration"),
    ("AUC", r"\bauc\b|area under"),
    ("average concentration", r"\bcav\b|average steady[- ]state plasma concentration"),
    ("hsCRP", r"c[- ]reactive|\bhs-?crp\b"),
    ("eGFR", r"\begfr\b|glomerular filtration"),
    ("UACR", r"\buacr\b|albumin[- ]to[- ]creatinine"),
    ("nephropathy", r"nephropathy"),
    ("MACE-3", r"mace-?3"),
    ("MACE-4", r"mace-?4"),
    ("myocardial infarction", r"myocardial infarction|\bmi\b"),
    ("stroke", r"\bstroke\b"),
    ("cardiovascular death", r"cardiovascular death|\bcv deaths?\b|death from (cv|cardiovascular)"),
    ("heart failure", r"heart failure"),
    ("revascularization", r"revasculari[sz]ation"),
    ("unstable angina", r"unstable angina"),
    ("microvascular composite", r"microvascular"),
    ("all-cause death", r"all[- ]cause (death|mortality)|died \(any cause\)|death \(any cause\)"),
    ("type 2 diabetes onset", r"onset of type 2 diabetes"),
    ("apnea-hypopnea index", r"apnea[- ]hypopnea|\bahi\b"),
    ("hypoxic burden", r"hypoxic burden"),
    ("Epworth sleepiness scale", r"epworth|\bess\b"),
    ("self-monitored blood glucose", r"self[- ]monitored|\bsmbg\b"),
    ("fasting insulin", r"fasting insulin"),
    ("insulin dose", r"insulin (glargine )?dose"),
    ("hypoglycaemia", r"hypoglyc"),
    ("hyperglycaemia perception", r"hyperglyc"),
    ("fasting glucose", r"fasting (serum |blood |plasma )?glucose|\bfsg\b|\bfbg\b"),
    ("HbA1c", r"hba1c|hemoglobin a1c|haemoglobin a1c|glycated h(a)?emoglobin|\ba1c\b"),
    ("waist-to-height ratio", r"waist[- ]to[- ]height|\bwh?thr\b"),
    ("waist circumference", r"waist circumference"),
    ("BMI", r"\bbmi\b|body mass index"),
    ("weight maintained after loss", r"maintain[^.]{0,40}(body )?weight lost|returning to[^.]{0,30}baseline weight"),
    ("body weight", r"body[- ]weight|weight (loss|reduction)|\bweight\b"),
    ("SF-36", r"sf[- ]?36|short form[- ]?(survey[- ]?)?36"),
    ("IWQOL-Lite-CT", r"iwqol|impact of weight on quality of life"),
    ("PROMIS", r"\bpromis\b"),
    ("DTSQ", r"\bdtsqc?\b|diabetes treatment satisfaction"),
    ("physical component score", r"physical[- ]component"),
    ("mental component score", r"mental[- ]component"),
    ("physical function", r"physical function"),
    ("total score", r"total score"),
    ("sleep disturbance", r"sleep disturbance"),
    ("sleep-related impairment", r"sleep[- ]related impairment"),
)
_METHOD_CONCEPTS: tuple[tuple[str, str], ...] = (("DXA", r"\bdxa\b|dual[- ]energy"), ("BIA", r"\bbia\b|bioelectrical"), ("MRI", r"\bmri\b|magnetic resonance"))

_THRESHOLD = re.compile(
    r"(≥|>=|=>|≤|<=|>|<|at least|greater than or equal to|less than or equal to|greater than|less than)"
    r"(?:\s*\(\s*[≥≤<>]=?\s*\))?\s*(\d+(?:\.\d+)?)\s*(%|percent\b|kg\b|cm\b)?",
    re.IGNORECASE,
)
_THRESHOLD_OP = {"≥": ">=", ">=": ">=", "=>": ">=", "at least": ">=", "greater than or equal to": ">=", "≤": "<=", "<=": "<=", "less than or equal to": "<=", ">": ">", "greater than": ">", "<": "<", "less than": "<"}


def _thresholds(text: str | None) -> tuple[str, ...]:
    found = []
    for op, number, unit in _THRESHOLD.findall(text or ""):
        suffix = "%" if unit.lower() in ("%", "percent") else unit.lower()
        found.append(f"{_THRESHOLD_OP[op.lower()]}{float(number):g}{suffix}")
    return tuple(found)


def _concepts(text: str, vocabulary: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    low = text.lower()
    return tuple(sorted({name for name, pattern in vocabulary if re.search(pattern, low)}))


_SUBSET = re.compile(r"participants with prediabetes[^)]*|extension subset|\bsub-?group\b[^)]*|\bsubset\b[^)]*|in patients with [^)]*", re.IGNORECASE)


@dataclass(frozen=True)
class EndpointDefinition:
    category: str
    measure: tuple[str, ...]  # recognised measurement concepts (analyte, instrument, compartment); () = none recognised
    method: tuple[str, ...]  # measurement method named in the title (DXA, BIA, MRI)
    threshold: tuple[str, ...]  # responder criterion, e.g. ('>=5%',)
    threshold_ambiguous: bool  # the title lists several criteria and no class says which one this value is
    unit_family: str | None
    subset: str | None  # analysis subset named in the title
    text_key: str  # normalised title + class, for an exact-text comparison when nothing is recognised

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@lru_cache(maxsize=8192)
def endpoint_definition(category: str, title: str, class_title: str | None, stored_threshold: float | None, unit: str | None) -> EndpointDefinition:
    """What a result measures, read from its stored endpoint title, class title, threshold and unit."""
    cls = (class_title or "").strip()
    class_concepts = _concepts(cls, _MEASURE_CONCEPTS) if cls else ()
    title_concepts = _concepts(title, _MEASURE_CONCEPTS)
    # A class title names the component this value belongs to ("DBP" of "Change in SBP, DBP"). When it names a
    # recognised measurement it replaces the title's list; when it names something else (a category, a time
    # point, a threshold) it is added to the title's measurement, because classes of one measure differ.
    if class_concepts:
        instruments = {"SF-36", "IWQOL-Lite-CT", "PROMIS", "DTSQ"} & set(title_concepts)
        measure = tuple(sorted(set(class_concepts) | instruments))
    else:
        measure = title_concepts
    class_thresholds = _thresholds(cls)
    title_thresholds = _thresholds(title)
    ambiguous = False
    if class_thresholds:
        threshold = class_thresholds
    elif len(title_thresholds) > 1 and not cls:
        threshold, ambiguous = title_thresholds, len(set(title_thresholds)) > 1
    elif title_thresholds:
        threshold = title_thresholds
    elif stored_threshold is not None:
        threshold = (f">={stored_threshold:g}%",) if category == EndpointCategory.WEIGHT_RESPONDER.value else (f"{stored_threshold:g}",)
    else:
        threshold = ()
    subset = _SUBSET.search(title)
    text_key = re.sub(r"[^a-z0-9%<>=≥≤.]+", " ", f"{title} | {cls}".lower()).strip()
    return EndpointDefinition(
        category=category,
        measure=measure,
        method=_concepts(f"{title} {cls}", _METHOD_CONCEPTS),
        threshold=threshold,
        threshold_ambiguous=ambiguous,
        unit_family=unit_family(unit),
        subset=re.sub(r"\s+", " ", subset.group(0).strip().lower()) if subset else None,
        text_key=text_key,
    )


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
    # arm identity (derived at read time; see ``resolve_arm``)
    arm_origin: str | None = None
    arm_group_key: str | None = None  # equal keys = one randomized group
    arm_group_resolved: bool = False
    arm_group_basis: str | None = None
    randomization_stratum: str = ""  # sub-study / period markers of the arm label; '' = main randomization
    labels: list[dict[str, str]] = field(default_factory=list)
    statement: str = ""
    #: The value exactly as the source printed it (for example '-25.0'), so its reported precision can be shown. None when not stored.
    value_text: str | None = None
    #: The arm's stored role in its trial (vocab.ArmRole: PLACEBO, ACTIVE_COMPARATOR, STUDY_DRUG, SEQUENCE, UNCLASSIFIED)
    #: and its label; None when the arm has no stored role (a database not re-ingested since roles were stored).
    arm_role: str | None = None
    arm_role_label: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["baseline_class"] = self.baseline_class
        return data

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
    def endpoint_definition(self) -> EndpointDefinition:
        return endpoint_definition(self.category, self.endpoint_title, self.class_title, self.threshold, self.unit)

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
    elif f.estimand_basis == EstimandBasis.INFERRED_BY_CURATOR.value:
        add("ESTIMAND_CURATOR", "Estimand per curator", "warn", "Label attributed by the curator; no checked quote from this source names the estimand.")

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
    lead_in = (f.design_features or {}).get("lead_in")
    lead_in_text = f" ({lead_in})" if isinstance(lead_in, str) else ""
    if f.baseline_class == "RANDOMIZATION_AFTER_LEAD_IN":
        add("WITHDRAWAL_BASELINE", "Baseline = randomization after lead-in", "warn", f"Change is measured from randomization following a lead-in period{lead_in_text}, not from study entry.")
    elif f.baseline_class == "STUDY_ENTRY_INCLUDING_LEAD_IN":
        add("INCLUDES_LEAD_IN", "Baseline = study entry (includes lead-in)", "warn", f"Change is measured from study entry and therefore includes the lead-in period{lead_in_text}.")
    elif f.baseline_class == "LEAD_IN_DESIGN_BASELINE_UNSPECIFIED":
        add("LEAD_IN_DESIGN", "Lead-in before randomization", "warn",
            f"This trial had a lead-in before randomization{lead_in_text}. The stored source says only 'baseline': whether that is study entry or randomization, "
            "the change is not the same quantity as a change from baseline in a trial without a lead-in.")
    elif f.baseline_class == "SUBSET_BASELINE":
        add("SUBSET_BASELINE", "Baseline of a subset", "warn", "Change is measured in, and from the baseline of, a named subset of participants.")
    if f.is_body_composition_lean:
        add("LEAN_MASS", "Lean mass ≠ muscle", "warn", LEAN_MASS_CAVEAT)
    if f.phase and "PHASE2" in f.phase and "PHASE3" not in f.phase:
        add("PHASE2", "Phase 2", "info", "Dose-finding study; smaller arms and wider uncertainty than Phase 3.")
    if f.verification_status in (VerificationStatus.PENDING_HUMAN_REVIEW.value, VerificationStatus.CURATED_PENDING_REVIEW.value):
        add("PENDING_REVIEW", "Pending human review", "muted", "Curated value whose quote could not be anchored automatically to stored source text.")
    elif f.verification_status == VerificationStatus.QUOTE_ANCHORED.value:
        add("QUOTE_ANCHORED", "Quote-anchored", "ok", "The locator quote was matched verbatim against the source text.")
    elif f.verification_status == VerificationStatus.MACHINE_EXTRACTED.value:
        add("MACHINE_EXTRACTED", "Machine-extracted", "ok", "Parsed directly from the structured registry payload whose hash is stored.")
    elif f.verification_status == VerificationStatus.HUMAN_VERIFIED.value:
        add("HUMAN_VERIFIED", "Human-verified", "ok", "A reviewer approved this exact evidence version against its source; the decision is in the review history.")
    elif f.verification_status == VerificationStatus.SOURCE_CHANGED_RECHECK_REQUIRED.value:
        add("RECHECK_REQUIRED", "Changed since review", "warn", "A reviewer decided on an earlier version; the stored evidence or its source changed afterwards and must be rechecked.")
    elif f.verification_status == VerificationStatus.REJECTED.value:
        add("REJECTED", "Rejected in review", "critical", "A reviewer rejected this evidence. It is shown for inspection only and is excluded from comparisons, summaries and reports.")
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
    elif f.category == EndpointCategory.WEIGHT_RESPONDER.value and f.class_title:
        # An outcome that lists several thresholds has none of its own: the class says which one this value is.
        endpoint = f"{f.endpoint_title} — {f.class_title}"
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
    elif f.estimand_basis == EstimandBasis.INFERRED_BY_CURATOR.value and f.estimand_label != EstimandLabel.NOT_STATED.value:
        estimand += " (curator's attribution; no checked source quote names it)"
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
        is_placebo=is_placebo_arm(arm),
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
        value_text=result.value_text,
        arm_role=arm.arm_role if arm is not None and arm.arm_role in ARM_ROLE_LABEL else None,
        arm_role_label=ARM_ROLE_LABEL.get(arm.arm_role) if arm is not None and arm.arm_role else None,
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
    if arm is not None:
        identity = resolve_arm(session, arm)
        finding.arm_origin = arm.origin
        finding.arm_group_key = identity.group_key
        finding.arm_group_resolved = identity.resolved
        finding.arm_group_basis = identity.basis
        finding.randomization_stratum = identity.stratum
    finding.labels = build_labels(finding)
    finding.statement = render_statement(finding)
    return finding


def derived_interval(f: Finding) -> dict[str, Any] | None:
    """Interval derived from a reported standard error — the ONE place this is computed.

    Returned separately from any interval the source reported and labelled INFERENCE. Nothing is derived when
    the source reported an interval, when no standard error is stored, or when the value is a geometric mean
    (the stored record does not say on which scale that standard error is expressed, so a symmetric interval
    could be wrong).
    """
    if f.ci_low is not None or f.value is None or f.dispersion_value is None:
        return None
    if not f.dispersion_type or "standard error" not in f.dispersion_type.lower():
        return None
    if "GEOMETRIC" in (f.param_type or "").upper():
        return None
    try:
        derived = ci_from_se(f.value, f.dispersion_value)
    except StatsInputError:
        return None
    derived["display"] = f"Derived 95% interval {_fmt(derived['ci_low'])} to {_fmt(derived['ci_high'])} (calculated here from the reported standard error; not reported by the source)."
    return derived


def get_finding(session: Session, result_id: int) -> Finding | None:
    result = session.get(OutcomeResult, result_id)
    return build_finding(session, result) if result else None


# --------------------------------------------------------------------------- reading a stored between-arm analysis
#
# A stored analysis is interpreted only as far as its own fields allow. The null value comes from the stated
# parameter type and unit (0 for a difference, 1 for a ratio); the confidence level and sidedness are used as
# stated; the direction ("treatment minus comparator") counts as established only when the curated source row
# states it or the stored arm-level values confirm it.

_RATIO_PARAMETER = re.compile(r"\bratio\b|\bodds\b|\bhazard\b|\brelative risk\b", re.IGNORECASE)
_DIFFERENCE_PARAMETER = re.compile(r"difference|\bdiff\b", re.IGNORECASE)
REGISTRY_ORDER_NOTE = "groups listed in registry order"


def effect_scale(param_type: str | None, unit: str | None) -> dict[str, Any]:
    """Scale of an estimate (DIFFERENCE, RATIO or NOT_ESTABLISHED) and the null value that goes with it."""
    names_ratio = bool(param_type and _RATIO_PARAMETER.search(param_type))
    names_difference = bool(param_type and _DIFFERENCE_PARAMETER.search(param_type))
    unit_is_ratio = unit_family(unit) == "RATIO"
    if names_ratio and names_difference:
        return {"scale": "NOT_ESTABLISHED", "null_value": None, "basis": f"The parameter type '{param_type}' names both a ratio and a difference."}
    if names_difference and unit_is_ratio:
        return {"scale": "NOT_ESTABLISHED", "null_value": None,
                "basis": f"The stored fields disagree: the parameter type is '{param_type}' but the unit is '{unit}'. Neither 0 nor 1 can be taken as the value of no difference."}
    if names_ratio or unit_is_ratio:
        basis = f"parameter type '{param_type}'" if names_ratio else f"unit '{unit}'"
        return {"scale": "RATIO", "null_value": 1.0, "basis": f"Ratio scale stated by the {basis}; no difference corresponds to 1."}
    if names_difference:
        return {"scale": "DIFFERENCE", "null_value": 0.0, "basis": f"Difference scale stated by the parameter type '{param_type}'; no difference corresponds to 0."}
    return {"scale": "NOT_ESTABLISHED", "null_value": None,
            "basis": f"The parameter type {repr(param_type) if param_type else 'is not stated and'} does not say whether the estimate is a difference or a ratio."}


def ci_level_status(ci_low: float | None, ci_high: float | None, ci_level: float | None) -> str:
    """NOT_REPORTED (no interval), LEVEL_NOT_REPORTED, INVALID_LEVEL (not a percentage between 50 and 100) or VALID."""
    if ci_low is None or ci_high is None:
        return "NOT_REPORTED"
    if ci_level is None:
        return "LEVEL_NOT_REPORTED"
    return "VALID" if 50 <= ci_level < 100 else "INVALID_LEVEL"


_P_VALUE = re.compile(r"\s*(?:p\s*)?(<=|>=|≤|≥|<|>|=)?\s*(\d*\.\d+|0|1)\s*", re.IGNORECASE)


def p_value_range(text: str | None) -> tuple[float, float] | None:
    """The range a reported p-value text allows: '<0.001' -> (0, 0.001); '0.752' -> (0.7515, 0.7525). None if not one plain p-value."""
    match = _P_VALUE.fullmatch(text or "")
    if match is None:
        return None
    op, number = match.group(1) or "=", match.group(2)
    value = float(number)
    if not 0 <= value <= 1:
        return None
    if op in ("<", "<=", "≤"):
        return (0.0, value)
    if op in (">", ">=", "≥"):
        return (value, 1.0)
    half_unit = 0.5 * 10 ** -(len(number.split(".")[1]) if "." in number else 0)
    return (max(0.0, value - half_unit), min(1.0, value + half_unit))


def interval_p_value_consistency(estimate: float | None, ci_low: float | None, ci_high: float | None, ci_level: float | None, level_status: str,
                                 ci_sides: str | None, scale: str, null_value: float | None, p_value_text: str | None, hypothesis_type: str | None) -> dict[str, Any]:
    """Whether a stored analysis agrees with itself: its interval, its estimate and its p-value.

    * ``P_VALUE_DISAGREES_WITH_INTERVAL`` — the two-sided interval excludes the null value while the reported p-value is at or above
      1 − level (or the interval strictly includes the null value while the p-value is below it). Rounding is allowed for: a p-value
      printed as 0.05 may be anything from 0.045 to 0.055, and an interval limit equal to the null value is not read either way.
    * ``ESTIMATE_OFF_CENTRE`` — the estimate lies outside its interval, or far from the middle of it on the scale on which such
      intervals are symmetric (the difference scale, or the log scale for a ratio): a sign or transcription error is likely.
    * ``CONSISTENT`` — checked, no disagreement; ``NOT_CHECKABLE`` — the stored fields do not allow the check.

    Nothing is corrected: an inconsistent record is labelled, and no conclusion is drawn from it.
    """
    if estimate is None or ci_low is None or ci_high is None or null_value is None or level_status != "VALID" or scale not in ("DIFFERENCE", "RATIO"):
        return {"status": "NOT_CHECKABLE", "detail": "The interval, its level, the estimate or the scale is not established, so interval and p-value cannot be checked against each other."}
    lo, hi = min(ci_low, ci_high), max(ci_low, ci_high)
    problems: list[str] = []
    off = False
    if not lo <= estimate <= hi:
        off = True
        problems.append(f"the estimate {_fmt(estimate)} lies outside its own interval {_fmt(ci_low)} to {_fmt(ci_high)}")
    elif hi > lo:
        if scale == "DIFFERENCE":
            centre, half_width, point = (lo + hi) / 2, (hi - lo) / 2, estimate
        elif lo > 0 and estimate > 0:
            centre, half_width, point = (math.log(lo) + math.log(hi)) / 2, (math.log(hi) - math.log(lo)) / 2, math.log(estimate)
        else:
            centre = half_width = point = None
        if centre is not None and half_width is not None and point is not None and abs(point - centre) > half_width / 2:
            off = True
            problems.append(f"the estimate {_fmt(estimate)} is far from the middle of its interval {_fmt(ci_low)} to {_fmt(ci_high)}"
                            + (" on the log scale" if scale == "RATIO" else "") + ", which a reported interval of this kind does not allow")
    disagrees = False
    p_range = p_value_range(p_value_text)
    if p_range is not None and hypothesis_type != "NON_INFERIORITY" and (ci_sides or "").upper() != "ONE_SIDED" and ci_level is not None:
        alpha = 1 - ci_level / 100
        excludes = not (lo <= null_value <= hi)
        strictly_includes = lo < null_value < hi
        if excludes and p_range[0] >= alpha:
            disagrees = True
            problems.append(f"the {_fmt(ci_level, 2)}% interval excludes {_fmt(null_value)} but the reported p-value ({p_value_text.strip()}) is not below {_fmt(alpha, 3)}")
        elif strictly_includes and p_range[1] < alpha:
            disagrees = True
            problems.append(f"the {_fmt(ci_level, 2)}% interval includes {_fmt(null_value)} but the reported p-value ({p_value_text.strip()}) is below {_fmt(alpha, 3)}")
    if not problems:
        checked = "interval and estimate" + (" and p-value" if p_range is not None and hypothesis_type != "NON_INFERIORITY" else "")
        return {"status": "CONSISTENT", "detail": f"Checked: the stored {checked} agree with each other."}
    status = "P_VALUE_DISAGREES_WITH_INTERVAL" if disagrees else "ESTIMATE_OFF_CENTRE"
    if disagrees and off:
        status = "P_VALUE_AND_ESTIMATE_DISAGREE_WITH_INTERVAL"
    return {"status": status, "detail": "The stored record is internally inconsistent: " + "; ".join(problems)
            + ". No statement about a between-arm difference is drawn from it; the source record needs checking."}


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def analysis_direction(a: StatisticalAnalysis, scale: str) -> dict[str, Any]:
    """Whether 'treatment versus comparator' is established for a stored analysis, and on what basis."""
    out: dict[str, Any] = {"treatment": a.treatment_label, "comparator": a.comparator_label, "established": False, "arm_values_check": "NOT_CHECKABLE",
                           "treatment_arm_value": None, "comparator_arm_value": None, "scale_note": None}
    if not a.treatment_label or not a.comparator_label or a.treatment_arm_id is None or a.comparator_arm_id is None:
        return {**out, "status": "NOT_AVAILABLE", "basis_kind": "NOT_ESTABLISHED", "stated_by": "NOT_STATED_BY_SOURCE", "confirmed_by_arm_values": False,
                "basis": "The stored analysis does not identify one treatment group and one comparator group."}
    rows = [
        r for r in a.study.results
        if r.endpoint_id == a.endpoint_id and r.scope in ("ARM", "POOLED") and r.value is not None and r.estimand_id == a.estimand_id
        and (a.timepoint_weeks is None or r.timepoint_weeks is None or r.timepoint_weeks == a.timepoint_weeks)
    ]
    trt = [r for r in rows if r.arm_id == a.treatment_arm_id]
    cmp_ = [r for r in rows if r.arm_id == a.comparator_arm_id]
    check = "NOT_CHECKABLE"
    if len(trt) == 1 and len(cmp_) == 1 and a.estimate is not None:
        t, c = trt[0], cmp_[0]
        out["treatment_arm_value"], out["comparator_arm_value"] = t.value, c.value
        diff = t.value - c.value
        if scale == "DIFFERENCE" and a.estimate != 0 and diff != 0:
            if _sign(a.estimate) == _sign(diff):
                check = "CONSISTENT"
                if 0.009 <= abs(a.estimate) / abs(diff) <= 0.011 and unit_family(t.unit) == "PERCENT_OF_PARTICIPANTS":
                    out["scale_note"] = "The estimate is about one hundredth of the difference between the arm percentages: it appears to be on a 0–1 proportion scale, while the stored unit label is the endpoint's."
            else:
                # A mirrored estimate (≈ comparator − treatment) contradicts the stated orientation. Risk differences are
                # sometimes posted on a 0–1 scale beside arm percentages, so that scale is tried too.
                scales = [a.estimate] + ([100 * a.estimate] if unit_family(t.unit) == "PERCENT_OF_PARTICIPANTS" and abs(a.estimate) <= 1 else [])
                mirrored = any(abs(e + diff) <= 0.5 * max(abs(e), abs(diff)) for e in scales)
                check = "CONTRADICTED" if mirrored else "INCONCLUSIVE"
        elif scale == "RATIO" and a.estimate > 0 and unit_family(t.unit) in ("PERCENT_OF_PARTICIPANTS", "PARTICIPANT_COUNT"):
            if unit_family(t.unit) == "PARTICIPANT_COUNT":
                diff = (100.0 * t.value / t.n_analyzed - 100.0 * c.value / c.n_analyzed) if t.n_analyzed and c.n_analyzed else 0.0
            log_ratio = _log(a.estimate)
            if abs(log_ratio) >= 0.1 and abs(diff) >= 1.0:
                check = "CONSISTENT" if _sign(log_ratio) == _sign(diff) else "CONTRADICTED"
            elif log_ratio != 0 and diff != 0 and _sign(log_ratio) == _sign(diff):
                check = "CONSISTENT"
    out["arm_values_check"] = check
    # How treatment and comparator were told apart is stored on the row (``direction_basis``). The note text is kept for
    # rows loaded before that column existed.
    basis = getattr(a, "direction_basis", None)
    registry_order = basis == DirectionBasis.NOT_STATED.value or REGISTRY_ORDER_NOTE in (a.comment or "")
    # Who states the direction, kept apart from whether the arm values confirm it, so a reader is told exactly which.
    stated_by = ("CURATED_SOURCE" if a.origin == "CURATED" else "REGISTRY_GROUP_DESCRIPTION" if basis == DirectionBasis.GROUP_DESCRIPTION.value
                 else "NOT_STATED_BY_SOURCE")
    out["stated_by"] = stated_by
    out["confirmed_by_arm_values"] = check == "CONSISTENT"
    if check == "CONTRADICTED":
        return {**out, "status": "CONTRADICTED_BY_ARM_VALUES", "basis_kind": "NOT_ESTABLISHED",
                "basis": "The sign of the stored estimate is opposite to (treatment value − comparator value) computed from the stored arm-level values, so which group is the reference is not established."}
    if check == "INCONCLUSIVE":
        # The stored arm values give the opposite sign. That is never support for the stated direction, whoever stated it.
        return {**out, "status": "SIGN_DISAGREES_WITH_ARM_VALUES", "basis_kind": "NOT_ESTABLISHED",
                "basis": "The sign of the stored estimate is opposite to (treatment value − comparator value) from the stored arm-level values of the same endpoint, "
                         "estimand and time point (the estimate is not their mirror image either), so the direction is not established"
                         + (" although the source names the contrast." if stated_by != "NOT_STATED_BY_SOURCE" else ".")}
    if check == "CONSISTENT":
        if stated_by == "REGISTRY_GROUP_DESCRIPTION":
            text = ("The registry analysis names the contrast (its group description states which group is compared with which), and the sign of the stored estimate "
                    "agrees with (treatment value − comparator value) from the stored arm-level values of the same endpoint, estimand and time point.")
            kind = "STATED_BY_REGISTRY_AND_CONFIRMED_BY_ARM_VALUES"
        elif stated_by == "CURATED_SOURCE":
            text = ("The curated source row states treatment and comparator, and the sign of the stored estimate agrees with (treatment value − comparator value) "
                    "from the stored arm-level values of the same endpoint, estimand and time point.")
            kind = "STATED_BY_CURATED_SOURCE_AND_CONFIRMED_BY_ARM_VALUES"
        else:
            text = ("The source does not state the direction: the reference group was identified by its role in the trial (the registry's arm type or a placebo-titled group), "
                    "and the sign of the stored estimate agrees with (treatment value − comparator value) from the stored arm-level values of the same endpoint, estimand and time point.")
            kind = "CONFIRMED_BY_ARM_VALUES"
        return {**out, "established": True, "status": "VERIFIED_AGAINST_ARM_VALUES", "basis_kind": kind, "basis": text}
    if a.origin == "CURATED":
        return {**out, "established": True, "status": "STATED_IN_CURATED_SOURCE", "basis_kind": "STATED_BY_CURATED_SOURCE",
                "basis": "Treatment and comparator are taken from the curated source row; no arm-level values are stored on the same endpoint row to check the sign against."}
    if basis == DirectionBasis.GROUP_DESCRIPTION.value:
        # The registry names the contrast itself ("X vs Placebo"): a statement by the source, like a curated row's.
        return {**out, "established": True, "status": "STATED_BY_REGISTRY", "basis_kind": "STATED_BY_REGISTRY",
                "basis": "The registry analysis names the contrast (its group description states which group is compared with which); "
                         "the sign could not also be checked against stored arm-level values of the same endpoint, estimand and time point."}
    if registry_order:
        return {**out, "status": "REGISTRY_ORDER_NOT_VERIFIED", "basis_kind": "NOT_ESTABLISHED",
                "basis": "The registry does not state which group is the reference; the groups are in registry order and the sign could not be checked against arm-level values."}
    return {**out, "status": "CONVENTION_NOT_VERIFIED", "basis_kind": "NOT_ESTABLISHED",
            "basis": "The placebo or comparator group was taken as the reference by convention (from the registry's arm role or a placebo-titled group); "
                     "the registry does not state it and the sign could not be checked against arm-level values."}


def _log(value: float) -> float:
    return math.log(value)


# --------------------------------------------------------------------------- one display rule for "which value leads"

DISPLAY_RULE = (
    "Display choice, not a judgement of correctness. When several stored sources report the same quantity, all of them are returned with their "
    "evidence tiers. The one shown first is the one reported with its own confidence interval; failing that, the one reported with a standard error "
    "or other dispersion; failing that, the one with a reported number analysed; remaining ties go to the record stored first. "
    "Evidence tier is not used to choose, and a value rejected in review is never shown."
)
DISPLAY_RULE_ADVERSE_EVENTS = (
    "Display choice, not a judgement of correctness. When several stored sources report the same adverse-event measure for an arm, all of them are "
    "returned with their evidence tiers. The one shown first is the one reported as a count with its denominator; failing that, the one reported as "
    "a percentage; remaining ties go to the record stored first. Evidence tier is not used to choose."
)


def _field(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def _row_id(row: Any) -> int:
    value = _field(row, "id")
    if value is None:
        value = _field(row, "result_id")
    return int(value) if value is not None else 0


def display_order(candidates: Sequence[Any], kind: str = "result") -> list[Any]:
    """THE shared ordering for several sources of one quantity (see ``DISPLAY_RULE``). The first element leads.

    ``kind`` is 'result' (outcome results or finding dicts) or 'adverse_event'. Records rejected in review are
    dropped. The rule looks only at how completely each record is reported — never at its evidence tier.
    """
    usable = [c for c in candidates if is_usable(_field(c, "verification_status"))]
    if kind == "adverse_event":
        def completeness(c: Any) -> tuple[int, int]:
            counted = _field(c, "n_affected") is not None and bool(_field(c, "n_at_risk"))
            return (0 if counted else 1 if _field(c, "percent") is not None else 2, _row_id(c))
    else:
        def completeness(c: Any) -> tuple[int, int]:
            has_ci = _field(c, "ci_low") is not None and _field(c, "ci_high") is not None
            has_dispersion = _field(c, "dispersion_value") is not None
            has_n = _field(c, "n_analyzed") is not None
            return (0 if has_ci else 1 if has_dispersion else 2 if has_n else 3, _row_id(c))
    return sorted(usable, key=completeness)


def display_lead(candidates: Sequence[Any], kind: str = "result") -> tuple[Any | None, list[Any]]:
    """(lead, others) by the shared display rule; (None, []) when nothing usable is stored."""
    ordered = display_order(candidates, kind)
    return (ordered[0], ordered[1:]) if ordered else (None, [])


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
    if a.direction_basis == "NOT_STATED":
        labels.append({"code": "REFERENCE_NOT_STATED", "text": "Reference group not stated", "tone": "warn", "detail": "The source does not say which group is the reference; the two groups are listed in posted order and the sign has no stated direction."})
    elif a.direction_basis in ("REGISTRY_ARM_TYPE", "PLACEBO_GROUP_TITLE"):
        labels.append({"code": "REFERENCE_FROM_ARM_ROLE", "text": "Reference group from arm role", "tone": "muted", "detail": "The registry analysis does not state the direction of the contrast; the comparator is the group the registry classifies as placebo or active comparator."})
    scale = effect_scale(a.param_type, a.unit)
    level_status = ci_level_status(a.ci_low, a.ci_high, a.ci_level)
    direction = analysis_direction(a, scale["scale"])
    if level_status == "INVALID_LEVEL":
        labels.append({"code": "CI_LEVEL_INVALID", "text": "Confidence level not valid", "tone": "warn", "detail": f"The stored confidence level ({a.ci_level:g}) is not a percentage between 50 and 100; the interval is shown as stored and is not interpreted."})
    elif level_status == "LEVEL_NOT_REPORTED":
        labels.append({"code": "CI_LEVEL_NOT_REPORTED", "text": "Confidence level not stated", "tone": "muted", "detail": "The source gives interval limits without a confidence level; nothing is inferred from them."})
    if scale["scale"] == "NOT_ESTABLISHED":
        labels.append({"code": "SCALE_NOT_ESTABLISHED", "text": "Difference or ratio not established", "tone": "warn", "detail": scale["basis"]})
    if not direction["established"]:
        labels.append({"code": "DIRECTION_NOT_ESTABLISHED", "text": "Direction not established", "tone": "warn", "detail": direction["basis"]})
    consistency = interval_p_value_consistency(a.estimate, a.ci_low, a.ci_high, a.ci_level, level_status, a.ci_sides, scale["scale"], scale["null_value"], a.p_value_text, a.hypothesis_type)
    if consistency["status"] not in ("CONSISTENT", "NOT_CHECKABLE"):
        labels.append({"code": "INTERNALLY_INCONSISTENT", "text": "Record internally inconsistent", "tone": "warn", "detail": consistency["detail"]})
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
        # Groups in the order the source posts them, how treatment/comparator were assigned, and the outcome class.
        "group_labels": list(a.group_labels or []),
        "direction_basis": a.direction_basis,
        "group_description": a.group_description,
        "class_title": a.class_title,
        "hypothesis_type": a.hypothesis_type,
        "param_type": a.param_type,
        "estimate": a.estimate,
        # The estimate exactly as the source printed it (reported precision); None when not stored.
        "estimate_text": a.estimate_text,
        "unit": a.unit,
        "ci_low": a.ci_low,
        "ci_high": a.ci_high,
        "ci_level": a.ci_level,
        "ci_status": "REPORTED" if a.ci_low is not None and a.ci_high is not None else "NOT_REPORTED",
        "ci_sides": (a.ci_sides or "NOT_STATED").upper(),
        "ci_level_status": level_status,
        "effect_scale": scale,
        "direction": direction,
        "p_value_text": a.p_value_text,
        "p_value_status": "REPORTED" if a.p_value_text else "NOT_REPORTED",
        # Whether the stored interval, estimate and p-value agree with each other (see interval_p_value_consistency).
        "internal_consistency": consistency,
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
        # The registry's serious-event table and its non-serious table are different counts of the same term.
        "measure_kind_label": "Participants with ≥1 SERIOUS event (within-trial incidence proportion)"
        if ae.is_serious and ae.measure_kind == "PARTICIPANTS_WITH_EVENT"
        else AE_KIND_LABEL.get(ae.measure_kind, ae.measure_kind),
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
