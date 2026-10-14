"""Efficacy and tolerability workbench.

Builds arm-level profiles from reported results, computes descriptive contrasts where
counts and denominators exist, and offers two explicitly preference-dependent or
hypothetical tools: a weighted utility summary and a dose-escalation scenario.

Nothing here estimates individual dose-response or recommends a dose. With aggregate data
the workbench describes arms; it does not model patients.

Rules that hold for every profile set:

* One arm, one profile. Registry and curated records of one randomized group are merged through the
  arm identity in ``findings.resolve_arm``; a reporting label that cannot be mapped stays separate.
* One estimand and one time point per efficacy metric. An arm without a value under that estimand at
  that week gets no value (it is listed under ``not_available``); another estimand or another week is
  never substituted. Which estimand and week were used, and why, is stated in ``selection``.
* Every number carries its estimand, time point, baseline, evidence tier and source. Where several
  sources report the same quantity all are returned; which one is shown first follows the single shared
  display rule in ``findings.display_order`` — a display choice, never a ranking by evidence tier.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import TIER_LABEL, EstimandLabel, EvidenceTier
from ..models import AdverseEvent, OutcomeResult, Study, StudyArm
from ..review.policy import is_usable
from .findings import (
    BASELINE_CLASS_LABEL,
    DISPLAY_RULE,
    DISPLAY_RULE_ADVERSE_EVENTS,
    ArmIdentity,
    ae_dto,
    arm_relation,
    baseline_class,
    build_finding,
    derived_interval,
    display_lead,
    resolve_arm,
)
from .safety import assert_single_measure_kind
from .stats import StatsInputError, _finite, _z, risk_difference, risk_ratio

EFFICACY_METRICS = {
    "weight_pct": ("WEIGHT_PCT_CHANGE", "Percent change in body weight", "%", "lower"),
    "weight_abs": ("WEIGHT_ABS_CHANGE", "Change in body weight", "kg", "lower"),
    "hba1c": ("HBA1C_CHANGE", "Change in HbA1c", "percentage points", "lower"),
}
GI_TERMS = {"nausea": "nausea", "vomiting": "vomiting", "diarrhoea": "diarrhoea", "diarrhea": "diarrhoea", "constipation": "constipation"}

IPD_REQUIREMENTS = [
    "Individual dose histories (dose at each visit, interruptions, down-titrations) and the reason for each change.",
    "Individual outcome trajectories aligned to those dose histories, to separate dose effects from time effects.",
    "Pharmacokinetic exposure per participant for exposure–response modelling.",
    "Timing and severity of adverse events per participant relative to dose changes.",
    "Baseline covariates per participant to examine effect modification.",
]

#: Order used only to break an exact tie when no estimand was requested (a display choice, stated in the output).
_ESTIMAND_TIE_ORDER = (EstimandLabel.TREATMENT_REGIMEN.value, EstimandLabel.EFFICACY.value, EstimandLabel.OTHER.value, EstimandLabel.NOT_STATED.value)


def _arm_key(arm: StudyArm) -> str:
    if arm.is_placebo:
        return "placebo"
    if arm.drug_slug and arm.dose_amount is not None and "+" not in arm.drug_slug and arm.drug_slug != "eloralintide-tirzepatide":
        return f"{arm.drug_slug}|{arm.dose_amount:g}|{arm.formulation_key or ''}"
    return arm.norm_label


def _tier_label(tier: str) -> str:
    return TIER_LABEL.get(EvidenceTier(tier), tier) if tier in EvidenceTier._value2member_map_ else tier


def _estimand_label(result: OutcomeResult) -> str:
    return result.estimand.label if result.estimand else EstimandLabel.NOT_STATED.value


def _source_brief(result: OutcomeResult) -> dict[str, Any]:
    """One alternative source of a quantity: enough to identify and open it."""
    return {
        "result_id": result.id,
        "value": result.value,
        "unit": result.unit,
        "ci_low": result.ci_low,
        "ci_high": result.ci_high,
        "ci_level": result.ci_level,
        "n_analyzed": result.n_analyzed,
        "tier": result.tier,
        "tier_label": _tier_label(result.tier),
        "source_key": result.source.key if result.source else None,
        "verification_status": result.verification_status,
        "arm_label": result.arm_label,
    }


def arm_profiles(session: Session, nct_id: str, estimand: str | None = None, timepoint_weeks: float | None = None) -> dict[str, Any]:
    """Arm-level efficacy and tolerability profile for one trial, each value with its finding."""
    study = session.scalar(select(Study).where(Study.nct_id == nct_id.upper()))
    if study is None:
        raise StatsInputError(f"Study {nct_id} is not on record")
    if timepoint_weeks is not None:
        timepoint_weeks = _finite(timepoint_weeks, "weeks", limit=1000)
    features = study.design_features or {}
    planned_weeks = features.get("treatment_weeks")
    identities: dict[int, ArmIdentity] = {arm.id: resolve_arm(session, arm) for arm in study.arms}
    members: dict[str, list[StudyArm]] = {}
    for arm in study.arms:
        members.setdefault(identities[arm.id].group_key, []).append(arm)

    def identity(arm: StudyArm) -> ArmIdentity:
        return identities[arm.id]

    def representative(group_key: str) -> StudyArm:
        """The record that names the group: the registered arm, unless only a reporting label states the dose."""
        rows = sorted(members[group_key], key=lambda x: x.id)
        registered = next((x for x in rows if f"arm:{x.id}" == group_key), rows[0])
        if registered.is_placebo or registered.dose_amount is not None:
            return registered
        return next((x for x in rows if x.dose_amount is not None), registered)

    profiles: dict[str, dict[str, Any]] = {}  # arm group key -> profile

    def profile_for(arm: StudyArm) -> dict[str, Any]:
        ident = identity(arm)
        if ident.group_key not in profiles:
            rep = representative(ident.group_key)
            n_values = [x.n_randomized for x in members[ident.group_key] if x.n_randomized]
            profiles[ident.group_key] = {
                "key": _arm_key(rep),
                "arm_group_key": ident.group_key,
                "arm_group_resolved": ident.resolved,
                "randomization_stratum": ident.stratum,
                "label": rep.label,
                # Every stored arm record that is this randomized group (registry arm, results group, curated labels).
                "labels_merged": [x.label for x in sorted(members[ident.group_key], key=lambda x: x.id)],
                "drug_slug": rep.drug_slug,
                "is_placebo": rep.is_placebo,
                "dose_amount": rep.dose_amount,
                "dose_unit": rep.dose_unit,
                "dose_text": rep.dose_text,
                "formulation_key": rep.formulation_key,
                # A stored 0 is the registry's first participant-flow period for a group that did not exist yet
                # (a lead-in). It is not a count of randomized participants and is not shown as one.
                "n_randomized": n_values[0] if n_values else None,
                "n_randomized_note": "The registry's first participant-flow period reports 0 participants started for this group; the number randomized is not available from the stored record."
                if not n_values and any(x.n_randomized == 0 for x in members[ident.group_key]) else None,
                "efficacy": {},
                "tolerability": {},
                "_norm": rep.norm_label,
            }
        return profiles[ident.group_key]

    results = [r for r in study.results if r.scope == "ARM" and r.value is not None and r.arm is not None and not r.arm.is_pooled and " minus " not in r.arm_label and is_usable(r.verification_status)]
    selection: dict[str, dict[str, Any]] = {}
    not_available: list[dict[str, Any]] = []

    for metric, (category, _label, _unit, _direction) in EFFICACY_METRICS.items():
        rows = [r for r in results if r.endpoint.category == category]
        if not rows:
            continue
        classes = {r.id: baseline_class(features, r.baseline_definition) for r in rows}
        excluded: Counter[str] = Counter()

        # -- one baseline per set
        if features.get("randomized_withdrawal"):
            wanted_baseline = "RANDOMIZATION_AFTER_LEAD_IN"
        else:
            by_class = Counter()
            for cls in {c for c in classes.values() if c != "SUBSET_BASELINE"}:
                by_class[cls] = len({identity(r.arm).group_key for r in rows if classes[r.id] == cls})
            wanted_baseline = max(sorted(by_class), key=lambda c: by_class[c]) if by_class else None
        kept = []
        for r in rows:
            if classes[r.id] != wanted_baseline:
                excluded[f"different baseline ({BASELINE_CLASS_LABEL.get(classes[r.id], classes[r.id])})"] += 1
            else:
                kept.append(r)

        # -- one time point per set
        arms_by_week: dict[float, set[str]] = {}
        for r in kept:
            if r.timepoint_weeks is not None:
                arms_by_week.setdefault(r.timepoint_weeks, set()).add(identity(r.arm).group_key)
        if timepoint_weeks is not None:
            week, week_basis = float(timepoint_weeks), "REQUESTED"
        elif planned_weeks is not None:
            week, week_basis = float(planned_weeks), "PLANNED_TREATMENT_DURATION (curated design feature)"
        elif arms_by_week:
            week = max(arms_by_week, key=lambda w: (len(arms_by_week[w]), w))
            week_basis = "MOST_ARMS_REPORTED (no planned treatment duration is on record: the week reported for the most arms, the later one on a tie — a display choice)"
        else:
            week, week_basis = None, "NOT_STATED (no result of this endpoint has a stated assessment week)"
        at_week = []
        for r in kept:
            if week is None:
                at_week.append(r)
            elif r.timepoint_weeks is None:
                excluded["assessment week not stated"] += 1
            elif abs(r.timepoint_weeks - week) > 0.5:
                excluded[f"other time point (week {r.timepoint_weeks:g})"] += 1
            else:
                at_week.append(r)

        # -- one estimand per set
        groups_by_estimand: dict[str, set[str]] = {}
        stated_by_estimand: Counter[str] = Counter()
        for r in at_week:
            label = _estimand_label(r)
            groups_by_estimand.setdefault(label, set()).add(identity(r.arm).group_key)
            if r.estimand is not None and r.estimand.basis == "STATED_BY_SOURCE":
                stated_by_estimand[label] += 1
        if estimand is not None:
            used, estimand_basis = estimand, "REQUESTED"
        elif groups_by_estimand:
            used = max(groups_by_estimand, key=lambda e: (len(groups_by_estimand[e]), stated_by_estimand[e], -(_ESTIMAND_TIE_ORDER.index(e) if e in _ESTIMAND_TIE_ORDER else len(_ESTIMAND_TIE_ORDER))))
            estimand_basis = "MOST_ARMS_REPORTED (no estimand was requested: the estimand reported for the most arms at this time point; ties go to the one more often stated by the source — a display choice)"
        else:
            used, estimand_basis = None, "NONE_AVAILABLE"
        selection[metric] = {
            "estimand": used,
            "estimand_basis": estimand_basis,
            "timepoint_weeks": week,
            "timepoint_basis": week_basis,
            "baseline_class": wanted_baseline,
            "baseline_label": BASELINE_CLASS_LABEL.get(wanted_baseline, wanted_baseline) if wanted_baseline else None,
            "arms_by_estimand_at_timepoint": {label: len(keys) for label, keys in sorted(groups_by_estimand.items())},
            "arms_by_week": {f"{w:g}": len(keys) for w, keys in sorted(arms_by_week.items())},
            "rows_not_used": dict(excluded),
        }

        by_group: dict[str, list[OutcomeResult]] = {}
        for r in at_week:
            by_group.setdefault(identity(r.arm).group_key, []).append(r)
        for _group_key, group_rows in by_group.items():
            profile = profile_for(group_rows[0].arm)
            matching = [r for r in group_rows if _estimand_label(r) == used]
            if not matching:
                others = Counter(_estimand_label(r) for r in group_rows)
                not_available.append({
                    "arm_group_key": profile["arm_group_key"], "arm": profile["label"], "metric": metric,
                    "reason": f"No {str(used).replace('_', ' ').lower()}-estimand value at week {week:g} is stored for this arm." if week is not None else f"No {str(used).replace('_', ' ').lower()}-estimand value is stored for this arm.",
                    "values_under_other_estimands": dict(others),
                    "note": "A value under another estimand is not substituted.",
                })
                continue
            best, others = display_lead(matching)
            finding = build_finding(session, best)
            derived = derived_interval(finding)
            profile["efficacy"][metric] = {
                "result_id": best.id,
                "value": best.value,
                "unit": best.unit,
                "ci_low": best.ci_low,
                "ci_high": best.ci_high,
                "ci_level": best.ci_level,
                "se": best.dispersion_value if best.dispersion_type and "standard error" in best.dispersion_type.lower() else None,
                # Derived from the reported standard error; an inference, separate from any reported interval.
                "derived_interval": derived,
                "n_analyzed": best.n_analyzed,
                "estimand_label": finding.estimand_label,
                "estimand_basis": finding.estimand_basis,
                "timepoint_weeks": best.timepoint_weeks,
                "baseline_definition": best.baseline_definition,
                "baseline_class": finding.baseline_class,
                "tier": best.tier,
                "tier_label": _tier_label(best.tier),
                "verification_status": best.verification_status,
                "labels": finding.labels,
                "statement": finding.statement,
                "source": finding.source,
                "arm_label_in_source": best.arm_label,
                "alternatives": len(others),
                "other_sources": [_source_brief(r) for r in others],
                "sources_report_same_value": len({r.value for r in matching}) == 1,
                "display_choice": DISPLAY_RULE if others else None,
            }

    events = [e for e in study.adverse_events if e.scope == "ARM" and e.arm is not None and not e.arm.is_pooled and is_usable(e.verification_status)]
    candidates: dict[tuple[str, str], list[AdverseEvent]] = {}
    for event in events:
        low = event.term.strip().lower()
        name = None
        if event.measure_kind == "TREATMENT_DISCONTINUATION_DUE_TO_AE":
            name = "treatment_discontinuation_ae"
        elif event.measure_kind == "STUDY_WITHDRAWAL_DUE_TO_AE":
            name = "study_withdrawal_ae"
        elif event.measure_kind == "PARTICIPANTS_WITH_EVENT":
            if low == "any serious adverse event" or ("serious adverse event" in low and "any" in low) or (low.startswith("participants with") and "serious" in low):
                name = "serious_ae_any"
            elif low in GI_TERMS and not event.is_serious:
                # The exact term from the non-serious (or publication) table; 'diarrhoea haemorrhagic' is another term.
                name = GI_TERMS[low]
        if name is not None:
            profile_for(event.arm)
            candidates.setdefault((identity(event.arm).group_key, name), []).append(event)

    def ae_entry(event: AdverseEvent) -> dict[str, Any]:
        dto = ae_dto(event)
        percent = dto["percent_reported"] if dto["percent_reported"] is not None else dto["percent_calculated"]
        return {
            "ae_id": event.id,
            "term": event.term,
            "is_serious": event.is_serious,
            "measure_kind": event.measure_kind,
            "measure_kind_label": dto["measure_kind_label"],
            "percent": percent,
            "percent_basis": "reported" if dto["percent_reported"] is not None else ("calculated from reported counts" if percent is not None else None),
            "n_affected": event.n_affected,
            "n_at_risk": event.n_at_risk,
            "period": event.period,
            "tier": event.tier,
            "tier_label": _tier_label(event.tier),
            "source": dto["source"],
            "verification_status": event.verification_status,
        }

    for (group_key, name), rows in candidates.items():
        # Different reporting periods are different quantities: the period stored first is shown, the others are listed.
        first_period = min(rows, key=lambda e: e.id).period
        same_period = [e for e in rows if e.period == first_period]
        lead, others = display_lead(same_period, kind="adverse_event")
        if lead is None:
            continue
        entry = ae_entry(lead)
        entry["other_sources"] = [ae_entry(e) for e in others]
        entry["other_periods"] = [ae_entry(e) for e in sorted(rows, key=lambda e: e.id) if e.period != first_period]
        entry["display_choice"] = DISPLAY_RULE_ADVERSE_EVENTS + " A measure reported for several periods is shown for the period stored first; the other periods are listed." if (others or entry["other_periods"]) else None
        profiles[group_key]["tolerability"][name] = entry

    ordered = sorted(profiles.values(), key=lambda p: (not p["is_placebo"], p["drug_slug"] or "", p["dose_amount"] if p["dose_amount"] is not None else 1e9, p["label"]))
    ordered = [p for p in ordered if p["efficacy"] or p["tolerability"]]
    key_counts = Counter(p["key"] for p in ordered)
    for p in ordered:  # two different arms must never share a key (e.g. three escalation schemes ending at the same dose)
        if key_counts[p["key"]] > 1:
            p["key"] = f"{p['key']}|{p.pop('_norm')}"
        else:
            p.pop("_norm")
    strata = sorted({p["randomization_stratum"] for p in ordered})
    notes = [
        "Arm-level values are descriptive. The randomized contrast with the control arm, where reported, is the treatment effect.",
        "Tolerability measures of different kinds (treatment discontinuation, study withdrawal, event incidence) are separate columns and are never combined.",
        "Values within a trial are comparable across its randomized arms; they are not comparable across trials without the checks in the comparison laboratory.",
        "Each efficacy metric uses one estimand, one time point and one baseline for all arms (see 'selection'); an arm without such a value is listed under 'not_available' and never given another estimand or week.",
    ]
    if len(strata) > 1:
        notes.append("This registration contains separately randomized sub-studies or periods (" + "; ".join(s or "main randomization" for s in strata) + "): arms of different ones were not randomized against each other.")
    messages = []
    for metric, sel in selection.items():
        if sel["estimand"] is None and sel["arms_by_week"] and sel["timepoint_weeks"] is not None:
            messages.append(
                f"No {EFFICACY_METRICS[metric][1][0].lower() + EFFICACY_METRICS[metric][1][1:]} is stored at week {sel['timepoint_weeks']:g} ({sel['timepoint_basis'].split(' (')[0].replace('_', ' ').lower()}). "
                f"Values exist at week(s) {', '.join(sel['arms_by_week'])}; none is substituted — request one of those weeks explicitly."
            )
    first = selection.get("weight_pct") or next(iter(selection.values()), {})
    return {
        "study": {"nct_id": study.nct_id, "display_name": study.display_name, "phase": study.phase, "diabetes_status": study.population.diabetes_status if study.population else "NOT_STATED", "design_features": study.design_features},
        "estimand_requested": estimand,
        "timepoint_weeks": timepoint_weeks or planned_weeks or first.get("timepoint_weeks"),
        "timepoint_requested": timepoint_weeks,
        "selection": selection,
        "not_available": not_available,
        "messages": messages,
        "arms": ordered,
        "display_rule": DISPLAY_RULE,
        "notes": notes,
    }


def arm_contrast(events_a: int, n_a: int, events_b: int, n_b: int, measure_kind_a: str, measure_kind_b: str, same_study: bool) -> dict[str, Any]:
    """Risk difference and risk ratio between two arms, refused unless the measure and trial match."""
    assert_single_measure_kind({measure_kind_a, measure_kind_b})
    if measure_kind_a != measure_kind_b:
        raise StatsInputError("Both arms must report the same adverse-event measure.")
    if not same_study:
        raise StatsInputError("Risk differences and ratios are only offered between arms of the same trial; arms of different trials are not a randomized contrast.")
    return {"risk_difference": risk_difference(events_a, n_a, events_b, n_b), "risk_ratio": risk_ratio(events_a, n_a, events_b, n_b), "measure_kind": measure_kind_a}


def _term_key(event: AdverseEvent) -> str:
    return " ".join(event.term.lower().replace("diarrhea", "diarrhoea").split())


def adverse_event_contrast(session: Session, a: AdverseEvent, b: AdverseEvent) -> dict[str, Any]:
    """Risk contrast between two stored adverse-event rows — refused unless it is one event in two randomized arms.

    Required: two different rows; one trial; single (non-pooled) arms that are two different groups of the same
    randomization; the same measure kind, term, seriousness class, organ system and reporting period; and counts
    with denominators on both sides (a missing count is not zero).
    """
    if a.id == b.id:
        raise StatsInputError("A row cannot be contrasted with itself: choose the same event in two different arms.")
    if a.study_id != b.study_id:
        raise StatsInputError("Risk differences and ratios are only offered between arms of the same trial; arms of different trials are not a randomized contrast.")
    assert_single_measure_kind({a.measure_kind, b.measure_kind})
    if a.measure_kind != b.measure_kind:
        raise StatsInputError("Both arms must report the same adverse-event measure.")
    if not (is_usable(a.verification_status) and is_usable(b.verification_status)):
        raise StatsInputError("At least one row was rejected in review and is excluded from calculations.")
    if a.scope != "ARM" or b.scope != "ARM" or a.arm is None or b.arm is None:
        raise StatsInputError("Both rows must belong to a single arm (a range across arms or an unattributed row cannot be contrasted).")
    if a.arm.is_pooled or b.arm.is_pooled:
        raise StatsInputError("Pooled groups are not single randomized arms; a contrast with a pooled group is not offered.")
    if _term_key(a) != _term_key(b):
        raise StatsInputError(f"The rows are different adverse-event terms ('{a.term}' and '{b.term}'); a contrast needs the same event in both arms.")
    if bool(a.is_serious) != bool(b.is_serious):
        raise StatsInputError("One row counts serious events and the other non-serious events; they are different quantities.")
    if a.organ_system and b.organ_system and a.organ_system != b.organ_system:
        raise StatsInputError(f"The rows are classified under different organ systems ('{a.organ_system}' and '{b.organ_system}').")
    if (a.period or "") != (b.period or ""):
        raise StatsInputError(f"The rows cover different reporting periods ('{a.period or 'not stated'}' and '{b.period or 'not stated'}'); events accumulate with time, so they are not comparable.")
    relation, why = arm_relation(session, a.arm, b.arm)
    if relation == "SAME_ARM":
        raise StatsInputError(f"Both rows belong to the same arm. {why} A contrast needs two different randomized arms.")
    if relation != "DISTINCT_ARMS":
        raise StatsInputError(f"The two rows are not established as two arms randomized against each other. {why}")
    if (a.study.allocation or "").upper() != "RANDOMIZED":
        raise StatsInputError("The trial's allocation is not recorded as randomized; a between-arm risk contrast is not offered.")
    contrast = arm_contrast(a.n_affected, a.n_at_risk, b.n_affected, b.n_at_risk, a.measure_kind, b.measure_kind, True)
    caveats = []
    if a.source_id != b.source_id:
        caveats.append("The two counts come from different sources, which may define or ascertain the event differently.")
    return {**contrast, "term": a.term, "period": a.period, "is_serious": bool(a.is_serious), "orientation": f"A − B (A = {a.arm_label}, B = {b.arm_label})", "caveats": caveats}


DEFAULT_UTILITY = {
    "weight_pct": {"weight": 0.6, "best": -25.0, "worst": 0.0},
    "treatment_discontinuation_ae": {"weight": 0.25, "best": 0.0, "worst": 20.0},
    "nausea": {"weight": 0.15, "best": 0.0, "worst": 50.0},
}


def utility_scores(profile_set: dict[str, Any], criteria: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """Weighted-sum summary of researcher-chosen criteria. Preference-dependent by construction."""
    criteria = criteria or DEFAULT_UTILITY
    for name, c in criteria.items():
        for field in ("weight", "best", "worst"):
            _finite(c.get(field), f"criterion '{name}': {field}")
        if c["weight"] < 0:
            raise StatsInputError(f"Criterion '{name}': the weight cannot be negative")
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
            entry = None
            if name in arm["efficacy"]:
                entry = arm["efficacy"][name]
                value = entry["value"]
            elif name in arm["tolerability"]:
                entry = arm["tolerability"][name]
                value = entry["percent"]
            if value is None:
                missing.append(name)
                continue
            partial = (value - c["worst"]) / (c["best"] - c["worst"])
            partial = min(1.0, max(0.0, partial))
            weight = c["weight"] / total_weight
            components.append({"criterion": name, "value": value, "partial_value": partial, "normalized_weight": weight, "contribution": partial * weight,
                               "estimand_label": entry.get("estimand_label"), "timepoint_weeks": entry.get("timepoint_weeks"), "period": entry.get("period"), "tier": entry.get("tier"),
                               "result_id": entry.get("result_id"), "ae_id": entry.get("ae_id")})
            score += partial * weight
        rows.append({"arm": arm["label"], "key": arm["key"], "randomization_stratum": arm.get("randomization_stratum", ""), "score": None if missing else score, "components": components, "missing_criteria": missing,
                     "note": "Score not computed: a criterion value is not reported for this arm (missing is not scored as zero)." if missing else None})
    return {
        "label": "Exploratory, preference-dependent summary — not a validated clinical endpoint",
        "kind": "INFERENCE",
        "criteria": criteria,
        "rows": rows,
        "selection": profile_set.get("selection", {}),
        "method": "Linear partial-value functions between researcher-chosen 'worst' and 'best' anchors, combined by a weighted sum (weights normalised to 1).",
        "caveats": [
            "The ordering of arms depends entirely on the chosen weights and anchors; different defensible choices can reverse it.",
            "Uncertainty in the underlying estimates is not propagated into the score.",
            "The score is not a regulatory or clinical benefit–risk assessment and does not support treatment decisions.",
            "Scores are comparable only between arms that were randomized against each other in the same trial.",
        ],
    }


def dose_scenario(profile_set: dict[str, Any], shares: dict[str, float], metric: str = "weight_pct", non_persistent_share: float = 0.0, non_persistent_value: float | None = None) -> dict[str, Any]:
    """Hypothetical mixture: what the population mean would be IF given shares ended at each studied dose.

    The scenario assumes that people who settle at a dose experience the average outcome of the
    trial arm randomized to that dose — an exchangeability assumption that aggregate data cannot test.
    The arm values used must share one estimand, one time point, one baseline and one randomization;
    the function checks this and refuses a mixture of different quantities.
    """
    arms = {a["key"]: a for a in profile_set["arms"]}
    shares = {key: _finite(share, f"share for '{key}'") for key, share in shares.items()}
    non_persistent_share = _finite(non_persistent_share, "non-persistent share")
    if non_persistent_value is not None:
        non_persistent_value = _finite(non_persistent_value, "value for participants who do not persist")
    total = sum(shares.values()) + non_persistent_share
    if any(s < 0 for s in shares.values()) or non_persistent_share < 0:
        raise StatsInputError("Shares cannot be negative")
    if abs(total - 1.0) > 1e-6:
        raise StatsInputError(f"Shares must sum to 1 (they sum to {total:.4f})")
    components = []
    used: list[dict[str, Any]] = []
    strata: set[str] = set()
    mean = 0.0
    variance: float | None = 0.0
    for key, share in shares.items():
        arm = arms.get(key)
        if arm is None or metric not in arm["efficacy"]:
            raise StatsInputError(f"No reported '{metric}' value for arm '{key}' in this trial; the scenario is not computed with a substitute value")
        entry = arm["efficacy"][metric]
        used.append(entry)
        strata.add(arm.get("randomization_stratum", ""))
        mean += share * entry["value"]
        if entry["se"] is None:
            variance = None
        elif variance is not None:
            variance += (share * entry["se"]) ** 2
        components.append({"arm": arm["label"], "share": share, "value": entry["value"], "se": entry["se"], "result_id": entry["result_id"],
                           "estimand_label": entry.get("estimand_label"), "timepoint_weeks": entry.get("timepoint_weeks"), "tier": entry.get("tier")})
    placebo_entry = None
    if non_persistent_share > 0:
        if non_persistent_value is None:
            placebo = next((a for a in profile_set["arms"] if a["is_placebo"] and metric in a["efficacy"] and (not strata or a.get("randomization_stratum", "") in strata)), None)
            if placebo is None:
                raise StatsInputError("A value for participants who do not persist is required (no placebo arm value is available to use as the assumption)")
            placebo_entry = placebo["efficacy"][metric]
            used.append(placebo_entry)
            non_persistent_value = placebo_entry["value"]
            assumption = f"Non-persistent participants assumed to have the placebo-arm mean ({non_persistent_value:g})."
        else:
            assumption = f"Non-persistent participants assumed to have a mean of {non_persistent_value:g} (researcher-specified)."
        mean += non_persistent_share * non_persistent_value
        components.append({"arm": "Not persisting on treatment (assumed)", "share": non_persistent_share, "value": non_persistent_value, "se": None,
                           "result_id": placebo_entry["result_id"] if placebo_entry else None})
    else:
        assumption = "All participants assumed to persist on treatment."

    # The mixture is only meaningful over one quantity: one estimand, one time point, one baseline, one randomization.
    estimands = {e.get("estimand_label") for e in used}
    weeks = {e.get("timepoint_weeks") for e in used}
    baselines = {e.get("baseline_class") for e in used}
    if len(estimands) > 1:
        raise StatsInputError(f"The arm values use different estimands ({', '.join(sorted(str(e) for e in estimands))}); a mixture of them is not one quantity and is not computed.")
    if len(weeks) > 1:
        raise StatsInputError(f"The arm values are from different time points (weeks {', '.join(sorted(str(w) for w in weeks))}); a mixture of them is not computed.")
    if len(baselines) > 1:
        raise StatsInputError("The arm values are measured from different baselines; a mixture of them is not computed.")
    if len(strata) > 1:
        raise StatsInputError("The arms belong to separately randomized sub-studies or periods; a mixture across them is not computed.")
    if not math.isfinite(mean):
        raise StatsInputError("The scenario mean is not a finite number for these inputs; no result is returned.")
    estimand_used, week_used = next(iter(estimands), None), next(iter(weeks), None)
    se = math.sqrt(variance) if variance is not None and non_persistent_share == 0 else None
    z = _z(0.95)
    return {
        "label": "HYPOTHETICAL SCENARIO — not an estimate of any real dosing strategy",
        "kind": "SIMULATION",
        "metric": metric,
        "estimand": estimand_used,
        "timepoint_weeks": week_used,
        "scenario_mean": mean,
        "scenario_se": se,
        "scenario_interval_95": [mean - z * se, mean + z * se] if se is not None else None,
        "interval_note": "Reflects only sampling error of the arm means (treated as independent), not uncertainty in the shares or in the exchangeability assumption."
        if se is not None
        else "No interval: a standard error is not reported for every arm used (or a non-persistence assumption is included).",
        "components": components,
        "assumptions": [
            "Participants who end at a given dose are assumed to have the average outcome of the arm randomized to that dose (exchangeability; untestable with aggregate data).",
            "Shares are researcher inputs, not observed proportions.",
            assumption,
            "All values come from one trial, one estimand "
            f"({str(estimand_used).replace('_', ' ').lower()}) and one time point ({'week ' + format(week_used, 'g') if week_used is not None else 'assessment week not stated'}); this was checked, not assumed.",
        ],
        "not_supported": [
            "Individualised dose–response estimation.",
            "Identification of an optimal dose or escalation schedule.",
            "Any treatment recommendation.",
        ],
        "individual_data_required": IPD_REQUIREMENTS,
    }
