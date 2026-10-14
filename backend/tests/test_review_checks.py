"""Automated evidence checks (incretinos.review.checks).

Three kinds of test:

* Synthetic records in a private in-memory database: for every code in the registry, one small record
  that must raise the flag and one that must not. These never depend on what the seeded evidence contains.
* The ten mis-attributions from the Session 2 audit (CUR-01), each of which loaded as QUOTE_ANCHORED.
* Properties over the seeded database: flags are well formed, quote-based checks never touch registry
  records, and the one-record and whole-database entry points agree.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from incretinos.db import Base
from incretinos.models import (
    AdverseEvent,
    Drug,
    Endpoint,
    Estimand,
    EvidenceClaim,
    EvidenceSource,
    OutcomeResult,
    StatisticalAnalysis,
    Study,
    StudyArm,
)
from incretinos.review import checks
from incretinos.review.checks import CHECKS, QUOTE_BASED_CODES, evaluate, evaluate_all
from incretinos.review.policy import EVIDENCE_TYPES, CheckFlag, risk_level
from incretinos.vocab import norm_arm_label

MODELS = {"result": OutcomeResult, "analysis": StatisticalAnalysis, "adverse_event": AdverseEvent, "claim": EvidenceClaim}

RESULT_QUOTE = (
    "At week 72, the mean change in body weight was -20.9% (95% CI, -21.8 to -19.9) with 15 mg of tirzepatide (n=630) "
    "and -3.1% (95% CI, -4.3 to -1.9) with placebo (n=643)"
)
ANALYSIS_QUOTE = "At week 72 the estimated treatment difference between tirzepatide 15 mg and placebo was -17.8 percentage points (95% CI, -19.3 to -16.3; P<0.001)"
AE_QUOTE = "Over 72 weeks, nausea was reported by 195 of 630 participants (31.0%) receiving tirzepatide 15 mg and by 61 of 643 (9.5%) receiving placebo"


class World:
    """A one-study evidence base. Every helper has defaults that describe a record with nothing to flag."""

    def __init__(self, session: Session) -> None:
        self.s = session
        self.release = EvidenceSource(key="web:trial-1-release", source_type="WEB", tier="PRESS_RELEASE", title="Topline release")
        self.paper = EvidenceSource(key="pubmed:1", source_type="PUBMED", tier="PEER_REVIEWED", title="Primary publication")
        self.registry = EvidenceSource(key="ctgov:NCT00000001", source_type="CTGOV", tier="REGISTRY_RESULTS", title="Registry record")
        self.study = Study(nct_id="NCT00000001", acronym="TRIAL-1", display_name="TRIAL-1", brief_title="A synthetic trial")
        session.add_all([self.release, self.paper, self.registry, self.study])
        session.add(Drug(slug="tirzepatide", name="Tirzepatide", code_name="LY3298176", brand_names=["Mounjaro", "Zepbound"]))
        session.flush()
        self._arms: dict[str, StudyArm] = {}
        self._endpoints: dict[tuple, Endpoint] = {}

    def arm(self, label: str, *, drug: str | None = "tirzepatide", dose: float | None = None, unit: str | None = "mg", placebo: bool = False) -> StudyArm:
        if label not in self._arms:
            arm = StudyArm(
                study_id=self.study.id, label=label, norm_label=norm_arm_label(label), drug_slug="placebo" if placebo else drug,
                is_placebo=placebo, dose_amount=dose, dose_unit=unit if dose is not None else None,
            )  # fmt: skip
            self.s.add(arm)
            self.s.flush()
            self._arms[label] = arm
        return self._arms[label]

    @property
    def high_dose(self) -> StudyArm:
        return self.arm("tirzepatide 15 mg", dose=15)

    @property
    def low_dose(self) -> StudyArm:
        return self.arm("tirzepatide 5 mg", dose=5)

    @property
    def placebo(self) -> StudyArm:
        return self.arm("placebo", placebo=True)

    def endpoint(self, title: str = "percent change in body weight from baseline", category: str = "WEIGHT_PCT_CHANGE", *, threshold: float | None = None, unit: str | None = "%") -> Endpoint:
        key = (title, category, threshold)
        if key not in self._endpoints:
            endpoint = Endpoint(study_id=self.study.id, title=title, category=category, threshold=threshold, unit=unit, origin="CURATED", kind="CURATED")
            self.s.add(endpoint)
            self.s.flush()
            self._endpoints[key] = endpoint
        return self._endpoints[key]

    def estimand(self, label: str, *, basis: str = "STATED_BY_SOURCE", quote: str | None = None, source: EvidenceSource | None = None) -> Estimand:
        estimand = Estimand(study_id=self.study.id, label=label, basis=basis, quote=quote, source_id=(source or self.release).id)
        self.s.add(estimand)
        self.s.flush()
        return estimand

    def result(self, *, arm: StudyArm | None = None, endpoint: Endpoint | None = None, estimand: Estimand | None = None, source: EvidenceSource | None = None, registry: bool = False, **fields: Any) -> OutcomeResult:
        arm = arm or self.high_dose
        source = self.registry if registry else (source or self.release)
        values: dict[str, Any] = dict(
            scope="ARM", value=-20.9, unit="%", ci_low=-21.8, ci_high=-19.9, ci_level=95.0, n_analyzed=630, timepoint_weeks=72.0,
            arm_label=arm.label, locator_quote=None if registry else RESULT_QUOTE,
        )  # fmt: skip
        values.update(fields)
        row = OutcomeResult(
            study_id=self.study.id, endpoint_id=(endpoint or self.endpoint()).id, arm_id=arm.id, estimand_id=estimand.id if estimand else None,
            origin="REGISTRY" if registry else "CURATED", source_id=source.id, tier=source.tier,
            verification_status="MACHINE_EXTRACTED" if registry else "QUOTE_ANCHORED", **values,
        )  # fmt: skip
        self.s.add(row)
        self.s.flush()
        return row

    def analysis(self, *, treatment: StudyArm | None = None, comparator: StudyArm | None = None, endpoint: Endpoint | None = None, estimand: Estimand | None = None, source: EvidenceSource | None = None, registry: bool = False, **fields: Any) -> StatisticalAnalysis:
        treatment, comparator = treatment or self.high_dose, comparator or self.placebo
        source = self.registry if registry else (source or self.release)
        values: dict[str, Any] = dict(
            hypothesis_type="SUPERIORITY", param_type="Estimated treatment difference", estimate=-17.8, unit="percentage points",
            ci_low=-19.3, ci_high=-16.3, ci_level=95.0, p_value_text="<0.001", timepoint_weeks=72.0,
            treatment_label=treatment.label, comparator_label=comparator.label, locator_quote=None if registry else ANALYSIS_QUOTE,
        )  # fmt: skip
        values.update(fields)
        endpoint = endpoint or self.endpoint("difference in percent change in body weight versus placebo", unit="percentage points")
        row = StatisticalAnalysis(
            study_id=self.study.id, endpoint_id=endpoint.id, estimand_id=estimand.id if estimand else None, treatment_arm_id=treatment.id,
            comparator_arm_id=comparator.id, origin="REGISTRY" if registry else "CURATED", source_id=source.id, tier=source.tier,
            verification_status="MACHINE_EXTRACTED" if registry else "QUOTE_ANCHORED", **values,
        )  # fmt: skip
        self.s.add(row)
        self.s.flush()
        return row

    def ae(self, *, arm: StudyArm | None = None, source: EvidenceSource | None = None, registry: bool = False, **fields: Any) -> AdverseEvent:
        arm = arm or self.high_dose
        source = self.registry if registry else (source or self.release)
        values: dict[str, Any] = dict(
            scope="ARM", term="nausea", category="GASTROINTESTINAL", measure_kind="PARTICIPANTS_WITH_EVENT", n_affected=195, n_at_risk=630,
            percent=None if registry else 31.0, period="72 weeks", arm_label=arm.label, locator_quote=None if registry else AE_QUOTE,
        )  # fmt: skip
        values.update(fields)
        row = AdverseEvent(
            study_id=self.study.id, arm_id=arm.id, origin="REGISTRY" if registry else "CURATED", source_id=source.id, tier=source.tier,
            verification_status="MACHINE_EXTRACTED" if registry else "QUOTE_ANCHORED", **values,
        )  # fmt: skip
        self.s.add(row)
        self.s.flush()
        return row

    def claim(self, statement: str = "In TRIAL-1, tirzepatide 15 mg reduced body weight by 20.9% at week 72.", quote: str | None = "In TRIAL-1, participants receiving tirzepatide 15 mg lost 20.9% of body weight at week 72", *, key: str = "claim-1") -> EvidenceClaim:
        row = EvidenceClaim(key=key, topic="RESULT", statement=statement, quote=quote, study_id=self.study.id, source_id=self.release.id, tier=self.release.tier, verification_status="QUOTE_ANCHORED")
        self.s.add(row)
        self.s.flush()
        return row


@pytest.fixture()
def world():
    """A private in-memory database; nothing here touches the seeded evidence."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield World(session)
    engine.dispose()


def etype_of(row: Any) -> str:
    return next(name for name, model in MODELS.items() if isinstance(row, model))


def flags_of(world: World, row: Any, **kwargs: Any) -> list[CheckFlag]:
    return evaluate(world.s, etype_of(row), row, **kwargs)


def codes(flags: list[CheckFlag]) -> set[str]:
    return {f.code for f in flags}


def explanation(flags: list[CheckFlag], code: str) -> str:
    return next(f.explanation for f in flags if f.code == code)


# --------------------------------------------------------------------------------------- the registry


def test_registry_is_well_formed():
    assert len(CHECKS) >= 40
    for code, spec in CHECKS.items():
        assert re.fullmatch(r"[A-Z][A-Z0-9_]+", code)
        assert spec["severity"] in ("HIGH", "MEDIUM", "LOW"), code
        assert spec["title"] and spec["title"][0].isupper(), code
        # one plain sentence: ends with a full stop and has none inside
        assert spec["help"].endswith(".") and ". " not in spec["help"], code
        assert set(spec["applies_to"]) <= set(EVIDENCE_TYPES) and spec["applies_to"], code
    assert QUOTE_BASED_CODES == {c for c, s in CHECKS.items() if s["quote_based"]}
    assert {"QUOTE_MULTIPLE_CANDIDATES", "ENDPOINT_THRESHOLD_CONTRADICTED", "NUMBER_NOT_IN_QUOTE"} <= QUOTE_BASED_CODES
    assert not {"INTERVAL_LIMITS_REVERSED", "AFFECTED_EXCEEDS_AT_RISK", "DUPLICATE_KEY_DIFFERENT_VALUES", "ANALYSIS_DIRECTION_VS_ARMS"} & QUOTE_BASED_CODES


def test_self_contained_records_raise_no_flag(world):
    """A quote that carries the value, its interval, the arm, n and the week leaves nothing to flag."""
    rows = [world.result(), world.analysis(), world.ae(), world.claim()]
    for row in rows:
        assert flags_of(world, row) == [], etype_of(row)
    everything = evaluate_all(world.s)
    assert everything == {(etype_of(row), row.id): [] for row in rows}


def test_unknown_evidence_type_and_wrong_model_are_rejected(world):
    row = world.result()
    with pytest.raises(ValueError):
        evaluate(world.s, "finding", row)
    with pytest.raises(TypeError):
        evaluate(world.s, "claim", row)


# ------------------------------------------------------------- one flagged and one clean record per code

Case = tuple[Callable[[World], Any], Callable[[World], Any], tuple[str, ...]]

TABLE_ROW = "% Change from baseline b -2.1 -7.4 -8.3 -11.1"
NO_INTERVAL: dict[str, Any] = dict(ci_low=None, ci_high=None, ci_level=None)
HR_QUOTE = "tirzepatide 15 mg versus placebo: hazard ratio 0.84 (95% CI, 0.75 to 0.94) at week 72; P<0.001"


def _responder(w: World, quote: str) -> OutcomeResult:
    endpoint = w.endpoint("proportion of participants with >=5% body-weight reduction", "WEIGHT_RESPONDER", unit="% of participants")
    return w.result(endpoint=endpoint, value=71.5, unit="% of participants", ci_low=None, ci_high=None, ci_level=None, locator_quote=quote)


def _with_estimand(w: World, label: str, quote: str, *, record_quote: str | None = None) -> OutcomeResult:
    return w.result(estimand=w.estimand(label, quote=record_quote), locator_quote=quote)


def _contrast(w: World, estimate: float, *, treated: float, placebo: float, unit: str = "kg") -> StatisticalAnalysis:
    """A registry analysis with one arm-level value per arm on the same endpoint and source."""
    endpoint = w.endpoint("Change From Baseline in Body Weight", "WEIGHT_ABS_CHANGE", unit=unit)
    plain = dict(endpoint=endpoint, registry=True, unit=unit, ci_low=None, ci_high=None, ci_level=None)
    w.result(arm=w.high_dose, value=treated, **plain)
    w.result(arm=w.placebo, value=placebo, **plain)
    return w.analysis(endpoint=endpoint, registry=True, estimate=estimate, unit=unit, ci_low=None, ci_high=None, ci_level=None, param_type="LS Mean Difference")


def _hazard_ratio(w: World, **fields: Any) -> StatisticalAnalysis:
    values: dict[str, Any] = dict(estimate=0.84, ci_low=0.75, ci_high=0.94, unit=None, param_type="Hazard ratio", locator_quote=HR_QUOTE)
    values.update(fields)
    return w.analysis(endpoint=w.endpoint("time to first MACE-3", "MACE", unit=None), **values)


def _pair_of_results(w: World, first: dict[str, Any], second: dict[str, Any]) -> OutcomeResult:
    row = w.result(**first)
    w.result(**second)
    return row


def _efficacy_result(w: World) -> dict[str, Any]:
    """The same arm, endpoint and week as the default result, from the publication and under the efficacy estimand."""
    quote = "-22.5% with tirzepatide 15 mg at week 72 (n=630), using the efficacy estimand"
    return dict(source=w.paper, value=-22.5, estimand=w.estimand("EFFICACY"), locator_quote=quote, **NO_INTERVAL)


CASES: dict[str, Case] = {
    # code: (record that must carry the flag, record that must not, words the explanation must quote)
    "QUOTE_MISSING": (lambda w: w.result(locator_quote=None), lambda w: w.result(), ("tirzepatide 15 mg",)),
    "NUMBER_NOT_IN_QUOTE": (lambda w: w.result(value=-0.9, ci_low=None, ci_high=None, ci_level=None), lambda w: w.result(), ("-0.9", "inside the number -20.9")),
    "VALUE_ONLY_AS_OTHER_TOKEN": (lambda w: w.result(value=15.0, ci_low=None, ci_high=None, ci_level=None), lambda w: w.result(), ("15", "a dose")),
    "N_NOT_IN_QUOTE": (lambda w: w.result(n_analyzed=99999), lambda w: w.result(), ("n_analyzed = 99999",)),
    "STATISTIC_NOT_IN_QUOTE": (lambda w: w.analysis(p_value_text="0.026"), lambda w: w.analysis(), ("p_value_text = 0.026",)),
    "TIMEPOINT_NOT_IN_QUOTE": (lambda w: w.result(timepoint_weeks=24.0), lambda w: w.result(), ("week 24", "week 72")),
    "QUOTE_MULTIPLE_CANDIDATES": (
        lambda w: w.result(value=-7.4, ci_low=None, ci_high=None, ci_level=None, locator_quote=TABLE_ROW),
        lambda w: w.result(),
        ("4 numbers without a unit", "-2.1, -7.4, -8.3, -11.1", "tirzepatide 15 mg"),
    ),
    "QUOTE_ARM_MISMATCH": (lambda w: w.result(arm=w.placebo), lambda w: w.result(arm=w.placebo, value=-3.1, ci_low=-4.3, ci_high=-1.9, n_analyzed=643), ("'15 mg'", "-20.9", "'placebo'")),
    "ARM_NOT_IN_QUOTE": (
        lambda w: w.result(ci_low=None, ci_high=None, ci_level=None, locator_quote="-20.9% (-23.9 kg; -52.7 lbs) at week 72 (n=630)"),
        lambda w: w.result(),
        ("tirzepatide 15 mg",),
    ),
    "QUOTE_TOKEN_SHARED": (
        lambda w: _pair_of_results(w, {}, dict(arm=w.low_dose)),
        lambda w: _pair_of_results(w, {}, dict(arm=w.placebo, value=-3.1, ci_low=-4.3, ci_high=-1.9, n_analyzed=643)),
        ("20.9", "1 time(s)", "2 records", "tirzepatide 5 mg"),
    ),
    "ARM_DOSE_INCONSISTENT": (
        lambda w: w.ae(arm=w.arm("30 mg/kg Bimagrumab", drug="bimagrumab", dose=30, unit="mg"), registry=True),
        lambda w: w.ae(arm=w.arm("30 mg/kg Bimagrumab", drug="bimagrumab", dose=30, unit="mg/kg"), registry=True),
        ("'30 mg/kg'", "dose_unit = 'mg'"),
    ),
    "ARM_DOSE_SIMPLIFIED": (
        lambda w: w.ae(arm=w.arm("tirzepatide MTD (10 or 15 mg)", dose=15), registry=True),
        lambda w: w.ae(registry=True),
        ("10, 15 mg", "dose_amount = 15"),
    ),
    "ENDPOINT_THRESHOLD_CONTRADICTED": (
        lambda w: _responder(w, "71.5% of patients receiving tirzepatide 15 mg lost ≥15% body weight at week 72 (n=630)"),
        lambda w: _responder(w, "71.5% of patients receiving tirzepatide 15 mg lost ≥5% body weight at week 72 (n=630)"),
        ("'≥15%'", ">=5% body-weight reduction", "5%"),
    ),
    "ENDPOINT_THRESHOLD_NOT_IN_QUOTE": (
        lambda w: _responder(w, "71.5% with tirzepatide 15 mg at week 72 (n=630)"),
        lambda w: _responder(w, "71.5% of patients receiving tirzepatide 15 mg had a body weight reduction of 5% or more at week 72 (n=630)"),
        (">=5% body-weight reduction", "states no threshold"),
    ),
    "ENDPOINT_KEYWORD_CONTRADICTED": (
        lambda w: w.result(endpoint=w.endpoint("change from baseline in HbA1c", "HBA1C_CHANGE", unit="% HbA1c"), unit="% HbA1c"),
        lambda w: w.result(endpoint=w.endpoint("change from baseline in HbA1c", "HBA1C_CHANGE", unit="% HbA1c"), unit="% HbA1c", locator_quote=RESULT_QUOTE.replace("body weight", "HbA1c")),
        ("'body weight'", "'HbA1c'"),
    ),
    "ENDPOINT_BASELINE_MISMATCH": (
        lambda w: w.result(endpoint=w.endpoint("percent change in body weight from randomization"), baseline_definition="study entry (week 0)", registry=True),
        lambda w: w.result(endpoint=w.endpoint("percent change in body weight from randomization"), baseline_definition="week 36 (randomization)", registry=True),
        ("from randomization", "'study entry (week 0)'"),
    ),
    "UNIT_CONTRADICTS_CATEGORY": (
        lambda w: w.result(registry=True, value=54.0, unit="percentage of participants", ci_low=None, ci_high=None, ci_level=None),
        lambda w: w.result(registry=True),
        ("'percentage of participants'", "WEIGHT_PCT_CHANGE"),
    ),
    "UNIT_MISSING": (lambda w: w.result(registry=True, unit=None), lambda w: w.result(registry=True), ("-20.9",)),
    "QUOTE_UNIT_MISMATCH": (
        lambda w: w.result(value=-23.9, ci_low=None, ci_high=None, ci_level=None, locator_quote="-20.9% (-23.9 kg; -52.7 lbs) with tirzepatide 15 mg at week 72 (n=630)"),
        lambda w: w.result(ci_low=None, ci_high=None, ci_level=None, locator_quote="-20.9% (-23.9 kg; -52.7 lbs) with tirzepatide 15 mg at week 72 (n=630)"),
        ("-23.9 kg", "'%'"),
    ),
    "UNIT_NOT_IN_QUOTE": (
        lambda w: _responder(w, "tirzepatide 15 mg, week 72, n=630, ≥5% responders: 71.5"),
        lambda w: _responder(w, "tirzepatide 15 mg, week 72, n=630, ≥5% responders: 71.5%"),
        ("71.5", "'% of participants'"),
    ),
    "VALUE_OUT_OF_RANGE": (
        lambda w: w.ae(registry=True, percent=131.0),
        lambda w: w.ae(registry=True, percent=31.0),
        ("percent = 131",),
    ),
    "ESTIMAND_CONTRADICTED_BY_QUOTE": (
        lambda w: _with_estimand(w, "TREATMENT_REGIMEN", RESULT_QUOTE + ", using the efficacy estimand"),
        lambda w: _with_estimand(w, "EFFICACY", RESULT_QUOTE + ", using the efficacy estimand"),
        ("'efficacy estimand'", "TREATMENT_REGIMEN"),
    ),
    "ESTIMAND_NOT_SUPPORTED": (
        lambda w: _with_estimand(w, "TREATMENT_REGIMEN", RESULT_QUOTE),
        lambda w: _with_estimand(w, "TREATMENT_REGIMEN", RESULT_QUOTE, record_quote="analysed according to the treatment-regimen estimand"),
        ("treatment-regimen estimand", "the estimand record has no quote"),
    ),
    "ESTIMAND_DIFFERENCE_UNEXPLAINED": (
        lambda w: _pair_of_results(w, dict(registry=True), _efficacy_result(w)),
        lambda w: _pair_of_results(w, dict(registry=True, estimand=w.estimand("TREATMENT_REGIMEN", basis="INFERRED_FROM_ANALYSIS_DESCRIPTION")), _efficacy_result(w)),
        ("-20.9 (no named estimand)", "-22.5 (EFFICACY, source pubmed:1)"),
    ),
    "BASELINE_DEFINITION_CONFLICT": (
        lambda w: _pair_of_results(w, dict(registry=True, baseline_definition="week 36 (randomization)"), dict(registry=True, value=-26.0, ci_low=None, ci_high=None, ci_level=None, baseline_definition="study entry (week 0)")),
        lambda w: _pair_of_results(w, dict(registry=True, baseline_definition="baseline (randomization, week 0)"), dict(registry=True, source=w.paper, baseline_definition="baseline (week 0)")),
        ("'week 36 (randomization)'", "'study entry (week 0)'", "-26"),
    ),
    "SIGN_CONTRADICTED_BY_QUOTE": (lambda w: w.result(value=20.9, ci_low=19.9, ci_high=21.8), lambda w: w.result(), ("-20.9", "value = 20.9")),
    "SIGN_NOT_IN_QUOTE": (
        lambda w: w.result(value=-6.7, ci_low=None, ci_high=None, ci_level=None, locator_quote="14.6 lbs (6.7%; 15 mg) at week 72 in 630 participants"),
        lambda w: w.result(value=-6.7, ci_low=None, ci_high=None, ci_level=None, locator_quote="participants lost 14.6 lbs (6.7%; 15 mg) at week 72 (n=630)"),
        ("value = -6.7", "prints 6.7 without a minus sign"),
    ),
    "INTERVAL_LIMITS_REVERSED": (lambda w: w.result(registry=True, ci_low=-19.9, ci_high=-21.8), lambda w: w.result(registry=True), ("ci_low = -19.9", "ci_high = -21.8")),
    "ESTIMATE_OUTSIDE_INTERVAL": (lambda w: w.analysis(registry=True, estimate=-12.0), lambda w: w.analysis(registry=True), ("estimate = -12", "-19.3 to -16.3")),
    "INTERVAL_ZERO_WIDTH": (lambda w: w.result(registry=True, ci_low=-20.9, ci_high=-20.9), lambda w: w.result(registry=True), ("both -20.9",)),
    "INTERVAL_WITHOUT_LEVEL": (lambda w: w.result(registry=True, ci_level=None), lambda w: w.result(registry=True), ("-21.8 and -19.9",)),
    "INTERVAL_SINGLE_LIMIT": (lambda w: w.analysis(registry=True, ci_high=None), lambda w: w.analysis(registry=True), ("ci_low = -19.3",)),
    "CI_LEVEL_IMPLAUSIBLE": (lambda w: w.analysis(registry=True, ci_level=-3.99), lambda w: w.analysis(registry=True, ci_level=97.5), ("ci_level = -3.99",)),
    "CI_NOT_NEXT_TO_VALUE": (
        lambda w: w.result(arm=w.placebo, value=-3.1, n_analyzed=643),  # the placebo value with the 15 mg interval
        lambda w: w.result(arm=w.placebo, value=-3.1, ci_low=-4.3, ci_high=-1.9, n_analyzed=643),
        ("-3.1 is followed by -4.3, -1.9", "-21.8 and -19.9", "after -20.9"),
    ),
    "ANALYSIS_DIRECTION_VS_ARMS": (
        lambda w: _contrast(w, 14.5, treated=-17.79, placebo=-3.31),
        lambda w: _contrast(w, -14.5, treated=-17.79, placebo=-3.31),
        ("14.5", "-17.79", "-3.31", "= -14.48", "opposite sign"),
    ),
    "ANALYSIS_SCALE_VS_ARMS": (
        lambda w: _contrast(w, 0.35, treated=53.0, placebo=18.0, unit="percentage of participants"),
        lambda w: _contrast(w, 35.0, treated=53.0, placebo=18.0, unit="percentage of participants"),
        ("0.35", "53", "18", "= 35", "only after multiplying it by 100 (35)"),
    ),
    "RATIO_NOT_POSITIVE": (
        lambda w: _hazard_ratio(w, registry=True, estimate=-0.84, ci_low=-0.94, ci_high=-0.75, locator_quote=None),
        lambda w: _hazard_ratio(w, registry=True, locator_quote=None),
        ("estimate = -0.84", "Hazard ratio"),
    ),
    "ANALYSIS_SCALE_AMBIGUOUS": (
        lambda w: _hazard_ratio(w, registry=True, locator_quote=None, unit="percentage of participants", param_type="Odds Ratio (OR)"),
        lambda w: _hazard_ratio(w, registry=True, locator_quote=None),
        ("'Odds Ratio (OR)'", "'percentage of participants'"),
    ),
    "ANALYSIS_REFERENCE_NOT_STATED": (
        lambda w: w.analysis(registry=True, comment="Registry does not state which group is the reference; groups listed in registry order."),
        lambda w: w.analysis(registry=True, comment="Mixed model for repeated measures."),
        ("'tirzepatide 15 mg' vs 'placebo'", "-17.8"),
    ),
    "HYPOTHESIS_FIELDS_INCONSISTENT": (
        lambda w: _hazard_ratio(w, registry=True, locator_quote=None, estimate=0.92, ci_low=0.83, ci_high=1.01, superiority_met="YES"),
        lambda w: _hazard_ratio(w, registry=True, locator_quote=None, superiority_met="YES"),
        ("superiority_met is YES", "0.83 to 1.01 contains 1"),
    ),
    "NI_MARGIN_MISSING": (
        lambda w: _hazard_ratio(w, registry=True, locator_quote=None, hypothesis_type="NON_INFERIORITY"),
        lambda w: _hazard_ratio(w, registry=True, locator_quote=None, hypothesis_type="NON_INFERIORITY", ni_margin=1.8, ni_met="YES"),
        ("NON_INFERIORITY", "0.84"),
    ),
    "AFFECTED_EXCEEDS_AT_RISK": (lambda w: w.ae(registry=True, n_affected=700), lambda w: w.ae(registry=True), ("n_affected = 700", "n_at_risk = 630")),
    "PERCENT_DISAGREES_WITH_COUNTS": (lambda w: w.ae(registry=True, percent=13.0), lambda w: w.ae(registry=True, percent=31.0), ("195 / 630 = 30.95%", "percent = 13")),
    "PERCENT_WITHOUT_DENOMINATOR": (lambda w: w.ae(registry=True, percent=31.0, n_at_risk=None, n_affected=None), lambda w: w.ae(registry=True, percent=31.0), ("31",)),
    "DENOMINATOR_ZERO": (lambda w: w.ae(registry=True, n_affected=0, n_at_risk=0), lambda w: w.ae(registry=True), ("n_at_risk = 0", "'nausea'")),
    "RESULT_WITHOUT_N": (lambda w: w.result(registry=True, n_analyzed=None), lambda w: w.result(registry=True), ("-20.9",)),
    "AE_KIND_CONTRADICTED": (
        lambda w: w.ae(term="discontinuation of study treatment for any reason", measure_kind="TREATMENT_DISCONTINUATION_DUE_TO_AE", registry=True),
        lambda w: w.ae(term="discontinuation of study treatment for any reason", measure_kind="DISCONTINUATION_ANY_REASON", registry=True),
        ("TREATMENT_DISCONTINUATION_DUE_TO_AE", "'any reason'"),
    ),
    "AE_TERM_NOT_IN_QUOTE": (lambda w: w.ae(term="any gastrointestinal adverse event"), lambda w: w.ae(), ("'any gastrointestinal adverse event'", "gastrointestinal")),
    "CLAIM_NO_NUMBER_IN_QUOTE": (
        lambda w: w.claim("TRIAL-1 on-treatment analysis with tirzepatide: MACE-3 HR 0.92 (95% CI 0.51 to 1.65).", "TRIAL-1 on-treatment analysis (not pre-specified) excludes events after discontinuing tirzepatide"),
        lambda w: w.claim("TRIAL-1 on-treatment analysis with tirzepatide: MACE-3 HR 0.92 (95% CI 0.51 to 1.65).", "In TRIAL-1 the on-treatment hazard ratio with tirzepatide was 0.92 (95% CI: 0.51 to 1.65)"),
        ("0.92, 0.51, 1.65",),
    ),
    "CLAIM_NUMBER_NOT_IN_QUOTE": (
        lambda w: w.claim("In TRIAL-1 (n=1273), tirzepatide 15 mg reduced body weight by 20.9% at week 72."),
        lambda w: w.claim(),
        ("1273", "20.9%"),
    ),
    "CLAIM_ENTITY_NOT_IN_QUOTE": (
        lambda w: w.claim(quote="participants receiving the 15 mg dose lost 20.9% of body weight at week 72"),
        lambda w: w.claim(quote="In TRIAL-1 (NCT00000001), participants receiving Zepbound 15 mg lost 20.9% of body weight at week 72"),
        ("'tirzepatide'", "'TRIAL-1'"),
    ),
    "DUPLICATE_KEY_DIFFERENT_VALUES": (
        lambda w: _pair_of_results(w, dict(registry=True), dict(registry=True, value=-18.4, ci_low=None, ci_high=None, ci_level=None)),
        lambda w: _pair_of_results(w, dict(registry=True), dict(registry=True, arm=w.placebo, value=-3.1, ci_low=None, ci_high=None, ci_level=None)),
        ("value -20.9", "value -18.4"),
    ),
    "DUPLICATE_ROW": (
        lambda w: _pair_of_results(w, dict(registry=True), dict(registry=True)),
        lambda w: _pair_of_results(w, dict(registry=True), dict(registry=True, timepoint_weeks=88.0)),
        ("-20.9", "1 other record(s)"),
    ),
}


def test_every_code_has_a_flagged_and_a_clean_case():
    assert set(CASES) | {"CHANGED_SINCE_LAST_INGESTION"} == set(CHECKS)


@pytest.mark.parametrize("code", sorted(CASES))
def test_check_flags_the_bad_record(world, code):
    make_flagged, _, quoted = CASES[code]
    row = make_flagged(world)
    flags = flags_of(world, row)
    assert code in codes(flags), f"{code} not raised; got {[(f.code, f.explanation) for f in flags]}"
    flag = next(f for f in flags if f.code == code)
    assert flag.severity == CHECKS[code]["severity"]
    assert etype_of(row) in CHECKS[code]["applies_to"]
    assert flag.fields, "a flag names the fields it is about"
    for words in quoted:
        assert words in flag.explanation, f"{words!r} not in: {flag.explanation}"
    # the whole-database pass says the same about this record
    assert evaluate_all(world.s)[(etype_of(row), row.id)] == flags


@pytest.mark.parametrize("code", sorted(CASES))
def test_check_stays_silent_on_the_good_record(world, code):
    _, make_clean, _ = CASES[code]
    row = make_clean(world)
    flags = flags_of(world, row)
    assert code not in codes(flags), explanation(flags, code)
    assert evaluate_all(world.s)[(etype_of(row), row.id)] == flags


# ------------------------------------------------------------------------- further cases per check


def test_value_matching_only_the_confidence_level_is_flagged(world):
    flags = flags_of(world, world.result(value=95.0, ci_low=None, ci_high=None, ci_level=None))
    assert "a confidence level" in explanation(flags, "VALUE_ONLY_AS_OTHER_TOKEN")


def test_table_row_without_arm_names_cannot_identify_an_arm(world):
    """CUR-03: eight unlabelled numbers across two trials."""
    quote = "% of patients who lost ≥5% body weight 26.8 59.6 63.1 71.5 26.8 47.4 54.7 67"
    flags = flags_of(world, _responder(world, quote))
    assert "8 numbers without a unit" in explanation(flags, "QUOTE_MULTIPLE_CANDIDATES")
    assert risk_level(flags) == "HIGH"


def test_respectively_binds_values_to_arms_in_order(world):
    quote = "Adverse events caused treatment discontinuation in 4.3%, 7.1%, and 2.6% of participants receiving 5-mg and 15-mg tirzepatide doses and placebo, respectively, over 72 weeks."
    term = dict(term="treatment discontinuation due to adverse events", measure_kind="TREATMENT_DISCONTINUATION_DUE_TO_AE", n_affected=None, n_at_risk=None, locator_quote=quote)
    right = world.ae(arm=world.high_dose, percent=7.1, **term)
    wrong = world.ae(arm=world.low_dose, percent=2.6, **term)
    assert not {"QUOTE_MULTIPLE_CANDIDATES", "QUOTE_ARM_MISMATCH", "ARM_NOT_IN_QUOTE"} & codes(flags_of(world, right))
    text = explanation(flags_of(world, wrong), "QUOTE_ARM_MISMATCH")
    assert "2.6 is value 3" in text and "'placebo'" in text and "tirzepatide 5 mg" in text


def test_respectively_without_an_arm_list_is_unresolved(world):
    quote = "(differences, -19.8 and -17.8 percentage points, respectively; P<0.001 for both comparisons) at week 72"
    flags = flags_of(world, world.analysis(ci_low=None, ci_high=None, ci_level=None, locator_quote=quote))
    assert "says 'respectively' without listing the arms" in explanation(flags, "QUOTE_MULTIPLE_CANDIDATES")


def test_a_list_of_values_next_to_one_arm_name_is_unresolved_but_the_named_arm_is_not(world):
    quote = "nausea (28.9%, 31.0% and 33.7%) vs. 10.4% with placebo over 72 weeks"
    plain = dict(n_affected=None, n_at_risk=None, locator_quote=quote)
    listed = flags_of(world, world.ae(arm=world.high_dose, percent=31.0, **plain))
    assert "4 percentages (28.9, 31.0, 33.7, 10.4)" in explanation(listed, "QUOTE_MULTIPLE_CANDIDATES")
    named = flags_of(world, world.ae(arm=world.placebo, percent=10.4, **plain))
    assert not {"QUOTE_MULTIPLE_CANDIDATES", "QUOTE_ARM_MISMATCH", "ARM_NOT_IN_QUOTE"} & codes(named)


def test_the_one_unlabelled_value_is_attributed_by_elimination_at_medium(world):
    """Every other value has its dose next to it; the placebo value is the one left, but 'placebo' is not in the quote."""
    quote = "The mean percent change in body weight at week 72 was -15.0% (5-mg dose) and -20.9% (15-mg dose) versus -3.1%"
    flags = flags_of(world, world.result(arm=world.placebo, value=-3.1, ci_low=None, ci_high=None, ci_level=None, n_analyzed=None, locator_quote=quote))
    assert "QUOTE_MULTIPLE_CANDIDATES" not in codes(flags)
    assert "'placebo'" in explanation(flags, "ARM_NOT_IN_QUOTE")
    assert risk_level(flags) == "MEDIUM"


def test_row_label_with_two_columns_of_the_same_unit_does_not_identify_the_endpoint(world):
    quote = "TZP 15 mg | -14.8 % | -34.4 lbs | -2.4 %"
    weight = world.result(value=-14.8, ci_low=None, ci_high=None, ci_level=None, locator_quote=quote)
    text = explanation(flags_of(world, weight), "QUOTE_MULTIPLE_CANDIDATES")
    assert "names only this record's own arm ('15 mg')" in text and "2 percentages (-14.8, -2.4)" in text


def test_analysis_estimate_after_its_parameter_name_is_not_ambiguous(world):
    quote = "from week 36 to week 72 was -5.5% with tirzepatide 15 mg vs 14.0% with placebo (difference, -17.8% [95% CI, -19.3% to -16.3%]; P<0.001)"
    flags = flags_of(world, world.analysis(locator_quote=quote))
    assert not {"QUOTE_MULTIPLE_CANDIDATES", "QUOTE_ARM_MISMATCH", "CI_NOT_NEXT_TO_VALUE"} & codes(flags)


def test_dose_named_next_to_the_only_value_must_be_the_arms_dose(world):
    flags = flags_of(world, world.result(arm=world.low_dose, ci_low=None, ci_high=None, ci_level=None, locator_quote="-20.9% with 15-mg doses at week 72 (n=630)"))
    text = explanation(flags, "QUOTE_ARM_MISMATCH")
    assert "'15-mg'" in text and "tirzepatide 5 mg" in text and "(5 mg)" in text


def test_maximum_tolerated_dose_arm_is_named_by_its_drug(world):
    """'MTD (10 or 15 mg)' describes a regimen: the drug name identifies the arm, no single dose does."""
    arm = world.arm("tirzepatide MTD (10 or 15 mg)", dose=15)
    quote = "-20.9% (95% CI, -21.8 to -19.9) with tirzepatide and -13.7% (95% CI, -14.9 to -12.6) with semaglutide at week 72 (n=630)"
    flags = flags_of(world, world.result(arm=arm, locator_quote=quote))
    assert not {"ARM_NOT_IN_QUOTE", "QUOTE_MULTIPLE_CANDIDATES", "QUOTE_ARM_MISMATCH"} & codes(flags)


def test_brand_name_counts_as_naming_the_drug(world):
    arm = world.arm("tirzepatide MTD", dose=None)
    quote = "6.1% of participants taking Zepbound discontinued treatment due to adverse events over 72 weeks"
    flags = flags_of(world, world.ae(arm=arm, percent=6.1, n_affected=None, n_at_risk=None, term="treatment discontinuation due to adverse events", measure_kind="TREATMENT_DISCONTINUATION_DUE_TO_AE", locator_quote=quote))
    assert "ARM_NOT_IN_QUOTE" not in codes(flags) and "AE_TERM_NOT_IN_QUOTE" not in codes(flags) and "AE_KIND_CONTRADICTED" not in codes(flags)


def test_quote_token_shared_is_silent_when_the_number_is_printed_once_per_record(world):
    quote = "discontinuation 10.6% (5 mg) and 10.6% (15 mg) over 72 weeks"
    plain = dict(percent=10.6, n_affected=None, n_at_risk=None, term="discontinuation due to adverse events", measure_kind="TREATMENT_DISCONTINUATION_DUE_TO_AE", locator_quote=quote)
    first, second = world.ae(arm=world.low_dose, **plain), world.ae(arm=world.high_dose, **plain)
    assert "QUOTE_TOKEN_SHARED" not in codes(flags_of(world, first)) | codes(flags_of(world, second))


def test_dose_label_that_disagrees_with_the_stored_amount(world):
    flags = flags_of(world, world.ae(arm=world.arm("tirzepatide 10 mg", dose=15), registry=True))
    assert "names the dose(s) 10 mg" in explanation(flags, "ARM_DOSE_INCONSISTENT")


def test_percent_stored_under_an_absolute_weight_category(world):
    """CUR-08: a '% reduction' filed where kilograms belong."""
    endpoint = world.endpoint("average body weight loss during open-label lead-in", "WEIGHT_ABS_CHANGE", unit="% reduction")
    flags = flags_of(world, world.result(endpoint=endpoint, registry=True, value=20.9, unit="% reduction", ci_low=None, ci_high=None, ci_level=None))
    assert "'% reduction' is a percentage" in explanation(flags, "UNIT_CONTRADICTS_CATEGORY")
    assert "SIGN_CONTRADICTED_BY_QUOTE" not in codes(flags)


def test_range_checks_cover_shares_counts_and_p_values(world):
    share = world.result(registry=True, endpoint=world.endpoint("Percentage of Participants Achieving ≥5% Weight Reduction", "WEIGHT_RESPONDER", unit="percentage of participants"), value=104.0, unit="percentage of participants", ci_low=None, ci_high=None, ci_level=None)
    assert "cannot be below 0 or above 100" in explanation(flags_of(world, share), "VALUE_OUT_OF_RANGE")
    count = world.ae(registry=True, n_affected=-3)
    assert "n_affected = -3" in explanation(flags_of(world, count), "VALUE_OUT_OF_RANGE")
    p_value = world.analysis(registry=True, p_value_text="1.7")
    assert "'1.7'" in explanation(flags_of(world, p_value), "VALUE_OUT_OF_RANGE")
    two_sided = world.analysis(registry=True, p_value_text="0.03 (2-sided)", timepoint_weeks=88.0)
    assert "VALUE_OUT_OF_RANGE" not in codes(flags_of(world, two_sided))
    change = world.result(registry=True, endpoint=world.endpoint("Change in apnea-hypopnea index", "OTHER", unit="events per hour"), value=-25.3, unit="events per hour", ci_low=None, ci_high=None, ci_level=None)
    assert "VALUE_OUT_OF_RANGE" not in codes(flags_of(world, change))


def test_estimand_supported_by_the_estimand_record_only_if_it_cites_the_same_source(world):
    estimand = world.estimand("TREATMENT_REGIMEN", quote="analysed according to the treatment-regimen estimand", source=world.paper)
    flags = flags_of(world, world.result(estimand=estimand))
    text = explanation(flags, "ESTIMAND_NOT_SUPPORTED")
    assert "another source (pubmed:1)" in text and "web:trial-1-release" in text


def test_loss_wording_with_a_positive_stored_change_is_a_contradiction(world):
    quote = "participants taking tirzepatide 15 mg lost an average of 64.4 lbs (25.9%) at week 72 (n=630)"
    positive = world.result(value=25.9, ci_low=None, ci_high=None, ci_level=None, locator_quote=quote)
    assert "'lost'" in explanation(flags_of(world, positive), "SIGN_CONTRADICTED_BY_QUOTE")
    negative = world.result(value=-25.9, ci_low=None, ci_high=None, ci_level=None, locator_quote=quote, timepoint_weeks=72.0, n_analyzed=630, source=world.paper)
    assert not {"SIGN_CONTRADICTED_BY_QUOTE", "SIGN_NOT_IN_QUOTE"} & codes(flags_of(world, negative))


def test_positive_regain_next_to_a_signed_loss_is_not_a_contradiction(world):
    """SURMOUNT-4 pattern: the quote prints signs, so an unsigned number is a genuine increase."""
    quote = "the mean change in body weight from week 36 to week 72 was -5.5% with tirzepatide 15 mg vs 14.0% with placebo (n=643), a smaller weight loss"
    flags = flags_of(world, world.result(arm=world.placebo, value=14.0, ci_low=None, ci_high=None, ci_level=None, n_analyzed=643, locator_quote=quote))
    assert "SIGN_CONTRADICTED_BY_QUOTE" not in codes(flags)


def test_estimate_on_the_edge_of_its_interval_within_rounding_is_not_flagged(world):
    flags = flags_of(world, world.analysis(registry=True, estimate=-19.0, ci_low=-18.6, ci_high=-14.1))
    assert "ESTIMATE_OUTSIDE_INTERVAL" not in codes(flags)


def test_scale_and_direction_are_both_reported_for_a_reversed_risk_difference(world):
    row = _contrast(world, -0.21, treated=52.0, placebo=32.0, unit="percentage of participants")
    flags = flags_of(world, row)
    assert {"ANALYSIS_SCALE_VS_ARMS", "ANALYSIS_DIRECTION_VS_ARMS"} <= codes(flags)
    assert "multiplying it by 100 (-21) and reversing its sign" in explanation(flags, "ANALYSIS_SCALE_VS_ARMS")


def test_model_based_difference_that_differs_from_raw_arm_values_is_not_flagged(world):
    """An adjusted estimate need not equal the difference of the arm values; only a mirror image or a 100-fold scale is reported."""
    flags = flags_of(world, _contrast(world, -17.6, treated=-29.6, placebo=-14.5))
    assert not {"ANALYSIS_DIRECTION_VS_ARMS", "ANALYSIS_SCALE_VS_ARMS"} & codes(flags)


def test_direction_needs_one_value_per_arm_and_ignores_differences_within_rounding(world):
    within_rounding = _contrast(world, 0.0, treated=60.0, placebo=59.0, unit="percentage of participants")
    assert not {"ANALYSIS_DIRECTION_VS_ARMS", "ANALYSIS_SCALE_VS_ARMS"} & codes(flags_of(world, within_rounding))


def test_hypothesis_fields_contradictions(world):
    ni_without_ni_test = _hazard_ratio(world, registry=True, locator_quote=None, ni_met="YES", hypothesis_type="SUPERIORITY")
    assert "ni_met is YES but hypothesis_type is SUPERIORITY" in explanation(flags_of(world, ni_without_ni_test), "HYPOTHESIS_FIELDS_INCONSISTENT")
    margin_crossed = _hazard_ratio(world, registry=True, locator_quote=None, hypothesis_type="NON_INFERIORITY", ni_margin=1.8, ni_met="YES", ci_high=1.9, timepoint_weeks=88.0)
    assert "ci_high = 1.9 is not below the margin 1.8" in explanation(flags_of(world, margin_crossed), "HYPOTHESIS_FIELDS_INCONSISTENT")


def test_adverse_event_kind_contradicted_by_term_and_quote(world):
    """CUR-05: an incidence stored as a discontinuation."""
    quote = "Dysesthesia occurred in 8.8% and 20.9% (5 mg and 15 mg, respectively) of patients treated with tirzepatide over 72 weeks"
    row = world.ae(term="dysesthesia", category="DISCONTINUATION", measure_kind="TREATMENT_DISCONTINUATION_DUE_TO_AE", percent=20.9, n_affected=None, n_at_risk=None, locator_quote=quote)
    text = explanation(flags_of(world, row), "AE_KIND_CONTRADICTED")
    assert "'dysesthesia'" in text and "Dysesthesia occurred in" in text
    supported = world.ae(term="dysesthesia", category="DISCONTINUATION", measure_kind="TREATMENT_DISCONTINUATION_DUE_TO_AE", percent=2.1, n_affected=None, n_at_risk=None, locator_quote="Dysesthesia led to treatment discontinuation in 2.1% of patients on tirzepatide 15 mg over 72 weeks")
    assert "AE_KIND_CONTRADICTED" not in codes(flags_of(world, supported))
    registry_incidence = world.ae(registry=True, term="Drug withdrawal syndrome")
    assert "AE_KIND_CONTRADICTED" not in codes(flags_of(world, registry_incidence))
    deaths = world.ae(registry=True, term="nausea", measure_kind="DEATHS", period="Week 0 to 72")
    assert "does not mention death" in explanation(flags_of(world, deaths), "AE_KIND_CONTRADICTED")


def test_adverse_event_term_is_found_in_inflected_form(world):
    quote = "6.1% of participants taking tirzepatide 15 mg discontinued treatment due to adverse events over 72 weeks"
    row = world.ae(term="treatment discontinuation due to adverse events", measure_kind="TREATMENT_DISCONTINUATION_DUE_TO_AE", percent=6.1, n_affected=None, n_at_risk=None, locator_quote=quote)
    assert "AE_TERM_NOT_IN_QUOTE" not in codes(flags_of(world, row))


def test_claim_numbers_ignore_identifiers_citations_dates_and_names(world):
    statement = (
        "TRIAL-1 primary publication: N Engl J Med 2022;387:205-216; PMID 35658024; DOI 10.1056/NEJMoa2206038; published online 2022-06-04; "
        "phase 3 trial in type 2 diabetes randomized 1:1:1; label section 14.2; presented at the 82nd Scientific Sessions in June 2022."
    )
    flags = flags_of(world, world.claim(statement, "TRIAL-1 was a phase 3 trial of tirzepatide"))
    assert not {"CLAIM_NUMBER_NOT_IN_QUOTE", "CLAIM_NO_NUMBER_IN_QUOTE"} & codes(flags)


def test_claim_without_a_quote_is_only_reported_as_missing_its_quote(world):
    flags = flags_of(world, world.claim(quote=None))
    assert codes(flags) == {"QUOTE_MISSING"} and risk_level(flags) == "HIGH"


def test_duplicate_claims(world):
    first, second = world.claim(), world.claim(key="claim-2")
    assert "DUPLICATE_ROW" in codes(flags_of(world, first)) and "DUPLICATE_ROW" in codes(flags_of(world, second))


def test_indistinguishable_registry_analyses_are_high(world):
    """ING-02: six lipid-class analyses stored with nothing to tell them apart."""
    rows = [world.analysis(registry=True, estimate=e, ci_low=None, ci_high=None, ci_level=None) for e in (-5.54, -6.57, 3.2)]
    flags = flags_of(world, rows[0])
    text = explanation(flags, "DUPLICATE_KEY_DIFFERENT_VALUES")
    assert "2 other record(s)" in text and "estimate -6.57" in text and "estimate 3.2" in text
    assert risk_level(flags) == "HIGH"


def test_quote_based_checks_never_apply_to_registry_records(world):
    """A registry record may carry text in locator_quote; it is still not read."""
    rows = [
        world.result(registry=True, locator_quote=TABLE_ROW, value=-99.0, ci_low=None, ci_high=None, ci_level=None),
        world.analysis(registry=True, locator_quote="hazard ratio 0.5"),
        world.ae(registry=True, locator_quote="vomiting 12%"),
    ]
    for row in rows:
        assert not QUOTE_BASED_CODES & codes(flags_of(world, row))


def test_unsaved_record_is_checked_from_the_objects_it_holds(world):
    arm, endpoint = world.placebo, world.endpoint()
    row = OutcomeResult(
        study_id=world.study.id, arm=arm, endpoint=endpoint, source_id=world.release.id, origin="CURATED", arm_label="placebo", scope="ARM",
        value=-20.9, unit="%", timepoint_weeks=72.0, n_analyzed=643, locator_quote=RESULT_QUOTE, tier="PRESS_RELEASE", verification_status="QUOTE_ANCHORED",
    )  # fmt: skip
    assert row.id is None
    flags = evaluate(world.s, "result", row)
    assert "QUOTE_ARM_MISMATCH" in codes(flags)
    assert world.s.scalar(select(OutcomeResult.id).limit(1)) is None, "evaluate must not save the record"


def test_flags_are_ordered_high_first_and_do_not_repeat(world):
    flags = flags_of(world, world.result(arm=world.placebo, timepoint_weeks=24.0, n_analyzed=None))
    severities = [f.severity for f in flags]
    assert severities == sorted(severities, key=["HIGH", "MEDIUM", "LOW"].index)
    assert {"HIGH", "MEDIUM", "LOW"} == set(severities)
    assert len(flags) == len(set(flags))


# ------------------------------------------------------------------- changed since the last ingestion


def test_changed_fields_are_listed_with_old_and_new_values(world):
    row = world.result()
    previous = {"value": -20.9, "unit": "%", "n_analyzed": 630, "locator_quote": RESULT_QUOTE, "timepoint_weeks": 72.0, "ci_low": -21.8}
    assert flags_of(world, row, previous_fields=previous) == []
    row.value, row.n_analyzed, row.unit = -21.9, None, "kg"
    flags = flags_of(world, row, previous_fields=previous)
    change = next(f for f in flags if f.code == "CHANGED_SINCE_LAST_INGESTION")
    assert change.severity == "MEDIUM" and change.fields == ("n_analyzed", "unit", "value")
    assert "3 field(s)" in change.explanation
    assert "value: -20.9 → -21.9" in change.explanation and "n_analyzed: 630 → empty" in change.explanation and "unit: '%' → 'kg'" in change.explanation


def test_change_comparison_ignores_float_noise_and_unknown_keys(world):
    row = world.claim()
    previous = {"statement": row.statement, "quote": row.quote, "not_a_column": 1}
    assert "CHANGED_SINCE_LAST_INGESTION" not in codes(flags_of(world, row, previous_fields=previous))
    result = world.result()
    assert "CHANGED_SINCE_LAST_INGESTION" not in codes(flags_of(world, result, previous_fields={"value": -20.900000000000002, "ci_level": 95}))
    row.quote = "In TRIAL-1, participants receiving tirzepatide 15 mg lost 20.9% of body weight at week 72 (revised)"
    assert "quote: " in explanation(flags_of(world, row, previous_fields=previous), "CHANGED_SINCE_LAST_INGESTION")


def test_evaluate_all_never_reports_changes(world):
    world.result()
    assert all(f.code != "CHANGED_SINCE_LAST_INGESTION" for flags in evaluate_all(world.s).values() for f in flags)


# ------------------------------------------------------- the audit's mis-attributions (CUR-01 harness)

AUDIT_QUOTE = "-20.9% (95% CI, -21.8 to -19.9) with 15-mg doses and -3.1% (95% CI, -4.3 to -1.9) with placebo"

MISATTRIBUTIONS: dict[str, tuple[Callable[[World], Any], str, str]] = {
    # every one of these loaded as QUOTE_ANCHORED in the audit's harness
    "arm swap: the 15 mg value stored on placebo": (lambda w: w.result(arm=w.placebo, locator_quote=AUDIT_QUOTE, **NO_INTERVAL), "QUOTE_ARM_MISMATCH", "HIGH"),
    "placebo value with the 15 mg interval": (lambda w: w.result(arm=w.placebo, value=-3.1, locator_quote=AUDIT_QUOTE), "CI_NOT_NEXT_TO_VALUE", "HIGH"),
    "interval limits reversed and mixed across arms": (lambda w: w.result(ci_low=-1.9, ci_high=-21.8, locator_quote=AUDIT_QUOTE), "INTERVAL_LIMITS_REVERSED", "HIGH"),
    "sign flipped": (lambda w: w.result(value=20.9, locator_quote=AUDIT_QUOTE, **NO_INTERVAL), "SIGN_CONTRADICTED_BY_QUOTE", "HIGH"),
    "value taken from the dose token": (lambda w: w.result(value=15.0, locator_quote=AUDIT_QUOTE, **NO_INTERVAL), "VALUE_ONLY_AS_OTHER_TOKEN", "HIGH"),
    "value taken from the confidence level": (lambda w: w.result(value=95.0, locator_quote=AUDIT_QUOTE, **NO_INTERVAL), "VALUE_ONLY_AS_OTHER_TOKEN", "HIGH"),
    "an arm that the quote does not contain": (lambda w: w.result(arm=w.arm("semaglutide 2.4 mg", drug="semaglutide", dose=2.4), locator_quote=AUDIT_QUOTE, **NO_INTERVAL), "QUOTE_ARM_MISMATCH", "HIGH"),
    "a hazard ratio read off a negative percent change": (lambda w: _hazard_ratio(w, treatment=w.placebo, comparator=w.high_dose, estimate=3.1, ci_low=1.9, ci_high=4.3, locator_quote=AUDIT_QUOTE), "SIGN_CONTRADICTED_BY_QUOTE", "HIGH"),
    "an adverse-event percentage read off a weight change": (lambda w: w.ae(term="death", category="DEATH", measure_kind="DEATHS", percent=20.9, n_affected=5000, n_at_risk=6000, locator_quote=AUDIT_QUOTE), "SIGN_CONTRADICTED_BY_QUOTE", "HIGH"),
    # these three cannot be settled by the quote; they are reported as resting on context outside it
    "wrong estimand and week": (lambda w: w.result(estimand=w.estimand("EFFICACY"), timepoint_weeks=24.0, locator_quote=AUDIT_QUOTE), "ESTIMAND_NOT_SUPPORTED", "MEDIUM"),
    "wrong week": (lambda w: w.result(timepoint_weeks=24.0, locator_quote=AUDIT_QUOTE), "TIMEPOINT_NOT_IN_QUOTE", "MEDIUM"),
    "invented n and standard error": (lambda w: w.result(n_analyzed=99999, dispersion_type="Standard Error", dispersion_value=0.37, locator_quote=AUDIT_QUOTE), "N_NOT_IN_QUOTE", "MEDIUM"),
}


@pytest.mark.parametrize("case", sorted(MISATTRIBUTIONS))
def test_audit_misattributions_are_flagged(world, case):
    make, code, level = MISATTRIBUTIONS[case]
    flags = flags_of(world, make(world))
    assert code in codes(flags), [(f.code, f.explanation) for f in flags]
    assert risk_level(flags) == level or (level == "MEDIUM" and risk_level(flags) == "HIGH" and code in codes(flags))
    if level == "HIGH":
        assert CHECKS[code]["severity"] == "HIGH"


# ---------------------------------------------------------------------------------- reading quotes


def test_numbers_in_a_quote_are_read_with_their_role():
    quote = checks._parse("57 (4·2%) of 1358 participants; HR 0·84, 95% CI 0·59–1·20; p<0·0001; −20.9% with 15-mg doses at week 72 (≥5% in MACE-4)")
    read = {t.text: (t.role, t.unit) for t in quote.toks}
    assert read["57"] == ("COUNT", "BARE") and read["4.2"] == ("VALUE", "PCT") and read["1358"] == ("N", "BARE")
    assert read["95"][0] == "CI_LEVEL" and read["0.59"][0] == "CI_BOUND" and read["1.20"][0] == "CI_BOUND"
    assert read["0.0001"][0] == "P" and read["-20.9"] == ("VALUE", "PCT") and read["15"][0] == "DOSE"
    assert read["72"][0] == "TIME" and read["5"][0] == "THRESHOLD" and read["4"][0] == "LABEL"


def test_ranges_dispersions_and_four_decimal_limits_are_read():
    ranges = checks._parse("higher in the bimagrumab groups (14.0–21.4%) than with placebo (3.6%)")
    low = next(t for t in ranges.toks if t.text == "14.0")
    assert low.pair is not None and ranges.toks[low.pair].role == "RANGE_HI" and low.unit == "PCT"
    spread = checks._parse("-11·9% (SE 0·6) with retatrutide 4 mg, -16·8% (0·7) with retatrutide 9 mg")
    assert [t.role for t in spread.toks if t.text in ("0.6", "0.7")] == ["PAREN_SINGLE", "PAREN_SINGLE"]
    # CUR-11: "0.9560" is the stored limit 0.956
    limits = checks._parse("24 mg 14.5 mg 0.98 (0.9316, 1.0217) 1.01 (0.9560, 1.0705)")
    assert limits.has_number(0.956) and [t.role for t in limits.equal(0.956)] == ["CI_BOUND"]
    assert not checks._parse("-20.9% with 15 mg").has_number(0.9)


# ------------------------------------------------------------------- properties of the seeded database


@pytest.fixture(scope="module")
def seeded_flags(seeded):
    from incretinos.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        yield evaluate_all(session)
    finally:
        session.close()


def test_every_stored_record_is_evaluated(db, seeded_flags):
    for etype, model in MODELS.items():
        ids = set(db.scalars(select(model.id)))
        assert ids and {i for (t, i) in seeded_flags if t == etype} == ids


def test_flags_on_seeded_evidence_are_well_formed_and_concrete(seeded_flags):
    for (etype, _), flags in seeded_flags.items():
        assert len(flags) == len(set(flags))
        for flag in flags:
            spec = CHECKS[flag.code]
            assert flag.severity == spec["severity"] and etype in spec["applies_to"], flag
            assert flag.fields and all(isinstance(f, str) and f for f in flag.fields), flag
            # every explanation quotes something it saw: a number or a quoted token
            assert re.search(r"\d|'[^']+'", flag.explanation), flag
            assert "should be" not in flag.explanation and "correct value" not in flag.explanation, flag
            assert flag.code != "CHANGED_SINCE_LAST_INGESTION"


def test_no_quote_based_flag_on_a_seeded_registry_record(db, seeded_flags):
    for etype in ("result", "analysis", "adverse_event"):
        model = MODELS[etype]
        registry_ids = set(db.scalars(select(model.id).where(model.origin == "REGISTRY")))
        assert registry_ids
        for row_id in registry_ids:
            quote_based = [f.code for f in seeded_flags[(etype, row_id)] if f.code in QUOTE_BASED_CODES]
            assert not quote_based, (etype, row_id, quote_based)


def test_checks_only_read(db):
    """Running the checks changes no verification status and leaves nothing to write."""

    def statuses() -> dict[str, list[tuple[int, str]]]:
        return {etype: sorted(tuple(r) for r in db.execute(select(model.id, model.verification_status))) for etype, model in MODELS.items()}

    before = statuses()
    everything = evaluate_all(db)
    for etype, model in MODELS.items():
        row = db.scalars(select(model).order_by(model.id.desc()).limit(1)).one()
        assert evaluate(db, etype, row, previous_fields={"verification_status": "QUOTE_ANCHORED"}) is not None
    assert not db.new and not db.dirty and not db.deleted
    assert statuses() == before
    assert len(everything) == sum(len(rows) for rows in before.values())


def test_evaluate_agrees_with_evaluate_all_on_a_sample(db, seeded_flags):
    rng = random.Random(20261013)
    for etype, model in MODELS.items():
        ids = sorted(i for (t, i) in seeded_flags if t == etype)
        flagged = [i for i in ids if seeded_flags[(etype, i)]]
        curated = sorted(db.scalars(select(model.id).where(model.origin == "CURATED")))
        sample = set(rng.sample(ids, min(25, len(ids)))) | set(rng.sample(flagged, min(25, len(flagged)))) | set(rng.sample(curated, min(40, len(curated))))
        for row_id in sorted(sample):
            row = db.get(model, row_id)
            assert evaluate(db, etype, row) == seeded_flags[(etype, row_id)], (etype, row_id)


# ------------------------------------------------------------------ arm names and values listed in order


def test_a_list_of_doses_followed_by_a_list_of_values_pairs_in_order(world):
    quote = "At 72 weeks, participants receiving tirzepatide 5 mg and 15 mg lost 15.0% and 20.9% of body weight (n=630), compared with 3.1% for placebo."
    plain = dict(ci_low=None, ci_high=None, ci_level=None, locator_quote=quote)
    high = flags_of(world, world.result(**plain))
    assert not {"QUOTE_MULTIPLE_CANDIDATES", "QUOTE_ARM_MISMATCH", "ARM_NOT_IN_QUOTE", "SIGN_NOT_IN_QUOTE"} & codes(high)
    placebo = flags_of(world, world.result(arm=world.placebo, value=-3.1, n_analyzed=None, **plain))
    assert not {"QUOTE_MULTIPLE_CANDIDATES", "QUOTE_ARM_MISMATCH", "ARM_NOT_IN_QUOTE"} & codes(placebo)


def test_table_header_pairs_arm_names_with_cells_in_order(world):
    quote = "Placebo 5 mg 15 mg | % change at week 72 | -3.1 -15.0 -20.9"
    plain = dict(ci_low=None, ci_high=None, ci_level=None, n_analyzed=None, locator_quote=quote)
    assert not {"QUOTE_MULTIPLE_CANDIDATES", "QUOTE_ARM_MISMATCH"} & codes(flags_of(world, world.result(**plain)))
    swapped = flags_of(world, world.result(arm=world.low_dose, **plain))
    text = explanation(swapped, "QUOTE_ARM_MISMATCH")
    assert "-20.9 is value 3" in text and "'15 mg'" in text and "tirzepatide 5 mg" in text


def test_arm_named_before_a_comparison_is_not_paired_by_position(world):
    """'Compared with placebo, X reduced weight by A vs B' is prose, not a table: no positional pairing, so no false mismatch."""
    quote = "Compared with placebo, tirzepatide 15 mg reduced body weight by 20.9% vs 3.1% at week 72 (n=630)"
    flags = flags_of(world, world.result(ci_low=None, ci_high=None, ci_level=None, locator_quote=quote))
    assert "QUOTE_ARM_MISMATCH" not in codes(flags)


def test_plus_minus_dispersion_is_not_a_second_candidate(world):
    endpoint = world.endpoint("change from baseline in body weight", "WEIGHT_ABS_CHANGE", unit="kg")
    quote = "body weight changed by -9.6 ± 0.5 kg with tirzepatide 15 mg at week 72 (n=630)"
    flags = flags_of(world, world.result(endpoint=endpoint, value=-9.6, unit="kg", ci_low=None, ci_high=None, ci_level=None, locator_quote=quote))
    assert flags == []


def test_arm_named_in_the_quote_by_the_group_name_in_its_label(world):
    """BELIEVE pattern: the quote calls the arm 'high-dose combination', the label carries that name as a remark."""
    combo = world.arm("bimagrumab 30 mg/kg + semaglutide 2.4 mg (high-dose combination)", drug="bimagrumab+semaglutide", dose=None)
    mono = world.arm("semaglutide 2.4 mg", drug="semaglutide", dose=2.4)
    quote = "−12.0 kg (−10.8%; bimagrumab 30 mg kg), −16.5 kg (−15.7%; semaglutide 2.4 mg) and −24.2 kg (−22.1%; high-dose combination) at week 72"
    plain = dict(ci_low=None, ci_high=None, ci_level=None, n_analyzed=None, locator_quote=quote)
    arm_codes = {"QUOTE_MULTIPLE_CANDIDATES", "QUOTE_ARM_MISMATCH", "ARM_NOT_IN_QUOTE"}
    assert not arm_codes & codes(flags_of(world, world.result(arm=combo, value=-22.1, **plain)))
    assert not arm_codes & codes(flags_of(world, world.result(arm=mono, value=-15.7, **plain)))
    swapped = flags_of(world, world.result(arm=mono, value=-22.1, source=world.paper, **plain))
    assert "names '2.4 mg' with -15.7, not with -22.1" in explanation(swapped, "QUOTE_MULTIPLE_CANDIDATES")
    half = flags_of(world, world.result(arm=combo, value=-10.8, source=world.paper, timepoint_weeks=72.0, class_title="other", **plain))
    assert "'30 mg kg' is the arm named next to -10.8" in explanation(half, "QUOTE_ARM_MISMATCH")


def test_values_swapped_between_arms_of_one_quote_are_caught(world):
    """Each value of a prose list given to a neighbouring arm."""
    quote = "The mean percent change in body weight at week 72 was -15.0% (5-mg dose) and -20.9% (15-mg dose) versus -3.1%"
    plain = dict(ci_low=None, ci_high=None, ci_level=None, n_analyzed=None, locator_quote=quote)
    for arm, value, code, words in (
        (world.low_dose, -20.9, "QUOTE_ARM_MISMATCH", "'15-mg' is the arm named next to -20.9"),
        (world.high_dose, -15.0, "QUOTE_ARM_MISMATCH", "'5-mg' is the arm named next to -15.0"),
        (world.placebo, -15.0, "QUOTE_ARM_MISMATCH", "'5-mg' is the arm named next to -15.0"),
        # the unlabelled value is not the 15 mg arm's by elimination: the quote names 15 mg with another value
        (world.high_dose, -3.1, "QUOTE_MULTIPLE_CANDIDATES", "names '15-mg' with -20.9, not with -3.1"),
    ):
        flags = flags_of(world, world.result(arm=arm, value=value, **plain))
        assert words in explanation(flags, code), (arm.label, value)
        assert risk_level(flags) == "HIGH"


PROSE = [
    # quotes in which the 15 mg value (-20.9) and the placebo value (-3.1) are each tied to their arm
    "weight change was -20.9% for tirzepatide 15 mg and -3.1% for placebo at week 72",
    "Tirzepatide 15 mg: -20.9%; placebo: -3.1% (week 72)",
    "At week 72: 15 mg -20.9% vs placebo -3.1%",
    "At week 72, weight changed by -20.9% vs -3.1% (15 mg vs placebo)",
    "At week 72, placebo (-3.1%) and tirzepatide 15 mg (-20.9%)",
    "In the 15 mg group, weight changed by -20.9% at week 72; in the placebo group, by -3.1%",
    "-20.9% and -3.1% with tirzepatide 15 mg and placebo at week 72",
    "At week 72 weight changed by -20.9% with tirzepatide 15 mg. Placebo-treated participants changed by -3.1%.",
    "-20.9% (95% CI, -21.8 to -19.9; 15 mg) and -3.1% (95% CI, -4.3 to -1.9; placebo) at week 72",
    "Compared with placebo (-3.1%), tirzepatide 15 mg changed body weight by -20.9% at week 72",
]


@pytest.mark.parametrize("quote", PROSE)
def test_prose_that_ties_each_value_to_its_arm_raises_no_arm_flag(world, quote):
    plain = dict(ci_low=None, ci_high=None, ci_level=None, n_analyzed=None, locator_quote=quote)
    arm_codes = {"QUOTE_MULTIPLE_CANDIDATES", "QUOTE_ARM_MISMATCH", "ARM_NOT_IN_QUOTE"}
    assert not arm_codes & codes(flags_of(world, world.result(arm=world.high_dose, value=-20.9, **plain)))
    assert not arm_codes & codes(flags_of(world, world.result(arm=world.placebo, value=-3.1, **plain)))


@pytest.mark.parametrize("quote", PROSE)
def test_the_same_prose_with_the_arms_swapped_is_flagged_high(world, quote):
    plain = dict(ci_low=None, ci_high=None, ci_level=None, n_analyzed=None, locator_quote=quote)
    for arm, value in ((world.placebo, -20.9), (world.high_dose, -3.1)):
        flags = flags_of(world, world.result(arm=arm, value=value, **plain))
        assert {"QUOTE_ARM_MISMATCH", "QUOTE_MULTIPLE_CANDIDATES"} & codes(flags), (arm.label, value, [(f.code, f.explanation) for f in flags])
        assert risk_level(flags) == "HIGH"
