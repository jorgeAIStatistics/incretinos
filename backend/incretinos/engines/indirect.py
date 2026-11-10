"""Anchored indirect comparison (Bucher method): feasibility first, calculation only when every requirement is met.

Two stored between-arm analyses — drug A versus a common comparator C in trial 1, drug B versus C in trial 2 —
can give an *anchored indirect estimate* of A versus B (Bucher HC, Guyatt GH, Griffith LE, Walter SD. J Clin
Epidemiol 1997;50:683-691):

    d_AB = d_AC − d_BC                          (differences; log ratios for ratio measures)
    SE_AB = sqrt(SE_AC² + SE_BC²)               (the two trials are independent)
    CI = d_AB ± z_{1−α/2} · SE_AB,  p = 2 · (1 − Φ(|d_AB| / SE_AB))

Each SE is recovered from the stored confidence interval: SE = (upper − lower) / (2 · z_{1−α/2}) at the interval's own
stated level (on the log scale for ratios). Nothing is imputed: no interval, no level or no stated sidedness means no SE
and no calculation.

The estimate keeps randomization *within* each trial (each contrast is a randomized comparison against C) but A and B
were never randomized against each other. It is valid only under transitivity: the trials must not differ in anything
that modifies the A-versus-C or B-versus-C effect. That assumption cannot be verified from two summary-level trials;
the engine states it, lists the effect modifiers with the values stored for each trial (or "not stored"), and never
presents the result as a randomized comparison or as a ranking.

Every requirement is reported as PASS, FAIL or CANNOT_ASSESS with the stored evidence behind it:

* comparator, network, estimand, outcome definition, effect scale, follow-up, contrast reliability, variance — any
  status other than PASS refuses the calculation and says why;
* population comparability, effect modifiers and transitivity — FAIL refuses; CANNOT_ASSESS does not refuse but is
  shown as an UNVERIFIED ASSUMPTION beside the result (never hidden).

This is not a network meta-analysis: exactly two trials, one common comparator, one contrast each.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from scipy import stats as sps
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..enums import EstimandLabel, EvidenceTier, Origin, VerificationStatus
from ..models import Drug, Formulation, OutcomeResult, StatisticalAnalysis, Study, StudyArm
from ..review.policy import canonical_status, exclusion_reason, is_usable
from .comparability import _SPECIAL_POPULATIONS, _analysis_baseline_class
from .findings import (
    BASELINE_CLASS_LABEL,
    CATEGORY_LABEL,
    LEAD_IN_FEATURE_KEYS,
    _fmt,
    analysis_dto,
    arm_stratum,
    endpoint_definition,
    has_lead_in_design,
)

PASS, FAIL, CANNOT_ASSESS = "PASS", "FAIL", "CANNOT_ASSESS"

METHOD_NAME = "Anchored indirect comparison (Bucher method)"
ESTIMATE_LABEL = "Anchored indirect estimate (Bucher) — not a randomized comparison"
REFERENCE = ("Bucher HC, Guyatt GH, Griffith LE, Walter SD. The results of direct and indirect treatment comparisons in meta-analysis "
             "of randomized controlled trials. J Clin Epidemiol 1997;50(6):683-691. doi:10.1016/S0895-4356(97)00049-8")

#: Requirements that must PASS for a calculation. Anything else (FAIL or CANNOT_ASSESS) refuses.
HARD_CHECKS = ("network", "comparator", "estimand", "outcome", "effect_scale", "follow_up", "reliability", "variance")
#: Requirements whose CANNOT_ASSESS is shown as an unverified assumption; their FAIL refuses.
ASSUMPTION_CHECKS = ("population", "effect_modifiers", "transitivity")

#: Statuses a contrast may have to enter a calculation: registry-posted and machine-extracted from a hashed snapshot, or
#: approved by an attested human decision. A curated value anchored to a quote, or anything awaiting review, is not enough.
ALLOWED_STATUS_TEXT = "MACHINE_EXTRACTED registry results (posted analyses parsed from a stored registry snapshot) or HUMAN_VERIFIED evidence"

#: Most a requested follow-up tolerance may be, in weeks. Default is 0: the same assessment week.
MAX_FOLLOW_UP_TOLERANCE_WEEKS = 12.0
MIN_JUSTIFICATION_CHARS = 30

#: (key, label, unit, minor, major) — the thresholds the comparability engine uses for the same quantities.
_NUMERIC_MODIFIERS: tuple[tuple[str, str, str, float, float], ...] = (
    ("baseline_weight", "Mean baseline body weight", "kg", 5.0, 10.0),
    ("baseline_bmi", "Mean baseline BMI", "kg/m²", 2.0, 4.0),
    ("age", "Mean age", "years", 5.0, 10.0),
    ("sex", "Female participants (calculated from registry counts)", "%", 10.0, 20.0),
)

_RATIO_KINDS = (("HAZARD_RATIO", r"\bhazard"), ("ODDS_RATIO", r"\bodds"), ("RISK_RATIO", r"relative risk|risk ratio|\brr\b"), ("RATE_RATIO", r"rate ratio"))
_BACKGROUND_TERMS = (
    ("lifestyle counselling", r"lifestyle (intervention|counsel|program)|counsell?ing"), ("reduced-calorie diet", r"reduced[- ]calorie|calorie[- ]restrict|hypocaloric"),
    ("increased physical activity", r"(increase[ds]?|recommended) physical activity|exercise program"),
    ("metformin", r"metformin"), ("SGLT2 inhibitor", r"sglt"), ("sulfonylurea", r"sulfonylurea"), ("insulin", r"\binsulin"),
    ("glucose-lowering therapy (unspecified)", r"glucose[- ]lowering|antihyperglyc|oral antidiabetic|\boam"),
)
_ESCALATION = re.compile(r"(?:start(?:ing|ed)? (?:at|with)|escalat|titrat|increas(?:ed|ing) (?:by|to|every))[^;*]*", re.IGNORECASE)
_BMI_CRITERION = re.compile(r"bmi|body mass index", re.IGNORECASE)
_BMI_THRESHOLD = re.compile(r"(?:≥|>=|greater than or equal to|>|of\s+at least|at least)\s*(\d{2}(?:\.\d)?)")


class IndirectInputError(ValueError):
    """The request itself is invalid (an unknown analysis, a tolerance without justification, a bad level)."""


@dataclass
class Check:
    key: str
    label: str
    status: str
    a: str
    b: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- small helpers


def _z(level: float) -> float:
    return float(sps.norm.ppf(1 - (1 - level) / 2))


def _decimals(value: float | None) -> int | None:
    """Decimal places of a stored number as it prints (the reported precision of a registry value)."""
    if value is None:
        return None
    text = repr(float(value))
    if "e" in text or "E" in text:
        return None
    return len(text.split(".")[1].rstrip("0")) if "." in text else 0


def se_from_ci(lower: float, upper: float, level_percent: float, scale: str) -> dict[str, Any]:
    """Standard error from a two-sided interval at a known level: (upper − lower) / (2 z), on the log scale for ratios."""
    if not 50 <= level_percent < 100:
        raise IndirectInputError("the confidence level must be a percentage between 50 and 100")
    z = _z(level_percent / 100)
    if scale == "RATIO":
        if lower <= 0 or upper <= 0:
            raise IndirectInputError("a ratio interval must have positive limits to be used on the log scale")
        lo, hi = math.log(lower), math.log(upper)
    else:
        lo, hi = lower, upper
    if hi <= lo:
        raise IndirectInputError("the interval has no width")
    return {"se": (hi - lo) / (2 * z), "z": z, "width": hi - lo, "on_log_scale": scale == "RATIO"}


def bucher(d_ac: float, se_ac: float, d_bc: float, se_bc: float, level: float = 0.95) -> dict[str, Any]:
    """Anchored indirect contrast on the analysis scale (a difference, or a log ratio): pure arithmetic, no checks."""
    if se_ac <= 0 or se_bc <= 0:
        raise IndirectInputError("standard errors must be positive")
    if not 0.5 <= level < 1:
        raise IndirectInputError("the confidence level must be at least 0.5 and below 1")
    d = d_ac - d_bc
    se = math.sqrt(se_ac**2 + se_bc**2)
    z = _z(level)
    stat = d / se
    return {"d": d, "se": se, "lower": d - z * se, "upper": d + z * se, "z_critical": z, "z_statistic": stat,
            "p_two_sided": float(2 * sps.norm.sf(abs(stat))), "level": level}


def _ratio_kind(param_type: str | None) -> str | None:
    low = (param_type or "").lower()
    for kind, pattern in _RATIO_KINDS:
        if re.search(pattern, low):
            return kind
    return None


def comparator_key(arm: StudyArm | None) -> str | None:
    """'placebo', 'active:<drug>' or None (not a usable common comparator: a sequence, an unclassified group, no drug)."""
    if arm is None:
        return None
    if arm.arm_role == "PLACEBO" or (arm.arm_role is None and arm.is_placebo):
        return "placebo"
    if arm.arm_role == "ACTIVE_COMPARATOR" and arm.drug_slug:
        return f"active:{arm.drug_slug}"
    return None


def treatment_drug(arm: StudyArm | None) -> str | None:
    """The drug of a treatment arm, or None when the arm is no single treatment (placebo, sequence, unclassified)."""
    if arm is None or not arm.drug_slug or arm.drug_slug == "placebo" or arm.arm_role in ("PLACEBO", "SEQUENCE", "UNCLASSIFIED"):
        return None
    return arm.drug_slug


def _drug_names(session: Session) -> dict[str, str]:
    return {slug: name for slug, name in session.execute(select(Drug.slug, Drug.name))}


def _comparator_label(key: str, names: dict[str, str]) -> str:
    if key == "placebo":
        return "Placebo"
    slug = key.split(":", 1)[1]
    return f"{names.get(slug, slug)} (active comparator)"


def _formulation_count(session: Session, slug: str) -> int:
    return int(session.scalar(select(func.count()).select_from(Formulation).join(Drug, Formulation.drug_id == Drug.id).where(Drug.slug == slug)) or 0)


def _summary(study: Study, key: str) -> float | None:
    entry = (study.population.summary or {}).get(key) if study.population else None
    value = entry.get("value") if isinstance(entry, dict) else None
    return float(value) if isinstance(value, int | float) else None


def _female_pct(study: Study) -> float | None:
    n, total = _summary(study, "female_n"), _summary(study, "sex_total_n")
    return 100.0 * n / total if n is not None and total else None


def _bmi_thresholds(study: Study) -> list[float]:
    found: set[float] = set()
    for criterion in study.inclusion:
        if _BMI_CRITERION.search(criterion.text):
            found.update(float(x) for x in _BMI_THRESHOLD.findall(criterion.text))
    return sorted(found, reverse=True)


def _snippet(text: str | None, limit: int = 220) -> str | None:
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _escalation(arm: StudyArm | None) -> str | None:
    match = _ESCALATION.search((arm.description or "") if arm else "")
    return _snippet(match.group(0)) if match else None


def _administration(arm: StudyArm | None) -> str | None:
    text = ((arm.description or "") + " " + (arm.label or "")).lower() if arm else ""
    route = "oral" if re.search(r"\boral|orally|tablet|capsule", text) else "subcutaneous" if re.search(r"subcutaneous|\bsc\b", text) else None
    frequency = "once weekly" if re.search(r"once[- ]weekly|\bqw\b|weekly", text) else "once daily" if re.search(r"once[- ]daily|\bqd\b|daily", text) else None
    if route is None and frequency is None:
        return None
    return ", ".join(x for x in (route, frequency) if x)


def _background(study: Study, comparator: StudyArm | None) -> list[str]:
    texts = [c.text for c in study.inclusion] + [(comparator.description or "") if comparator else ""]
    joined = " ".join(texts).lower()
    return [name for name, pattern in _BACKGROUND_TERMS if re.search(pattern, joined)]


def _special(study: Study) -> list[str]:
    text = " ".join(study.conditions or []).lower()
    return sorted(name for name, pattern in _SPECIAL_POPULATIONS.items() if re.search(pattern, text))


def _arm_rows(row: StatisticalAnalysis, arm_id: int | None) -> list[OutcomeResult]:
    return [r for r in row.study.results if r.endpoint_id == row.endpoint_id and r.arm_id == arm_id and r.estimand_id == row.estimand_id
            and (row.timepoint_weeks is None or r.timepoint_weeks == row.timepoint_weeks) and is_usable(r.verification_status, r.review_hold)]


# --------------------------------------------------------------------------- one contrast, described from stored fields only


def describe_contrast(session: Session, row: StatisticalAnalysis, names: dict[str, str] | None = None) -> dict[str, Any]:
    """Everything the requirements read about one stored between-arm analysis, with its provenance."""
    names = names if names is not None else _drug_names(session)
    dto = analysis_dto(row)
    study, t_arm, c_arm = row.study, row.treatment_arm, row.comparator_arm
    population = study.population
    definition = endpoint_definition(row.endpoint.category, row.endpoint.title, row.class_title, row.endpoint.threshold, row.unit or row.endpoint.unit)
    t_rows, c_rows = _arm_rows(row, row.treatment_arm_id), _arm_rows(row, row.comparator_arm_id)
    analysis_population = sorted({r.analysis_population for r in t_rows + c_rows if r.analysis_population})
    drug = treatment_drug(t_arm)
    ckey = comparator_key(c_arm)
    design = study.design_features or {}
    return {
        "analysis_id": row.id,
        "study_id": study.id,
        "nct_id": study.nct_id,
        "study_name": study.display_name,
        "phase": study.phase,
        "start_date": study.start_date,
        "drug": drug,
        "drug_name": names.get(drug, drug) if drug else None,
        "treatment": {"arm_id": row.treatment_arm_id, "label": row.treatment_label, "arm_label": t_arm.label if t_arm else None, "dose_amount": t_arm.dose_amount if t_arm else None,
                      "dose_unit": t_arm.dose_unit if t_arm else None, "formulation_key": t_arm.formulation_key if t_arm else None, "arm_role": t_arm.arm_role if t_arm else None,
                      "is_pooled": bool(t_arm.is_pooled) if t_arm else None, "stratum": arm_stratum(t_arm.label) if t_arm else "", "escalation": _escalation(t_arm),
                      "administration": _administration(t_arm), "n_analyzed": t_rows[0].n_analyzed if len(t_rows) == 1 else None},
        "comparator": {"arm_id": row.comparator_arm_id, "label": row.comparator_label, "arm_label": c_arm.label if c_arm else None, "key": ckey,
                       "display": _comparator_label(ckey, names) if ckey else "not a usable common comparator", "drug": c_arm.drug_slug if c_arm else None,
                       "dose_amount": c_arm.dose_amount if c_arm else None, "dose_unit": c_arm.dose_unit if c_arm else None, "formulation_key": c_arm.formulation_key if c_arm else None,
                       "formulations_on_record": _formulation_count(session, c_arm.drug_slug) if c_arm is not None and c_arm.drug_slug and ckey != "placebo" else None,
                       "arm_role": c_arm.arm_role if c_arm else None, "is_pooled": bool(c_arm.is_pooled) if c_arm else None, "stratum": arm_stratum(c_arm.label) if c_arm else "",
                       "administration": _administration(c_arm), "n_analyzed": c_rows[0].n_analyzed if len(c_rows) == 1 else None},
        "endpoint": {"id": row.endpoint_id, "title": row.endpoint.title, "category": row.endpoint.category, "category_label": CATEGORY_LABEL.get(row.endpoint.category, row.endpoint.category),
                     "role": row.endpoint.role, "class_title": row.class_title, "unit": row.unit or row.endpoint.unit, "unit_family": definition.unit_family,
                     "measure": list(definition.measure), "method": list(definition.method), "threshold": list(definition.threshold),
                     "threshold_ambiguous": definition.threshold_ambiguous, "subset": definition.subset, "text_key": definition.text_key,
                     "baseline_class": _analysis_baseline_class(row)},
        "estimand_label": dto["estimand_label"],
        "estimand_basis": dto["estimand_basis"],
        "timepoint_weeks": row.timepoint_weeks,
        "param_type": row.param_type,
        "effect_scale": dto["effect_scale"],
        "ratio_kind": _ratio_kind(row.param_type),
        "estimate": row.estimate,
        "estimate_text": row.estimate_text,
        "ci_low": row.ci_low,
        "ci_high": row.ci_high,
        "ci_level": row.ci_level,
        "ci_level_status": dto["ci_level_status"],
        "ci_sides": dto["ci_sides"],
        "p_value_text": row.p_value_text,
        "hypothesis_type": row.hypothesis_type,
        "multiplicity_controlled": row.multiplicity_controlled,
        "method": row.method,
        "direction": {k: dto["direction"].get(k) for k in ("established", "status", "basis_kind", "basis")},
        "internal_consistency": dto["internal_consistency"],
        "verification_status": canonical_status(row.verification_status),
        "review_hold": bool(row.review_hold),
        "exclusion_reason": exclusion_reason(row.verification_status, row.review_hold),
        "origin": row.origin,
        "tier": row.tier,
        "source": dto["source"],
        "source_location": row.source_location,
        "locator_quote": row.locator_quote,
        "analysis_population": analysis_population,
        "population": {
            "diabetes_status": population.diabetes_status if population else "NOT_STATED",
            "diabetes_basis": population.diabetes_basis if population else None,
            "conditions": list(study.conditions or []),
            "special_populations": _special(study),
            "lead_in": {k: design.get(k) for k in LEAD_IN_FEATURE_KEYS if design.get(k)},
            "has_lead_in": has_lead_in_design(design),
            "bmi_thresholds": _bmi_thresholds(study),
            "min_age": population.min_age if population else None,
            "max_age": population.max_age if population else None,
            "sex": population.sex if population else None,
            "age_mean_years": _summary(study, "age_mean_years"),
            "weight_mean_kg": _summary(study, "weight_mean_kg"),
            "bmi_mean": _summary(study, "bmi_mean"),
            "female_pct": _female_pct(study),
            "countries": sorted(study.countries or []),
            "background": _background(study, c_arm),
            "treatment_weeks": design.get("treatment_weeks"),
        },
        "labels": dto["labels"],
    }


# --------------------------------------------------------------------------- the requirements


def _side_text(s: dict[str, Any]) -> str:
    return f"{s['study_name']} ({s['nct_id']}): {s['treatment']['label']} vs {s['comparator']['label']}"


def _check_network(a: dict[str, Any], b: dict[str, Any]) -> Check:
    problems, unknown = [], []
    if a["study_id"] == b["study_id"]:
        problems.append("both contrasts come from one trial; their shared comparator arm makes them correlated, and the trial's own randomized "
                        "comparison of the two arms (the comparison laboratory above) answers the question directly")
    for side, s in (("A", a), ("B", b)):
        if s["drug"] is None:
            unknown.append(f"the treatment group of contrast {side} ({s['treatment']['label']}) is not one identified study drug")
        if s["treatment"]["is_pooled"]:
            problems.append(f"the treatment group of contrast {side} ({s['treatment']['label']}) pools several doses or groups")
        if s["comparator"]["is_pooled"]:
            problems.append(f"the comparator group of contrast {side} pools several groups")
    if a["drug"] and b["drug"] and a["drug"] == b["drug"]:
        problems.append(f"both contrasts are of {a['drug_name']}: an anchored comparison of a drug with itself across trials is not a comparison of two treatments")
    for side, s in (("A", a), ("B", b)):
        if s["drug"] and s["comparator"]["drug"] and s["drug"] == s["comparator"]["drug"]:
            problems.append(f"in contrast {side} the treatment and the comparator are the same drug")
    status = FAIL if problems else CANNOT_ASSESS if unknown else PASS
    detail = ("; ".join(problems + unknown) + ".") if problems or unknown else (
        f"Two different trials and two different drugs: {a['drug_name']} (trial {a['nct_id']}) and {b['drug_name']} (trial {b['nct_id']}), each compared with its own randomized comparator group.")
    return Check("network", "Two trials, two different drugs, one contrast each", status, _side_text(a), _side_text(b), detail[0].upper() + detail[1:],
                 {"trial_a": a["nct_id"], "trial_b": b["nct_id"], "drug_a": a["drug"], "drug_b": b["drug"]})


def _check_comparator(a: dict[str, Any], b: dict[str, Any]) -> Check:
    ca, cb = a["comparator"], b["comparator"]
    shown = lambda c: f"{c['arm_label'] or c['label']} [{c['display']}]" + (f", {_fmt(c['dose_amount'], 2)} {c['dose_unit'] or ''}".rstrip() if c["dose_amount"] is not None else "")  # noqa: E731
    evidence = {"a": {k: ca[k] for k in ("key", "arm_role", "drug", "dose_amount", "formulation_key")}, "b": {k: cb[k] for k in ("key", "arm_role", "drug", "dose_amount", "formulation_key")}}
    if ca["key"] is None or cb["key"] is None:
        return Check("comparator", "Same common comparator", CANNOT_ASSESS, shown(ca), shown(cb),
                     "At least one comparator group is not identified as placebo or as one active comparator drug (it is a sequence, an unclassified group or has no drug on record).", evidence)
    if ca["key"] != cb["key"]:
        return Check("comparator", "Same common comparator", FAIL, shown(ca), shown(cb),
                     f"Different comparators ({ca['display']} versus {cb['display']}): the two contrasts share no anchor, so their difference is not an anchored comparison.", evidence)
    if ca["key"] == "placebo":
        return Check("comparator", "Same common comparator", PASS, shown(ca), shown(cb),
                     "Both contrasts are against a placebo group (arm role PLACEBO on record). Placebos of different trials are assumed exchangeable; how each was administered is listed among the effect modifiers.", evidence)
    if ca["dose_amount"] is None or cb["dose_amount"] is None:
        return Check("comparator", "Same common comparator", CANNOT_ASSESS, shown(ca), shown(cb),
                     f"Both comparators are {ca['display']}, but the dose is not stored for at least one of them, so the same active comparator at the same dose is not established.", evidence)
    if ca["dose_amount"] != cb["dose_amount"] or (ca["dose_unit"] or "").lower() != (cb["dose_unit"] or "").lower():
        return Check("comparator", "Same common comparator", FAIL, shown(ca), shown(cb),
                     f"The same active comparator drug at different doses ({_fmt(ca['dose_amount'], 2)} versus {_fmt(cb['dose_amount'], 2)} {ca['dose_unit'] or ''}): not one common comparator.".replace("  ", " "), evidence)
    if ca["formulation_key"] and cb["formulation_key"] and ca["formulation_key"] != cb["formulation_key"]:
        return Check("comparator", "Same common comparator", FAIL, shown(ca), shown(cb), f"Same dose but different formulations ({ca['formulation_key']} versus {cb['formulation_key']}).", evidence)
    if not (ca["formulation_key"] and cb["formulation_key"]) and (ca["formulations_on_record"] or 0) > 1:
        return Check("comparator", "Same common comparator", CANNOT_ASSESS, shown(ca), shown(cb),
                     f"Same drug and dose, but {ca['formulations_on_record']} formulations of the drug are on record and the formulation is not stored for both comparator groups.", evidence)
    return Check("comparator", "Same common comparator", PASS, shown(ca), shown(cb),
                 f"The same active comparator at the same dose ({ca['display']}, {_fmt(ca['dose_amount'], 2)} {ca['dose_unit'] or ''}).".replace(" )", ")"), evidence)


def _check_estimand(a: dict[str, Any], b: dict[str, Any]) -> Check:
    la, lb = a["estimand_label"], b["estimand_label"]
    shown = lambda s: f"{s['estimand_label'].replace('_', ' ').lower()} ({s['estimand_basis'].replace('_', ' ').lower()})"  # noqa: E731
    evidence = {"a": {"label": la, "basis": a["estimand_basis"]}, "b": {"label": lb, "basis": b["estimand_basis"]}}
    unknown = [s for s in (a, b) if s["estimand_label"] in (EstimandLabel.NOT_STATED.value, EstimandLabel.OTHER.value)]
    if unknown:
        return Check("estimand", "Compatible estimand", CANNOT_ASSESS, shown(a), shown(b),
                     "The estimand is not stated for " + " and ".join(f"{s['study_name']}" for s in unknown)
                     + ": the two contrasts cannot be shown to answer the same question (how treatment discontinuation and rescue therapy are handled).", evidence)
    if la != lb:
        return Check("estimand", "Compatible estimand", FAIL, shown(a), shown(b),
                     f"Different estimands ({la.replace('_', ' ').lower()} versus {lb.replace('_', ' ').lower()}): the contrasts answer different questions, so their difference is not interpretable.", evidence)
    inferred = [s["study_name"] for s in (a, b) if s["estimand_basis"] != "STATED_BY_SOURCE"]
    note = (" Note: the label is inferred, not stated by the source, for " + " and ".join(inferred)
            + " (inferred from the registry analysis description: data after discontinuation of study treatment excluded).") if inferred else " Stated by the source for both."
    return Check("estimand", "Compatible estimand", PASS, shown(a), shown(b), f"Same estimand label ({la.replace('_', ' ').lower()}).{note}", evidence)


def _check_outcome(a: dict[str, Any], b: dict[str, Any]) -> Check:
    ea, eb = a["endpoint"], b["endpoint"]

    def shown(e: dict[str, Any]) -> str:
        bits = [e["title"] + (f" — {e['class_title']}" if e["class_title"] else ""), f"unit {e['unit'] or 'not recorded'}",
                f"baseline: {BASELINE_CLASS_LABEL.get(e['baseline_class'] or '', 'not determinable')}"]
        if e["threshold"]:
            bits.append("criterion " + ", ".join(e["threshold"]))
        return " · ".join(bits)

    fails, unknown = [], []
    if ea["category"] != eb["category"]:
        fails.append(f"different endpoint categories ({ea['category_label']} versus {eb['category_label']})")
    if ea["unit_family"] is None or eb["unit_family"] is None:
        unknown.append("the unit is not recorded for at least one contrast")
    elif ea["unit_family"] != eb["unit_family"]:
        fails.append(f"different units ({ea['unit']} versus {eb['unit']})")
    if ea["measure"] or eb["measure"]:
        if ea["measure"] != eb["measure"] or ea["method"] != eb["method"]:
            fails.append("different measurements (" + (", ".join(ea["measure"] + ea["method"]) or "unrecognised") + " versus " + (", ".join(eb["measure"] + eb["method"]) or "unrecognised") + ")")
    elif ea["text_key"] != eb["text_key"]:
        unknown.append("what the endpoint measures is not recognised for either contrast and the titles differ")
    if ea["threshold_ambiguous"] or eb["threshold_ambiguous"]:
        unknown.append("the responder criterion is ambiguous for at least one contrast")
    elif ea["threshold"] != eb["threshold"]:
        fails.append("different responder criteria (" + (", ".join(ea["threshold"]) or "none") + " versus " + (", ".join(eb["threshold"]) or "none") + ")")
    if ea["subset"] or eb["subset"]:
        if ea["subset"] != eb["subset"]:
            fails.append(f"an analysis restricted to a subset ({ea['subset'] or 'whole trial'} versus {eb['subset'] or 'whole trial'})")
    if ea["baseline_class"] is None or eb["baseline_class"] is None:
        unknown.append("the baseline the change is measured from cannot be determined for at least one contrast")
    elif ea["baseline_class"] != eb["baseline_class"]:
        fails.append(f"changes measured from different baselines ({BASELINE_CLASS_LABEL.get(ea['baseline_class'], ea['baseline_class'])} versus {BASELINE_CLASS_LABEL.get(eb['baseline_class'], eb['baseline_class'])})")
    evidence = {"a": {k: ea[k] for k in ("category", "unit", "unit_family", "measure", "threshold", "baseline_class", "subset")},
                "b": {k: eb[k] for k in ("category", "unit", "unit_family", "measure", "threshold", "baseline_class", "subset")}}
    if fails:
        return Check("outcome", "Compatible outcome definition", FAIL, shown(ea), shown(eb), "Not the same outcome: " + "; ".join(fails + unknown) + ".", evidence)
    if unknown:
        return Check("outcome", "Compatible outcome definition", CANNOT_ASSESS, shown(ea), shown(eb), "Not established as the same outcome: " + "; ".join(unknown) + ".", evidence)
    return Check("outcome", "Compatible outcome definition", PASS, shown(ea), shown(eb),
                 f"Same endpoint category ({ea['category_label']}), measurement ({', '.join(ea['measure'])}), unit and baseline class"
                 + (f", same responder criterion ({', '.join(ea['threshold'])})" if ea["threshold"] else "") + ".", evidence)


def _check_scale(a: dict[str, Any], b: dict[str, Any]) -> Check:
    sa, sb = a["effect_scale"]["scale"], b["effect_scale"]["scale"]
    shown = lambda s: f"{s['param_type'] or 'parameter not stated'} → {s['effect_scale']['scale'].replace('_', ' ').lower()}"  # noqa: E731
    evidence = {"a": {"scale": sa, "param_type": a["param_type"], "ratio_kind": a["ratio_kind"]}, "b": {"scale": sb, "param_type": b["param_type"], "ratio_kind": b["ratio_kind"]}}
    if "NOT_ESTABLISHED" in (sa, sb):
        why = a["effect_scale"]["basis"] if sa == "NOT_ESTABLISHED" else b["effect_scale"]["basis"]
        return Check("effect_scale", "Compatible effect scale", CANNOT_ASSESS, shown(a), shown(b), f"Whether the estimate is a difference or a ratio is not established: {why}", evidence)
    if sa != sb:
        return Check("effect_scale", "Compatible effect scale", FAIL, shown(a), shown(b),
                     "One contrast is a difference and the other a ratio: they cannot be subtracted on a common scale.", evidence)
    if sa == "RATIO":
        if a["ratio_kind"] is None or b["ratio_kind"] is None:
            return Check("effect_scale", "Compatible effect scale", CANNOT_ASSESS, shown(a), shown(b), "Both are ratios, but the kind of ratio (hazard, odds, risk) is not stated for both.", evidence)
        if a["ratio_kind"] != b["ratio_kind"]:
            return Check("effect_scale", "Compatible effect scale", FAIL, shown(a), shown(b), f"Different ratio measures ({a['ratio_kind']} versus {b['ratio_kind']}).", evidence)
        return Check("effect_scale", "Compatible effect scale", PASS, shown(a), shown(b), f"Both are {a['ratio_kind'].replace('_', ' ').lower()}s; the calculation is done on the log scale.", evidence)
    return Check("effect_scale", "Compatible effect scale", PASS, shown(a), shown(b), "Both are differences (treatment − comparator); the calculation is done on the difference scale.", evidence)


def _check_follow_up(a: dict[str, Any], b: dict[str, Any], tolerance: float, justification: str | None) -> Check:
    wa, wb = a["timepoint_weeks"], b["timepoint_weeks"]
    shown = lambda w: f"week {_fmt(w, 0)}" if w is not None else "time point not stated"  # noqa: E731
    evidence = {"a_weeks": wa, "b_weeks": wb, "tolerance_weeks": tolerance, "justification": justification}
    if wa is None or wb is None:
        return Check("follow_up", "Comparable follow-up", CANNOT_ASSESS, shown(wa), shown(wb), "The assessment week is not stated for at least one contrast.", evidence)
    gap = abs(wa - wb)
    if gap == 0:
        return Check("follow_up", "Comparable follow-up", PASS, shown(wa), shown(wb), f"Same assessment week ({_fmt(wa, 0)}).", evidence)
    if gap <= tolerance:
        return Check("follow_up", "Comparable follow-up", PASS, shown(wa), shown(wb),
                     f"Assessment weeks differ by {_fmt(gap, 0)}, within the tolerance of {_fmt(tolerance, 0)} weeks requested with this justification: “{justification}”. "
                     "The tolerance is the requester's judgement, not a property of the data.", evidence)
    return Check("follow_up", "Comparable follow-up", FAIL, shown(wa), shown(wb),
                 f"Assessment weeks differ by {_fmt(gap, 0)} (tolerance {_fmt(tolerance, 0)}). Weight and glycaemic effects keep changing over time, so contrasts at different weeks are different quantities.", evidence)


def _reliability_problems(s: dict[str, Any]) -> tuple[list[str], list[str]]:
    fails, unknown = [], []
    status = canonical_status(s["verification_status"])
    if status == VerificationStatus.REJECTED.value or s["review_hold"]:
        fails.append(s["exclusion_reason"] or "rejected in review")
    elif status in (VerificationStatus.PENDING_HUMAN_REVIEW.value, VerificationStatus.SOURCE_CHANGED_RECHECK_REQUIRED.value):
        fails.append(f"status {status}: the record awaits a human decision")
    elif status == VerificationStatus.HUMAN_VERIFIED.value:
        pass
    elif status == VerificationStatus.MACHINE_EXTRACTED.value and s["origin"] == Origin.REGISTRY.value and s["tier"] == EvidenceTier.REGISTRY_RESULTS.value:
        pass
    else:
        fails.append(f"status {status} ({s['origin'].lower()}, {s['tier'].replace('_', ' ').lower()}); only {ALLOWED_STATUS_TEXT} enter a calculation")
    if not s["direction"]["established"]:
        fails.append(f"the direction of the contrast is not established ({s['direction']['basis']})")
    consistency = s["internal_consistency"]["status"]
    if consistency not in ("CONSISTENT", "NOT_CHECKABLE"):
        fails.append(f"the record is internally inconsistent ({s['internal_consistency']['detail']})")
    elif consistency == "NOT_CHECKABLE":
        unknown.append("its interval, estimate and p-value could not be checked against each other")
    if s["estimate"] is None:
        fails.append("no estimate is stored")
    return fails, unknown


def _check_reliability(a: dict[str, Any], b: dict[str, Any]) -> Check:
    shown = lambda s: f"{s['verification_status']} · {s['tier'].replace('_', ' ').lower()} · direction {'established' if s['direction']['established'] else 'not established'} · {s['internal_consistency']['status'].replace('_', ' ').lower()}"  # noqa: E731
    fails, unknown = [], []
    for side, s in (("A", a), ("B", b)):
        f, u = _reliability_problems(s)
        fails += [f"contrast {side}: {x}" for x in f]
        unknown += [f"contrast {side}: {x}" for x in u]
    evidence = {side: {"verification_status": s["verification_status"], "origin": s["origin"], "tier": s["tier"], "direction_status": s["direction"]["status"],
                       "internal_consistency": s["internal_consistency"]["status"]} for side, s in (("a", a), ("b", b))}
    if fails:
        return Check("reliability", "Reliable contrast estimates", FAIL, shown(a), shown(b), "; ".join(fails + unknown) + ".", evidence)
    if unknown:
        return Check("reliability", "Reliable contrast estimates", CANNOT_ASSESS, shown(a), shown(b), "; ".join(unknown) + ".", evidence)
    return Check("reliability", "Reliable contrast estimates", PASS, shown(a), shown(b),
                 f"Both contrasts are {ALLOWED_STATUS_TEXT.split(' (')[0]} or human-verified, the direction (treatment − comparator) is established for both, "
                 "and each record's interval, estimate and p-value agree with each other. Neither is pending review, rejected or held.", evidence)


def _variance(s: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
    """(status, reason, se block) for one contrast."""
    if s["ci_low"] is None or s["ci_high"] is None:
        return FAIL, "no confidence interval is stored, and no standard error is imputed", None
    if s["ci_level_status"] == "LEVEL_NOT_REPORTED":
        return FAIL, "the confidence level of the interval is not stated, so no standard error can be derived from it", None
    if s["ci_level_status"] != "VALID":
        return FAIL, f"the stored confidence level ({s['ci_level']}) is not a valid percentage", None
    if s["ci_sides"] == "ONE_SIDED":
        return FAIL, "the interval is one-sided; a standard error is derived only from a two-sided interval", None
    if s["ci_sides"] != "TWO_SIDED":
        return FAIL, "whether the interval is one- or two-sided is not stated, so the standard error cannot be derived from it", None
    scale = s["effect_scale"]["scale"]
    try:
        block = se_from_ci(min(s["ci_low"], s["ci_high"]), max(s["ci_low"], s["ci_high"]), s["ci_level"], scale)
    except IndirectInputError as exc:
        return FAIL, str(exc), None
    decimals = [d for d in (_decimals(s["ci_low"]), _decimals(s["ci_high"])) if d is not None]
    if decimals and scale != "RATIO":
        half_unit = 0.5 * 10 ** -min(decimals)
        block["rounding_bound"] = half_unit / block["z"]
        block["rounding_note"] = (f"The interval limits are reported to {min(decimals)} decimal place(s); rounding alone can move the derived SE by up to "
                                  f"±{_fmt(block['rounding_bound'], 4)}.")
    block["formula"] = (f"SE = (ln {_fmt(s['ci_high'])} − ln {_fmt(s['ci_low'])}) / (2 × {_fmt(block['z'], 4)})" if scale == "RATIO"
                        else f"SE = ({_fmt(s['ci_high'])} − ({_fmt(s['ci_low'])})) / (2 × {_fmt(block['z'], 4)})")
    return PASS, f"two-sided {_fmt(s['ci_level'], 2)}% CI → SE {_fmt(block['se'], 4)}", block


def _check_variance(a: dict[str, Any], b: dict[str, Any]) -> tuple[Check, dict[str, Any] | None, dict[str, Any] | None]:
    shown = lambda s: (f"{_fmt(s['ci_level'], 2) if s['ci_level'] is not None else 'level not stated'}% CI {_fmt(s['ci_low'])} to {_fmt(s['ci_high'])} · {s['ci_sides'].replace('_', ' ').lower()}"  # noqa: E731
                       if s["ci_low"] is not None and s["ci_high"] is not None else "no confidence interval stored")
    (sa, ra, va), (sb, rb, vb) = _variance(a), _variance(b)
    evidence = {"a": va, "b": vb}
    if FAIL in (sa, sb):
        reasons = [f"contrast {side}: {r}" for side, st, r in (("A", sa, ra), ("B", sb, rb)) if st == FAIL]
        return Check("variance", "Variance information", FAIL, shown(a), shown(b), "; ".join(reasons) + ".", evidence), None, None
    return Check("variance", "Variance information", PASS, shown(a), shown(b),
                 f"Standard errors derived from the reported intervals (SE = (upper − lower) / (2 · z_(1−α/2)){', on the log scale' if a['effect_scale']['scale'] == 'RATIO' else ''}): "
                 f"contrast A {ra}; contrast B {rb}. This assumes each interval is a symmetric normal-approximation (Wald-type) interval; "
                 "for model-based intervals with large degrees of freedom the t and normal quantiles are practically equal.", evidence), va, vb


def _check_population(a: dict[str, Any], b: dict[str, Any]) -> Check:
    pa, pb = a["population"], b["population"]
    items: list[dict[str, Any]] = []

    def add(key: str, label: str, va: Any, vb: Any, status: str, note: str) -> None:
        items.append({"key": key, "label": label, "a": va, "b": vb, "status": status, "note": note})

    # Diabetes status.
    da, db_ = pa["diabetes_status"], pb["diabetes_status"]
    if "NOT_STATED" in (da, db_) or "MIXED" in (da, db_):
        add("diabetes_status", "Diabetes status", da, db_, CANNOT_ASSESS, "Not stated (or mixed) for at least one trial.")
    elif da != db_:
        add("diabetes_status", "Diabetes status", da, db_, FAIL, "One population has type 2 diabetes and the other does not; diabetes status modifies weight and glycaemic response.")
    else:
        add("diabetes_status", "Diabetes status", da, db_, PASS, f"Both {da.replace('_', ' ')} (basis on record for each).")
    # Indication / special population.
    sa, sb = pa["special_populations"], pb["special_populations"]
    if sa != sb:
        add("indication", "Indication and special populations", ", ".join(pa["conditions"]), ", ".join(pb["conditions"]), FAIL,
            f"Different special populations in the registered conditions ({', '.join(sa) or 'none'} versus {', '.join(sb) or 'none'}).")
    else:
        add("indication", "Indication and special populations", ", ".join(pa["conditions"]), ", ".join(pb["conditions"]), PASS,
            "No special population (sleep apnoea, osteoarthritis, cardiovascular, kidney or liver disease) distinguishes the registered conditions" + (f"; both include {', '.join(sa)}" if sa else "") + ".")
    # Enrichment / lead-in.
    if pa["has_lead_in"] != pb["has_lead_in"]:
        add("design_enrichment", "Lead-in, run-in or randomized-withdrawal design", pa["lead_in"] or "none on record", pb["lead_in"] or "none on record", FAIL,
            "One trial randomized participants after a lead-in (selected or already treated), the other at study entry: the randomized populations differ.")
    else:
        add("design_enrichment", "Lead-in, run-in or randomized-withdrawal design", pa["lead_in"] or "none on record", pb["lead_in"] or "none on record", PASS,
            "Both trials have the same kind of design on record" + (" (both with a lead-in)." if pa["has_lead_in"] else " (randomization at study entry)."))
    # Key eligibility: BMI thresholds and age.
    if not pa["bmi_thresholds"] or not pb["bmi_thresholds"]:
        add("bmi_eligibility", "BMI eligibility thresholds", pa["bmi_thresholds"] or "not found in stored criteria", pb["bmi_thresholds"] or "not found in stored criteria", CANNOT_ASSESS,
            "No BMI threshold could be read from the stored inclusion criteria of at least one trial.")
    elif pa["bmi_thresholds"] != pb["bmi_thresholds"]:
        add("bmi_eligibility", "BMI eligibility thresholds", pa["bmi_thresholds"], pb["bmi_thresholds"], FAIL, "Different BMI thresholds for inclusion: the trials enrolled different body-size populations.")
    else:
        add("bmi_eligibility", "BMI eligibility thresholds", pa["bmi_thresholds"], pb["bmi_thresholds"], PASS, "The same BMI thresholds appear in both trials' inclusion criteria (read from stored criterion text).")
    if pa["min_age"] is None or pb["min_age"] is None:
        add("age_eligibility", "Minimum age", pa["min_age"] or "not stored", pb["min_age"] or "not stored", CANNOT_ASSESS, "Not stored for at least one trial.")
    elif pa["min_age"] != pb["min_age"]:
        add("age_eligibility", "Minimum age", pa["min_age"], pb["min_age"], FAIL, "Different minimum ages (for example adolescents in one trial only).")
    else:
        add("age_eligibility", "Minimum age", pa["min_age"], pb["min_age"], PASS, "Same minimum age.")
    strata = (a["treatment"]["stratum"], b["treatment"]["stratum"])
    if any(strata) and strata[0] != strata[1]:
        add("substudy", "Sub-study or period", strata[0] or "main randomization", strata[1] or "main randomization", FAIL, "At least one contrast belongs to a sub-study or period of its trial.")
    statuses = {i["status"] for i in items}
    status = FAIL if FAIL in statuses else CANNOT_ASSESS if CANNOT_ASSESS in statuses else PASS
    fails = [f"{i['label']}: {i['note']}" for i in items if i["status"] == FAIL]
    unknown = [f"{i['label']}: {i['note']}" for i in items if i["status"] == CANNOT_ASSESS]
    detail = ("Not comparable — " + " ".join(fails)) if fails else ("Not established — " + " ".join(unknown)) if unknown else (
        "Diabetes status, indication, design (no lead-in difference), BMI and age eligibility agree. This concerns who could enrol, "
        "not how the enrolled populations were distributed; see the effect modifiers.")
    return Check("population", "Population comparability (eligibility level)", status, f"{a['study_name']}: {pa['diabetes_status'].replace('_', ' ')}", f"{b['study_name']}: {pb['diabetes_status'].replace('_', ' ')}",
                 detail, {"items": items})


def _effect_modifiers(a: dict[str, Any], b: dict[str, Any], tolerance_weeks: float = 0.0) -> list[dict[str, Any]]:
    pa, pb = a["population"], b["population"]
    out: list[dict[str, Any]] = []
    values = {"baseline_weight": (pa["weight_mean_kg"], pb["weight_mean_kg"]), "baseline_bmi": (pa["bmi_mean"], pb["bmi_mean"]),
              "age": (pa["age_mean_years"], pb["age_mean_years"]), "sex": (pa["female_pct"], pb["female_pct"])}
    for key, label, unit, minor, major in _NUMERIC_MODIFIERS:
        va, vb = values[key]
        if va is None or vb is None:
            out.append({"key": key, "label": label, "a": f"{_fmt(va, 1)} {unit}" if va is not None else "not stored", "b": f"{_fmt(vb, 1)} {unit}" if vb is not None else "not stored",
                        "assessment": "NOT_STORED", "note": "Not stored for both trials (registry baseline table), so balance cannot be assessed."})
            continue
        gap = abs(va - vb)
        assessment = "MAJOR_DIFFERENCE" if gap >= major else "DIFFERS" if gap >= minor else "SIMILAR"
        out.append({"key": key, "label": label, "a": f"{_fmt(va, 1)} {unit}", "b": f"{_fmt(vb, 1)} {unit}", "assessment": assessment,
                    "note": f"Difference {_fmt(gap, 1)} {unit} (similar below {_fmt(minor, 0)}, major from {_fmt(major, 0)}). Trial means only: the distributions and the effect of this factor on the treatment effect are not stored."})
    da, db_ = pa["diabetes_status"], pb["diabetes_status"]
    out.append({"key": "diabetes_status", "label": "Diabetes status", "a": da, "b": db_, "assessment": "SIMILAR" if da == db_ and da not in ("NOT_STATED", "MIXED") else "NOT_STORED" if "NOT_STATED" in (da, db_) else "MAJOR_DIFFERENCE",
                "note": "Population-level classification. The share with prediabetes (a likely modifier of weight response) is not stored."})
    ba, bb = pa["background"], pb["background"]
    out.append({"key": "background_therapy", "label": "Background therapy", "a": ", ".join(ba) or "not stored", "b": ", ".join(bb) or "not stored",
                "assessment": "NOT_STORED" if not ba or not bb else "DESCRIBED",
                "note": "Terms found in the stored inclusion criteria and comparator-arm description; the background therapy actually received is not stored."})
    ea, eb = a["treatment"]["escalation"], b["treatment"]["escalation"]
    out.append({"key": "dose_escalation", "label": "Dose-escalation scheme", "a": ea or "not stored", "b": eb or "not stored", "assessment": "NOT_STORED" if not ea or not eb else "DESCRIBED",
                "note": "From the stored arm descriptions. Two different drugs necessarily escalate differently; escalation affects tolerability, discontinuation and hence the contrast."})
    wa, wb = a["timepoint_weeks"], b["timepoint_weeks"]
    out.append({"key": "follow_up", "label": "Follow-up (assessment week)", "a": f"week {_fmt(wa, 0)}" if wa is not None else "not stored", "b": f"week {_fmt(wb, 0)}" if wb is not None else "not stored",
                "assessment": "NOT_STORED" if wa is None or wb is None else "SIMILAR" if wa == wb else "DIFFERS" if abs(wa - wb) <= tolerance_weeks else "MAJOR_DIFFERENCE",
                "note": "Assessment week of each contrast." + (f" A difference of up to {_fmt(tolerance_weeks, 0)} weeks was accepted with a written justification; it remains an assumption." if tolerance_weeks else "")})
    ca, cb = pa["countries"], pb["countries"]
    shared = sorted(set(ca) & set(cb))
    out.append({"key": "region", "label": "Region (countries with sites)", "a": f"{len(ca)} countries: {', '.join(ca)}" if ca else "not stored",
                "b": f"{len(cb)} countries: {', '.join(cb)}" if cb else "not stored", "assessment": "NOT_STORED" if not ca or not cb else "DESCRIBED",
                "note": f"{len(shared)} of {len(set(ca) | set(cb))} countries are shared ({', '.join(shared) or 'none'}). The share of participants by region is not used here." if ca and cb else "Not stored for both trials."})
    xa, xb = a["comparator"]["administration"], b["comparator"]["administration"]
    out.append({"key": "comparator_administration", "label": "Comparator administration (placebo route and frequency)", "a": xa or "not stored", "b": xb or "not stored",
                "assessment": "NOT_STORED" if not xa or not xb else "SIMILAR" if xa == xb else "DIFFERS",
                "note": "Read from the comparator-arm description. Placebo response may differ between an injection and a daily tablet or capsule."})
    out.append({"key": "trial_era", "label": "Trial start (calendar era)", "a": a["start_date"] or "not stored", "b": b["start_date"] or "not stored",
                "assessment": "NOT_STORED" if not a["start_date"] or not b["start_date"] else "DESCRIBED",
                "note": "Standard of care, lifestyle support and the background use of other weight-management drugs can change over time."})
    return out


def _check_effect_modifiers(a: dict[str, Any], b: dict[str, Any], modifiers: list[dict[str, Any]]) -> Check:
    major = [m for m in modifiers if m["assessment"] == "MAJOR_DIFFERENCE"]
    open_ = [m for m in modifiers if m["assessment"] in ("NOT_STORED", "DIFFERS", "DESCRIBED")]
    if major:
        return Check("effect_modifiers", "Plausible effect modifiers", FAIL, f"{len(modifiers)} listed", f"{len(modifiers)} listed",
                     "Major imbalance in a plausible effect modifier: " + "; ".join(f"{m['label']} ({m['a']} versus {m['b']})" for m in major) + ".", {"modifiers": modifiers})
    if open_:
        return Check("effect_modifiers", "Plausible effect modifiers", CANNOT_ASSESS, f"{len(modifiers)} listed", f"{len(modifiers)} listed",
                     f"No major imbalance in the stored values, but {len(open_)} of {len(modifiers)} modifiers are not stored, only described, or differ: "
                     + ", ".join(m["label"].split(" (")[0].lower() for m in open_) + ". Balance in these is an unverified assumption.", {"modifiers": modifiers})
    return Check("effect_modifiers", "Plausible effect modifiers", PASS, f"{len(modifiers)} listed", f"{len(modifiers)} listed",
                 "Every listed modifier is stored for both trials and similar at the trial-mean level.", {"modifiers": modifiers})


def _check_transitivity(a: dict[str, Any], b: dict[str, Any], population: Check, modifiers_check: Check, modifiers: list[dict[str, Any]]) -> Check:
    assumed = (f"Assumed: had the participants of {a['study_name']} received {b['drug_name'] or 'drug B'} instead, its effect relative to "
               f"{a['comparator']['display'].lower()} would have been the one observed in {b['study_name']} (and vice versa) — i.e. the two trials do not differ in "
               "anything that modifies either drug's effect against the common comparator, and the comparator groups are exchangeable.")
    supports = [f"{i['label']}: {i['note']}" for i in population.evidence.get("items", []) if i["status"] == PASS]
    supports += [f"{m['label']}: {m['a']} versus {m['b']}" for m in modifiers if m["assessment"] == "SIMILAR"]
    undermines = [f"{i['label']}: {i['note']}" for i in population.evidence.get("items", []) if i["status"] == FAIL]
    undermines += [f"{m['label']}: {m['a']} versus {m['b']}" for m in modifiers if m["assessment"] in ("MAJOR_DIFFERENCE", "DIFFERS")]
    unknown = [m["label"] for m in modifiers if m["assessment"] in ("NOT_STORED", "DESCRIBED")]
    unknown += [i["label"] for i in population.evidence.get("items", []) if i["status"] == CANNOT_ASSESS]
    status = FAIL if FAIL in (population.status, modifiers_check.status) else CANNOT_ASSESS
    statement = assumed + (" With two trials and only trial-level summaries, transitivity cannot be tested: there is no closed loop (no direct A-versus-B evidence) "
                           "to check consistency against, and no individual data to adjust for modifiers.")
    return Check("transitivity", "Transitivity", status, a["study_name"], b["study_name"], statement,
                 {"assumed": assumed, "supported_by": supports, "undermined_by": undermines, "unverifiable": unknown})


# --------------------------------------------------------------------------- the assessment


def evaluate(a: dict[str, Any], b: dict[str, Any], level: float = 0.95, tolerance_weeks: float = 0.0, justification: str | None = None) -> dict[str, Any]:
    """Feasibility checklist for two described contrasts, and the anchored estimate only when every requirement allows it."""
    level = _valid_level(level)
    tolerance_weeks, justification = _valid_tolerance(tolerance_weeks, justification)
    variance, var_a, var_b = _check_variance(a, b)
    population = _check_population(a, b)
    modifiers = _effect_modifiers(a, b, tolerance_weeks)
    modifiers_check = _check_effect_modifiers(a, b, modifiers)
    checks = [_check_network(a, b), _check_comparator(a, b), _check_estimand(a, b), _check_outcome(a, b), _check_scale(a, b),
              _check_follow_up(a, b, tolerance_weeks, justification), _check_reliability(a, b), variance, population, modifiers_check,
              _check_transitivity(a, b, population, modifiers_check, modifiers)]
    by_key = {c.key: c for c in checks}
    refusals = [f"{c.label}: {c.detail}" for c in checks if (c.key in HARD_CHECKS and c.status != PASS) or (c.key in ASSUMPTION_CHECKS and c.status == FAIL)]
    assumptions = [{"key": c.key, "label": c.label, "detail": c.detail} for c in checks if c.key in ASSUMPTION_CHECKS and c.status == CANNOT_ASSESS]
    feasible = not refusals
    network = {
        "nodes": [{"id": "A", "label": a["drug_name"] or a["treatment"]["label"], "detail": a["treatment"]["label"]},
                  {"id": "C", "label": a["comparator"]["display"], "detail": f"{a['comparator']['arm_label'] or a['comparator']['label']} / {b['comparator']['arm_label'] or b['comparator']['label']}"},
                  {"id": "B", "label": b["drug_name"] or b["treatment"]["label"], "detail": b["treatment"]["label"]}],
        "edges": [{"from": "A", "to": "C", "trial": a["study_name"], "nct_id": a["nct_id"], "analysis_id": a["analysis_id"], "kind": "RANDOMIZED_WITHIN_TRIAL"},
                  {"from": "B", "to": "C", "trial": b["study_name"], "nct_id": b["nct_id"], "analysis_id": b["analysis_id"], "kind": "RANDOMIZED_WITHIN_TRIAL"},
                  {"from": "A", "to": "B", "trial": None, "nct_id": None, "analysis_id": None, "kind": "INDIRECT_NOT_RANDOMIZED"}],
        "text": (f"{a['drug_name'] or 'A'} ({a['treatment']['label']}) — randomized against {a['comparator']['display'].lower()} in {a['study_name']} ({a['nct_id']}); "
                 f"{b['drug_name'] or 'B'} ({b['treatment']['label']}) — randomized against {b['comparator']['display'].lower()} in {b['study_name']} ({b['nct_id']}). "
                 f"{a['drug_name'] or 'A'} and {b['drug_name'] or 'B'} were never randomized against each other: the A–B link is inferred through the common comparator."),
    }
    out: dict[str, Any] = {
        "method": METHOD_NAME,
        "label": ESTIMATE_LABEL,
        "reference": REFERENCE,
        "feasible": feasible,
        "status": "CALCULATED" if feasible else "REFUSED",
        "checks": [c.as_dict() for c in checks],
        "refusal_reasons": refusals,
        "unverified_assumptions": assumptions,
        "effect_modifiers": modifiers,
        "network": network,
        "inputs": [_input_dto("A", a, var_a), _input_dto("B", b, var_b)],
        "result": None,
        "not_a_randomized_comparison": (f"{a['drug_name'] or 'Drug A'} and {b['drug_name'] or 'drug B'} were not randomized against each other. Each input is a randomized "
                                        "contrast within its own trial; their difference is an indirect estimate that is unbiased only if transitivity holds."),
        "not_a_ranking": "This is one pairwise indirect estimate. It is not a ranking of treatments and must not be presented as one.",
    }
    if not feasible:
        out["headline"] = "Refused: an anchored indirect estimate cannot be calculated validly from these two contrasts."
        return out
    assert var_a is not None and var_b is not None
    ratio = a["effect_scale"]["scale"] == "RATIO"
    d_ac = math.log(a["estimate"]) if ratio else a["estimate"]
    d_bc = math.log(b["estimate"]) if ratio else b["estimate"]
    calc = bucher(d_ac, var_a["se"], d_bc, var_b["se"], level)
    out["result"] = _result_dto(a, b, calc, ratio, level, by_key)
    out["headline"] = (f"{ESTIMATE_LABEL}: {a['treatment']['label']} {'÷' if ratio else '−'} {b['treatment']['label']} = {out['result']['estimate_text']} "
                       f"({_fmt(level * 100, 2)}% CI {out['result']['ci_text']}). Valid only under the unverified assumptions listed.")
    return out


def _valid_level(level: Any) -> float:
    if isinstance(level, bool) or not isinstance(level, int | float) or math.isnan(level) or not 0.5 <= float(level) <= 0.999:
        raise IndirectInputError("ci_level must be a number from 0.5 to 0.999 (e.g. 0.95)")
    return float(level)


def _valid_tolerance(tolerance: Any, justification: str | None) -> tuple[float, str | None]:
    if isinstance(tolerance, bool) or not isinstance(tolerance, int | float) or math.isnan(tolerance) or not 0 <= float(tolerance) <= MAX_FOLLOW_UP_TOLERANCE_WEEKS:
        raise IndirectInputError(f"followup_tolerance_weeks must be between 0 and {MAX_FOLLOW_UP_TOLERANCE_WEEKS:g}")
    tolerance = float(tolerance)
    text = (justification or "").strip() or None
    if tolerance > 0 and (text is None or len(text) < MIN_JUSTIFICATION_CHARS):
        raise IndirectInputError(f"a follow-up tolerance above 0 weeks needs a written justification of at least {MIN_JUSTIFICATION_CHARS} characters")
    return tolerance, text if tolerance > 0 else None


def _input_dto(side: str, s: dict[str, Any], var: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "side": side, "analysis_id": s["analysis_id"], "nct_id": s["nct_id"], "study_name": s["study_name"], "drug": s["drug"], "drug_name": s["drug_name"],
        "treatment_label": s["treatment"]["label"], "comparator_label": s["comparator"]["label"], "comparator": s["comparator"]["display"],
        "orientation": f"{s['treatment']['label']} {'÷' if s['effect_scale']['scale'] == 'RATIO' else '−'} {s['comparator']['label']}",
        "endpoint_title": s["endpoint"]["title"], "category": s["endpoint"]["category"], "unit": s["endpoint"]["unit"], "timepoint_weeks": s["timepoint_weeks"],
        "estimand_label": s["estimand_label"], "estimand_basis": s["estimand_basis"], "param_type": s["param_type"], "scale": s["effect_scale"]["scale"],
        "estimate": s["estimate"], "estimate_text": s["estimate_text"], "ci_low": s["ci_low"], "ci_high": s["ci_high"], "ci_level": s["ci_level"], "ci_sides": s["ci_sides"],
        "p_value_text": s["p_value_text"], "hypothesis_type": s["hypothesis_type"], "multiplicity_controlled": s["multiplicity_controlled"],
        "se": var["se"] if var else None, "se_formula": var.get("formula") if var else None, "se_rounding_note": var.get("rounding_note") if var else None,
        "n_treatment": s["treatment"]["n_analyzed"], "n_comparator": s["comparator"]["n_analyzed"], "analysis_population": s["analysis_population"],
        "verification_status": s["verification_status"], "origin": s["origin"], "tier": s["tier"], "source": s["source"], "source_location": s["source_location"],
        "locator_quote": s["locator_quote"], "diabetes_status": s["population"]["diabetes_status"],
    }


def _result_dto(a: dict[str, Any], b: dict[str, Any], calc: dict[str, Any], ratio: bool, level: float, checks: dict[str, Check]) -> dict[str, Any]:
    back = math.exp if ratio else (lambda x: x)
    estimate, lower, upper = back(calc["d"]), back(calc["lower"]), back(calc["upper"])
    null = 1.0 if ratio else 0.0
    excludes = not (lower <= null <= upper)
    level_text = f"{_fmt(level * 100, 2)}%"
    p = calc["p_two_sided"]
    p_text = "<0.0001" if p < 0.0001 else f"{p:.4f}"
    if ratio:
        formula = (f"ln(ratio_AB) = ln({_fmt(a['estimate'])}) − ln({_fmt(b['estimate'])}) = {_fmt(calc['d'], 4)}; "
                   f"SE = √(SE_AC² + SE_BC²) = {_fmt(calc['se'], 4)} (log scale); CI = exp(ln ratio_AB ± {_fmt(calc['z_critical'], 4)} × SE)")
    else:
        formula = (f"d_AB = d_AC − d_BC = {_fmt(a['estimate'])} − ({_fmt(b['estimate'])}) = {_fmt(calc['d'], 4)}; "
                   f"SE_AB = √(SE_AC² + SE_BC²) = {_fmt(calc['se'], 4)}; CI = d_AB ± {_fmt(calc['z_critical'], 4)} × SE_AB")
    reading = (f"Under the assumptions listed, the anchored indirect estimate of {a['treatment']['label']} {'÷' if ratio else '−'} {b['treatment']['label']} on "
               f"{a['endpoint']['category_label']} at week {_fmt(a['timepoint_weeks'], 0)} ({a['estimand_label'].replace('_', ' ').lower()} estimand) is {_fmt(estimate)}"
               f" ({level_text} CI {_fmt(lower)} to {_fmt(upper)}). The interval {'excludes' if excludes else 'includes'} {_fmt(null)}, the value of no difference, "
               "but this rests on transitivity, which is not verified; it is not a randomized result, not a head-to-head trial and not a ranking.")
    return {
        "label": ESTIMATE_LABEL,
        "kind": "INFERENCE",
        "scale": "RATIO" if ratio else "DIFFERENCE",
        "orientation": f"{a['treatment']['label']} {'÷' if ratio else '−'} {b['treatment']['label']} ({a['drug_name']} {'÷' if ratio else '−'} {b['drug_name']})",
        "unit": None if ratio else a["endpoint"]["unit"],
        "estimate": estimate, "ci_low": lower, "ci_high": upper, "ci_level": level, "ci_sides": "TWO_SIDED",
        "estimate_text": _fmt(estimate), "ci_text": f"{_fmt(lower)} to {_fmt(upper)}",
        "se": calc["se"], "z_statistic": calc["z_statistic"], "z_critical": calc["z_critical"], "p_value": p, "p_value_text": p_text,
        "log_scale": {"d": calc["d"], "lower": calc["lower"], "upper": calc["upper"]} if ratio else None,
        "null_value": null, "ci_excludes_null": excludes,
        "formula": formula,
        "reading": reading,
        "assumptions": [
            "Transitivity: no difference between the two trials in any modifier of the treatment effects relative to the common comparator (unverifiable here; see the effect modifiers).",
            "The comparator groups of the two trials are exchangeable (same comparator, comparable administration and background care).",
            "Each standard error is recovered from a symmetric two-sided normal-approximation interval at its stated level.",
            "The two trials are independent, so the covariance of the two contrasts is zero.",
            f"Both contrasts estimate the same estimand ({a['estimand_label'].replace('_', ' ').lower()}); its label is {'inferred' if 'STATED_BY_SOURCE' not in (a['estimand_basis'], b['estimand_basis']) else 'partly inferred' if a['estimand_basis'] != b['estimand_basis'] else 'stated'} from the source.",
            "A fixed (common) effect per contrast: one trial each, so between-trial heterogeneity cannot be estimated and is not included in the interval.",
        ],
        "follow_up_tolerance": checks["follow_up"].evidence.get("justification"),
    }


# --------------------------------------------------------------------------- listing what can be chosen


def _contrast_rows(session: Session, category: str | None) -> list[StatisticalAnalysis]:
    query = select(StatisticalAnalysis).order_by(StatisticalAnalysis.id)
    rows = []
    for row in session.scalars(query):
        if category is not None and row.endpoint.category != category:
            continue
        if treatment_drug(row.treatment_arm) is None or comparator_key(row.comparator_arm) is None:
            continue
        rows.append(row)
    return rows


def options(session: Session, category: str | None = None) -> dict[str, Any]:
    """The drugs and common comparators that stored between-arm analyses offer (per endpoint category when given)."""
    names = _drug_names(session)
    drugs: dict[str, dict[str, Any]] = {}
    comparators: dict[str, int] = {}
    categories: dict[str, int] = {}
    for row in _contrast_rows(session, None):
        categories[row.endpoint.category] = categories.get(row.endpoint.category, 0) + 1
        if category is not None and row.endpoint.category != category:
            continue
        drug, key = treatment_drug(row.treatment_arm), comparator_key(row.comparator_arm)
        assert drug is not None and key is not None
        entry = drugs.setdefault(drug, {"slug": drug, "name": names.get(drug, drug), "comparators": {}, "analyses": 0})
        entry["analyses"] += 1
        entry["comparators"][key] = entry["comparators"].get(key, 0) + 1
        comparators[key] = comparators.get(key, 0) + 1
    return {
        "category": category,
        "categories": [{"key": k, "label": CATEGORY_LABEL.get(k, k), "analyses": n} for k, n in sorted(categories.items())],
        "drugs": [{**d, "comparators": [{"key": k, "label": _comparator_label(k, names), "analyses": n} for k, n in sorted(d["comparators"].items())]} for d in sorted(drugs.values(), key=lambda d: d["name"].lower())],
        "comparators": [{"key": k, "label": _comparator_label(k, names), "analyses": n} for k, n in sorted(comparators.items())],
    }


def candidate_summary(s: dict[str, Any]) -> dict[str, Any]:
    """One stored contrast as offered for selection: what it is, and what would stop it entering a calculation on its own."""
    flags, _ = _reliability_problems(s)
    var_status, var_reason, _ = _variance(s)
    if var_status != PASS:
        flags.append(var_reason)
    if s["estimand_label"] in (EstimandLabel.NOT_STATED.value, EstimandLabel.OTHER.value):
        flags.append("estimand not stated")
    if s["effect_scale"]["scale"] == "NOT_ESTABLISHED":
        flags.append("scale (difference or ratio) not established")
    return {
        "analysis_id": s["analysis_id"], "nct_id": s["nct_id"], "study_name": s["study_name"], "drug": s["drug"], "drug_name": s["drug_name"],
        "treatment_label": s["treatment"]["label"], "comparator_label": s["comparator"]["label"], "comparator_key": s["comparator"]["key"], "comparator": s["comparator"]["display"],
        "endpoint_title": s["endpoint"]["title"], "class_title": s["endpoint"]["class_title"], "category": s["endpoint"]["category"], "unit": s["endpoint"]["unit"],
        "timepoint_weeks": s["timepoint_weeks"], "estimand_label": s["estimand_label"], "estimand_basis": s["estimand_basis"], "param_type": s["param_type"],
        "estimate": s["estimate"], "estimate_text": s["estimate_text"], "ci_low": s["ci_low"], "ci_high": s["ci_high"], "ci_level": s["ci_level"], "ci_sides": s["ci_sides"],
        "verification_status": s["verification_status"], "tier": s["tier"], "origin": s["origin"], "diabetes_status": s["population"]["diabetes_status"],
        "usable_alone": not flags, "problems": flags,
    }


def candidates(session: Session, drug: str, comparator: str, category: str | None = None) -> list[dict[str, Any]]:
    names = _drug_names(session)
    out = []
    for row in _contrast_rows(session, category):
        if treatment_drug(row.treatment_arm) == drug and comparator_key(row.comparator_arm) == comparator:
            out.append(candidate_summary(describe_contrast(session, row, names)))
    return out


def assess(session: Session, analysis_a: int, analysis_b: int, level: float = 0.95, tolerance_weeks: float = 0.0, justification: str | None = None) -> dict[str, Any]:
    """Feasibility and (when feasible) the anchored estimate for two stored analyses: A versus C, and B versus C."""
    if analysis_a == analysis_b:
        raise IndirectInputError("choose two different analyses: one contrast cannot anchor against itself")
    rows = [session.get(StatisticalAnalysis, analysis_a), session.get(StatisticalAnalysis, analysis_b)]
    if any(r is None for r in rows):
        raise LookupError("analysis not found (or excluded from analytic use after review)")
    names = _drug_names(session)
    a, b = (describe_contrast(session, r, names) for r in rows)  # type: ignore[arg-type]
    result = evaluate(a, b, level, tolerance_weeks, justification)
    result["multiplicity_note"] = _selection_note(session, a, b)
    return result


def _selection_note(session: Session, a: dict[str, Any], b: dict[str, Any]) -> str | None:
    if a["drug"] is None or b["drug"] is None or a["comparator"]["key"] is None:
        return None
    na = len(candidates(session, a["drug"], a["comparator"]["key"], a["endpoint"]["category"]))
    nb = len(candidates(session, b["drug"], a["comparator"]["key"], a["endpoint"]["category"]))
    if na * nb <= 1:
        return None
    return (f"{na} stored contrasts of {a['drug_name']} and {nb} of {b['drug_name']} against {a['comparator']['display'].lower()} exist on this endpoint ({na * nb} possible pairs: "
            "doses, time points, estimands). Choosing one pair after seeing the results is a selective, unadjusted comparison; report every feasible pair, not the most favourable.")


def scan(session: Session, category: str | None = None) -> dict[str, Any]:
    """Every cross-drug pair of stored contrasts that share a comparator, with its verdict (for audit and for the UI's shortcuts)."""
    names = _drug_names(session)
    sides = [describe_contrast(session, row, names) for row in _contrast_rows(session, category)]
    pairs: list[dict[str, Any]] = []
    for i, a in enumerate(sides):
        for b in sides[i + 1:]:
            if a["drug"] == b["drug"] or a["comparator"]["key"] != b["comparator"]["key"] or a["endpoint"]["category"] != b["endpoint"]["category"]:
                continue
            first, second = (a, b) if (a["drug"] or "") <= (b["drug"] or "") else (b, a)
            verdict = evaluate(first, second)
            failing = [c for c in verdict["checks"] if (c["key"] in HARD_CHECKS and c["status"] != PASS) or (c["key"] in ASSUMPTION_CHECKS and c["status"] == FAIL)]
            pairs.append({"a": first["analysis_id"], "b": second["analysis_id"], "a_label": f"{first['study_name']} · {first['treatment']['label']}",
                          "b_label": f"{second['study_name']} · {second['treatment']['label']}", "category": first["endpoint"]["category"],
                          "comparator": first["comparator"]["key"], "feasible": verdict["feasible"], "failing": [c["key"] for c in failing],
                          "first_reason": f"{failing[0]['label']}: {failing[0]['status']}" if failing else None,
                          "estimate": verdict["result"]["estimate"] if verdict["result"] else None,
                          "ci": [verdict["result"]["ci_low"], verdict["result"]["ci_high"]] if verdict["result"] else None})
    counts: dict[str, int] = {}
    for p in pairs:
        for key in p["failing"]:
            counts[key] = counts.get(key, 0) + 1
    return {"category": category, "pairs_examined": len(pairs), "feasible": [p for p in pairs if p["feasible"]], "failing_requirement_counts": counts, "pairs": pairs}
