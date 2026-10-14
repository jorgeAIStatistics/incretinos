"""Efficacy and tolerability workbench.

Builds arm-level profiles from reported results, computes descriptive contrasts where
counts and denominators exist, and offers two explicitly preference-dependent or
hypothetical tools: a weighted utility summary and a dose-escalation scenario.

Nothing here estimates individual dose-response or recommends a dose. With aggregate data
the workbench describes arms; it does not model patients.
"""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import TIER_RANK, EvidenceTier
from ..models import AdverseEvent, OutcomeResult, Study, StudyArm
from .findings import ae_dto, build_finding
from .safety import assert_single_measure_kind
from .stats import StatsInputError, risk_difference, risk_ratio

EFFICACY_METRICS = {
    "weight_pct": ("WEIGHT_PCT_CHANGE", "Percent change in body weight", "%", "lower"),
    "weight_abs": ("WEIGHT_ABS_CHANGE", "Change in body weight", "kg", "lower"),
    "hba1c": ("HBA1C_CHANGE", "Change in HbA1c", "percentage points", "lower"),
}
GI_TERMS = ["nausea", "vomiting", "diarrhoea", "diarrhea", "constipation"]

IPD_REQUIREMENTS = [
    "Individual dose histories (dose at each visit, interruptions, down-titrations) and the reason for each change.",
    "Individual outcome trajectories aligned to those dose histories, to separate dose effects from time effects.",
    "Pharmacokinetic exposure per participant for exposure–response modelling.",
    "Timing and severity of adverse events per participant relative to dose changes.",
    "Baseline covariates per participant to examine effect modification.",
]


def _arm_key(arm: StudyArm) -> str:
    if arm.is_placebo:
        return "placebo"
    if arm.drug_slug and arm.dose_amount is not None and "+" not in arm.drug_slug and arm.drug_slug != "eloralintide-tirzepatide":
        return f"{arm.drug_slug}|{arm.dose_amount:g}|{arm.formulation_key or ''}"
    return arm.norm_label


def _rank(result: OutcomeResult, estimand: str | None) -> tuple:
    label = result.estimand.label if result.estimand else "NOT_STATED"
    tier = TIER_RANK.get(EvidenceTier(result.tier), 9) if result.tier in EvidenceTier._value2member_map_ else 9
    return (0 if (estimand is None or label == estimand) else 1, tier, 0 if result.ci_low is not None or result.dispersion_value is not None else 1, result.id)


def arm_profiles(session: Session, nct_id: str, estimand: str | None = None, timepoint_weeks: float | None = None) -> dict[str, Any]:
    """Arm-level efficacy and tolerability profile for one trial, each value with its finding."""
    study = session.scalar(select(Study).where(Study.nct_id == nct_id.upper()))
    if study is None:
        raise StatsInputError(f"Study {nct_id} is not on record")
    results = [r for r in study.results if r.scope == "ARM" and r.value is not None and r.arm is not None and not r.arm.is_pooled and " minus " not in r.arm_label]
    primary_weeks = timepoint_weeks or (study.design_features or {}).get("treatment_weeks")
    profiles: dict[str, dict[str, Any]] = {}

    def profile_for(arm: StudyArm) -> dict[str, Any]:
        key = _arm_key(arm)
        if key not in profiles:
            profiles[key] = {
                "key": key,
                "label": arm.label,
                "drug_slug": arm.drug_slug,
                "is_placebo": arm.is_placebo,
                "dose_amount": arm.dose_amount,
                "dose_unit": arm.dose_unit,
                "dose_text": arm.dose_text,
                "formulation_key": arm.formulation_key,
                "n_randomized": arm.n_randomized,
                "efficacy": {},
                "tolerability": {},
            }
        if arm.n_randomized and not profiles[key]["n_randomized"]:
            profiles[key]["n_randomized"] = arm.n_randomized
        return profiles[key]

    for metric, (category, _label, _unit, _direction) in EFFICACY_METRICS.items():
        by_arm: dict[str, list[OutcomeResult]] = {}
        for r in results:
            if r.endpoint.category != category:
                continue
            if primary_weeks is not None and r.timepoint_weeks is not None and abs(r.timepoint_weeks - float(primary_weeks)) > 0.5:
                continue
            if study.design_features.get("randomized_withdrawal") and not (r.baseline_definition and "andomization" in r.baseline_definition):
                continue
            by_arm.setdefault(_arm_key(r.arm), []).append(r)
        for _key, rows in by_arm.items():
            best = sorted(rows, key=lambda r: _rank(r, estimand))[0]
            finding = build_finding(session, best)
            profile_for(best.arm)["efficacy"][metric] = {
                "result_id": best.id,
                "value": best.value,
                "unit": best.unit,
                "ci_low": best.ci_low,
                "ci_high": best.ci_high,
                "ci_level": best.ci_level,
                "se": best.dispersion_value if best.dispersion_type and "standard error" in best.dispersion_type.lower() else None,
                "n_analyzed": best.n_analyzed,
                "estimand_label": finding.estimand_label,
                "estimand_basis": finding.estimand_basis,
                "timepoint_weeks": best.timepoint_weeks,
                "tier": best.tier,
                "labels": finding.labels,
                "statement": finding.statement,
                "source": finding.source,
                "alternatives": len(rows) - 1,
            }

    events = [e for e in study.adverse_events if e.scope == "ARM" and e.arm is not None and not e.arm.is_pooled]

    def add_ae(profile: dict[str, Any], name: str, event: AdverseEvent) -> None:
        dto = ae_dto(event)
        percent = dto["percent_reported"] if dto["percent_reported"] is not None else dto["percent_calculated"]
        existing = profile["tolerability"].get(name)
        candidate = {
            "ae_id": event.id,
            "term": event.term,
            "measure_kind": event.measure_kind,
            "measure_kind_label": dto["measure_kind_label"],
            "percent": percent,
            "percent_basis": "reported" if dto["percent_reported"] is not None else ("calculated from reported counts" if percent is not None else None),
            "n_affected": event.n_affected,
            "n_at_risk": event.n_at_risk,
            "period": event.period,
            "tier": event.tier,
            "source": dto["source"],
            "verification_status": event.verification_status,
        }
        # Prefer peer-reviewed / label values over registry over press release; keep one per measure.
        def score(c: dict[str, Any]) -> tuple:
            return (TIER_RANK.get(EvidenceTier(c["tier"]), 9) if c["tier"] in EvidenceTier._value2member_map_ else 9, 0 if c["percent"] is not None else 1)

        if existing is None or score(candidate) < score(existing):
            profile["tolerability"][name] = candidate

    for event in events:
        profile = profile_for(event.arm)
        low = event.term.lower()
        if event.measure_kind == "TREATMENT_DISCONTINUATION_DUE_TO_AE":
            add_ae(profile, "treatment_discontinuation_ae", event)
        elif event.measure_kind == "STUDY_WITHDRAWAL_DUE_TO_AE":
            add_ae(profile, "study_withdrawal_ae", event)
        elif event.measure_kind == "PARTICIPANTS_WITH_EVENT":
            if low == "any serious adverse event" or ("serious adverse event" in low and "any" in low) or low.startswith("participants with") and "serious" in low:
                add_ae(profile, "serious_ae_any", event)
            # The registry's serious-event table lists the same terms with far smaller counts (participants with a
            # SERIOUS event of that term). The tolerability measure is the incidence of the event, not that subset.
            for term in GI_TERMS if not event.is_serious else []:
                if low == term or low.startswith(term):
                    add_ae(profile, "diarrhoea" if term.startswith("diarr") else term, event)

    ordered = sorted(profiles.values(), key=lambda p: (not p["is_placebo"], p["drug_slug"] or "", p["dose_amount"] if p["dose_amount"] is not None else 1e9, p["label"]))
    ordered = [p for p in ordered if p["efficacy"] or p["tolerability"]]
    return {
        "study": {"nct_id": study.nct_id, "display_name": study.display_name, "phase": study.phase, "diabetes_status": study.population.diabetes_status if study.population else "NOT_STATED", "design_features": study.design_features},
        "estimand_requested": estimand,
        "timepoint_weeks": primary_weeks,
        "arms": ordered,
        "notes": [
            "Arm-level values are descriptive. The randomized contrast with the control arm, where reported, is the treatment effect.",
            "Tolerability measures of different kinds (treatment discontinuation, study withdrawal, event incidence) are separate columns and are never combined.",
            "Values within a trial are comparable across its randomized arms; they are not comparable across trials without the checks in the comparison laboratory.",
        ],
    }


def arm_contrast(events_a: int, n_a: int, events_b: int, n_b: int, measure_kind_a: str, measure_kind_b: str, same_study: bool) -> dict[str, Any]:
    """Risk difference and risk ratio between two arms, refused unless the measure and trial match."""
    assert_single_measure_kind({measure_kind_a, measure_kind_b})
    if measure_kind_a != measure_kind_b:
        raise StatsInputError("Both arms must report the same adverse-event measure.")
    if not same_study:
        raise StatsInputError("Risk differences and ratios are only offered between arms of the same trial; arms of different trials are not a randomized contrast.")
    return {"risk_difference": risk_difference(events_a, n_a, events_b, n_b), "risk_ratio": risk_ratio(events_a, n_a, events_b, n_b), "measure_kind": measure_kind_a}


DEFAULT_UTILITY = {
    "weight_pct": {"weight": 0.6, "best": -25.0, "worst": 0.0},
    "treatment_discontinuation_ae": {"weight": 0.25, "best": 0.0, "worst": 20.0},
    "nausea": {"weight": 0.15, "best": 0.0, "worst": 50.0},
}


def utility_scores(profile_set: dict[str, Any], criteria: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """Weighted-sum summary of researcher-chosen criteria. Preference-dependent by construction."""
    criteria = criteria or DEFAULT_UTILITY
    total_weight = sum(c.get("weight", 0) for c in criteria.values())
    if total_weight <= 0:
        raise StatsInputError("At least one criterion needs a positive weight")
    for name, c in criteria.items():
        if c.get("best") == c.get("worst"):
            raise StatsInputError(f"Criterion '{name}': best and worst anchors must differ")
    rows = []
    for arm in profile_set["arms"]:
        components, missing = [], []
        score = 0.0
        for name, c in criteria.items():
            value = None
            if name in arm["efficacy"]:
                value = arm["efficacy"][name]["value"]
            elif name in arm["tolerability"]:
                value = arm["tolerability"][name]["percent"]
            if value is None:
                missing.append(name)
                continue
            partial = (value - c["worst"]) / (c["best"] - c["worst"])
            partial = min(1.0, max(0.0, partial))
            weight = c["weight"] / total_weight
            components.append({"criterion": name, "value": value, "partial_value": partial, "normalized_weight": weight, "contribution": partial * weight})
            score += partial * weight
        rows.append({"arm": arm["label"], "key": arm["key"], "score": None if missing else score, "components": components, "missing_criteria": missing,
                     "note": "Score not computed: a criterion value is not reported for this arm (missing is not scored as zero)." if missing else None})
    return {
        "label": "Exploratory, preference-dependent summary — not a validated clinical endpoint",
        "kind": "INFERENCE",
        "criteria": criteria,
        "rows": rows,
        "method": "Linear partial-value functions between researcher-chosen 'worst' and 'best' anchors, combined by a weighted sum (weights normalised to 1).",
        "caveats": [
            "The ordering of arms depends entirely on the chosen weights and anchors; different defensible choices can reverse it.",
            "Uncertainty in the underlying estimates is not propagated into the score.",
            "The score is not a regulatory or clinical benefit–risk assessment and does not support treatment decisions.",
            "Scores are comparable only between arms of the same trial.",
        ],
    }


def dose_scenario(profile_set: dict[str, Any], shares: dict[str, float], metric: str = "weight_pct", non_persistent_share: float = 0.0, non_persistent_value: float | None = None) -> dict[str, Any]:
    """Hypothetical mixture: what the population mean would be IF given shares ended at each studied dose.

    The scenario assumes that people who settle at a dose experience the average outcome of the
    trial arm randomized to that dose — an exchangeability assumption that aggregate data cannot test.
    """
    arms = {a["key"]: a for a in profile_set["arms"]}
    total = sum(shares.values()) + non_persistent_share
    if any(s < 0 for s in shares.values()) or non_persistent_share < 0:
        raise StatsInputError("Shares cannot be negative")
    if abs(total - 1.0) > 1e-6:
        raise StatsInputError(f"Shares must sum to 1 (they sum to {total:.4f})")
    components = []
    mean = 0.0
    variance: float | None = 0.0
    for key, share in shares.items():
        arm = arms.get(key)
        if arm is None or metric not in arm["efficacy"]:
            raise StatsInputError(f"No reported '{metric}' value for arm '{key}' in this trial; the scenario is not computed with a substitute value")
        entry = arm["efficacy"][metric]
        mean += share * entry["value"]
        if entry["se"] is None:
            variance = None
        elif variance is not None:
            variance += (share * entry["se"]) ** 2
        components.append({"arm": arm["label"], "share": share, "value": entry["value"], "se": entry["se"], "result_id": entry["result_id"]})
    if non_persistent_share > 0:
        if non_persistent_value is None:
            placebo = next((a for a in profile_set["arms"] if a["is_placebo"] and metric in a["efficacy"]), None)
            if placebo is None:
                raise StatsInputError("A value for participants who do not persist is required (no placebo arm value is available to use as the assumption)")
            non_persistent_value = placebo["efficacy"][metric]["value"]
            assumption = f"Non-persistent participants assumed to have the placebo-arm mean ({non_persistent_value:g})."
        else:
            assumption = f"Non-persistent participants assumed to have a mean of {non_persistent_value:g} (researcher-specified)."
        mean += non_persistent_share * non_persistent_value
        components.append({"arm": "Not persisting on treatment (assumed)", "share": non_persistent_share, "value": non_persistent_value, "se": None, "result_id": None})
    else:
        assumption = "All participants assumed to persist on treatment."
    se = math.sqrt(variance) if variance is not None and non_persistent_share == 0 else None
    return {
        "label": "HYPOTHETICAL SCENARIO — not an estimate of any real dosing strategy",
        "kind": "SIMULATION",
        "metric": metric,
        "scenario_mean": mean,
        "scenario_se": se,
        "scenario_interval_95": [mean - 1.96 * se, mean + 1.96 * se] if se is not None else None,
        "interval_note": "Reflects only sampling error of the arm means (treated as independent), not uncertainty in the shares or in the exchangeability assumption."
        if se is not None
        else "No interval: a standard error is not reported for every arm used (or a non-persistence assumption is included).",
        "components": components,
        "assumptions": [
            "Participants who end at a given dose are assumed to have the average outcome of the arm randomized to that dose (exchangeability; untestable with aggregate data).",
            "Shares are researcher inputs, not observed proportions.",
            assumption,
            "All values come from one trial, one estimand and one time point.",
        ],
        "not_supported": [
            "Individualised dose–response estimation.",
            "Identification of an optimal dose or escalation schedule.",
            "Any treatment recommendation.",
        ],
        "individual_data_required": IPD_REQUIREMENTS,
    }
