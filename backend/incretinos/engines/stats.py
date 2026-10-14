"""Deterministic statistical calculations.

All numerical work happens here, outside any language model. Every function returns
the method used and the assumptions made, and refuses inputs it cannot handle rather
than patching them (no zero-filling, no silent continuity corrections).

Every numeric input is checked before use: a missing value, NaN, an infinity or an absurd
magnitude raises ``StatsInputError`` (the API turns that into a 422). A calculation whose
result would not be a usable finite number is refused in the same way; nothing here returns
a confident number computed from a nonsensical input.

References for the formulas are given in docs/architecture.md; the test-suite checks
each against statsmodels/scipy or a published worked example.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import numpy as np
from scipy import stats as sps

#: Largest absolute value accepted for any continuous input (means, differences, standard deviations).
MAX_MAGNITUDE = 1e9
#: Largest sample size or event count a design calculation will return. Above this the inputs describe
#: no feasible study (for example a hazard ratio indistinguishable from 1) and the request is refused.
MAX_DESIGN_SIZE = 1e9
#: Largest per-group size accepted as an input.
MAX_GROUP_SIZE = 10_000_000
#: Simulation budget: simulated trials x participants per arm. The simulation runs in fixed-size blocks,
#: so this cap bounds CPU time while peak memory is bounded by ``SIMULATION_BLOCK_CELLS`` alone.
MAX_SIMULATION_CELLS = 5_000_000
#: Cells (trials x participants per arm) held in memory at once: about 50 bytes each, i.e. roughly 12 MB.
SIMULATION_BLOCK_CELLS = 250_000
MIN_SIMULATIONS, MAX_SIMULATIONS = 100, 20_000
#: Completers-only analysis: share of simulated trials allowed to have an arm with fewer than two completers.
MAX_UNDEFINED_REPLICATE_FRACTION = 0.01


class StatsInputError(ValueError):
    """Inputs are missing or invalid for the requested calculation."""


def _finite(value: Any, name: str, *, limit: float = MAX_MAGNITUDE) -> float:
    """A real, finite number of plausible magnitude — or a clear refusal."""
    if value is None:
        raise StatsInputError(f"{name} is required (a missing value is not zero)")
    if isinstance(value, bool) or not isinstance(value, int | float | np.integer | np.floating):
        raise StatsInputError(f"{name} must be a number")
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise StatsInputError(f"{name} must be a finite number (NaN and infinity are not accepted)")
    if abs(number) > limit:
        raise StatsInputError(f"{name} is outside the accepted range (absolute value above {limit:g})")
    return number


def _positive(value: Any, name: str, *, limit: float = MAX_MAGNITUDE) -> float:
    number = _finite(value, name, limit=limit)
    if number <= 0:
        raise StatsInputError(f"{name} must be greater than 0")
    return number


def _probability(value: Any, name: str) -> float:
    """Strictly between 0 and 1 (type I error, power, confidence level)."""
    number = _finite(value, name)
    if not 0 < number < 1:
        raise StatsInputError(f"{name} must be strictly between 0 and 1")
    return number


def _whole(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    number = _finite(value, name, limit=float(maximum))
    if number != int(number):
        raise StatsInputError(f"{name} must be a whole number")
    if int(number) < minimum:
        raise StatsInputError(f"{name} must be at least {minimum}")
    return int(number)


def _design_size(value: float, what: str) -> float:
    """Refuse a design whose required size is not a usable finite number."""
    if math.isnan(value) or math.isinf(value) or value > MAX_DESIGN_SIZE:
        raise StatsInputError(
            f"These assumptions imply more than {MAX_DESIGN_SIZE:g} {what}: the assumed effect is too small relative to the variability "
            "(or the type I error too strict) for any feasible study. No number is returned."
        )
    return value


def _z(level: float) -> float:
    return float(sps.norm.ppf(1 - (1 - _probability(level, "confidence level")) / 2))


def _check_counts(events: Any, n: Any, name: str) -> tuple[int, int]:
    if events is None or n is None:
        raise StatsInputError(f"{name}: event count and denominator are both required (a missing count is not zero)")
    for value in (events, n):
        if isinstance(value, bool) or not isinstance(value, int | float | np.integer | np.floating):
            raise StatsInputError(f"{name}: counts must be integers")
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise StatsInputError(f"{name}: counts must be finite integers")
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
    estimate, se = _finite(estimate, "estimate"), _finite(se, "standard error")
    if se < 0:
        raise StatsInputError("standard error cannot be negative")
    level = _probability(level, "confidence level")
    if df is not None:
        df = _positive(df, "degrees of freedom")  # df = 0 is an error, not a silent switch to the normal quantile
        crit = float(sps.t.ppf(1 - (1 - level) / 2, df))
    else:
        crit = _z(level)
    return {
        "estimate": estimate,
        "ci_low": estimate - crit * se,
        "ci_high": estimate + crit * se,
        "ci_level": level,
        "method": f"{'t' if df is not None else 'normal'} approximation from reported standard error",
        "kind": "INFERENCE",
        "note": "Derived here from the reported estimate and standard error; not reported by the source.",
    }


# --------------------------------------------------------------------------- design calculations


def n_two_means(delta: float, sd: float, alpha: float = 0.05, power: float = 0.9, ratio: float = 1.0, two_sided: bool = True) -> dict[str, Any]:
    """Sample size per group for a two-sample comparison of means (normal approximation).

    n1 = (1 + 1/k) * (sd * (z_{1-α/2} + z_{1-β}) / δ)²,  n2 = k·n1
    """
    delta, sd, ratio = _finite(delta, "delta"), _positive(sd, "sd"), _positive(ratio, "allocation ratio")
    alpha, power = _probability(alpha, "alpha"), _probability(power, "power")
    if delta == 0:
        raise StatsInputError("delta must be non-zero, sd and ratio positive, alpha and power between 0 and 1")
    za = float(sps.norm.ppf(1 - alpha / (2 if two_sided else 1)))
    zb = float(sps.norm.ppf(power))
    with np.errstate(all="ignore"):
        n1 = float(np.float64(1 + 1 / ratio) * (np.float64(sd) * (za + zb) / np.float64(delta)) ** 2)
    _design_size(max(n1, ratio * n1), "participants per group")
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
    n1 = _finite(n1, "n1", limit=MAX_GROUP_SIZE)
    delta, sd, ratio, alpha = _finite(delta, "delta"), _positive(sd, "sd"), _positive(ratio, "allocation ratio"), _probability(alpha, "alpha")
    if n1 <= 1:
        raise StatsInputError("n1 must exceed 1; sd and ratio must be positive")
    se = sd * math.sqrt(1 / n1 + 1 / (ratio * n1))
    za = float(sps.norm.ppf(1 - alpha / (2 if two_sided else 1)))
    power = float(sps.norm.cdf(abs(delta) / se - za))
    return {"power": power, "method": "normal approximation", "assumptions": {"n_group_1": n1, "delta": delta, "sd": sd, "alpha": alpha, "allocation_ratio": ratio, "two_sided": two_sided}}


def n_two_proportions(p1: float, p2: float, alpha: float = 0.05, power: float = 0.9, ratio: float = 1.0) -> dict[str, Any]:
    """Sample size per group for two proportions (pooled variance under H0, unpooled under H1; no continuity correction)."""
    p1, p2, ratio = _finite(p1, "p1"), _finite(p2, "p2"), _positive(ratio, "allocation ratio")
    alpha, power = _probability(alpha, "alpha"), _probability(power, "power")
    if not (0 < p1 < 1 and 0 < p2 < 1) or p1 == p2:
        raise StatsInputError("p1 and p2 must be distinct proportions strictly between 0 and 1")
    za, zb = float(sps.norm.ppf(1 - alpha / 2)), float(sps.norm.ppf(power))
    pbar = (p1 + ratio * p2) / (1 + ratio)
    with np.errstate(all="ignore"):
        n1 = float((za * math.sqrt((1 + 1 / ratio) * pbar * (1 - pbar)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2) / ratio)) ** 2 / np.float64((p1 - p2) ** 2))
    _design_size(max(n1, ratio * n1), "participants per group")
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
    margin, sd, true_difference = _finite(margin, "margin"), _positive(sd, "sd"), _finite(true_difference, "true difference")
    ratio, alpha_one_sided, power = _positive(ratio, "allocation ratio"), _probability(alpha_one_sided, "one-sided alpha"), _probability(power, "power")
    if margin <= 0 or margin - true_difference <= 0:
        raise StatsInputError("margin must be positive and exceed the assumed true difference")
    za, zb = float(sps.norm.ppf(1 - alpha_one_sided)), float(sps.norm.ppf(power))
    with np.errstate(all="ignore"):
        n1 = float(np.float64(1 + 1 / ratio) * (np.float64(sd) * (za + zb) / np.float64(margin - true_difference)) ** 2)
    _design_size(max(n1, ratio * n1), "participants per group")
    if n1 < 1:
        raise StatsInputError("These assumptions imply fewer than one participant per group: the margin is implausibly wide relative to the standard deviation. No number is returned.")
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
    hazard_ratio, allocation_ratio = _finite(hazard_ratio, "hazard ratio"), _positive(allocation_ratio, "allocation ratio")
    alpha, power = _probability(alpha, "alpha"), _probability(power, "power")
    if hazard_ratio <= 0 or hazard_ratio == 1:
        raise StatsInputError("hazard ratio must be positive and different from 1")
    za = float(sps.norm.ppf(1 - alpha / (2 if two_sided else 1)))
    zb = float(sps.norm.ppf(power))
    p1 = allocation_ratio / (1 + allocation_ratio)
    with np.errstate(all="ignore"):
        events = float((za + zb) ** 2 / (p1 * (1 - p1) * np.float64(math.log(hazard_ratio)) ** 2))
    _design_size(events, "events")
    return {
        "design": "time-to-event, superiority (log-rank)",
        "events_required": math.ceil(events),
        "unrounded_events": events,
        "method": "Schoenfeld approximation",
        "assumptions": {"hazard_ratio": hazard_ratio, "alpha": alpha, "power": power, "allocation_ratio": allocation_ratio, "two_sided": two_sided, "proportional_hazards": True},
    }


def events_noninferiority_hr(margin: float, true_hazard_ratio: float = 1.0, alpha_one_sided: float = 0.025, power: float = 0.9, allocation_ratio: float = 1.0) -> dict[str, Any]:
    """Events needed to exclude a hazard-ratio non-inferiority margin: d = (z_α + z_β)² / (p₁p₂ (ln M − ln HR)²)."""
    margin, true_hazard_ratio = _finite(margin, "margin"), _finite(true_hazard_ratio, "true hazard ratio")
    allocation_ratio, alpha_one_sided, power = _positive(allocation_ratio, "allocation ratio"), _probability(alpha_one_sided, "one-sided alpha"), _probability(power, "power")
    if margin <= 1 or true_hazard_ratio <= 0 or true_hazard_ratio >= margin:
        raise StatsInputError("margin must exceed 1 and the assumed true hazard ratio must be below the margin")
    za, zb = float(sps.norm.ppf(1 - alpha_one_sided)), float(sps.norm.ppf(power))
    p1 = allocation_ratio / (1 + allocation_ratio)
    with np.errstate(all="ignore"):
        events = float((za + zb) ** 2 / (p1 * (1 - p1) * np.float64(math.log(margin) - math.log(true_hazard_ratio)) ** 2))
    _design_size(events, "events")
    return {
        "design": "time-to-event, non-inferiority",
        "events_required": math.ceil(events),
        "unrounded_events": events,
        "method": "Schoenfeld-type approximation on the log hazard-ratio scale",
        "assumptions": {"margin": margin, "true_hazard_ratio": true_hazard_ratio, "alpha_one_sided": alpha_one_sided, "power": power, "allocation_ratio": allocation_ratio, "proportional_hazards": True},
    }


def _decimal_fraction(value: float) -> Fraction:
    """The decimal number the caller wrote (0.07 means 7/100), not its binary floating-point neighbour."""
    return Fraction(repr(float(value)))


def inflate_for_attrition(n: int, attrition: float) -> dict[str, Any]:
    """Participants to randomize so that ``n`` remain evaluable: the smallest integer ≥ n / (1 − attrition).

    Computed in exact rational arithmetic: floating-point division rounds 465 / (1 − 0.07) up to 501 although
    the exact quotient is 500.
    """
    n = _whole(n, "n", minimum=1, maximum=int(MAX_DESIGN_SIZE) * 4)
    attrition = _finite(attrition, "attrition")
    if not 0 <= attrition < 1:
        raise StatsInputError("attrition must be in [0, 1)")
    return {"n_evaluable": n, "attrition": attrition, "n_to_randomize": math.ceil(Fraction(n) / (1 - _decimal_fraction(attrition))), "method": "n / (1 − attrition), rounded up (exact arithmetic)"}


def attrition_sensitivity(delta: float, sd: float, n_randomized_per_group: int, attrition_rates: list[float], alpha: float = 0.05) -> list[dict[str, Any]]:
    """Power across attrition scenarios when only completers contribute (a completers-only simplification)."""
    delta, sd, alpha = _finite(delta, "delta"), _positive(sd, "sd"), _probability(alpha, "alpha")
    n_randomized_per_group = _whole(n_randomized_per_group, "n_randomized_per_group", minimum=1, maximum=MAX_GROUP_SIZE)
    out = []
    for rate in attrition_rates:
        rate = _finite(rate, "attrition rate")
        if not 0 <= rate < 1:
            raise StatsInputError("attrition rates must be in [0, 1)")
        # floor(n · (1 − rate)) in exact arithmetic: 90 at 30% is 63, not the 62 that floating point gives.
        evaluable = math.floor(Fraction(n_randomized_per_group) * (1 - _decimal_fraction(rate)))
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
    mean_treatment, mean_control = _finite(mean_treatment, "treatment mean"), _finite(mean_control, "control mean")
    missing_treatment, missing_control = _finite(missing_treatment, "missing-data proportion (treatment)"), _finite(missing_control, "missing-data proportion (control)")
    if not (0 <= missing_treatment < 1 and 0 <= missing_control < 1):
        raise StatsInputError("missing-data proportions must be in [0, 1)")
    rows = []
    for delta in deltas:
        delta = _finite(delta, "delta")
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


def check_simulation_budget(n_per_arm: int, n_sims: int) -> None:
    """Refuse a simulation whose size (trials x participants per arm) exceeds the documented budget."""
    cells = int(n_per_arm) * int(n_sims)
    if cells > MAX_SIMULATION_CELLS:
        raise StatsInputError(
            f"Simulation too large: n_per_arm x n_sims = {cells:,} exceeds the limit of {MAX_SIMULATION_CELLS:,}. "
            f"With {int(n_per_arm):,} participants per arm at most {MAX_SIMULATION_CELLS // int(n_per_arm):,} simulated trials can be run; reduce one of the two."
        )


def _two_sample_t(sum_t: np.ndarray, ss_t: np.ndarray, n_t: np.ndarray, sum_c: np.ndarray, ss_c: np.ndarray, n_c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Mean difference and two-sided p-value of the equal-variance two-sample t-test, row by row.

    ``ss`` are sums of squared deviations from each arm's own mean. Rows must have at least two
    observations per arm; the caller excludes the others.
    """
    diff = sum_t / n_t - sum_c / n_c
    dof = n_t + n_c - 2
    se = np.sqrt((ss_t + ss_c) / dof * (1 / n_t + 1 / n_c))
    p = 2 * sps.t.sf(np.abs(diff / se), dof)
    return diff, p


def simulate_two_arm_trial(spec: SimulationSpec) -> dict[str, Any]:
    """Monte Carlo simulation of a two-arm trial with a normal endpoint. Entirely SYNTHETIC.

    Trials are simulated in fixed-size blocks, so memory use does not grow with ``n_sims``; the total
    size is capped by ``MAX_SIMULATION_CELLS``. In the completers-only analysis a simulated trial in
    which either arm has fewer than two completers has no defined t-test: such trials are counted and
    reported, excluded from every summary (never turned into a zero difference or a non-rejection), and
    the simulation is refused when they exceed ``MAX_UNDEFINED_REPLICATE_FRACTION``.
    """
    n_per_arm = _whole(spec.n_per_arm, "n_per_arm", minimum=1, maximum=MAX_GROUP_SIZE)
    n_sims = _whole(spec.n_sims, "n_sims", minimum=1, maximum=MAX_SIMULATIONS)
    sd = _finite(spec.sd, "sd")
    if n_per_arm < 3 or sd <= 0 or n_sims < MIN_SIMULATIONS:
        raise StatsInputError("n_per_arm ≥ 3, sd > 0 and 100 ≤ n_sims ≤ 20000 are required")
    mean_treatment, mean_control = _finite(spec.mean_treatment, "mean_treatment"), _finite(spec.mean_control, "mean_control")
    dropout_treatment, dropout_control = _finite(spec.dropout_treatment, "dropout_treatment"), _finite(spec.dropout_control, "dropout_control")
    alpha = _probability(spec.alpha, "alpha")
    seed = _whole(spec.seed, "seed", minimum=0, maximum=2**63 - 1)
    if not (0 <= dropout_treatment < 1 and 0 <= dropout_control < 1):
        raise StatsInputError("dropout proportions must be in [0, 1)")
    if spec.missing_handling not in ("COMPLETERS", "JUMP_TO_REFERENCE"):
        raise StatsInputError("missing_handling must be COMPLETERS or JUMP_TO_REFERENCE")
    check_simulation_budget(n_per_arm, n_sims)

    rng = np.random.default_rng(seed)
    block = max(1, SIMULATION_BLOCK_CELLS // n_per_arm)
    diffs: list[np.ndarray] = []
    p_values: list[np.ndarray] = []
    not_analysable = 0
    done = 0
    while done < n_sims:
        rows = min(block, n_sims - done)
        done += rows
        shape = (rows, n_per_arm)
        trt = rng.normal(mean_treatment, sd, shape)
        ctl = rng.normal(mean_control, sd, shape)
        drop_t = rng.random(shape) < dropout_treatment
        drop_c = rng.random(shape) < dropout_control
        if spec.missing_handling == "JUMP_TO_REFERENCE":
            # Treatment-arm dropouts take outcomes from the control distribution (treatment-policy-like scenario).
            trt = np.where(drop_t, rng.normal(mean_control, sd, shape), trt)
            keep_t = np.ones(shape, dtype=bool)
            keep_c = keep_t
        else:
            keep_t, keep_c = ~drop_t, ~drop_c
        n_t, n_c = keep_t.sum(axis=1), keep_c.sum(axis=1)
        valid = (n_t >= 2) & (n_c >= 2)
        not_analysable += int(rows - valid.sum())
        if not valid.any():
            continue
        trt, ctl, keep_t, keep_c, n_t, n_c = trt[valid], ctl[valid], keep_t[valid], keep_c[valid], n_t[valid], n_c[valid]
        sum_t, sum_c = np.where(keep_t, trt, 0.0).sum(axis=1), np.where(keep_c, ctl, 0.0).sum(axis=1)
        ss_t = np.where(keep_t, (trt - (sum_t / n_t)[:, None]) ** 2, 0.0).sum(axis=1)
        ss_c = np.where(keep_c, (ctl - (sum_c / n_c)[:, None]) ** 2, 0.0).sum(axis=1)
        diff, p = _two_sample_t(sum_t, ss_t, n_t, sum_c, ss_c, n_c)
        diffs.append(diff)
        p_values.append(p)

    analysed = n_sims - not_analysable
    undefined_fraction = not_analysable / n_sims
    if analysed < 2 or undefined_fraction > MAX_UNDEFINED_REPLICATE_FRACTION:
        raise StatsInputError(
            f"In {not_analysable:,} of {n_sims:,} simulated trials ({undefined_fraction:.1%}) an arm had fewer than two completers, so the "
            "completers-only t-test is undefined. Power and the distribution of the estimated difference are not reported for such a design: "
            "increase n_per_arm or lower the dropout proportions."
        )
    all_diffs, all_p = np.concatenate(diffs), np.concatenate(p_values)
    if not (np.isfinite(all_diffs).all() and np.isfinite(all_p).all()):
        raise StatsInputError("The simulation produced non-finite values for these inputs; no result is returned.")
    rejected = float(np.mean(all_p < alpha))
    mc_se = math.sqrt(rejected * (1 - rejected) / analysed)
    warnings: list[str] = []
    if not_analysable:
        warnings.append(
            f"{not_analysable:,} of {n_sims:,} simulated trials had fewer than two completers in an arm; the t-test is undefined for them. "
            f"They are excluded from the rejection rate and from every summary below (based on {analysed:,} trials), not counted as non-rejections."
        )
    return {
        "label": "SYNTHETIC SIMULATION — not trial data",
        "kind": "SIMULATION",
        "seed": seed,
        "n_sims": n_sims,
        "n_sims_analysed": analysed,
        "n_sims_not_analysable": not_analysable,
        "warnings": warnings,
        "empirical_power": rejected,
        "monte_carlo_se": mc_se,
        "mean_estimated_difference": float(np.mean(all_diffs)),
        "sd_of_estimated_difference": float(np.std(all_diffs, ddof=1)),
        "percentiles_of_estimated_difference": {str(q): float(np.percentile(all_diffs, q)) for q in (2.5, 25, 50, 75, 97.5)},
        "assumptions": {
            "n_per_arm": n_per_arm,
            "mean_treatment": mean_treatment,
            "mean_control": mean_control,
            "sd": sd,
            "dropout_treatment": dropout_treatment,
            "dropout_control": dropout_control,
            "missing_handling": spec.missing_handling,
            "alpha": alpha,
            "outcome_distribution": "normal, common standard deviation, independent participants",
            "analysis": "two-sample t-test at the final visit (no covariate adjustment, no repeated measures)",
        },
        "limits": {"max_cells": MAX_SIMULATION_CELLS, "cells_used": n_per_arm * n_sims, "block_cells": SIMULATION_BLOCK_CELLS},
        "caveat": "Outcomes are drawn from the stated distributions. They are not derived from, and must not be presented as, data from any Lilly trial. "
        "The rejection rate is the operating characteristic of this simplified design under these assumptions; it is not a forecast of trial success.",
    }
