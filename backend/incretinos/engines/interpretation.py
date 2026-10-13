"""Deterministic scientific interpretation.

For every finding the engine states what was observed, in whom, on which endpoint and
analysis, what the result does and does not establish, what is missing, and which source
supports it. The text is assembled from rules over structured fields — no language model
is involved — so the same evidence always yields the same reading.

``guard_statement`` is the complementary check: it scans free text (for example a
model-drafted summary) for claims the evidence does not license.
"""

from __future__ import annotations

import re
from typing import Any

from ..enums import AE_KIND_LABEL, DISCONTINUATION_KINDS, EndpointRole, EstimandLabel, EvidenceTier, MechanismAction, ReceptorTarget, TriState
from .estimands import ESTIMAND_EXPLAINER
from .findings import CATEGORY_LABEL, EFFICACY_CATEGORIES, LEAN_MASS_CAVEAT, Finding, _fmt, format_ci, format_value, population_phrase
from .stats import ci_from_se

# --------------------------------------------------------------------------- pharmacologic class


def classify_drug(mechanisms: list[tuple[str, str]]) -> dict[str, Any]:
    """Derive the pharmacologic class from recorded receptor actions (never typed by hand)."""
    agonist = {t for t, action in mechanisms if action == MechanismAction.AGONIST.value}
    blockade = {t for t, action in mechanisms if action in (MechanismAction.ANTIBODY_BLOCKADE.value, MechanismAction.ANTAGONIST.value)}
    g, gip, gcg, amy = ReceptorTarget.GLP1R.value, ReceptorTarget.GIPR.value, ReceptorTarget.GCGR.value, ReceptorTarget.AMYR.value
    if agonist == {amy}:
        label = "Selective amylin receptor agonist"
    elif agonist == {g}:
        label = "GLP-1 receptor agonist"
    elif agonist == {gip}:
        label = "GIP receptor agonist"
    elif agonist == {gip, g}:
        label = "Dual GIP and GLP-1 receptor agonist"
    elif agonist == {gip, g, gcg}:
        label = "Triple GIP, GLP-1 and glucagon receptor agonist"
    elif agonist == {amy, gip, g}:
        label = "Combination: amylin receptor agonist + dual GIP and GLP-1 receptor agonist"
    elif ReceptorTarget.ACTRII.value in blockade and not agonist:
        label = "Activin type II receptor antibody"
    elif agonist:
        label = "Agonist of " + ", ".join(sorted(agonist))
    else:
        label = "Mechanism not on record"
    return {
        "class_label": label,
        "agonist_targets": sorted(agonist),
        "blocked_targets": sorted(blockade),
        "is_glp1_receptor_agonist": g in agonist,
        "is_amylin_receptor_agonist": amy in agonist,
        "is_gip_receptor_agonist": gip in agonist,
        "is_glucagon_receptor_agonist": gcg in agonist,
        "basis": "Derived from recorded receptor actions and their cited sources.",
    }


# --------------------------------------------------------------------------- finding interpretation


def interpret_finding(f: Finding) -> dict[str, Any]:
    endpoint = CATEGORY_LABEL.get(f.category, f.endpoint_title)
    observed = f"{f.arm_label}: {f.param_type.replace('_', ' ').lower() if f.param_type else 'reported value'} for {endpoint} = {format_value(f)}"
    if f.timepoint_weeks is not None:
        observed += f" at week {_fmt(f.timepoint_weeks, 0)}"
    if f.baseline_definition:
        observed += f", measured from {f.baseline_definition}"
    observed += f" ({format_ci(f)}" + (f"; {f.dispersion_type} {_fmt(f.dispersion_value)}" if f.dispersion_value is not None else "") + ")."

    population = population_phrase(f)
    if f.n_analyzed is not None:
        population += f"; {f.n_analyzed} participants analysed in this arm"
    if f.diabetes_basis:
        population += f". Basis for diabetes status — {f.diabetes_basis}"

    analysis_bits = []
    if f.estimand_label != EstimandLabel.NOT_STATED.value:
        analysis_bits.append(ESTIMAND_EXPLAINER[f.estimand_label])
        analysis_bits.append("Estimand label stated by the source." if "STATED" in f.estimand_basis else "Estimand label inferred from the registry's analysis-population text (an inference, not a statement by the source).")
    else:
        analysis_bits.append(ESTIMAND_EXPLAINER[EstimandLabel.NOT_STATED.value])
    if f.analysis_method:
        analysis_bits.append(f"Analysis model as described: {f.analysis_method[:400]}")
    if f.analysis_population:
        analysis_bits.append(f"Analysis population as described: {f.analysis_population[:400]}")

    establishes: list[str] = []
    not_established: list[str] = []
    missing: list[str] = []

    establishes.append(f"That this value was reported for this arm, endpoint and time point by the cited source ({(f.source or {}).get('tier_label', f.tier)}).")
    if f.is_placebo:
        establishes.append("The placebo-arm response under the trial's lifestyle/background intervention — the reference against which active arms are judged.")
    not_established.append("A treatment effect: an arm-level value is not a comparison. The effect is the randomized contrast with the trial's control arm.")
    if f.estimand_label == EstimandLabel.EFFICACY.value:
        not_established.append("The effect in all randomized participants as actually treated: the efficacy estimand assumes hypothetical continuation on treatment.")
    elif f.estimand_label == EstimandLabel.TREATMENT_REGIMEN.value:
        not_established.append("The effect in participants who remain on treatment: the treatment-regimen estimand averages over those who discontinued.")
    not_established.append("How this arm compares with arms of other trials (no randomization between trials).")
    not_established.append("The response of any individual patient: this is a group average.")
    if f.category in EFFICACY_CATEGORIES:
        not_established.append("Clinical-event benefit: weight and glycaemic measures are intermediate endpoints.")
    if f.role in (EndpointRole.EXPLORATORY.value, EndpointRole.POST_HOC.value):
        not_established.append("Any confirmatory conclusion: the endpoint is exploratory/post hoc.")
    if f.multiplicity_controlled == TriState.NO.value:
        not_established.append("Statistical significance in the confirmatory sense: the endpoint was outside multiplicity control.")
    if f.baseline_class == "RANDOMIZATION_AFTER_LEAD_IN":
        not_established.append("Change from study entry: this value is measured from randomization after an open-label lead-in, in participants who completed and tolerated that lead-in.")
    if f.is_body_composition_lean:
        not_established.append("Loss of skeletal muscle mass, strength or function. " + LEAN_MASS_CAVEAT)
    if f.phase and "PHASE2" in f.phase and "PHASE3" not in f.phase:
        not_established.append("Phase 3-level precision or generalisability: Phase 2 dose-finding arm.")
    if f.tier == EvidenceTier.PRESS_RELEASE.value:
        not_established.append("Anything beyond the sponsor's topline summary: no peer review, usually no confidence intervals, analysis details unpublished.")
    if f.scope == "RANGE_ACROSS_ARMS":
        not_established.append("The value for any particular arm: only a range across arms was disclosed.")

    names = {
        "confidence_interval": "Confidence interval for this arm-level value (not reported in this source; none estimated).",
        "n_analyzed": "Number of participants analysed.",
        "estimand_label": "Estimand (handling of treatment discontinuation) — not stated.",
        "estimand_label_stated_by_source": "An explicit estimand label from the source (the label shown is inferred).",
        "value": "The value itself (reported as not available).",
    }
    for key in f.missing_fields:
        missing.append(names.get(key, key))
    if f.multiplicity_controlled == TriState.NOT_STATED.value:
        missing.append("Whether the endpoint was inside the trial's multiplicity-control strategy.")
    if f.baseline_definition is None and f.category in EFFICACY_CATEGORIES:
        missing.append("Explicit baseline definition in this source.")

    derived = None
    if f.ci_low is None and f.value is not None and f.dispersion_value is not None and f.dispersion_type and "standard error" in f.dispersion_type.lower():
        derived = ci_from_se(f.value, f.dispersion_value)
        derived["display"] = f"Derived 95% interval {_fmt(derived['ci_low'])} to {_fmt(derived['ci_high'])} (calculated here from the reported standard error; not reported by the source)."

    return {
        "result_id": f.result_id,
        "statement": f.statement,
        "observed": observed,
        "population": population,
        "endpoint": {
            "title": f.endpoint_title,
            "category": f.category,
            "role": f.role,
            "multiplicity_controlled": f.multiplicity_controlled,
            "is_surrogate": f.category in EFFICACY_CATEGORIES or f.category in ("BLOOD_PRESSURE", "LIPIDS", "BODY_COMPOSITION"),
        },
        "analysis": analysis_bits,
        "establishes": establishes,
        "does_not_establish": not_established,
        "missing": missing,
        "derived_interval": derived,
        "labels": f.labels,
        "sources": [f.source] if f.source else [],
        "source_location": f.source_location,
        "locator_quote": f.locator_quote,
        "verification_status": f.verification_status,
        "epistemic_kind": "DOCUMENTED",
    }


def interpret_analysis(a: dict[str, Any]) -> dict[str, Any]:
    """Reading of a between-arm analysis (dict from ``findings.analysis_dto``)."""
    estimate, lo, hi = a["estimate"], a["ci_low"], a["ci_high"]
    param = a["param_type"] or "estimate"
    is_ratio = bool(re.search(r"ratio", param, re.IGNORECASE))
    null = 1.0 if is_ratio else 0.0
    observed = f"{a['treatment_label']} versus {a['comparator_label'] or 'comparator not stated'} — {param}: {_fmt(estimate)}"
    observed += f" ({_fmt(a['ci_level'], 1)}% CI {_fmt(lo)} to {_fmt(hi)})" if lo is not None and hi is not None else " (confidence interval not reported in this source)"
    observed += f"; p {a['p_value_text']}." if a["p_value_text"] else "; p-value not reported in this source."

    establishes: list[str] = []
    not_established: list[str] = []
    missing = []
    ci_excludes_null = None if lo is None or hi is None else not (lo <= null <= hi)
    exploratory = a["role"] in (EndpointRole.EXPLORATORY.value, EndpointRole.POST_HOC.value)
    uncontrolled = a["multiplicity_controlled"] == TriState.NO.value

    if a["hypothesis_type"] == "NON_INFERIORITY":
        margin = a["ni_margin"]
        if a["ni_met"] == TriState.YES.value:
            detail = f" (upper confidence limit {_fmt(hi)} is below the pre-specified margin of {_fmt(margin)})" if hi is not None and margin is not None else ""
            establishes.append(f"Non-inferiority on this endpoint: the treatment is not worse than the comparator by more than the pre-specified margin{detail}.")
        elif a["ni_met"] == TriState.NO.value:
            not_established.append("Non-inferiority: the margin was not excluded.")
        else:
            missing.append("Whether the non-inferiority margin was met (not stated in the stored source).")
        if a["superiority_met"] != TriState.YES.value:
            reason = []
            if ci_excludes_null is False:
                reason.append(f"the confidence interval includes {_fmt(null)}")
            if a["superiority_tested"] != TriState.YES.value:
                reason.append("the stored source reports a non-inferiority conclusion and does not report a superiority test")
            not_established.append("Superiority of the treatment over the comparator" + (": " + "; ".join(reason) if reason else "") + ". Non-inferiority must not be read as superiority.")
        if is_ratio and estimate is not None and estimate < 1 and ci_excludes_null is False:
            not_established.append(f"A reduction in risk: a point estimate below 1 with an interval spanning {_fmt(lo)} to {_fmt(hi)} is compatible with benefit, no difference, or harm up to the upper limit.")
    elif a["hypothesis_type"] == "SUPERIORITY":
        if ci_excludes_null and not exploratory and not uncontrolled:
            establishes.append("A between-arm difference in this trial on this endpoint and estimand (the confidence interval excludes no difference)."
                               + (" Multiplicity control is documented." if a["multiplicity_controlled"] == TriState.YES.value else " Whether this comparison was inside multiplicity control is not stated in the stored source."))
        elif ci_excludes_null is False:
            not_established.append("A between-arm difference: the confidence interval includes no difference.")
        elif ci_excludes_null is None:
            missing.append("A confidence interval, without which the precision of the difference cannot be judged.")
    else:
        missing.append("The hypothesis tested (superiority, non-inferiority or descriptive) is not stated in the stored source.")
        if ci_excludes_null is True:
            not_established.append("A confirmatory conclusion: the interval excludes no difference, but the test type and its place in the testing hierarchy are not on record.")
        elif ci_excludes_null is False:
            not_established.append("A between-arm difference: the confidence interval includes no difference.")

    if exploratory:
        not_established.append("Any confirmatory claim: the endpoint is exploratory. The result is hypothesis-generating.")
    if uncontrolled:
        not_established.append("Statistical significance in the confirmatory sense: the result was not controlled for multiplicity, so its p-value is nominal.")
    if a["tier"] == EvidenceTier.PRESS_RELEASE.value:
        not_established.append("Peer-reviewed confirmation: this figure comes from a sponsor press release.")
    not_established.append("Effects in populations, doses or durations other than those studied.")
    if a["multiplicity_controlled"] == TriState.NOT_STATED.value:
        missing.append("Multiplicity-control status of this comparison.")
    if a["ci_status"] == "NOT_REPORTED":
        missing.append("Confidence interval (not reported; none estimated).")
    if a["p_value_status"] == "NOT_REPORTED":
        missing.append("P-value (not reported).")
    if a["estimand_label"] == EstimandLabel.NOT_STATED.value:
        missing.append("Estimand for this analysis.")

    return {
        "analysis_id": a["id"],
        "observed": observed,
        "hypothesis_type": a["hypothesis_type"],
        "ci_excludes_no_difference": ci_excludes_null,
        "ci_excludes_no_difference_kind": "INFERENCE (read from the reported interval)",
        "establishes": establishes,
        "does_not_establish": not_established,
        "missing": missing,
        "labels": a["labels"],
        "sources": [a["source"]] if a["source"] else [],
        "locator_quote": a["locator_quote"],
        "verification_status": a["verification_status"],
    }


def describe_adverse_event(ae: dict[str, Any]) -> str:
    """Sentence that names the measure so discontinuation is never read as incidence."""
    kind = ae["measure_kind"]
    label = AE_KIND_LABEL.get(kind, kind)
    if ae["scope"] == "RANGE_ACROSS_ARMS":
        value = f"{_fmt(ae['percent_low'])}% to {_fmt(ae['percent_high'])}% (range across {ae['arm_label']}; not attributable to any single arm)"
    elif ae["percent_reported"] is not None:
        value = f"{_fmt(ae['percent_reported'])}% (as reported)"
    elif ae["n_affected"] is not None and ae["n_at_risk"]:
        value = f"{ae['n_affected']} of {ae['n_at_risk']} participants ({_fmt(ae['percent_calculated'])}%, calculated from reported counts)"
    else:
        value = "not reported"
    tail = ""
    if kind in DISCONTINUATION_KINDS:
        tail = " This counts participants who stopped; it is not the incidence of any adverse event."
    return f"{ae['study_name']} — {ae['arm_label']}: {label.lower()} [{ae['term']}] = {value}.{tail}"


# --------------------------------------------------------------------------- language guard

_PATTERNS = {
    "NI_AS_SUPERIORITY": r"\bsuperior(ity)?\b|\bbetter than\b|\breduced (the )?risk\b|\bsignificant(ly)? (reduc|lower)|\bcardiovascular benefit\b|\bcardioprotect",
    "CROSS_TRIAL_SUPERIORITY": r"\bsuperior(ity)?\b|\boutperform|\bmore effective than\b|\bbeats?\b|\bstatistically significant|\bsignificantly (greater|more|better|larger)|\bbest[- ]in[- ]class\b",
    "LEAN_AS_MUSCLE": r"\bmuscle (loss|wasting|atrophy)\b|\bloss of (skeletal )?muscle\b|\blost muscle\b|\bsarcopenia\b",
    "SPONTANEOUS_AS_INCIDENCE": r"\bincidence\b|\bevent rate\b|\brate of\b|\brisk of\b|\b\d+(\.\d+)?\s?% of (patients|users|people)\b|\bper \d[\d,]* (patients|users|person)",
    "SPONTANEOUS_AS_CAUSAL": r"\bcauses?\b|\bcaused by\b|\bproves?\b|\bdue to the drug\b|\binduced by\b",
    "DISCONTINUATION_AS_INCIDENCE": r"\bincidence of\b|\bexperienced (an? )?adverse event|\badverse[- ]event rate\b",
    "RECOMMENDATION": r"\b(you|patients?) should (take|start|switch|use|stop)\b|\bI recommend\b|\bthe best (drug|treatment|choice) (is|for)\b|\bprescribe\b|\bshould be (prescribed|preferred)\b",
    "UNLABELLED_EXPLORATORY": r"\bdemonstrated\b|\bproved\b|\bconfirmed\b|\bestablished\b",
}
_NEGATION = re.compile(r"\b(not|no|never|cannot|can't|does not|do not|without|neither|nor|rather than|must not|should not|is not)\b", re.IGNORECASE)


def _hits(code: str, text: str) -> list[str]:
    """Clauses that assert the pattern. A negation shortly before or after the phrase clears it."""
    found = []
    for clause in re.split(r"(?<=[.!?;])\s+", text):
        match = re.search(_PATTERNS[code], clause, re.IGNORECASE)
        if not match:
            continue
        before = clause[max(0, match.start() - 90) : match.start()]
        after = clause[match.end() : match.end() + 45]
        if _NEGATION.search(before) or _NEGATION.search(after):
            continue
        found.append(clause.strip()[:200])
    return found


def guard_statement(
    text: str,
    *,
    hypothesis_type: str | None = None,
    superiority_met: str | None = None,
    cross_trial: bool = False,
    lean_mass: bool = False,
    data_family: str | None = None,
    measure_kind: str | None = None,
    exploratory: bool = False,
) -> list[dict[str, str]]:
    """Return claims in ``text`` that the described evidence does not license (empty list = clean)."""
    violations: list[dict[str, str]] = []

    def add(code: str, message: str) -> None:
        for sentence in _hits(code, text):
            violations.append({"code": code, "message": message, "sentence": sentence})

    if hypothesis_type == "NON_INFERIORITY" and superiority_met != TriState.YES.value:
        add("NI_AS_SUPERIORITY", "A non-inferiority result is described in terms that imply superiority or benefit.")
    if cross_trial:
        add("CROSS_TRIAL_SUPERIORITY", "A cross-trial comparison is described as superiority or as statistically significant.")
    if lean_mass:
        add("LEAN_AS_MUSCLE", "A change in lean/fat-free mass is described as muscle loss without qualification.")
    if data_family == "SPONTANEOUS":
        add("SPONTANEOUS_AS_INCIDENCE", "Spontaneous-report counts are described as an incidence, rate or risk.")
        add("SPONTANEOUS_AS_CAUSAL", "Spontaneous reports are described as showing causation.")
    if measure_kind in DISCONTINUATION_KINDS:
        add("DISCONTINUATION_AS_INCIDENCE", "Discontinuation due to adverse events is described as adverse-event incidence.")
    if exploratory:
        add("UNLABELLED_EXPLORATORY", "An exploratory or multiplicity-uncontrolled result is described with confirmatory language.")
    add("RECOMMENDATION", "The text makes a treatment recommendation, which this platform does not provide.")
    return violations
