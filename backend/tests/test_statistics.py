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


# =========================================================================== Session 2: calculation audit (C9–C13, F01, F07)
#
# Names introduced in Session 2 are imported inside the tests so that each test fails on its own against
# the code it was written to correct.

SM1, SM3, SM4, SM_OSA, ATTAIN1, TRIUMPH1, ELORA_P2, ACHIEVE3 = "NCT04184622", "NCT04657016", "NCT04660643", "NCT05412004", "NCT05869903", "NCT05929066", "NCT06230523", "NCT06045221"


# ---- C9: one arm, one estimand, one time point per profile set


def test_c9_requested_estimand_is_never_mixed_and_arms_are_not_duplicated(db):
    for estimand in ("EFFICACY", "TREATMENT_REGIMEN"):
        profiles = benefit_risk.arm_profiles(db, ATTAIN1, estimand=estimand)
        entries = [a["efficacy"]["weight_pct"] for a in profiles["arms"] if "weight_pct" in a["efficacy"]]
        assert {e["estimand_label"] for e in entries} == {estimand} and {e["timepoint_weeks"] for e in entries} == {72.0}
        active = [a for a in profiles["arms"] if not a["is_placebo"]]
        assert sorted(a["dose_amount"] for a in active) == [6, 12, 36]  # three randomized doses, not six (capsule and tablet labels are one arm)
        assert len({a["arm_group_key"] for a in profiles["arms"]}) == len(profiles["arms"]) == len({a["key"] for a in profiles["arms"]})
        assert profiles["selection"]["weight_pct"]["estimand"] == estimand and profiles["selection"]["weight_pct"]["estimand_basis"] == "REQUESTED"
    top = next(a for a in profiles["arms"] if a["dose_amount"] == 36)
    assert any("FOUNDAYO 17.2" in label for label in top["labels_merged"])  # the label's tablet dose is the same arm, not a second profile


def test_c9_an_arm_without_the_requested_estimand_gets_no_value(db):
    profiles = benefit_risk.arm_profiles(db, SM3, estimand="TREATMENT_REGIMEN")  # only efficacy-estimand values are stored
    assert not any("weight_pct" in a["efficacy"] for a in profiles["arms"])
    missing = [n for n in profiles["not_available"] if n["metric"] == "weight_pct"]
    assert len(missing) == 2 and all(n["values_under_other_estimands"] == {"EFFICACY": 1} and "not substituted" in n["note"] for n in missing)
    efficacy = benefit_risk.arm_profiles(db, SM3, estimand="EFFICACY")
    assert sum("weight_pct" in a["efficacy"] for a in efficacy["arms"]) == 2


def test_c9_without_a_request_one_estimand_and_one_week_are_chosen_and_stated(db):
    profiles = benefit_risk.arm_profiles(db, TRIUMPH1)  # no planned duration on record; rows at week 80 and (extension subset) week 104
    entries = [a["efficacy"]["weight_pct"] for a in profiles["arms"] if "weight_pct" in a["efficacy"]]
    assert len(entries) == 4 and len({e["estimand_label"] for e in entries}) == 1 and {e["timepoint_weeks"] for e in entries} == {80.0}
    selection = profiles["selection"]["weight_pct"]
    assert selection["timepoint_basis"].startswith("MOST_ARMS_REPORTED") and selection["estimand_basis"].startswith("MOST_ARMS_REPORTED") and "display choice" in selection["estimand_basis"]
    assert selection["arms_by_estimand_at_timepoint"] == {"EFFICACY": 4, "TREATMENT_REGIMEN": 4}
    assert any("subset" in reason for reason in selection["rows_not_used"])  # the extension-subset rows are named as not used, not blended in
    for entry in entries:  # every number carries its estimand, time point, baseline, tier and source
        assert entry["estimand_label"] and entry["timepoint_weeks"] and entry["baseline_class"] and entry["tier"] and entry["tier_label"] and entry["source"]["key"]


def test_c9_no_fallback_to_another_week(db):
    profiles = benefit_risk.arm_profiles(db, SM4)  # planned randomized treatment is 52 weeks; values are stored at study weeks 64 and 88
    assert not any(a["efficacy"] for a in profiles["arms"])
    assert profiles["selection"]["weight_pct"]["arms_by_week"] == {"64": 2, "88": 2}
    assert any("week(s) 64, 88" in m and "none is substituted" in m for m in profiles["messages"])
    at88 = benefit_risk.arm_profiles(db, SM4, timepoint_weeks=88)
    entries = [a["efficacy"]["weight_pct"] for a in at88["arms"] if "weight_pct" in a["efficacy"]]
    assert sorted(e["value"] for e in entries) == [-6.7, 14.8] and {e["baseline_class"] for e in entries} == {"RANDOMIZATION_AFTER_LEAD_IN"}


def test_c9_different_arms_never_share_a_profile(db):
    profiles = benefit_risk.arm_profiles(db, ELORA_P2)  # three escalation schemes all end at 9 mg
    nine = [a for a in profiles["arms"] if a["drug_slug"] == "eloralintide" and a["dose_amount"] == 9]
    assert len(nine) >= 3 and len({a["key"] for a in nine}) == len(nine)
    assert {"9 mg Eloralintide", "6/9 mg Eloralintide", "3/6/9 mg Eloralintide"} <= {a["label"] for a in nine}
    osa = benefit_risk.arm_profiles(db, SM_OSA)  # two separately randomized sub-studies in one registration
    assert {a["randomization_stratum"] for a in osa["arms"]} == {"SUBSTUDY GPI1", "SUBSTUDY GPI2"} and any("not randomized against each other" in n for n in osa["notes"])


def test_c9_dose_scenario_checks_that_it_mixes_one_quantity(db):
    import copy

    profiles = benefit_risk.arm_profiles(db, SM1, estimand="EFFICACY")
    out = benefit_risk.dose_scenario(profiles, {"tirzepatide|15|": 0.6, "tirzepatide|10|": 0.4})
    assert out["estimand"] == "EFFICACY" and out["timepoint_weeks"] == 72.0 and any("this was checked, not assumed" in a for a in out["assumptions"])
    assert out["scenario_interval_95"][0] == pytest.approx(out["scenario_mean"] - 1.959963984540054 * out["scenario_se"])  # the normal quantile, not 1.96
    mixed = copy.deepcopy(profiles)
    next(a for a in mixed["arms"] if a["key"] == "tirzepatide|10|")["efficacy"]["weight_pct"]["estimand_label"] = "TREATMENT_REGIMEN"
    with pytest.raises(StatsInputError, match="different estimands"):
        benefit_risk.dose_scenario(mixed, {"tirzepatide|15|": 0.6, "tirzepatide|10|": 0.4})
    other_week = copy.deepcopy(profiles)
    next(a for a in other_week["arms"] if a["key"] == "tirzepatide|10|")["efficacy"]["weight_pct"]["timepoint_weeks"] = 176.0
    with pytest.raises(StatsInputError, match="different time points"):
        benefit_risk.dose_scenario(other_week, {"tirzepatide|15|": 0.6, "tirzepatide|10|": 0.4})
    for bad in (float("nan"), float("inf")):
        with pytest.raises(StatsInputError):
            benefit_risk.dose_scenario(profiles, {"tirzepatide|15|": 0.7}, non_persistent_share=0.3, non_persistent_value=bad)
    with pytest.raises(StatsInputError):
        benefit_risk.utility_scores(profiles, {"weight_pct": {"weight": 1, "best": float("nan"), "worst": 0}})


def test_s2_one_implementation_of_the_derived_interval(db, client):
    from incretinos.engines.findings import build_finding, derived_interval
    from incretinos.models import OutcomeResult

    profiles = benefit_risk.arm_profiles(db, SM1, estimand="EFFICACY")
    entry = next(a for a in profiles["arms"] if a["key"] == "tirzepatide|15|")["efficacy"]["weight_pct"]
    assert entry["ci_low"] is None and entry["derived_interval"]["kind"] == "INFERENCE"
    assert entry["derived_interval"]["ci_low"] == pytest.approx(stats.ci_from_se(-22.5, 0.39)["ci_low"])
    body = client.get(f"/api/findings/{entry['result_id']}").json()
    assert body["interpretation"]["derived_interval"]["ci_low"] == pytest.approx(entry["derived_interval"]["ci_low"])
    # A geometric mean with a standard error gets no symmetric interval: the scale of that standard error is not on record.
    geometric = next(r for r in db.query(OutcomeResult) if r.param_type and "GEOMETRIC" in r.param_type and r.dispersion_value is not None and r.dispersion_type and "standard error" in r.dispersion_type.lower() and r.ci_low is None)
    assert derived_interval(build_finding(db, geometric)) is None
    assert any("geometric mean" in m for m in client.get(f"/api/findings/{geometric.id}").json()["interpretation"]["missing"])


# ---- C10: a risk contrast is one event in two different randomized arms


def _ae(db, nct: str, term: str, arm: str, *, serious: bool = False, kind: str = "PARTICIPANTS_WITH_EVENT", period: str | None = None):
    from sqlalchemy import select

    from incretinos.models import Study

    study = db.scalar(select(Study).where(Study.nct_id == nct))
    for e in sorted(study.adverse_events, key=lambda x: x.id):
        if e.term.lower() == term.lower() and e.arm_label.lower() == arm.lower() and bool(e.is_serious) == serious and e.measure_kind == kind and (period is None or e.period == period):
            return e
    raise AssertionError(f"No adverse-event row {nct} / {term} / {arm}")


def test_c10_risk_contrast_refuses_anything_but_one_event_in_two_randomized_arms(db, client):
    from incretinos.engines.benefit_risk import adverse_event_contrast

    nausea_high, nausea_placebo = _ae(db, SM1, "Nausea", "15 mg Tirzepatide"), _ae(db, SM1, "Nausea", "Placebo")
    constipation_high, constipation_placebo = _ae(db, SM1, "Constipation", "15 mg Tirzepatide"), _ae(db, SM1, "Constipation", "Placebo")
    ok = adverse_event_contrast(db, nausea_high, nausea_placebo)
    assert ok["risk_difference"]["kind"] == "INFERENCE" and ok["risk_difference"]["estimate"] == pytest.approx(201 / 630 - 63 / 643) and ok["term"] == "Nausea"
    with pytest.raises(StatsInputError, match="different adverse-event terms"):
        adverse_event_contrast(db, nausea_high, constipation_placebo)
    with pytest.raises(StatsInputError, match="different adverse-event terms"):
        adverse_event_contrast(db, nausea_high, constipation_high)  # two terms in one arm
    with pytest.raises(StatsInputError, match="cannot be contrasted with itself"):
        adverse_event_contrast(db, nausea_high, nausea_high)
    withdrawal = _ae(db, SM1, "Did not complete study period — reason: adverse event", "Placebo", kind="STUDY_WITHDRAWAL_DUE_TO_AE")
    with pytest.raises(InvalidInferenceError):
        adverse_event_contrast(db, nausea_high, withdrawal)
    early = _ae(db, SM1, "Did not complete study period — reason: adverse event", "15 mg Tirzepatide", kind="STUDY_WITHDRAWAL_DUE_TO_AE", period="Primary Treatment Period")
    later = _ae(db, SM1, "Did not complete study period — reason: adverse event", "Placebo", kind="STUDY_WITHDRAWAL_DUE_TO_AE", period="Safety Follow-up Period")
    with pytest.raises(StatsInputError, match="different reporting periods"):
        adverse_event_contrast(db, early, later)
    other_trial = _ae(db, SM3, "Nausea", "Placebo")
    with pytest.raises(StatsInputError, match="same trial"):
        adverse_event_contrast(db, nausea_high, other_trial)
    # The same checks through the API: 422 with the reason, and a valid contrast still works.
    for a, b in ((nausea_high, constipation_placebo), (nausea_high, nausea_high), (nausea_high, constipation_high)):
        assert client.post("/api/stats/risk-contrast", json={"ae_a": a.id, "ae_b": b.id}).status_code == 422
    body = client.post("/api/stats/risk-contrast", json={"ae_a": nausea_high.id, "ae_b": nausea_placebo.id}).json()
    assert body["risk_ratio"]["estimate"] == pytest.approx((201 / 630) / (63 / 643)) and body["orientation"].startswith("A − B")


def test_c10_two_records_of_one_arm_are_not_contrasted(db):
    from sqlalchemy import select

    from incretinos.engines.benefit_risk import adverse_event_contrast
    from incretinos.models import Study

    study = db.scalar(select(Study).where(Study.nct_id == ATTAIN1))
    registry = next(e for e in study.adverse_events if e.term.lower() == "nausea" and e.origin == "REGISTRY" and e.arm_label == "36 mg Orforglipron" and not e.is_serious)
    twin = next(e for e in study.adverse_events if e.term.lower() == "nausea" and e.origin == "REGISTRY" and e.arm_label == "Placebo" and not e.is_serious)
    curated_arm = next(a for a in study.arms if a.origin == "CURATED" and a.dose_amount == 36 and a.formulation_key == "orforglipron-capsule")
    twin.arm_id, twin.arm = curated_arm.id, curated_arm  # the same counts, now filed under the curated label of the 36 mg arm
    db.flush()
    with pytest.raises(StatsInputError, match="same arm"):
        adverse_event_contrast(db, registry, twin)


# ---- C11: completers-only simulation


def test_c11_undefined_replicates_are_not_zeros_and_not_non_rejections():
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # the old code emitted divide-by-zero warnings and returned medians of exactly 0.0
        with pytest.raises(StatsInputError, match="fewer than two completers"):
            stats.simulate_two_arm_trial(stats.SimulationSpec(n_per_arm=3, mean_treatment=-5, mean_control=0, sd=5, dropout_treatment=0.9, dropout_control=0.9, n_sims=2000, seed=1))
        out = stats.simulate_two_arm_trial(stats.SimulationSpec(n_per_arm=10, mean_treatment=-5, mean_control=0, sd=5, dropout_treatment=0.4, dropout_control=0.4, n_sims=4000, seed=1))
    assert out["n_sims_not_analysable"] > 0 and out["n_sims_analysed"] + out["n_sims_not_analysable"] == 4000
    assert out["warnings"] and "not counted as non-rejections" in out["warnings"][0]
    assert out["mean_estimated_difference"] == pytest.approx(-5, abs=0.3) and all(v != 0.0 for v in out["percentiles_of_estimated_difference"].values())
    assert out["monte_carlo_se"] == pytest.approx(math.sqrt(out["empirical_power"] * (1 - out["empirical_power"]) / out["n_sims_analysed"]))
    clean = stats.simulate_two_arm_trial(stats.SimulationSpec(n_per_arm=100, mean_treatment=-15, mean_control=-10, sd=10, dropout_treatment=0.3, dropout_control=0.3, n_sims=500, seed=3))
    assert clean["n_sims_not_analysable"] == 0 and clean["warnings"] == [] and clean["n_sims_analysed"] == 500


def test_c11_block_wise_t_test_matches_scipy():
    rng = np.random.default_rng(11)
    x, y = rng.normal(0, 1, (40, 17)), rng.normal(0.4, 1, (40, 23))
    diff, p = stats._two_sample_t(x.sum(1), ((x - x.mean(1, keepdims=True)) ** 2).sum(1), np.full(40, 17), y.sum(1), ((y - y.mean(1, keepdims=True)) ** 2).sum(1), np.full(40, 23))
    reference = sps.ttest_ind(x, y, axis=1, equal_var=True)
    assert np.allclose(p, reference.pvalue, atol=1e-12) and np.allclose(diff, x.mean(1) - y.mean(1))


# ---- C12: attrition rounding and input checks


def test_c12_attrition_rounding_is_exact():
    from fractions import Fraction

    assert stats.inflate_for_attrition(465, 0.07)["n_to_randomize"] == 500  # floating point gave 501
    assert stats.inflate_for_attrition(21, 0.30)["n_to_randomize"] == 30 and stats.inflate_for_attrition(42, 0.3)["n_to_randomize"] == 60
    for n in range(1, 400):
        for pct in range(0, 51):
            assert stats.inflate_for_attrition(n, pct / 100)["n_to_randomize"] == math.ceil(Fraction(n * 100, 100 - pct)), (n, pct)
            assert stats.attrition_sensitivity(5, 10, n + 2, [pct / 100])[0]["n_evaluable_per_group"] == math.floor(Fraction((n + 2) * (100 - pct), 100)), (n, pct)
    assert stats.attrition_sensitivity(5, 10, 90, [0.3])[0]["n_evaluable_per_group"] == 63 and stats.attrition_sensitivity(5, 10, 500, [0.07])[0]["n_evaluable_per_group"] == 465


@pytest.mark.parametrize(
    "call",
    [
        lambda: stats.inflate_for_attrition(-5, 0.2),
        lambda: stats.inflate_for_attrition(100, float("nan")),
        lambda: stats.n_noninferiority_means(0.4, 1.1, ratio=-1),
        lambda: stats.n_noninferiority_means(0.4, 1.1, ratio=0),
        lambda: stats.n_noninferiority_means(float("inf"), 1.0),
        lambda: stats.events_logrank(0.8, alpha=0),
        lambda: stats.events_logrank(0.8, power=1),
        lambda: stats.events_logrank(1.0000000000001),
        lambda: stats.power_two_means(50, 3, 8, alpha=1.5),
        lambda: stats.ci_from_se(1.0, 0.5, level=1.5, df=10),
        lambda: stats.ci_from_se(1.0, 0.5, df=0),
        lambda: stats.ci_from_se(float("nan"), 0.5),
        lambda: stats.n_two_means(float("nan"), 1),
        lambda: stats.n_two_means(1e-200, 1),
        lambda: stats.n_two_means(1, float("inf")),
        lambda: stats.n_two_means(1, 1, alpha=1e-300),
        lambda: stats.n_two_proportions(0.5, 0.5000000001),
        lambda: stats.risk_difference(float("nan"), 100, 5, 100),
        lambda: stats.missing_data_delta_scenarios(float("inf"), 0, 0.1, 0.1, [1]),
        lambda: stats.simulate_two_arm_trial(stats.SimulationSpec(n_per_arm=100, mean_treatment=float("nan"), mean_control=0, sd=1)),
        lambda: stats.simulate_two_arm_trial(stats.SimulationSpec(n_per_arm=100, mean_treatment=0, mean_control=0, sd=1, alpha=1.5)),
    ],
)
def test_c12_invalid_inputs_raise_a_stats_input_error_not_a_crash_or_a_number(call):
    with pytest.raises(StatsInputError):
        call()


# ---- C13: an absent count is not 0


def test_c13_absent_counts_stay_absent(db, client):
    profiles = benefit_risk.arm_profiles(db, SM4, timepoint_weeks=88)
    randomized = [a for a in profiles["arms"] if a["label"] in ("Tirzepatide MTD", "Placebo")]
    assert len(randomized) == 2
    for arm in randomized:  # the registry's first participant-flow period (the lead-in) reports 0 for groups that did not exist yet
        assert arm["n_randomized"] is None and "not available" in arm["n_randomized_note"]
    assert next(a for a in profiles["arms"] if a["label"] == "Tirzepatide (lead-in)")["n_randomized"] == 783
    terms = client.get(f"/api/safety/terms?nct={ACHIEVE3}").json()["terms"]
    percent_only = next(t for t in terms if t["term"] == "any gastrointestinal adverse event")  # reported as a percentage only
    assert percent_only["max_participants_affected"] is None and percent_only["count_status"] == "NOT_REPORTED"
    counted = [t for t in terms if t["max_participants_affected"] is not None]
    assert counted and terms.index(counted[0]) == 0 and all(t["count_status"] == "REPORTED" for t in counted)


# ---- F01 and F07: calculation endpoints refuse oversized and non-finite inputs


def _post_raw(client, path: str, body: str):
    return client.post(path, content=body, headers={"Content-Type": "application/json"})


def test_f01_simulation_size_is_capped_before_any_memory_is_allocated(client):
    import time
    import tracemalloc

    started = time.perf_counter()
    response = client.post("/api/design/simulate", json={"n_per_arm": 20000, "mean_treatment": -5, "mean_control": 0, "sd": 5, "n_sims": 20000})
    assert response.status_code == 422 and time.perf_counter() - started < 2
    assert "5,000,000" in str(response.json()["detail"]) and "400,000,000" in str(response.json()["detail"])
    assert stats.MAX_SIMULATION_CELLS == 5_000_000
    with pytest.raises(StatsInputError, match="Simulation too large"):  # the engine enforces the same cap for any other caller
        stats.simulate_two_arm_trial(stats.SimulationSpec(n_per_arm=20000, mean_treatment=-5, mean_control=0, sd=5, n_sims=20000))
    # At the cap, memory is bounded by the block size, not by the number of simulated trials.
    for handling in ("COMPLETERS", "JUMP_TO_REFERENCE"):
        tracemalloc.start()
        out = stats.simulate_two_arm_trial(stats.SimulationSpec(n_per_arm=250, mean_treatment=-5, mean_control=0, sd=20, dropout_treatment=0.2, dropout_control=0.2, missing_handling=handling, n_sims=20000))
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert out["limits"]["cells_used"] == stats.MAX_SIMULATION_CELLS and peak < 64 * 2**20, (handling, peak)
    assert client.post("/api/design/simulate", json={"n_per_arm": 150, "mean_treatment": -15, "mean_control": -3, "sd": 9, "dropout_treatment": 0.15, "dropout_control": 0.2, "n_sims": 2000}).status_code == 200


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/design/power", '{"design":"two_means","delta":NaN,"sd":1}'),
        ("/api/design/power", '{"design":"two_means","delta":1e-200,"sd":1}'),
        ("/api/design/power", '{"design":"two_means","delta":1,"sd":1e200}'),
        ("/api/design/power", '{"design":"two_means","delta":1,"sd":Infinity}'),
        ("/api/design/power", '{"design":"two_means","delta":1,"sd":1,"alpha":1e-300}'),
        ("/api/design/power", '{"design":"noninferiority_means","margin":Infinity,"sd":1}'),
        ("/api/design/power", '{"design":"logrank_events","hazard_ratio":1.0000000000001}'),
        ("/api/design/simulate", '{"n_per_arm":100,"mean_treatment":NaN,"mean_control":0,"sd":5}'),
        ("/api/design/simulate", '{"n_per_arm":100,"mean_treatment":1e308,"mean_control":0,"sd":1e308}'),
        ("/api/design/simulate", '{"n_per_arm":3,"mean_treatment":-5,"mean_control":0,"sd":5,"dropout_treatment":0.9,"dropout_control":0.9}'),
        ("/api/design/sensitivity", '{"delta":NaN,"sd":1,"n_randomized_per_group":100}'),
        ("/api/design/sensitivity", '{"delta":5,"sd":10,"n_randomized_per_group":100,"deltas":[Infinity]}'),
        ("/api/design/sensitivity", '{"delta":5,"sd":10,"n_randomized_per_group":100,"mean_treatment":-Infinity,"mean_control":0}'),
        ("/api/benefit-risk/utility", '{"nct":"NCT04184622","criteria":{"weight_pct":{"weight":1,"best":NaN,"worst":0}}}'),
        ("/api/benefit-risk/utility", '{"nct":"NCT04184622","criteria":{"weight_pct":{"weight":1,"best":1e300,"worst":0}}}'),
        ("/api/benefit-risk/dose-scenario", '{"nct":"NCT04184622","shares":{},"non_persistent_share":1,"non_persistent_value":NaN}'),
        ("/api/benefit-risk/dose-scenario", '{"nct":"NCT04184622","shares":{"tirzepatide|15|":NaN}}'),
        ("/api/benefit-risk/dose-scenario", '{"nct":"NCT04184622","shares":{},"non_persistent_share":1,"non_persistent_value":-3}'),
        ("/api/safety/disproportionality", '{"drug_event":120,"drug_total":10000,"event_total":5000,"all_total":1e400}'),
    ],
)
def test_f07_non_finite_and_absurd_inputs_get_a_422_with_a_reason(client, path, body):
    response = _post_raw(client, path, body)
    assert response.status_code == 422, (path, body, response.status_code, response.text[:200])
    detail = response.json()["detail"]
    text = detail if isinstance(detail, str) else " ".join(str(item.get("msg", "")) for item in detail)
    assert len(text) > 15 and "NaN" not in response.text.replace('"NaN"', "")  # a reason is given, and the reply is valid JSON


def test_f07_query_numbers_are_checked_too(client):
    assert client.get("/api/benefit-risk/NCT04184622?weeks=NaN").status_code == 422
    assert client.get("/api/benefit-risk/NCT04184622?weeks=1e9").status_code == 422
    assert client.get("/api/harmonize?formulation=orforglipron-capsule&amount=Infinity&target=foundayo-tablet").status_code == 422
    ok = client.post("/api/design/power", json={"design": "two_means", "delta": 5, "sd": 10, "attrition": 0.07})
    assert ok.status_code == 200 and ok.json()["n_group_1"] == 85 and ok.json()["with_attrition"]["n_to_randomize"] == 183  # 170 / 0.93 = 182.8


def test_f07_disproportionality_rejects_non_finite_counts():
    for bad in (float("nan"), float("inf"), True, "12"):
        with pytest.raises(StatsInputError):
            disproportionality(bad, 10_000, 5_000, 2_000_000)
    with pytest.raises(StatsInputError):
        disproportionality(120, 10_000, 5_000, 2_000_000, level=1.5)
