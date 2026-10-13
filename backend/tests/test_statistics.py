"""Statistical calculations checked against independent implementations and published values."""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import stats as sps
from statsmodels.stats.power import NormalIndPower
from statsmodels.stats.proportion import confint_proportions_2indep, proportion_confint

from incretinos.engines import benefit_risk, stats
from incretinos.engines.safety import InvalidInferenceError, disproportionality
from incretinos.engines.stats import StatsInputError


def test_wilson_interval_matches_statsmodels():
    for events, n in ((0, 20), (3, 50), (201, 630), (629, 630)):
        low, high = stats.wilson_interval(events, n)
        ref_low, ref_high = proportion_confint(events, n, alpha=0.05, method="wilson")
        assert low == pytest.approx(ref_low, abs=1e-12) and high == pytest.approx(ref_high, abs=1e-12)


def test_risk_difference_newcombe_matches_statsmodels():
    for a, na, b, nb in ((201, 630, 61, 643), (39, 630, 17, 643), (5, 40, 1, 38)):
        ours = stats.risk_difference(a, na, b, nb)
        ref_low, ref_high = confint_proportions_2indep(a, na, b, nb, method="newcomb", compare="diff", alpha=0.05, correction=False)
        assert ours["estimate"] == pytest.approx(a / na - b / nb)
        assert ours["ci_low"] == pytest.approx(ref_low, abs=1e-9) and ours["ci_high"] == pytest.approx(ref_high, abs=1e-9)
        assert ours["kind"] == "INFERENCE" and "descriptive" in ours["interpretation"]


def test_risk_ratio_katz_interval():
    out = stats.risk_ratio(201, 630, 61, 643)
    rr = (201 / 630) / (61 / 643)
    se = math.sqrt(1 / 201 - 1 / 630 + 1 / 61 - 1 / 643)
    assert out["estimate"] == pytest.approx(rr)
    assert out["ci_low"] == pytest.approx(rr * math.exp(-1.959963984540054 * se)) and out["ci_high"] == pytest.approx(rr * math.exp(1.959963984540054 * se))


def test_risk_ratio_with_zero_cell_is_not_patched():
    out = stats.risk_ratio(0, 100, 5, 100)
    assert out["estimate"] is None and out["ci_low"] is None and "no continuity correction" in out["not_estimable"]


@pytest.mark.parametrize("args", [(None, 100, 5, 100), (5, None, 5, 100), (5, 0, 5, 100), (101, 100, 5, 100), (-1, 100, 5, 100), (2.5, 100, 5, 100)])
def test_missing_or_invalid_counts_raise_instead_of_defaulting(args):
    with pytest.raises(StatsInputError):
        stats.risk_difference(*args)
    with pytest.raises(StatsInputError):
        stats.risk_ratio(*args)


def test_ci_from_se_is_flagged_as_inference():
    out = stats.ci_from_se(-22.5, 0.39)
    assert out["ci_low"] == pytest.approx(-22.5 - 1.959963984540054 * 0.39) and out["kind"] == "INFERENCE"
    with pytest.raises(StatsInputError):
        stats.ci_from_se(-22.5, None)


def test_sample_size_two_means_matches_statsmodels_and_textbook():
    out = stats.n_two_means(delta=5, sd=10, alpha=0.05, power=0.9)
    reference = NormalIndPower().solve_power(effect_size=0.5, alpha=0.05, power=0.9, ratio=1.0, alternative="two-sided")
    assert out["unrounded_n_group_1"] == pytest.approx(reference, rel=1e-4)
    assert out["n_group_1"] == 85  # 2·(10·(1.960+1.282)/5)² = 84.06 → 85 per group
    unequal = stats.n_two_means(delta=5, sd=10, power=0.9, ratio=2.0)
    assert unequal["unrounded_n_group_1"] == pytest.approx(NormalIndPower().solve_power(effect_size=0.5, alpha=0.05, power=0.9, ratio=2.0), rel=1e-4)
    assert unequal["n_group_2"] >= 2 * unequal["n_group_1"] - 1


def test_power_two_means_is_inverse_of_sample_size():
    n = stats.n_two_means(delta=3, sd=8, power=0.8)["n_group_1"]
    assert stats.power_two_means(n, 3, 8)["power"] >= 0.8
    assert stats.power_two_means(n - 2, 3, 8)["power"] < 0.8 + 0.01


def test_sample_size_two_proportions_textbook_value():
    # Fleiss, pooled-variance formula without continuity correction: p1=0.60, p2=0.50, α=0.05 two-sided, power 0.80 → 387.3 → 388 per group.
    out = stats.n_two_proportions(0.60, 0.50, alpha=0.05, power=0.80)
    assert out["unrounded_n_group_1"] == pytest.approx(387.3, abs=0.2) and out["n_group_1"] == 388


def test_schoenfeld_events_published_value():
    # HR 0.80, two-sided α 0.05, power 0.80, 1:1 allocation → 4(1.960+0.842)²/(ln 0.8)² = 630.5 → 631 events.
    out = stats.events_logrank(0.8, alpha=0.05, power=0.8)
    assert out["unrounded_events"] == pytest.approx(630.5, abs=0.2) and out["events_required"] == 631
    assert stats.events_logrank(0.8, power=0.9)["events_required"] == 845  # 4(1.960+1.282)²/(ln 0.8)² = 844.0…
    assert stats.events_logrank(1.25, power=0.8)["events_required"] == 631  # symmetric on the log scale


def test_noninferiority_events_for_fda_cv_safety_margin():
    # Excluding HR 1.8 with true HR 1.0, one-sided α 0.025, 90% power: (1.960+1.282)²·4/(ln 1.8)² = 121.7 → 122 events.
    out = stats.events_noninferiority_hr(1.8, 1.0, alpha_one_sided=0.025, power=0.9)
    assert out["events_required"] == 122
    assert stats.events_noninferiority_hr(1.3, 1.0, alpha_one_sided=0.025, power=0.9)["events_required"] == 611
    with pytest.raises(StatsInputError):
        stats.events_noninferiority_hr(0.9)


def test_noninferiority_means_and_attrition_inflation():
    out = stats.n_noninferiority_means(margin=0.4, sd=1.1, alpha_one_sided=0.025, power=0.9)
    assert out["unrounded_n_group_1"] == pytest.approx(2 * (1.1 * (1.959964 + 1.281552) / 0.4) ** 2, rel=1e-5)
    assert stats.inflate_for_attrition(170, 0.2)["n_to_randomize"] == 213
    with pytest.raises(StatsInputError):
        stats.inflate_for_attrition(100, 1.0)


def test_simulation_is_reproducible_labelled_and_consistent_with_theory():
    spec = stats.SimulationSpec(n_per_arm=86, mean_treatment=-15, mean_control=-10, sd=10, n_sims=4000, seed=7)
    one, two = stats.simulate_two_arm_trial(spec), stats.simulate_two_arm_trial(spec)
    assert one == two and one["seed"] == 7 and one["kind"] == "SIMULATION" and one["label"].startswith("SYNTHETIC")
    analytic = stats.power_two_means(86, 5, 10)["power"]
    assert abs(one["empirical_power"] - analytic) < 4 * one["monte_carlo_se"] + 0.01
    assert one["mean_estimated_difference"] == pytest.approx(-5, abs=0.15)
    other_seed = stats.simulate_two_arm_trial(stats.SimulationSpec(n_per_arm=86, mean_treatment=-15, mean_control=-10, sd=10, n_sims=4000, seed=8))
    assert other_seed["empirical_power"] != one["empirical_power"]


def test_simulation_type_one_error_and_dropout_scenarios():
    null = stats.simulate_two_arm_trial(stats.SimulationSpec(n_per_arm=100, mean_treatment=-10, mean_control=-10, sd=10, n_sims=6000, seed=1))
    assert 0.035 < null["empirical_power"] < 0.065  # rejection rate under no effect ≈ α
    full = stats.simulate_two_arm_trial(stats.SimulationSpec(n_per_arm=100, mean_treatment=-15, mean_control=-10, sd=10, n_sims=3000, seed=2))
    j2r = stats.simulate_two_arm_trial(stats.SimulationSpec(n_per_arm=100, mean_treatment=-15, mean_control=-10, sd=10, dropout_treatment=0.3, missing_handling="JUMP_TO_REFERENCE", n_sims=3000, seed=2))
    completers = stats.simulate_two_arm_trial(stats.SimulationSpec(n_per_arm=100, mean_treatment=-15, mean_control=-10, sd=10, dropout_treatment=0.3, dropout_control=0.3, n_sims=3000, seed=2))
    assert j2r["mean_estimated_difference"] == pytest.approx(-3.5, abs=0.2)  # 70% × −5 under jump-to-reference
    assert j2r["empirical_power"] < completers["empirical_power"] < full["empirical_power"]
    with pytest.raises(StatsInputError):
        stats.simulate_two_arm_trial(stats.SimulationSpec(n_per_arm=2, mean_treatment=0, mean_control=0, sd=1))


def test_missing_data_delta_scenarios_are_mixtures():
    rows = stats.missing_data_delta_scenarios(-20.0, -3.0, 0.2, 0.2, [0, 5, 17])
    assert rows[0]["difference"] == pytest.approx(-17.0) and rows[1]["marginal_treatment_mean"] == pytest.approx(-19.0)
    assert rows[2]["marginal_treatment_mean"] == pytest.approx(-16.6) and all(r["kind"] == "SIMULATION" for r in rows)


def test_attrition_sensitivity_monotone():
    rows = stats.attrition_sensitivity(5, 10, 100, [0, 0.2, 0.4])
    powers = [r["power"] for r in rows]
    assert powers == sorted(powers, reverse=True) and rows[2]["n_evaluable_per_group"] == 60


# --------------------------------------------------------------------------- disproportionality


def test_disproportionality_matches_hand_calculation():
    a, drug_total, event_total, all_total = 120, 10_000, 5_000, 2_000_000
    out = disproportionality(a, drug_total, event_total, all_total)
    b, c, d = drug_total - a, event_total - a, all_total - drug_total - (event_total - a)
    assert out["table"] == {"a_drug_and_event": a, "b_drug_without_event": b, "c_event_without_drug": c, "d_neither": d}
    assert out["prr"] == pytest.approx((a / (a + b)) / (c / (c + d))) and out["ror"] == pytest.approx((a * d) / (b * c))
    se = math.sqrt(1 / a + 1 / b + 1 / c + 1 / d)
    assert out["ror_ci"][0] == pytest.approx(math.exp(math.log(out["ror"]) - sps.norm.ppf(0.975) * se))
    assert out["is_incidence"] is False and out["supports_causality"] is False and len(out["caveats"]) >= 5


def test_disproportionality_refuses_thin_or_inconsistent_tables():
    assert disproportionality(2, 1000, 500, 100000)["prr"] is None  # fewer than 3 reports
    with pytest.raises(StatsInputError):
        disproportionality(50, 40, 500, 100000)  # more drug–event reports than drug reports
    with pytest.raises(StatsInputError):
        disproportionality(None, 40, 500, 100000)


# --------------------------------------------------------------------------- benefit–risk workbench


def test_arm_profiles_keep_measures_separate(db):
    profiles = benefit_risk.arm_profiles(db, "NCT04184622", estimand="TREATMENT_REGIMEN")
    arm = next(a for a in profiles["arms"] if a["key"] == "tirzepatide|15|")
    assert arm["efficacy"]["weight_pct"]["value"] == -20.9 and arm["efficacy"]["weight_pct"]["estimand_label"] == "TREATMENT_REGIMEN"
    tol = arm["tolerability"]
    assert tol["treatment_discontinuation_ae"]["measure_kind"] == "TREATMENT_DISCONTINUATION_DUE_TO_AE" and tol["treatment_discontinuation_ae"]["percent"] == 6.2
    assert tol["study_withdrawal_ae"]["measure_kind"] == "STUDY_WITHDRAWAL_DUE_TO_AE" and tol["study_withdrawal_ae"]["percent"] == pytest.approx(100 * 6 / 630, abs=0.01)
    assert tol["nausea"]["measure_kind"] == "PARTICIPANTS_WITH_EVENT" and tol["nausea"]["percent_basis"] == "calculated from reported counts"
    assert tol["nausea"]["percent"] == pytest.approx(100 * tol["nausea"]["n_affected"] / tol["nausea"]["n_at_risk"], abs=0.01)
    efficacy = benefit_risk.arm_profiles(db, "NCT04184622", estimand="EFFICACY")
    assert next(a for a in efficacy["arms"] if a["key"] == "tirzepatide|15|")["efficacy"]["weight_pct"]["value"] == -22.5


def test_utility_score_is_preference_dependent_and_never_scores_missing_as_zero(db):
    profiles = benefit_risk.arm_profiles(db, "NCT04184622", estimand="TREATMENT_REGIMEN")
    efficacy_first = benefit_risk.utility_scores(profiles, {"weight_pct": {"weight": 1, "best": -25, "worst": 0}, "nausea": {"weight": 0, "best": 0, "worst": 50}})
    tolerability_first = benefit_risk.utility_scores(profiles, {"weight_pct": {"weight": 0.05, "best": -25, "worst": 0}, "nausea": {"weight": 0.95, "best": 0, "worst": 50}})
    best_a = max(efficacy_first["rows"], key=lambda r: r["score"])["arm"]
    best_b = max(tolerability_first["rows"], key=lambda r: r["score"])["arm"]
    assert best_a == "15 mg Tirzepatide" and best_b == "Placebo"  # the ordering reverses with the weights
    assert "not a validated clinical endpoint" in efficacy_first["label"] and len(efficacy_first["caveats"]) >= 3
    elora = benefit_risk.arm_profiles(db, "NCT06603571")
    scores = benefit_risk.utility_scores(elora)
    combo = next(r for r in scores["rows"] if r["arm"].startswith("eloralintide 9 mg + tirzepatide 15"))
    assert combo["score"] is None and "treatment_discontinuation_ae" in combo["missing_criteria"]  # only a range was disclosed
    with pytest.raises(StatsInputError):
        benefit_risk.utility_scores(profiles, {"weight_pct": {"weight": 1, "best": 0, "worst": 0}})


def test_dose_scenario_is_a_labelled_mixture(db):
    profiles = benefit_risk.arm_profiles(db, "NCT04184622", estimand="EFFICACY")
    out = benefit_risk.dose_scenario(profiles, {"tirzepatide|15|": 0.6, "tirzepatide|10|": 0.25, "tirzepatide|5|": 0.15})
    assert out["scenario_mean"] == pytest.approx(0.6 * -22.5 + 0.25 * -21.4 + 0.15 * -16.0)
    assert out["scenario_se"] == pytest.approx(math.sqrt((0.6 * 0.39) ** 2 + (0.25 * 0.39) ** 2 + (0.15 * 0.39) ** 2))
    assert out["label"].startswith("HYPOTHETICAL") and out["kind"] == "SIMULATION" and out["individual_data_required"] and "Any treatment recommendation." in out["not_supported"]
    with_dropout = benefit_risk.dose_scenario(profiles, {"tirzepatide|15|": 0.7}, non_persistent_share=0.3)
    assert with_dropout["scenario_mean"] == pytest.approx(0.7 * -22.5 + 0.3 * -2.4) and with_dropout["scenario_se"] is None
    with pytest.raises(StatsInputError, match="sum to 1"):
        benefit_risk.dose_scenario(profiles, {"tirzepatide|15|": 0.5})
    with pytest.raises(StatsInputError, match="not computed with a substitute"):
        benefit_risk.dose_scenario(profiles, {"tirzepatide|20|": 1.0})


def test_arm_contrasts_only_within_one_trial_and_one_measure():
    out = benefit_risk.arm_contrast(201, 630, 61, 643, "PARTICIPANTS_WITH_EVENT", "PARTICIPANTS_WITH_EVENT", same_study=True)
    assert out["risk_ratio"]["estimate"] > 1 and out["risk_difference"]["ci_low"] > 0
    with pytest.raises(StatsInputError, match="same trial"):
        benefit_risk.arm_contrast(201, 630, 61, 643, "PARTICIPANTS_WITH_EVENT", "PARTICIPANTS_WITH_EVENT", same_study=False)
    with pytest.raises(InvalidInferenceError):
        benefit_risk.arm_contrast(39, 630, 61, 643, "TREATMENT_DISCONTINUATION_DUE_TO_AE", "PARTICIPANTS_WITH_EVENT", same_study=True)
    with pytest.raises(StatsInputError):
        benefit_risk.arm_contrast(None, 630, 61, 643, "PARTICIPANTS_WITH_EVENT", "PARTICIPANTS_WITH_EVENT", same_study=True)


def test_numpy_generator_seed_is_recorded_in_output():
    out = stats.simulate_two_arm_trial(stats.SimulationSpec(n_per_arm=30, mean_treatment=-1, mean_control=0, sd=3, n_sims=200, seed=123))
    assert out["seed"] == 123 and out["assumptions"]["n_per_arm"] == 30 and isinstance(np.float64(out["empirical_power"]), np.floating)
