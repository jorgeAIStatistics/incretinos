"""Deterministic statistical calculations.

All numerical work happens here, outside any language model. Every function returns
the method used and the assumptions made, and refuses inputs it cannot handle rather
than patching them (no zero-filling, no silent continuity corrections).

References for the formulas are given in docs/architecture.md; the test-suite checks
each against statsmodels/scipy or a published worked example.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import stats as sps


class StatsInputError(ValueError):
    """Inputs are missing or invalid for the requested calculation."""


def _z(level: float) -> float:
    if not 0 < level < 1:
        raise StatsInputError("confidence level must be between 0 and 1")
    return float(sps.norm.ppf(1 - (1 - level) / 2))


def _check_counts(events: Any, n: Any, name: str) -> tuple[int, int]:
    if events is None or n is None:
        raise StatsInputError(f"{name}: event count and denominator are both required (a missing count is not zero)")
    if int(events) != events or int(n) != n:
        raise StatsInputError(f"{name}: counts must be integers")
    events, n = int(events), int(n)
    if n <= 0:
        raise StatsInputError(f"{name}: denominator must be positive")
    if events < 0 or events > n:
        raise StatsInputError(f"{name}: events must be between 0 and the denominator")
    return events, n


def wilson_interval(events: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a single proportion."""
    events, n = _check_counts(events, n, "proportion")
    z = _z(level)
    p = events / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def risk_difference(events_a: int, n_a: int, events_b: int, n_b: int, level: float = 0.95) -> dict[str, Any]:
    """Risk difference (A − B) with a Newcombe hybrid-score interval (method 10)."""
    ea, na = _check_counts(events_a, n_a, "group A")
    eb, nb = _check_counts(events_b, n_b, "group B")
    pa, pb = ea / na, eb / nb
    la, ua = wilson_interval(ea, na, level)
    lb, ub = wilson_interval(eb, nb, level)
    diff = pa - pb
    lower = diff - math.sqrt((pa - la) ** 2 + (ub - pb) ** 2)
    upper = diff + math.sqrt((ua - pa) ** 2 + (pb - lb) ** 2)
    return {
        "measure": "risk_difference",
        "estimate": diff,
        "ci_low": lower,
        "ci_high": upper,
        "ci_level": level,
        "risk_a": pa,
        "risk_b": pb,
        "method": "Newcombe hybrid score interval (Wilson limits for each proportion)",
        "kind": "INFERENCE",
        "interpretation": "Unadjusted, post hoc contrast calculated from reported counts. It is descriptive, not a pre-specified or multiplicity-controlled analysis.",
    }


def risk_ratio(events_a: int, n_a: int, events_b: int, n_b: int, level: float = 0.95) -> dict[str, Any]:
    """Risk ratio (A / B) with a log-scale Wald (Katz) interval. Undefined when either count is zero."""
    ea, na = _check_counts(events_a, n_a, "group A")
    eb, nb = _check_counts(events_b, n_b, "group B")
    base = {
        "measure": "risk_ratio",
        "ci_level": level,
        "risk_a": ea / na,
        "risk_b": eb / nb,
        "method": "Katz log-scale Wald interval",
        "kind": "INFERENCE",
        "interpretation": "Unadjusted, post hoc contrast calculated from reported counts. It is descriptive, not a pre-specified or multiplicity-controlled analysis.",
    }
    if ea == 0 or eb == 0:
        return {**base, "estimate": None, "ci_low": None, "ci_high": None,
                "not_estimable": "A risk ratio and its log-scale interval are undefined when either group has zero events; no continuity correction is applied."}
    rr = (ea / na) / (eb / nb)
    se = math.sqrt(1 / ea - 1 / na + 1 / eb - 1 / nb)
    z = _z(level)
    return {**base, "estimate": rr, "ci_low": math.exp(math.log(rr) - z * se), "ci_high": math.exp(math.log(rr) + z * se)}


def ci_from_se(estimate: float | None, se: float | None, level: float = 0.95, df: float | None = None) -> dict[str, Any]:
    """Normal- (or t-) approximation interval from a reported estimate and standard error.

    This is a *derived* interval. Callers must present it as an inference, separately from
    any interval the source itself reported.
    """
    if estimate is None or se is None:
        raise StatsInputError("estimate and standard error are both required")
    if se < 0:
        raise StatsInputError("standard error cannot be negative")
    crit = float(sps.t.ppf(1 - (1 - level) / 2, df)) if df else _z(level)
    return {
        "estimate": estimate,
        "ci_low": estimate - crit * se,
        "ci_high": estimate + crit * se,
        "ci_level": level,
        "method": f"{'t' if df else 'normal'} approximation from reported standard error",
        "kind": "INFERENCE",
        "note": "Derived here from the reported estimate and standard error; not reported by the source.",
    }


# --------------------------------------------------------------------------- design calculations


def n_two_means(delta: float, sd: float, alpha: float = 0.05, power: float = 0.9, ratio: float = 1.0, two_sided: bool = True) -> dict[str, Any]:
    """Sample size per group for a two-sample comparison of means (normal approximation).

    n1 = (1 + 1/k) * (sd * (z_{1-α/2} + z_{1-β}) / δ)²,  n2 = k·n1
    """
    if delta == 0 or sd <= 0 or not 0 < alpha < 1 or not 0 < power < 1 or ratio <= 0:
        raise StatsInputError("delta must be non-zero, sd and ratio positive, alpha and power between 0 and 1")
    za = float(sps.norm.ppf(1 - alpha / (2 if two_sided else 1)))
    zb = float(sps.norm.ppf(power))
    n1 = (1 + 1 / ratio) * (sd * (za + zb) / delta) ** 2
    return {
        "design": "two-arm parallel, continuous endpoint, superiority",
        "n_group_1": math.ceil(n1),
        "n_group_2": math.ceil(ratio * n1),
        "n_total": math.ceil(n1) + math.ceil(ratio * n1),
        "unrounded_n_group_1": n1,
        "method": "normal approximation; n1 = (1+1/k)(σ(z₁₋α/₂+z₁₋β)/δ)²",
        "assumptions": {"delta": delta, "sd": sd, "alpha": alpha, "power": power, "allocation_ratio": ratio, "two_sided": two_sided},
    }


def power_two_means(n1: int, delta: float, sd: float, alpha: float = 0.05, ratio: float = 1.0, two_sided: bool = True) -> dict[str, Any]:
    if n1 <= 1 or sd <= 0 or ratio <= 0:
        raise StatsInputError("n1 must exceed 1; sd and ratio must be positive")
    se = sd * math.sqrt(1 / n1 + 1 / (ratio * n1))
    za = float(sps.norm.ppf(1 - alpha / (2 if two_sided else 1)))
    power = float(sps.norm.cdf(abs(delta) / se - za))
    return {"power": power, "method": "normal approximation", "assumptions": {"n_group_1": n1, "delta": delta, "sd": sd, "alpha": alpha, "allocation_ratio": ratio, "two_sided": two_sided}}


def n_two_proportions(p1: float, p2: float, alpha: float = 0.05, power: float = 0.9, ratio: float = 1.0) -> dict[str, Any]:
    """Sample size per group for two proportions (pooled variance under H0, unpooled under H1; no continuity correction)."""
    if not (0 < p1 < 1 and 0 < p2 < 1) or p1 == p2 or ratio <= 0:
        raise StatsInputError("p1 and p2 must be distinct proportions strictly between 0 and 1")
    za, zb = float(sps.norm.ppf(1 - alpha / 2)), float(sps.norm.ppf(power))
    pbar = (p1 + ratio * p2) / (1 + ratio)
    n1 = (za * math.sqrt((1 + 1 / ratio) * pbar * (1 - pbar)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2) / ratio)) ** 2 / (p1 - p2) ** 2
    return {
        "design": "two-arm parallel, binary endpoint, superiority",
        "n_group_1": math.ceil(n1),
        "n_group_2": math.ceil(ratio * n1),
        "n_total": math.ceil(n1) + math.ceil(ratio * n1),
        "unrounded_n_group_1": n1,
        "method": "normal approximation with pooled variance under H0 (no continuity correction)",
        "assumptions": {"p1": p1, "p2": p2, "alpha": alpha, "power": power, "allocation_ratio": ratio},
    }


def n_noninferiority_means(margin: float, sd: float, true_difference: float = 0.0, alpha_one_sided: float = 0.025, power: float = 0.9, ratio: float = 1.0) -> dict[str, Any]:
    """Sample size per group for non-inferiority on a mean difference (margin > 0, higher = worse)."""
    if margin <= 0 or sd <= 0 or margin - true_difference <= 0:
        raise StatsInputError("margin must be positive and exceed the assumed true difference")
    za, zb = float(sps.norm.ppf(1 - alpha_one_sided)), float(sps.norm.ppf(power))
    n1 = (1 + 1 / ratio) * (sd * (za + zb) / (margin - true_difference)) ** 2
    return {
        "design": "two-arm parallel, continuous endpoint, non-inferiority",
        "n_group_1": math.ceil(n1),
        "n_group_2": math.ceil(ratio * n1),
        "n_total": math.ceil(n1) + math.ceil(ratio * n1),
        "unrounded_n_group_1": n1,
        "method": "normal approximation; n1 = (1+1/k)(σ(z₁₋α+z₁₋β)/(M−δ))²",
        "assumptions": {"margin": margin, "sd": sd, "true_difference": true_difference, "alpha_one_sided": alpha_one_sided, "power": power, "allocation_ratio": ratio},
    }


def events_logrank(hazard_ratio: float, alpha: float = 0.05, power: float = 0.9, allocation_ratio: float = 1.0, two_sided: bool = True) -> dict[str, Any]:
    """Number of events for a log-rank test (Schoenfeld): d = (z_α + z_β)² / (p₁p₂ (ln HR)²)."""
    if hazard_ratio <= 0 or hazard_ratio == 1:
        raise StatsInputError("hazard ratio must be positive and different from 1")
    za = float(sps.norm.ppf(1 - alpha / (2 if two_sided else 1)))
    zb = float(sps.norm.ppf(power))
    p1 = allocation_ratio / (1 + allocation_ratio)
    events = (za + zb) ** 2 / (p1 * (1 - p1) * math.log(hazard_ratio) ** 2)
    return {
        "design": "time-to-event, superiority (log-rank)",
        "events_required": math.ceil(events),
        "unrounded_events": events,
        "method": "Schoenfeld approximation",
        "assumptions": {"hazard_ratio": hazard_ratio, "alpha": alpha, "power": power, "allocation_ratio": allocation_ratio, "two_sided": two_sided, "proportional_hazards": True},
    }


def events_noninferiority_hr(margin: float, true_hazard_ratio: float = 1.0, alpha_one_sided: float = 0.025, power: float = 0.9, allocation_ratio: float = 1.0) -> dict[str, Any]:
    """Events needed to exclude a hazard-ratio non-inferiority margin: d = (z_α + z_β)² / (p₁p₂ (ln M − ln HR)²)."""
    if margin <= 1 or true_hazard_ratio <= 0 or true_hazard_ratio >= margin:
        raise StatsInputError("margin must exceed 1 and the assumed true hazard ratio must be below the margin")
    za, zb = float(sps.norm.ppf(1 - alpha_one_sided)), float(sps.norm.ppf(power))
    p1 = allocation_ratio / (1 + allocation_ratio)
    events = (za + zb) ** 2 / (p1 * (1 - p1) * (math.log(margin) - math.log(true_hazard_ratio)) ** 2)
    return {
        "design": "time-to-event, non-inferiority",
        "events_required": math.ceil(events),
        "unrounded_events": events,
        "method": "Schoenfeld-type approximation on the log hazard-ratio scale",
        "assumptions": {"margin": margin, "true_hazard_ratio": true_hazard_ratio, "alpha_one_sided": alpha_one_sided, "power": power, "allocation_ratio": allocation_ratio, "proportional_hazards": True},
    }


def inflate_for_attrition(n: int, attrition: float) -> dict[str, Any]:
    if not 0 <= attrition < 1:
        raise StatsInputError("attrition must be in [0, 1)")
    return {"n_evaluable": n, "attrition": attrition, "n_to_randomize": math.ceil(n / (1 - attrition)), "method": "n / (1 − attrition)"}


def attrition_sensitivity(delta: float, sd: float, n_randomized_per_group: int, attrition_rates: list[float], alpha: float = 0.05) -> list[dict[str, Any]]:
    """Power across attrition scenarios when only completers contribute (a completers-only simplification)."""
    out = []
    for rate in attrition_rates:
        evaluable = math.floor(n_randomized_per_group * (1 - rate))
        if evaluable <= 1:
            out.append({"attrition": rate, "n_evaluable_per_group": evaluable, "power": None})
            continue
        out.append({"attrition": rate, "n_evaluable_per_group": evaluable, "power": power_two_means(evaluable, delta, sd, alpha)["power"]})
    return out


def missing_data_delta_scenarios(mean_treatment: float, mean_control: float, missing_treatment: float, missing_control: float, deltas: list[float]) -> list[dict[str, Any]]:
    """Delta-adjustment (tipping-point style) scenarios on aggregate means.

    Observed-case means are assumed to describe completers. For each δ, participants with
    missing data in the treatment arm are assumed to have a mean worse than completers by δ
    (control-arm missing data are assumed to behave like control completers). The marginal
    treatment-arm mean is then a mixture. This is a scenario, not an estimate.
    """
    if not (0 <= missing_treatment < 1 and 0 <= missing_control < 1):
        raise StatsInputError("missing-data proportions must be in [0, 1)")
    rows = []
    for delta in deltas:
        marginal_trt = (1 - missing_treatment) * mean_treatment + missing_treatment * (mean_treatment + delta)
        rows.append({"delta": delta, "marginal_treatment_mean": marginal_trt, "marginal_control_mean": mean_control, "difference": marginal_trt - mean_control, "kind": "SIMULATION"})
    return rows


@dataclass
class SimulationSpec:
    n_per_arm: int
    mean_treatment: float
    mean_control: float
    sd: float
    dropout_treatment: float = 0.0
    dropout_control: float = 0.0
    missing_handling: str = "COMPLETERS"  # COMPLETERS | JUMP_TO_REFERENCE
    n_sims: int = 2000
    alpha: float = 0.05
    seed: int = 20261013


def simulate_two_arm_trial(spec: SimulationSpec) -> dict[str, Any]:
    """Monte Carlo simulation of a two-arm trial with a normal endpoint. Entirely SYNTHETIC."""
    if spec.n_per_arm < 3 or spec.sd <= 0 or spec.n_sims < 100 or spec.n_sims > 20000:
        raise StatsInputError("n_per_arm ≥ 3, sd > 0 and 100 ≤ n_sims ≤ 20000 are required")
    if not (0 <= spec.dropout_treatment < 1 and 0 <= spec.dropout_control < 1):
        raise StatsInputError("dropout proportions must be in [0, 1)")
    if spec.missing_handling not in ("COMPLETERS", "JUMP_TO_REFERENCE"):
        raise StatsInputError("missing_handling must be COMPLETERS or JUMP_TO_REFERENCE")
    rng = np.random.default_rng(spec.seed)
    shape = (spec.n_sims, spec.n_per_arm)
    trt = rng.normal(spec.mean_treatment, spec.sd, shape)
    ctl = rng.normal(spec.mean_control, spec.sd, shape)
    drop_t = rng.random(shape) < spec.dropout_treatment
    drop_c = rng.random(shape) < spec.dropout_control
    if spec.missing_handling == "JUMP_TO_REFERENCE":
        # Treatment-arm dropouts take outcomes from the control distribution (treatment-policy-like scenario).
        trt = np.where(drop_t, rng.normal(spec.mean_control, spec.sd, shape), trt)
        t_stat, p = sps.ttest_ind(trt, ctl, axis=1, equal_var=True)
        diffs = trt.mean(axis=1) - ctl.mean(axis=1)
    else:
        trt_m = np.ma.masked_array(trt, mask=drop_t)
        ctl_m = np.ma.masked_array(ctl, mask=drop_c)
        nt, nc = trt_m.count(axis=1), ctl_m.count(axis=1)
        mt, mc = trt_m.mean(axis=1), ctl_m.mean(axis=1)
        vt, vc = trt_m.var(axis=1, ddof=1), ctl_m.var(axis=1, ddof=1)
        pooled = ((nt - 1) * vt + (nc - 1) * vc) / (nt + nc - 2)
        se = np.sqrt(pooled * (1 / nt + 1 / nc))
        diffs = np.asarray(mt - mc, dtype=float)
        t_stat = diffs / np.asarray(se, dtype=float)
        p = 2 * sps.t.sf(np.abs(t_stat), np.asarray(nt + nc - 2, dtype=float))
    rejected = float(np.mean(np.asarray(p) < spec.alpha))
    mc_se = math.sqrt(rejected * (1 - rejected) / spec.n_sims)
    return {
        "label": "SYNTHETIC SIMULATION — not trial data",
        "kind": "SIMULATION",
        "seed": spec.seed,
        "n_sims": spec.n_sims,
        "empirical_power": rejected,
        "monte_carlo_se": mc_se,
        "mean_estimated_difference": float(np.mean(diffs)),
        "sd_of_estimated_difference": float(np.std(diffs, ddof=1)),
        "percentiles_of_estimated_difference": {str(q): float(np.percentile(diffs, q)) for q in (2.5, 25, 50, 75, 97.5)},
        "assumptions": {
            "n_per_arm": spec.n_per_arm,
            "mean_treatment": spec.mean_treatment,
            "mean_control": spec.mean_control,
            "sd": spec.sd,
            "dropout_treatment": spec.dropout_treatment,
            "dropout_control": spec.dropout_control,
            "missing_handling": spec.missing_handling,
            "alpha": spec.alpha,
            "outcome_distribution": "normal, common standard deviation, independent participants",
            "analysis": "two-sample t-test at the final visit (no covariate adjustment, no repeated measures)",
        },
        "caveat": "Outcomes are drawn from the stated distributions. They are not derived from, and must not be presented as, data from any Lilly trial. "
        "The rejection rate is the operating characteristic of this simplified design under these assumptions; it is not a forecast of trial success.",
    }
