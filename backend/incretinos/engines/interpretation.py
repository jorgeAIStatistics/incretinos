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

from ..enums import (
    AE_KIND_LABEL,
    DISCONTINUATION_KINDS,
    EndpointRole,
    EstimandLabel,
    EvidenceTier,
    MechanismAction,
    ReceptorTarget,
    TriState,
)
from .estimands import ESTIMAND_EXPLAINER
from .findings import (
    CATEGORY_LABEL,
    EFFICACY_CATEGORIES,
    LEAN_MASS_CAVEAT,
    Finding,
    _fmt,
    derived_interval,
    effect_scale,
    format_ci,
    format_value,
    interval_p_value_consistency,
    population_phrase,
)

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
        if "STATED" in f.estimand_basis:
            analysis_bits.append("Estimand label stated by the source.")
        elif f.estimand_basis == "INFERRED_BY_CURATOR":
            analysis_bits.append("Estimand label attributed by the curator: no checked quote from this source names the estimand (not a statement by the source).")
        else:
            analysis_bits.append("Estimand label inferred from the registry's analysis-population text (an inference, not a statement by the source).")
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
        not_established.append("Change from study entry: this value is measured from randomization after a lead-in period, in participants who completed and tolerated that lead-in.")
    elif f.baseline_class == "STUDY_ENTRY_INCLUDING_LEAD_IN":
        not_established.append("The change after randomization alone: this value is measured from study entry and includes the lead-in period.")
    elif f.baseline_class == "LEAD_IN_DESIGN_BASELINE_UNSPECIFIED":
        not_established.append("A change comparable with trials that had no lead-in: participants went through a lead-in period before randomization, and the stored source does not say whether "
                               "'baseline' is study entry or randomization.")
    elif f.baseline_class == "SUBSET_BASELINE":
        not_established.append("The change in all randomized participants: this value describes a named subset.")
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

    derived = derived_interval(f)
    if derived is None and f.ci_low is None and f.dispersion_value is not None and "GEOMETRIC" in (f.param_type or "").upper():
        missing.append("A derived interval: the value is a geometric mean and the stored record does not say on which scale its standard error is expressed, so none is calculated.")

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


def _interval_phrase(a: dict[str, Any]) -> str:
    """The reported interval with its stated level and sidedness — or an explicit statement of what is absent."""
    lo, hi = a["ci_low"], a["ci_high"]
    if lo is None or hi is None:
        return "confidence interval not reported in this source"
    status = a.get("ci_level_status") or ("VALID" if a.get("ci_level") is not None and 50 <= a["ci_level"] < 100 else "LEVEL_NOT_REPORTED" if a.get("ci_level") is None else "INVALID_LEVEL")
    if status == "VALID":
        sides = {"TWO_SIDED": "two-sided ", "ONE_SIDED": "one-sided "}.get(a.get("ci_sides") or "", "")
        return f"{sides}{_fmt(a['ci_level'], 2)}% CI {_fmt(lo)} to {_fmt(hi)}"
    if status == "LEVEL_NOT_REPORTED":
        return f"interval {_fmt(lo)} to {_fmt(hi)}, confidence level not stated"
    return f"interval {_fmt(lo)} to {_fmt(hi)}, stored with a confidence level of {_fmt(a['ci_level'], 2)} that is not a valid percentage"


def interpret_analysis(a: dict[str, Any]) -> dict[str, Any]:
    """Reading of a between-arm analysis (dict from ``findings.analysis_dto``).

    The analysis is read only as far as its stored fields allow:

    * the null value is 0 for a difference and 1 for a ratio, taken from the stated parameter type and
      unit; when those do not establish the scale, the interval is not read against any null;
    * the interval is read at its stated confidence level and sidedness; an interval whose level is
      missing or invalid supports no statement about excluding the null;
    * "X versus Y" is written only when the direction is established (stated by the curated source row
      or confirmed by the stored arm-level values); otherwise the two groups are named without a direction.
    """
    estimate, lo, hi = a["estimate"], a["ci_low"], a["ci_high"]
    param = a["param_type"] or "estimate"
    scale = a.get("effect_scale") or effect_scale(a["param_type"], a.get("unit"))
    is_ratio = scale["scale"] == "RATIO"
    null = scale["null_value"]
    direction = a.get("direction") or {"established": bool(a.get("treatment_label") and a.get("comparator_label")), "status": "NOT_ASSESSED", "basis": "Direction was not assessed for this record."}
    level_status = a.get("ci_level_status") or ("NOT_REPORTED" if lo is None or hi is None else "VALID" if a.get("ci_level") is not None and 50 <= a["ci_level"] < 100 else "LEVEL_NOT_REPORTED" if a.get("ci_level") is None else "INVALID_LEVEL")
    level_text = f"{_fmt(a['ci_level'], 2)}%" if level_status == "VALID" else ""

    if direction["established"]:
        groups = f"{a['treatment_label']} versus {a['comparator_label'] or 'comparator not stated'}"
    elif a.get("direction_basis") == "NOT_STATED":
        # The source does not say which group is the reference: name the groups in posted order, do not assert a direction.
        groups = f"Groups {' and '.join(a.get('group_labels') or [str(a['treatment_label'])])} (the source does not state which is the reference; the sign has no stated direction)"
    elif a.get("treatment_label") and a.get("comparator_label"):
        groups = f"Between {a['treatment_label']} and {a['comparator_label']} (which group is the reference is not established)"
    else:
        groups = f"{a.get('treatment_label') or 'Groups not identified'}"
    if a.get("class_title"):
        groups += f" [{a['class_title']}]"
    observed = f"{groups} — {param}: {_fmt(estimate)} ({_interval_phrase(a)})"
    observed += f"; p {a['p_value_text']}." if a["p_value_text"] else "; p-value not reported in this source."

    establishes: list[str] = []
    not_established: list[str] = []
    missing = []
    # The interval is read against the null only when the scale and the confidence level are both established.
    readable = null is not None and level_status == "VALID" and lo is not None and hi is not None
    # A record whose interval contradicts its own p-value (or whose estimate sits outside the middle of its interval) is
    # not read at all: the disagreement is stated and no conclusion about a difference is drawn either way.
    consistency = a.get("internal_consistency") or interval_p_value_consistency(
        estimate, lo, hi, a.get("ci_level"), level_status, a.get("ci_sides"), scale["scale"], null, a.get("p_value_text"), a.get("hypothesis_type"))
    inconsistent = consistency["status"] not in ("CONSISTENT", "NOT_CHECKABLE")
    if inconsistent:
        readable = False
    ci_excludes_null = (not (lo <= null <= hi)) if readable else None
    exploratory = a["role"] in (EndpointRole.EXPLORATORY.value, EndpointRole.POST_HOC.value)
    uncontrolled = a["multiplicity_controlled"] == TriState.NO.value
    one_sided = (a.get("ci_sides") or "") == "ONE_SIDED"
    conventional = level_status == "VALID" and a["ci_level"] >= 95 and not one_sided  # two-sided 95% or stricter
    null_text = _fmt(null) if null is not None else ""

    if a["hypothesis_type"] == "NON_INFERIORITY":
        margin = a["ni_margin"]
        if a["ni_met"] == TriState.YES.value:
            detail = ""
            if readable and margin is not None and hi < margin:
                detail = f" (the upper limit of the {level_text} confidence interval, {_fmt(hi)}, is below the pre-specified margin of {_fmt(margin)})"
            establishes.append(f"Non-inferiority on this endpoint: the treatment is not worse than the comparator by more than the pre-specified margin{detail}.")
        elif a["ni_met"] == TriState.NO.value:
            not_established.append("Non-inferiority: the margin was not excluded.")
        else:
            missing.append("Whether the non-inferiority margin was met (not stated in the stored source).")
        if a["superiority_met"] != TriState.YES.value:
            reason = []
            if ci_excludes_null is False:
                reason.append(f"the {level_text} confidence interval includes {null_text}")
            if a["superiority_tested"] != TriState.YES.value:
                reason.append("the stored source reports a non-inferiority conclusion and does not report a superiority test")
            not_established.append("Superiority of the treatment over the comparator" + (": " + "; ".join(reason) if reason else "") + ". Non-inferiority must not be read as superiority.")
        if is_ratio and estimate is not None and estimate < 1 and ci_excludes_null is False:
            not_established.append(f"A reduction in risk: a point estimate below 1 with an interval spanning {_fmt(lo)} to {_fmt(hi)} is compatible with benefit, no difference, or harm up to the upper limit.")
    elif a["hypothesis_type"] == "SUPERIORITY":
        if ci_excludes_null and conventional and not exploratory and not uncontrolled:
            establishes.append(f"A between-arm difference in this trial on this endpoint and estimand (the {level_text} confidence interval excludes {null_text}, the value of no difference)."
                               + (" Multiplicity control is documented." if a["multiplicity_controlled"] == TriState.YES.value else " Whether this comparison was inside multiplicity control is not stated in the stored source."))
        elif ci_excludes_null and not conventional:
            not_established.append(f"A difference at the conventional two-sided 5% level: the interval is reported at the {'one-sided ' if one_sided else ''}{level_text} level.")
        elif ci_excludes_null is False:
            not_established.append(f"A between-arm difference: the {level_text} confidence interval includes {null_text}, the value of no difference.")
        elif lo is None or hi is None:
            missing.append("A confidence interval, without which the precision of the difference cannot be judged.")
    else:
        if a["hypothesis_type"] in (None, "NOT_STATED"):
            missing.append("The hypothesis tested (superiority, non-inferiority or descriptive) is not stated in the stored source.")
        else:
            missing.append(f"A confirmatory hypothesis: the stored analysis is registered as '{str(a['hypothesis_type']).replace('_', ' ').lower()}', not as a superiority or non-inferiority test.")
        if ci_excludes_null is True:
            not_established.append(f"A confirmatory conclusion: the {level_text} interval excludes {null_text}, but the test type and its place in the testing hierarchy are not on record.")
        elif ci_excludes_null is False:
            not_established.append(f"A between-arm difference: the {level_text} confidence interval includes {null_text}, the value of no difference.")

    if inconsistent:
        not_established.insert(0, f"Any conclusion about a between-arm difference, in either direction: {consistency['detail']}")
        missing.append("A consistent record of this analysis (interval, estimate and p-value that agree); the source needs checking.")
    if scale["scale"] == "NOT_ESTABLISHED":
        not_established.append(f"Whether the interval excludes 'no difference': {scale['basis']}")
        missing.append("The scale of the estimate (difference or ratio), and with it the value that would mean no difference.")
    if lo is not None and hi is not None and level_status != "VALID":
        not_established.append("Anything about statistical significance from the interval: its confidence level is "
                               + ("not stated." if level_status == "LEVEL_NOT_REPORTED" else f"stored as {_fmt(a['ci_level'], 2)}, which is not a valid percentage."))
        missing.append("A valid confidence level for the reported interval.")
    if not direction["established"]:
        not_established.append(f"Which group the estimate favours: {direction['basis']}")
        missing.append("The direction of the comparison (which group is the reference).")
    if direction.get("scale_note"):
        not_established.append(f"The magnitude on the endpoint's own scale: {direction['scale_note']}")

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
        "effect_scale": scale["scale"],
        "null_value": null,
        "ci_level": a.get("ci_level") if level_status == "VALID" else None,
        "ci_level_status": level_status,
        "ci_sides": a.get("ci_sides") or "NOT_STATED",
        "direction_established": bool(direction["established"]),
        "direction_status": direction.get("status"),
        "ci_excludes_no_difference": ci_excludes_null,
        "ci_excludes_no_difference_kind": "INFERENCE (read from the reported interval at its stated confidence level, against the null value of the stated scale)"
        if not inconsistent else "NOT_READ (the stored interval, estimate and p-value disagree with each other)",
        "internal_consistency": consistency,
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
#
# A heuristic screen for claims the evidence does not license. It is deliberately biased towards flagging:
# a claim is cleared only by a negation that grammatically governs it inside the same clause. A negation
# word that merely stands nearby ("without doubt", "no question", "not only … but") clears nothing.

_SIGNIFICANCE = (
    r"\bstatistically\b|(?<!clinically )\bsignificant(ly)?\b|\bstatistical significance\b|\bp\s*[<≤=]\s*0?\.\d+"
)
_PATTERNS = {
    "NI_AS_SUPERIORITY": r"\bsuperior(ity)?\b|\bbetter than\b|\boutperform(s|ed|ing)?\b|\bmore effective\b|\breduced (the )?risk\b|"
    r"\bsignificant(ly)? (reduc|lower|better|greater|fewer|improv)|\bcardiovascular benefit\b|\bcardioprotect",
    "CROSS_TRIAL_SUPERIORITY": r"\bsuperior(ity)?\b|\boutperform(s|ed|ing)?\b|\bmore (effective|efficacious|potent)\b|\bbetter than\b|\bbeats?\b|\bbest[- ]in[- ]class\b|"
    r"\b(stronger|better|best|most effective) (drug|agent|medicine|treatment|therapy|option|choice)\b|" + _SIGNIFICANCE,
    "LEAN_AS_MUSCLE": r"\bmuscle (loss|wasting|atrophy)\b|\bloss of (skeletal )?muscle\b|\blost muscle\b|\bsarcopenia\b",
    "SPONTANEOUS_AS_INCIDENCE": r"\bincidence\b|\bevent rate\b|\brate of\b|\brisk of\b|\b\d+(\.\d+)?\s?% of (patients|users|people)\b|\bper \d[\d,]* (patients|users|person)",
    "SPONTANEOUS_AS_CAUSAL": r"\bcauses?\b|\bcaused by\b|\bproves?\b|\bdue to the drug\b|\binduced by\b",
    "DISCONTINUATION_AS_INCIDENCE": r"\bincidence of\b|\bexperienced (an? )?adverse event|\badverse[- ]event rate\b",
    "RECOMMENDATION": r"\b(you|patients?) should (take|start|switch|use|stop)\b|\bI recommend\b|\bthe best (drug|treatment|choice) (is|for)\b|\bprescribe\b|\bshould be (prescribed|preferred)\b",
    "UNLABELLED_EXPLORATORY": r"\bdemonstrated\b|\bproved\b|\bconfirmed\b|\bestablished\b",
}

# A between-drug comparative carrying a p-value is a significance claim even without the word "significant".
_SENTENCE_PATTERNS = {
    "CROSS_TRIAL_SUPERIORITY": r"\b(?:more|greater|larger|bigger|better|higher|stronger|fewer|less|lower)\b[^.;]{0,80}\bthan\b[^.;]{0,80}\bp\s*[<≤=]\s*0?\.\d+",
}

# Phrases that contain a negation word but assert rather than deny. They are blanked before negation is looked for.
_PSEUDO_NEGATION = re.compile(
    r"\bnot (?:only|just|merely|simply|least|to mention|surprising(?:ly)?)\b|\b(?:no|without|beyond)(?: a| any)? (?:doubt|question|surprise|wonder|dispute|exaggeration)\b|"
    r"\bno denying\b|\bcannot be (?:denied|overstated)\b|\b(?:could|can)(?:n't| not|not) be (?:more|clearer)\b|\bno(?:thing)? (?:less|short) (?:than|of)\b|\bundoubtedly\b|\bno longer in doubt\b",
    re.IGNORECASE,
)
# A negated verb, or a statement that evidence is absent, that can govern a claim later in the same clause.
_GOVERNING_NEGATION = re.compile(
    r"\b(?:do|does|did|is|are|was|were|has|have|had|can|could|should|must|may|might|will|would)(?:n't| not| never)\b|\bcannot\b|\bcan't\b|\bnever\b|"
    r"\bnot (?:be |been )?(?:known|clear|established|demonstrated|shown|possible|permitted|appropriate|valid|supported|licensed)\b|\b(?:unknown|unclear) whether\b|"
    r"\bfail(?:s|ed)? to\b|\bneither\b|\bnor\b|\brather than\b|\binstead of\b|"
    r"\b(?:no|without|lack of|absence of|insufficient) (?:\w+ ){0,2}?(?:evidence|proof|indication|basis|data|trials?|stud(?:y|ies)|tests?|claims?|reason|support|comparison)\b",
    re.IGNORECASE,
)
# A negating word immediately in front of the claim: "no significant", "not superior", "non-significant".
_NEGATING_DETERMINER = re.compile(r"\b(?:no|not|non|without)\b(?:[- ]+(?:a|an|any|the|valid|formal|statistically|clinically))*[- ]*$", re.IGNORECASE)
# The claim is the subject of a negated predicate: "superiority was not demonstrated".
_NEGATED_PREDICATE = re.compile(
    r"\b(?:(?:(?:was|were|is|are|has|have|had)(?:n't| not| never)(?: been| yet)?|(?:could|can|should|may|must)(?:n't| not)(?: be)?|cannot be)\s+(?:\w+ly\s+)?"
    r"(?:demonstrated|shown|observed|established|found|seen|reached|achieved|met|tested|claimed|reported|detected|proven|proved|supported|confirmed|evaluated|assessed)"
    r"|remains? (?:unproven|untested|unknown|unestablished))\b",
    re.IGNORECASE,
)
_PREDICATE_BARRIER = re.compile(r"\b(?:to|than|over|versus|vs|and|compared|with)\b", re.IGNORECASE)
_CLAUSE_BREAK = re.compile(r"[;:—–()]|,|\bbut\b|\bhowever\b|\balthough\b|\bthough\b|\bwhereas\b|\bwhile\b|\byet\b|\bnevertheless\b|\bnonetheless\b", re.IGNORECASE)
_MAX_NEGATION_DISTANCE = 12  # words allowed between a governing negation and the claim
_MAX_PREDICATE_DISTANCE = 6  # words allowed between the claim and its negated predicate


def _blank(match: re.Match[str]) -> str:
    return " " * len(match.group(0))


def _governed_by_negation(clause: str, start: int, end: int) -> bool:
    """True when a negation inside this clause grammatically governs the phrase at [start, end)."""
    before, after = clause[:start], clause[end:]
    if _NEGATING_DETERMINER.search(before):
        return True
    if any(len(before[neg.end():].split()) <= _MAX_NEGATION_DISTANCE for neg in _GOVERNING_NEGATION.finditer(before)):
        return True
    predicate = _NEGATED_PREDICATE.search(after)
    if predicate is None:
        return False
    between = after[: predicate.start()]
    return len(between.split()) <= _MAX_PREDICATE_DISTANCE and not _PREDICATE_BARRIER.search(between)


def _hits(code: str, text: str) -> list[str]:
    """Sentences that assert the pattern. Only a negation governing the phrase, in the same clause, clears it."""
    found = []
    pattern = re.compile(_PATTERNS[code], re.IGNORECASE)
    sentence_pattern = re.compile(_SENTENCE_PATTERNS[code], re.IGNORECASE) if code in _SENTENCE_PATTERNS else None
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        masked = _PSEUDO_NEGATION.sub(_blank, sentence)
        asserted = False
        carried = False  # a cleared claim extends its negation over a following list ("not superior, better, or more effective")
        for raw in _CLAUSE_BREAK.split(masked):
            clause = raw.strip()
            if not clause:
                continue
            continuation = bool(re.match(r"(?:or|nor|and)\b", clause, re.IGNORECASE)) or len(clause.split()) <= 3
            matches = list(pattern.finditer(clause))
            if not matches:
                carried = carried and continuation
                continue
            cleared = [_governed_by_negation(clause, m.start(), m.end()) or (carried and continuation) for m in matches]
            if not all(cleared):
                asserted = True
            carried = all(cleared)
        if not asserted and sentence_pattern is not None:
            wide = sentence_pattern.search(masked)
            asserted = bool(wide and not _governed_by_negation(masked, wide.start(), wide.start()))
        if asserted:
            found.append(sentence.strip()[:200])
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
    """Return claims in ``text`` that the described evidence does not license (empty list = clean).

    The check is a pattern screen, not a parser: it catches the wordings listed in ``_PATTERNS`` and clears a
    hit only when a negation governs it within the same clause. It errs towards flagging and cannot recognise
    every paraphrase of a superiority claim.
    """
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
