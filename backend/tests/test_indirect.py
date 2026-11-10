"""Anchored indirect comparison (Bucher): formulas, independent references, coverage, refusals, the seeded case and the API.

References used to validate the arithmetic:

1. Independent implementation: a weighted least-squares fit of the two-trial network (arm-level means with known
   standard errors; study and treatment indicators; C as reference) by numpy linear algebra and by statsmodels WLS
   with the scale fixed at 1. The A − B contrast of that fit and its standard error must equal the Bucher estimate.
2. Monte Carlo: coverage of the Bucher interval built from the *reported* intervals of simulated trials (the same path
   the engine takes: interval -> SE -> indirect estimate).
3. The SE-from-interval step against statsmodels' own standard error of a fitted treatment effect.

A published worked example was sought (Song F, Altman DG, Glenny AM, Deeks JJ. BMJ 2003;326:472, web extra "Adjusted
indirect comparison of competing interventions: method and worked example"; the Cochrane Handbook, chapter 11; the R for
HTA textbook, section 10.1.2). The BMJ web extra could not be retrieved (the PMC copy sits behind a browser check and
bmj.com refused the request); the Handbook chapter gives no numerical two-trial example; the textbook example gives odds
ratios without standard errors. No numbers from a source that could not be read are used here; no example is invented.
"""

from __future__ import annotations

import copy
import math

import numpy as np
import pytest
import statsmodels.api as sm
from scipy import stats as sps
from sqlalchemy import select

from incretinos.engines import indirect
from incretinos.engines.findings import analysis_dto
from incretinos.engines.interpretation import interpret_analysis
from incretinos.models import StatisticalAnalysis

from .conftest import analysis_id

SM1, ATTAIN1, SM2, TRIUMPH1, SM5, ACHIEVE3 = "NCT04184622", "NCT05869903", "NCT04657003", "NCT05929066", "NCT05822830", "NCT06045221"


# --------------------------------------------------------------------------- formulas


def test_se_from_a_two_sided_interval_on_each_scale():
    z = sps.norm.ppf(0.975)
    assert indirect.se_from_ci(-21.2, -19.0, 95.0, "DIFFERENCE")["se"] == pytest.approx(2.2 / (2 * z))
    assert indirect.se_from_ci(-3.0, 1.0, 90.0, "DIFFERENCE")["se"] == pytest.approx(4.0 / (2 * sps.norm.ppf(0.95)))
    ratio = indirect.se_from_ci(0.8, 1.25, 95.0, "RATIO")
    assert ratio["on_log_scale"] and ratio["se"] == pytest.approx((math.log(1.25) - math.log(0.8)) / (2 * z))
    for bad in ((1.0, 1.0, 95.0, "DIFFERENCE"), (-1.0, 1.0, 95.0, "RATIO"), (0.0, 1.0, 40.0, "DIFFERENCE"), (0.0, 1.0, 100.0, "DIFFERENCE")):
        with pytest.raises(indirect.IndirectInputError):
            indirect.se_from_ci(*bad)


def test_bucher_arithmetic():
    out = indirect.bucher(-20.1, 0.5, -11.48, 0.4, 0.95)
    assert out["d"] == pytest.approx(-8.62) and out["se"] == pytest.approx(math.sqrt(0.25 + 0.16))
    assert out["lower"] == pytest.approx(-8.62 - sps.norm.ppf(0.975) * out["se"]) and out["upper"] == pytest.approx(-8.62 + sps.norm.ppf(0.975) * out["se"])
    assert out["p_two_sided"] == pytest.approx(2 * sps.norm.sf(8.62 / out["se"]))
    ninety = indirect.bucher(1.0, 1.0, 0.0, 1.0, 0.90)
    assert ninety["upper"] - ninety["lower"] == pytest.approx(2 * sps.norm.ppf(0.95) * math.sqrt(2))
    assert indirect.bucher(0.0, 1.0, 0.0, 1.0)["p_two_sided"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- independent reference: WLS on the two-trial network


def _network_design() -> np.ndarray:
    # Rows: trial 1 arm A, trial 1 arm C, trial 2 arm B, trial 2 arm C. Columns: study 1, study 2, treatment A, treatment B (C = reference).
    return np.array([[1, 0, 1, 0], [1, 0, 0, 0], [0, 1, 0, 1], [0, 1, 0, 0]], dtype=float)


@pytest.mark.parametrize("arms", [
    # (mean, SE) per arm: A and C in trial 1, B and C in trial 2.
    ((-22.5, 0.40), (-2.4, 0.38), (-12.35, 0.30), (-0.87, 0.29)),
    ((1.7, 0.9), (0.2, 1.1), (-0.4, 0.5), (0.3, 0.45)),
    ((10.0, 2.0), (4.0, 2.5), (3.0, 1.0), (5.0, 1.5)),
])
def test_bucher_equals_a_weighted_least_squares_fit_of_the_two_trial_network(arms):
    y = np.array([m for m, _ in arms])
    se = np.array([s for _, s in arms])
    x, w = _network_design(), np.diag(1 / se**2)
    # numpy linear algebra: beta = (X'WX)^-1 X'Wy, cov = (X'WX)^-1 (known variances, scale 1).
    cov = np.linalg.inv(x.T @ w @ x)
    beta = cov @ x.T @ w @ y
    contrast = np.array([0, 0, 1, -1])
    lin_est, lin_se = contrast @ beta, math.sqrt(contrast @ cov @ contrast)
    # statsmodels WLS with the residual scale fixed at 1, so the arm SEs are taken as known.
    fit = sm.WLS(y, x, weights=1 / se**2).fit(cov_type="fixed scale")
    t = fit.t_test(contrast)
    # Bucher from the two within-trial contrasts.
    d_ac, se_ac = y[0] - y[1], math.hypot(se[0], se[1])
    d_bc, se_bc = y[2] - y[3], math.hypot(se[2], se[3])
    ours = indirect.bucher(d_ac, se_ac, d_bc, se_bc)
    for est, s in ((lin_est, lin_se), (float(np.squeeze(t.effect)), float(np.squeeze(t.sd)))):
        assert ours["d"] == pytest.approx(est, abs=1e-10) and ours["se"] == pytest.approx(s, abs=1e-10)
    low, high = np.squeeze(t.conf_int(alpha=0.05))
    assert ours["lower"] == pytest.approx(low, abs=1e-9) and ours["upper"] == pytest.approx(high, abs=1e-9)
    assert ours["p_two_sided"] == pytest.approx(float(np.squeeze(t.pvalue)), rel=1e-9, abs=1e-300)


def test_se_recovered_from_an_interval_matches_statsmodels_standard_error():
    rng = np.random.default_rng(20261109)
    n = 400
    treat = np.repeat([1.0, 0.0], n // 2)
    y = -10 * treat + rng.normal(0, 8, n)
    fit = sm.OLS(y, sm.add_constant(treat)).fit()
    low, high = fit.conf_int(alpha=0.05)[1]
    # statsmodels uses a t quantile (df = 398); the engine uses the normal one, so the two agree within their ratio.
    recovered = indirect.se_from_ci(low, high, 95.0, "DIFFERENCE")["se"]
    ratio = sps.t.ppf(0.975, n - 2) / sps.norm.ppf(0.975)
    assert recovered == pytest.approx(fit.bse[1] * ratio, rel=1e-10)
    assert abs(recovered / fit.bse[1] - 1) < 0.004  # 0.3% for df = 398


# --------------------------------------------------------------------------- Monte Carlo coverage


def test_bucher_interval_coverage_on_simulated_trials():
    """Simulate trial pairs, report each trial's 95% CI, rebuild the SEs from those intervals, and check coverage of the true A − B."""
    rng = np.random.default_rng(7)
    reps, n1, n2, sd = 4000, 150, 220, 9.0
    mu = {"A": -15.0, "B": -9.0, "C1": -2.0, "C2": -1.0}  # different placebo responses: anchoring removes them
    truth = (mu["A"] - mu["C1"]) - (mu["B"] - mu["C2"])
    z = sps.norm.ppf(0.975)
    covered = 0
    for _ in range(reps):
        a, c1 = rng.normal(mu["A"], sd, n1), rng.normal(mu["C1"], sd, n1)
        b, c2 = rng.normal(mu["B"], sd, n2), rng.normal(mu["C2"], sd, n2)
        contrasts = []
        for t, c in ((a, c1), (b, c2)):
            d = t.mean() - c.mean()
            s = math.sqrt(t.var(ddof=1) / len(t) + c.var(ddof=1) / len(c))
            lo, hi = round(d - z * s, 2), round(d + z * s, 2)  # reported to 2 decimals, as registries do
            contrasts.append((round(d, 2), indirect.se_from_ci(lo, hi, 95.0, "DIFFERENCE")["se"]))
        out = indirect.bucher(contrasts[0][0], contrasts[0][1], contrasts[1][0], contrasts[1][1])
        covered += out["lower"] <= truth <= out["upper"]
    coverage = covered / reps
    assert abs(coverage - 0.95) < 3 * math.sqrt(0.95 * 0.05 / reps), coverage


def test_bucher_interval_coverage_for_log_risk_ratios():
    rng = np.random.default_rng(11)
    reps, n = 3000, 500
    p = {"A": 0.30, "C1": 0.15, "B": 0.24, "C2": 0.12}
    truth = math.log((p["A"] / p["C1"]) / (p["B"] / p["C2"]))
    z = sps.norm.ppf(0.975)
    covered = 0
    for _ in range(reps):
        logs = []
        for t, c in (("A", "C1"), ("B", "C2")):
            et, ec = rng.binomial(n, p[t]), rng.binomial(n, p[c])
            rr = (et / n) / (ec / n)
            s = math.sqrt(1 / et - 1 / n + 1 / ec - 1 / n)
            lo, hi = rr * math.exp(-z * s), rr * math.exp(z * s)
            logs.append((math.log(rr), indirect.se_from_ci(lo, hi, 95.0, "RATIO")["se"]))
        out = indirect.bucher(logs[0][0], logs[0][1], logs[1][0], logs[1][1])
        covered += out["lower"] <= truth <= out["upper"]
    assert abs(covered / reps - 0.95) < 0.015, covered / reps


# --------------------------------------------------------------------------- the seeded case: SURMOUNT-1 × ATTAIN-1


def _ids(db) -> dict[str, int]:
    return {"sm1_15": analysis_id(db, SM1, "15 mg Tirzepatide"), "attain_36": analysis_id(db, ATTAIN1, "36 mg Orforglipron"),
            "sm2_15": analysis_id(db, SM2, "15 mg Tirzepatide"), "triumph1_12": analysis_id(db, TRIUMPH1, "12 mg", weeks=80, origin="CURATED"),
            "sm5": analysis_id(db, SM5, "Tirzepatide", comparator_contains="Semaglutide", estimand="EFFICACY"),
            "achieve3": analysis_id(db, ACHIEVE3, "12 mg Orforglipron", comparator_contains="7 mg Semaglutide", weeks=52)}


def _check(result: dict, key: str) -> dict:
    return next(c for c in result["checks"] if c["key"] == key)


def test_surmount1_attain1_is_calculated_from_the_stored_rows_only(db):
    ids = _ids(db)
    a_row, b_row = db.get(StatisticalAnalysis, ids["sm1_15"]), db.get(StatisticalAnalysis, ids["attain_36"])
    out = indirect.assess(db, ids["sm1_15"], ids["attain_36"])
    assert out["feasible"] and out["status"] == "CALCULATED"
    for key in indirect.HARD_CHECKS:
        assert _check(out, key)["status"] == indirect.PASS, (key, _check(out, key)["detail"])
    assert _check(out, "population")["status"] == indirect.PASS
    # What cannot be verified is shown, not hidden.
    assert _check(out, "effect_modifiers")["status"] == indirect.CANNOT_ASSESS and _check(out, "transitivity")["status"] == indirect.CANNOT_ASSESS
    assert {x["key"] for x in out["unverified_assumptions"]} == {"effect_modifiers", "transitivity"}
    modifiers = {m["key"]: m for m in out["effect_modifiers"]}
    assert {"baseline_weight", "baseline_bmi", "sex", "age", "diabetes_status", "background_therapy", "dose_escalation", "follow_up", "region"} <= set(modifiers)
    assert modifiers["baseline_bmi"]["a"] == "not stored" and modifiers["background_therapy"]["assessment"] == "NOT_STORED"
    assert modifiers["comparator_administration"]["assessment"] == "DIFFERS"  # weekly injection versus daily oral placebo
    # The numbers are the stored ones, and the formula is Bucher's.
    z = sps.norm.ppf(0.975)
    se_a = (a_row.ci_high - a_row.ci_low) / (2 * z)
    se_b = (b_row.ci_high - b_row.ci_low) / (2 * z)
    r = out["result"]
    assert r["estimate"] == pytest.approx(a_row.estimate - b_row.estimate) == pytest.approx(-20.1 - (-11.48))
    assert r["se"] == pytest.approx(math.hypot(se_a, se_b))
    assert r["ci_low"] == pytest.approx(r["estimate"] - z * r["se"]) and r["ci_high"] == pytest.approx(r["estimate"] + z * r["se"])
    assert r["kind"] == "INFERENCE" and "not a randomized comparison" in r["label"] and "Bucher" in r["label"]
    assert "not a randomized result" in r["reading"] and "not a ranking" in r["reading"]
    assert "never randomized" in out["network"]["text"] and [e["kind"] for e in out["network"]["edges"]].count("INDIRECT_NOT_RANDOMIZED") == 1
    assert {i["verification_status"] for i in out["inputs"]} == {"MACHINE_EXTRACTED"} and all(i["source"] for i in out["inputs"])
    assert out["multiplicity_note"] and "selective" in out["multiplicity_note"]
    # A 90% interval is narrower around the same estimate.
    ninety = indirect.assess(db, ids["sm1_15"], ids["attain_36"], level=0.90)["result"]
    assert ninety["estimate"] == pytest.approx(r["estimate"]) and ninety["ci_high"] - ninety["ci_low"] < r["ci_high"] - r["ci_low"]


def test_on_the_seed_only_surmount1_week72_by_attain1_pairs_are_feasible_for_weight(db):
    scan = indirect.scan(db, "WEIGHT_PCT_CHANGE")
    assert scan["pairs_examined"] > 100 and scan["feasible"]
    trials = {(p["a_label"].split(" · ")[0], p["b_label"].split(" · ")[0]) for p in scan["feasible"]}
    assert trials == {("ATTAIN-1", "SURMOUNT-1")}, trials
    assert len(scan["feasible"]) == 9  # 3 orforglipron doses x 3 tirzepatide doses, week 72, efficacy estimand


def test_scientifically_invalid_pairs_on_the_seed_are_refused(db):
    ids = _ids(db)
    t2d = indirect.assess(db, ids["sm2_15"], ids["attain_36"])  # SURMOUNT-2 (T2D) x ATTAIN-1 (no T2D)
    assert not t2d["feasible"] and t2d["result"] is None and t2d["status"] == "REFUSED"
    assert _check(t2d, "population")["status"] == indirect.FAIL and "type 2 diabetes" in _check(t2d, "population")["detail"]
    week80 = indirect.assess(db, ids["triumph1_12"], ids["sm1_15"])  # TRIUMPH-1 week 80 (treatment-regimen, no CI stored) x SURMOUNT-1 week 72
    assert not week80["feasible"] and week80["result"] is None
    assert {_check(week80, k)["status"] for k in ("follow_up", "estimand", "variance", "reliability")} == {indirect.FAIL}
    comparator = indirect.assess(db, ids["sm5"], ids["achieve3"])  # semaglutide 2.4 mg (SC) versus 7 mg (oral) as the "common" comparator
    assert not comparator["feasible"] and _check(comparator, "comparator")["status"] != indirect.PASS
    same_trial = indirect.assess(db, ids["sm1_15"], analysis_id(db, SM1, "5 mg Tirzepatide"))
    assert _check(same_trial, "network")["status"] == indirect.FAIL and not same_trial["feasible"]


def test_pending_review_input_is_refused(db):
    ids = _ids(db)
    row = db.get(StatisticalAnalysis, ids["attain_36"])
    row.verification_status = "PENDING_HUMAN_REVIEW"  # in this session only; the fixture rolls it back
    out = indirect.assess(db, ids["sm1_15"], ids["attain_36"])
    assert not out["feasible"] and _check(out, "reliability")["status"] == indirect.FAIL and "awaits a human decision" in _check(out, "reliability")["detail"]


# --------------------------------------------------------------------------- every FAIL path refuses (one change at a time)


@pytest.fixture()
def valid_pair(db):
    ids = _ids(db)
    names = indirect._drug_names(db)
    a = indirect.describe_contrast(db, db.get(StatisticalAnalysis, ids["sm1_15"]), names)
    b = indirect.describe_contrast(db, db.get(StatisticalAnalysis, ids["attain_36"]), names)
    assert indirect.evaluate(a, b)["feasible"]
    return a, b


def _set(side: dict, path: str, value) -> None:
    *head, last = path.split(".")
    for key in head:
        side = side[key]
    side[last] = value


FAIL_PATHS = [
    ("different estimand", [("estimand_label", "TREATMENT_REGIMEN"), ("estimand_basis", "STATED_BY_SOURCE")], "estimand", indirect.FAIL),
    ("estimand not stated", [("estimand_label", "NOT_STATED")], "estimand", indirect.CANNOT_ASSESS),
    ("T2D versus no T2D", [("population.diabetes_status", "T2D")], "population", indirect.FAIL),
    ("week 80 versus week 72", [("timepoint_weeks", 80.0)], "follow_up", indirect.FAIL),
    ("missing CI", [("ci_low", None), ("ci_high", None)], "variance", indirect.FAIL),
    ("unknown confidence level", [("ci_level", None), ("ci_level_status", "LEVEL_NOT_REPORTED")], "variance", indirect.FAIL),
    ("sidedness not stated", [("ci_sides", "NOT_STATED")], "variance", indirect.FAIL),
    ("one-sided interval", [("ci_sides", "ONE_SIDED")], "variance", indirect.FAIL),
    ("pending human review", [("verification_status", "PENDING_HUMAN_REVIEW")], "reliability", indirect.FAIL),
    ("rejected", [("verification_status", "REJECTED"), ("exclusion_reason", "Rejected in review on this exact content.")], "reliability", indirect.FAIL),
    ("held after a rejection", [("review_hold", True), ("exclusion_reason", "held")], "reliability", indirect.FAIL),
    ("quote-anchored curated value", [("verification_status", "QUOTE_ANCHORED"), ("origin", "CURATED"), ("tier", "PEER_REVIEWED")], "reliability", indirect.FAIL),
    ("direction not established", [("direction.established", False), ("direction.basis", "registry order")], "reliability", indirect.FAIL),
    ("internally inconsistent", [("internal_consistency", {"status": "P_VALUE_DISAGREES_WITH_INTERVAL", "detail": "x"})], "reliability", indirect.FAIL),
    ("ratio versus difference", [("effect_scale", {"scale": "RATIO", "null_value": 1.0, "basis": "ratio"}), ("ratio_kind", "HAZARD_RATIO")], "effect_scale", indirect.FAIL),
    ("scale not established", [("effect_scale", {"scale": "NOT_ESTABLISHED", "null_value": None, "basis": "unknown"})], "effect_scale", indirect.CANNOT_ASSESS),
    ("different comparator", [("comparator.key", "active:semaglutide"), ("comparator.display", "Semaglutide (active comparator)")], "comparator", indirect.FAIL),
    ("different unit", [("endpoint.unit_family", "KILOGRAM"), ("endpoint.unit", "kg")], "outcome", indirect.FAIL),
    ("different baseline", [("endpoint.baseline_class", "RANDOMIZATION_AFTER_LEAD_IN")], "outcome", indirect.FAIL),
    ("different category", [("endpoint.category", "WEIGHT_ABS_CHANGE")], "outcome", indirect.FAIL),
    ("lead-in design in one trial", [("population.has_lead_in", True), ("population.lead_in", {"lead_in": "12 weeks"})], "population", indirect.FAIL),
    ("major imbalance in age", [("population.age_mean_years", 60.0)], "effect_modifiers", indirect.FAIL),
    ("same trial", [("study_id", None)], "network", indirect.FAIL),
    ("same drug", [("drug", "tirzepatide"), ("drug_name", "Tirzepatide")], "network", indirect.FAIL),
]


@pytest.mark.parametrize("name,changes,key,status", FAIL_PATHS, ids=[p[0] for p in FAIL_PATHS])
def test_each_failed_requirement_refuses_the_calculation(valid_pair, name, changes, key, status):
    a, b = copy.deepcopy(valid_pair[0]), copy.deepcopy(valid_pair[1])
    for path, value in changes:
        _set(b, path, a["study_id"] if path == "study_id" else value)
    out = indirect.evaluate(a, b)
    assert _check(out, key)["status"] == status, (name, _check(out, key))
    assert not out["feasible"] and out["result"] is None and out["status"] == "REFUSED"
    assert any(r.startswith(_check(out, key)["label"]) for r in out["refusal_reasons"]), out["refusal_reasons"]


def test_a_follow_up_tolerance_needs_a_justification_and_is_reported(valid_pair):
    a, b = copy.deepcopy(valid_pair[0]), copy.deepcopy(valid_pair[1])
    b["timepoint_weeks"] = 68.0
    with pytest.raises(indirect.IndirectInputError):
        indirect.evaluate(a, b, tolerance_weeks=4)
    out = indirect.evaluate(a, b, tolerance_weeks=4, justification="Both drugs reach their weight plateau before week 64 in their own trials.")
    assert _check(out, "follow_up")["status"] == indirect.PASS and "requester's judgement" in _check(out, "follow_up")["detail"]
    assert out["result"]["follow_up_tolerance"].startswith("Both drugs")
    # A tolerance is no licence: the effect-modifier list still records the different weeks, as an unverified assumption.
    assert next(m for m in out["effect_modifiers"] if m["key"] == "follow_up")["assessment"] == "DIFFERS"
    assert _check(out, "effect_modifiers")["status"] == indirect.CANNOT_ASSESS
    beyond = indirect.evaluate(a, b, tolerance_weeks=2, justification="Both drugs reach their weight plateau before week 64 in their own trials.")
    assert _check(beyond, "follow_up")["status"] == indirect.FAIL and not beyond["feasible"]


def test_ratio_contrasts_are_combined_on_the_log_scale(valid_pair):
    a, b = copy.deepcopy(valid_pair[0]), copy.deepcopy(valid_pair[1])
    for side, (est, lo, hi) in ((a, (0.80, 0.70, 0.914)), (b, (0.90, 0.78, 1.04))):
        side.update({"estimate": est, "ci_low": lo, "ci_high": hi, "ratio_kind": "HAZARD_RATIO", "param_type": "Hazard Ratio",
                     "effect_scale": {"scale": "RATIO", "null_value": 1.0, "basis": "hazard ratio"}})
    out = indirect.evaluate(a, b)
    assert out["feasible"], out["refusal_reasons"]
    z = sps.norm.ppf(0.975)
    se = math.hypot((math.log(0.914) - math.log(0.70)) / (2 * z), (math.log(1.04) - math.log(0.78)) / (2 * z))
    r = out["result"]
    assert r["scale"] == "RATIO" and r["estimate"] == pytest.approx(0.80 / 0.90) and r["se"] == pytest.approx(se)
    assert r["ci_low"] == pytest.approx(math.exp(math.log(0.8 / 0.9) - z * se)) and r["null_value"] == 1.0 and "÷" in r["orientation"]


# --------------------------------------------------------------------------- direct randomized comparisons: the reported effect, never beyond its fields


def test_direct_analysis_reports_scale_orientation_interval_estimand_population_and_multiplicity(db):
    row = db.get(StatisticalAnalysis, analysis_id(db, SM1, "15 mg Tirzepatide"))
    dto = analysis_dto(row)
    effect = interpret_analysis(dto)["reported_effect"]
    assert effect["scale"] == "DIFFERENCE" and effect["orientation"] == "15 mg Tirzepatide − Placebo"
    assert effect["ci"]["level"] == 95.0 and effect["ci"]["sides"] == "TWO_SIDED" and "two-sided" in effect["ci"]["text"]
    assert effect["p_value_text"] == "<0.001" and effect["estimand"] == {"label": "EFFICACY", "basis": "INFERRED_FROM_ANALYSIS_DESCRIPTION"}
    assert "excluding data after prematurely stopping study drug" in effect["analysis_population"] and "625 versus 635" in effect["analysis_population"]
    assert effect["multiplicity"]["status"] == "NOT_STATED" and "not stated" in effect["plain_reading"]
    assert dto["analysis_population"]["n_treatment"] == 625 and dto["analysis_population"]["status"] == "STATED"


def test_direct_reading_draws_nothing_from_an_interval_of_unknown_level_and_keeps_ni_apart():
    base = {"id": 0, "estimate": -2.0, "ci_low": -3.0, "ci_high": -1.0, "ci_level": None, "ci_sides": "TWO_SIDED", "param_type": "LS Mean Difference", "unit": "kg",
            "treatment_label": "Drug", "comparator_label": "Placebo", "p_value_text": None, "role": "PRIMARY", "multiplicity_controlled": "YES", "hypothesis_type": "SUPERIORITY",
            "ni_margin": None, "ni_met": "NOT_STATED", "superiority_tested": "NOT_STATED", "superiority_met": "NOT_STATED", "tier": "PEER_REVIEWED", "ci_status": "REPORTED",
            "p_value_status": "NOT_REPORTED", "estimand_label": "EFFICACY", "labels": [], "source": None, "locator_quote": None, "verification_status": "QUOTE_ANCHORED",
            "direction": {"established": True, "status": "STATED_IN_CURATED_SOURCE", "basis": "stated"}}
    unknown = interpret_analysis(base)["reported_effect"]
    assert "excludes" not in unknown["plain_reading"] and "No statement about significance" in unknown["plain_reading"] and unknown["ci"]["level"] is None
    ni = interpret_analysis({**base, "ci_level": 95.0, "hypothesis_type": "NON_INFERIORITY", "ni_met": "YES"})["reported_effect"]
    assert "non-inferiority" in ni["plain_reading"] and "Non-inferiority is not superiority" in ni["plain_reading"]
    sides = interpret_analysis({**base, "ci_level": 95.0, "ci_sides": "NOT_STATED"})
    assert "sidedness not stated" in sides["reported_effect"]["plain_reading"] and any("one- or two-sided" in m for m in sides["missing"])
    pending = interpret_analysis({**base, "ci_level": 95.0, "verification_status": "PENDING_HUMAN_REVIEW"})
    assert "awaits human review" in pending["reported_effect"]["plain_reading"]
    hidden = interpret_analysis({**base, "ci_level": 95.0, "direction": {"established": False, "status": "REGISTRY_ORDER_NOT_VERIFIED", "basis": "order"}})["reported_effect"]
    assert hidden["operator"] is None and "not established" in hidden["orientation"] and "excludes" not in hidden["plain_reading"]


# --------------------------------------------------------------------------- API


def test_api_assess_options_candidates_and_feasible(client, db):
    ids = _ids(db)
    ok = client.post("/api/indirect/assess", json={"a": ids["sm1_15"], "b": ids["attain_36"]})
    assert ok.status_code == 200 and ok.json()["status"] == "CALCULATED" and ok.json()["result"]["estimate"] == pytest.approx(-8.62)
    refused = client.post("/api/indirect/assess", json={"a": ids["sm2_15"], "b": ids["attain_36"]})
    assert refused.status_code == 200 and refused.json()["status"] == "REFUSED" and refused.json()["result"] is None and refused.json()["refusal_reasons"]
    assert client.post("/api/indirect/assess", json={"a": ids["sm1_15"], "b": ids["sm1_15"]}).status_code == 422
    assert client.post("/api/indirect/assess", json={"a": ids["sm1_15"], "b": 2**31 - 1}).status_code == 404
    assert client.post("/api/indirect/assess", json={"a": ids["sm1_15"], "b": ids["attain_36"], "followup_tolerance_weeks": 4}).status_code == 422
    assert client.post("/api/indirect/assess", json={"a": ids["sm1_15"], "b": ids["attain_36"], "extra": 1}).status_code == 422
    options = client.get("/api/indirect/options", params={"category": "WEIGHT_PCT_CHANGE"}).json()
    assert {"tirzepatide", "orforglipron"} <= {d["slug"] for d in options["drugs"]} and "placebo" in {c["key"] for c in options["comparators"]}
    rows = client.get("/api/indirect/candidates", params={"drug": "orforglipron", "comparator": "placebo", "category": "WEIGHT_PCT_CHANGE"}).json()["rows"]
    assert ids["attain_36"] in {r["analysis_id"] for r in rows} and all(r["drug"] == "orforglipron" and r["comparator_key"] == "placebo" for r in rows)
    assert next(r for r in rows if r["analysis_id"] == ids["attain_36"])["usable_alone"] is True
    assert client.get("/api/indirect/candidates", params={"drug": "x", "comparator": "placebo", "category": "BOGUS"}).status_code == 422
    feasible = client.get("/api/indirect/feasible").json()
    assert len(feasible["feasible"]) == 9 and "pairs" not in feasible


def test_no_analysis_becomes_human_verified_by_running_the_engine(db):
    before = {a.id: a.verification_status for a in db.scalars(select(StatisticalAnalysis))}
    indirect.scan(db, "WEIGHT_PCT_CHANGE")
    assert {a.id: a.verification_status for a in db.scalars(select(StatisticalAnalysis))} == before and not db.dirty and not db.new
