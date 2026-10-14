"""The twelve mandatory scientific validation cases, each with a positive and a negative check,
plus the comparability classifications they rely on.

Positive: the rule passes on the seeded evidence base.
Negative: deliberately corrupting the evidence (inside a rolled-back session) makes the rule fail,
which shows the rule actually detects the error it exists for.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from incretinos.curation.loader import CurationError, load_drug_evidence
from incretinos.curation.schema import AdverseEventDef, DrugsFile, ResultDef
from incretinos.db import rejected_visible
from incretinos.engines import validation as v
from incretinos.engines.comparability import compare_findings, harmonize_dose
from incretinos.engines.findings import analysis_dto, build_finding, get_finding
from incretinos.engines.interpretation import (
    classify_drug,
    describe_adverse_event,
    guard_statement,
    interpret_analysis,
    interpret_finding,
)
from incretinos.engines.safety import (
    InvalidInferenceError,
    assert_single_data_family,
    assert_single_measure_kind,
    causality_from_spontaneous_reports,
    incidence_from_spontaneous_reports,
)
from incretinos.engines.stats import ci_from_se
from incretinos.models import (
    AdverseEvent,
    Drug,
    DrugMechanism,
    ExposureEquivalence,
    HumanStatusWriteError,
    LabelRecord,
    OutcomeResult,
    StatisticalAnalysis,
    Study,
)
from incretinos.review.lifecycle import record_decision

from .conftest import result_id


def test_all_validation_rules_pass_on_seeded_evidence(db):
    results = v.run_all(db)
    failed = [(r.rule_id, r.violations) for r in results if not r.passed]
    assert not failed, failed
    assert len(results) >= 15 and all(r.checked > 0 for r in results if r.rule_id not in ("V09",))


# ---- Case 1: retatrutide is investigational unless an authoritative regulatory source confirms approval


def test_case01_retatrutide_is_investigational(db):
    reta = db.scalar(select(Drug).where(Drug.slug == "retatrutide"))
    assert reta.regulatory_status == "INVESTIGATIONAL"
    assert db.scalar(select(LabelRecord).where(LabelRecord.drug_id == reta.id)) is None
    assert v.v01_approval_requires_regulatory_source(db).passed


def test_case01_marking_retatrutide_approved_without_fda_labeling_fails(db):
    reta = db.scalar(select(Drug).where(Drug.slug == "retatrutide"))
    reta.regulatory_status = "APPROVED"  # cites the sponsor pipeline page — not a regulatory source
    db.flush()
    result = v.v01_approval_requires_regulatory_source(db)
    assert not result.passed and any("Retatrutide" in x for x in result.violations)


def test_case01_loader_refuses_approval_cited_to_press_release(db, tmp_path, monkeypatch):
    from incretinos.config import get_settings
    from incretinos.curation import loader

    original = loader.read_drugs(get_settings().curated_dir)
    doctored = original.model_dump(mode="json")
    for drug in doctored["drugs"]:
        if drug["slug"] == "retatrutide":
            drug["regulatory"]["status"] = "APPROVED"
    monkeypatch.setattr(loader, "read_drugs", lambda _dir: DrugsFile.model_validate(doctored))
    with pytest.raises(CurationError, match="must cite FDA labeling"):
        load_drug_evidence(db, get_settings().curated_dir)


# ---- Case 2: eloralintide is a selective amylin receptor agonist (and GIP-only agents are not GLP-1 RAs)


def test_case02_mechanism_classes(db):
    def cls(slug: str) -> dict:
        drug = db.scalar(select(Drug).where(Drug.slug == slug))
        return classify_drug([(m.target, m.action) for m in drug.mechanisms])

    elora = cls("eloralintide")
    assert elora["class_label"] == "Selective amylin receptor agonist" and not elora["is_glp1_receptor_agonist"] and elora["is_amylin_receptor_agonist"]
    macu = cls("macupatide")
    assert macu["class_label"] == "GIP receptor agonist" and not macu["is_glp1_receptor_agonist"]
    assert cls("tirzepatide")["class_label"] == "Dual GIP and GLP-1 receptor agonist"
    assert cls("retatrutide")["class_label"] == "Triple GIP, GLP-1 and glucagon receptor agonist"
    assert cls("brenipatide")["is_glp1_receptor_agonist"] and cls("brenipatide")["is_gip_receptor_agonist"]
    assert cls("eloralintide-tirzepatide")["class_label"].startswith("Combination")


def test_case02_misclassifying_eloralintide_as_glp1_fails(db):
    elora = db.scalar(select(Drug).where(Drug.slug == "eloralintide"))
    source_id = elora.mechanisms[0].source_id
    db.add(DrugMechanism(drug_id=elora.id, target="GLP1R", action="AGONIST", source_id=source_id))
    db.flush()
    db.refresh(elora)
    assert not v.v02_mechanism_classification(db).passed


# ---- Case 3: Foundayo capsule/tablet harmonized only by documented bioequivalence


def test_case03_documented_pairs_only(db):
    mapping = {1: 0.8, 3: 2.5, 6: 5.5, 12: 9, 24: 14.5, 36: 17.2}
    for capsule, tablet in mapping.items():
        out = harmonize_dose(db, "orforglipron-capsule", capsule, "foundayo-tablet")
        assert out["status"] == "DOCUMENTED" and out["equivalent"]["amount"] == tablet and out["basis"] == "DOCUMENTED_BIOEQUIVALENCE"
        assert out["source_key"] == "pubmed:41994902" and out["verification_status"] == "QUOTE_ANCHORED"
        back = harmonize_dose(db, "foundayo-tablet", tablet, "orforglipron-capsule")
        assert back["equivalent"]["amount"] == capsule
    ratios = {round(t / c, 3) for c, t in mapping.items()}
    assert len(ratios) == 6  # no constant ratio exists, so none can be applied


@pytest.mark.parametrize("amount", [2, 9, 18, 30, 36.5, 72])
def test_case03_no_arithmetic_conversion_for_undocumented_doses(db, amount):
    out = harmonize_dose(db, "orforglipron-capsule", amount, "foundayo-tablet")
    assert out["status"] == "NOT_DOCUMENTED" and out["equivalent"] is None


def test_case03_equivalence_without_source_fails(db):
    row = db.scalar(select(ExposureEquivalence))
    row.source_id, row.quote = None, None
    db.flush()
    assert not v.v03_formulation_harmonization(db).passed


# ---- Case 4: ATTAIN-1 efficacy-estimand and treatment-regimen results stay distinct


def test_case04_attain1_estimands_are_separate_rows_with_different_values(db):
    efficacy = get_finding(db, result_id(db, "NCT05869903", "36 mg", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72))
    regimen = get_finding(db, result_id(db, "NCT05869903", "36 mg", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=72))
    assert efficacy.value == -12.35 and regimen.value == -11.2
    assert efficacy.estimand_basis == "INFERRED_FROM_ANALYSIS_DESCRIPTION" and regimen.estimand_basis == "STATED_BY_SOURCE"
    assert "efficacy estimand" in efficacy.statement and "treatment-regimen estimand" in regimen.statement
    comparison = compare_findings(db, efficacy, regimen)
    assert comparison.classification == "NOT SUITABLE FOR EFFICACY COMPARISON"
    assert comparison.descriptive_difference is None
    assert any(f.key == "estimand" and f.assessment == "BLOCKING" for f in comparison.factors)


def test_case04_merging_estimand_values_fails(db):
    study = db.scalar(select(Study).where(Study.nct_id == "NCT05869903"))
    rows = [r for r in study.results if r.endpoint.category == "WEIGHT_PCT_CHANGE" and r.timepoint_weeks == 72 and "36" in r.arm_label and r.estimand is not None]
    efficacy = next(r for r in rows if r.estimand.label == "EFFICACY")
    regimen = next(r for r in rows if r.estimand.label == "TREATMENT_REGIMEN")
    regimen.value = efficacy.value  # the treatment-regimen row now shows the efficacy number
    db.flush()
    assert not v.v04_attain1_estimands_distinct(db).passed


# ---- Case 5: ACHIEVE-4 cardiovascular non-inferiority is not superiority


def _achieve4_mace4(db) -> StatisticalAnalysis:
    study = db.scalar(select(Study).where(Study.nct_id == "NCT05803421"))
    return next(a for a in study.analyses if a.endpoint.category == "MACE" and a.hypothesis_type == "NON_INFERIORITY")


def test_case05_achieve4_reading(db):
    analysis = _achieve4_mace4(db)
    assert (analysis.estimate, analysis.ci_low, analysis.ci_high, analysis.ci_level, analysis.ni_margin, analysis.ni_met) == (0.84, 0.59, 1.20, 95, 1.8, "YES")
    assert analysis.tier == "PEER_REVIEWED" and analysis.verification_status == "QUOTE_ANCHORED"
    dto = analysis_dto(analysis)
    reading = interpret_analysis(dto)
    assert any(label["code"] == "NON_INFERIORITY" for label in dto["labels"])
    assert any("Non-inferiority" in s and "1.8" in s for s in reading["establishes"])
    assert any("Superiority" in s and "includes 1" in s for s in reading["does_not_establish"])
    assert reading["ci_excludes_no_difference"] is False
    assert not guard_statement(" ".join(reading["establishes"]), hypothesis_type="NON_INFERIORITY", superiority_met="NOT_STATED")


def test_case05_superiority_language_is_caught():
    for text in ("Orforglipron was superior to insulin glargine on MACE-4.", "Orforglipron significantly reduced cardiovascular events.", "The trial demonstrated cardiovascular benefit."):
        assert guard_statement(text, hypothesis_type="NON_INFERIORITY", superiority_met="NOT_STATED"), text
    assert not guard_statement("Orforglipron was non-inferior to insulin glargine; superiority was not demonstrated.", hypothesis_type="NON_INFERIORITY", superiority_met="NOT_STATED")


def test_case05_relabeling_noninferiority_as_superiority_met_is_detected(db):
    analysis = _achieve4_mace4(db)
    analysis.hypothesis_type = "NOT_STATED"
    db.flush()
    assert not v.v05_noninferiority_not_superiority(db).passed  # ACHIEVE-4 MACE-4 must be on record as non-inferiority


# ---- Case 6: exploratory and multiplicity-uncontrolled results are labelled


def test_case06_achieve4_mortality_is_labelled_exploratory_and_uncontrolled(db):
    study = db.scalar(select(Study).where(Study.nct_id == "NCT05803421"))
    death = next(a for a in study.analyses if a.endpoint.category == "MORTALITY")
    dto = analysis_dto(death)
    codes = {label["code"] for label in dto["labels"]}
    assert {"EXPLORATORY", "MULTIPLICITY_UNCONTROLLED", "PRESS_RELEASE"} <= codes
    reading = interpret_analysis(dto)
    assert reading["ci_excludes_no_difference"] is True and not reading["establishes"]  # nominal interval excludes 1, yet nothing is established
    assert any("not controlled for multiplicity" in s for s in reading["does_not_establish"])
    assert guard_statement("Orforglipron demonstrated a reduction in all-cause death.", exploratory=True)


def test_case06_unstated_multiplicity_is_labelled_not_assumed(db):
    analysis = db.scalar(select(StatisticalAnalysis).where(StatisticalAnalysis.origin == "REGISTRY"))
    codes = {label["code"] for label in analysis_dto(analysis)["labels"]}
    assert "MULTIPLICITY_NOT_STATED" in codes and "MULTIPLICITY_UNCONTROLLED" not in codes


# ---- Case 7: EloraTZP efficacy always carries population, duration, dose and limitations


def test_case07_eloratzp_statement_is_self_qualifying(db):
    finding = get_finding(db, result_id(db, "NCT06603571", "9 mg + tirzepatide 15", estimand="EFFICACY"))
    statement = finding.statement
    for needle in ("EloraTZP Phase 2b", "NCT06603571", "type 2 diabetes", "eloralintide 9 mg + tirzepatide 15 mg", "-23.3%", "week 48", "efficacy estimand", "confidence interval not reported", "n not reported", "Phase 2 dose-finding", "press"):
        assert needle in statement, needle
    codes = {label["code"] for label in finding.labels}
    assert {"PRESS_RELEASE", "ESTIMAND_EFFICACY", "CI_NOT_REPORTED", "PHASE2"} <= codes


def test_case07_api_never_returns_a_bare_number(client, db):
    rid = result_id(db, "NCT06603571", "9 mg + tirzepatide 15", estimand="EFFICACY")
    body = client.get(f"/api/findings/{rid}").json()
    finding = body["finding"]
    assert finding["diabetes_status"] == "T2D" and finding["timepoint_weeks"] == 48 and finding["phase"] == "PHASE2" and finding["statement"] and finding["labels"]
    assert body["interpretation"]["does_not_establish"]


def test_case07_discontinuation_range_is_not_attributed_to_one_arm(db):
    study = db.scalar(select(Study).where(Study.nct_id == "NCT06603571"))
    ranges = [ae for ae in study.adverse_events if ae.scope == "RANGE_ACROSS_ARMS"]
    combo = next(ae for ae in ranges if ae.percent_low == 10.8 and ae.percent_high == 27.0)
    assert combo.arm_id is None and combo.percent is None and combo.measure_kind == "TREATMENT_DISCONTINUATION_DUE_TO_AE"
    sentence = describe_adverse_event(__import__("incretinos.engines.findings", fromlist=["ae_dto"]).ae_dto(combo))
    assert "range across" in sentence and "not attributable to any single arm" in sentence and "not the incidence of any adverse event" in sentence


# ---- Case 8: adverse-event discontinuation is never confused with adverse-event incidence


def test_case08_measure_kinds_cannot_be_mixed():
    with pytest.raises(InvalidInferenceError):
        assert_single_measure_kind({"PARTICIPANTS_WITH_EVENT", "TREATMENT_DISCONTINUATION_DUE_TO_AE"})
    with pytest.raises(InvalidInferenceError):
        assert_single_measure_kind({"STUDY_WITHDRAWAL_DUE_TO_AE", "TREATMENT_DISCONTINUATION_DUE_TO_AE"})
    assert_single_measure_kind({"PARTICIPANTS_WITH_EVENT"})


def test_case08_storing_discontinuation_as_incidence_fails(db):
    row = db.scalar(select(AdverseEvent).where(AdverseEvent.measure_kind == "TREATMENT_DISCONTINUATION_DUE_TO_AE"))
    row.measure_kind = "PARTICIPANTS_WITH_EVENT"
    db.flush()
    assert not v.v08_discontinuation_is_not_incidence(db).passed


def test_case08_curation_schema_requires_explicit_measure_kind():
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        AdverseEventDef.model_validate({"arm": "x", "term": "Discontinuation due to AE", "category": "DISCONTINUATION", "percent": 6.2, "source": "pubmed:1", "quote": "6.2%"})


# ---- Case 9: spontaneous FDA reports are never incidence or proof of causality


def test_case09_no_incidence_or_causality_from_faers():
    with pytest.raises(InvalidInferenceError, match="number of exposed patients is unknown"):
        incidence_from_spontaneous_reports(17092, 160663)
    with pytest.raises(InvalidInferenceError):
        causality_from_spontaneous_reports()
    with pytest.raises(InvalidInferenceError):
        assert_single_data_family({"TRIAL", "SPONTANEOUS"})


def test_case09_api_marks_report_counts_and_refuses_the_question(client):
    body = client.get("/api/safety/spontaneous/tirzepatide").json()
    assert body["is_incidence"] is False and body["supports_causality"] is False and body["unit"].startswith("reports")
    assert len(body["caveats"]) >= 4 and body["total_reports"] > 0
    answer = client.post("/api/assistant/ask", json={"question": "Does tirzepatide cause pancreatitis according to FAERS?"}).json()
    assert answer["intent"] == "REFUSED_SPONTANEOUS_INFERENCE" and answer["blocks"][0]["kind"] == "REFUSAL"


def test_case09_language_guard():
    assert guard_statement("FAERS shows an incidence of pancreatitis of 2% of patients.", data_family="SPONTANEOUS")
    assert guard_statement("These reports prove tirzepatide causes gastroparesis.", data_family="SPONTANEOUS")
    assert not guard_statement("Report counts cannot be read as incidence and do not show that the drug causes the event.", data_family="SPONTANEOUS")


# ---- Case 10: SURMOUNT-4 weight change uses the randomized-withdrawal baseline


def test_case10_surmount4_baseline(db):
    withdrawal = get_finding(db, result_id(db, "NCT04660643", "tirzepatide", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=88, baseline_contains="randomization"))
    placebo = get_finding(db, result_id(db, "NCT04660643", "placebo", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=88, baseline_contains="randomization"))
    assert (withdrawal.value, placebo.value) == (-6.7, 14.8)
    assert withdrawal.baseline_definition == "Randomization (Week 36)" and withdrawal.baseline_class == "RANDOMIZATION_AFTER_LEAD_IN"
    assert "from Randomization (Week 36) at week 88" in withdrawal.statement
    assert any(label["code"] == "WITHDRAWAL_BASELINE" for label in withdrawal.labels)
    assert compare_findings(db, withdrawal, placebo).classification == "DIRECT RANDOMIZED COMPARISON"
    entry = get_finding(db, result_id(db, "NCT04184622", "15 mg", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72))
    cross = compare_findings(db, withdrawal, entry)
    assert cross.classification == "NOT SUITABLE FOR EFFICACY COMPARISON" and any(f.key == "baseline" and f.assessment == "BLOCKING" for f in cross.factors)
    whole = get_finding(db, result_id(db, "NCT04660643", "tirzepatide", tier="REGISTRY_RESULTS", weeks=88, baseline_contains="baseline (week 0)"))
    assert compare_findings(db, withdrawal, whole).classification == "NOT SUITABLE FOR EFFICACY COMPARISON"  # week 36→88 vs week 0→88 of the same arm


def test_case10_dropping_the_baseline_definition_fails(db):
    study = db.scalar(select(Study).where(Study.nct_id == "NCT04660643"))
    row = next(r for r in study.results if r.endpoint.category == "WEIGHT_PCT_CHANGE")
    row.baseline_definition = None
    db.flush()
    assert not v.v10_randomized_withdrawal_baseline(db).passed


# ---- Case 11: lean / fat-free mass is not skeletal muscle


def test_case11_lean_mass_results_carry_the_qualification(db):
    rows = [r for r in db.scalars(select(OutcomeResult).join(OutcomeResult.endpoint)) if r.endpoint.category == "BODY_COMPOSITION"]
    lean = [build_finding(db, r) for r in rows]
    lean = [f for f in lean if f.is_body_composition_lean]
    assert lean, "expected DXA lean-mass results (BELIEVE / SURMOUNT-1 substudy)"
    for finding in lean[:10]:
        assert any(label["code"] == "LEAN_MASS" for label in finding.labels)
        assert "not a direct measurement of skeletal muscle" in finding.statement
        assert any("skeletal muscle" in s for s in interpret_finding(finding)["does_not_establish"])


def test_case11_language_guard():
    assert guard_statement("Participants lost muscle on tirzepatide.", lean_mass=True)
    assert guard_statement("The drug caused muscle loss of 10%.", lean_mass=True)
    assert not guard_statement("DXA lean mass decreased; this is not a direct measure of muscle loss.", lean_mass=True)


# ---- Case 12: an absent confidence interval is marked unavailable


def test_case12_missing_ci_is_explicit_and_never_filled(db, client):
    rid = result_id(db, "NCT06603571", "tirzepatide 15 mg", estimand="EFFICACY")
    finding = client.get(f"/api/findings/{rid}").json()
    f = finding["finding"]
    assert f["ci_low"] is None and f["ci_high"] is None and f["ci_level"] is None and f["n_analyzed"] is None
    assert "confidence_interval" in f["missing_fields"] and "n_analyzed" in f["missing_fields"]
    assert any(label["code"] == "CI_NOT_REPORTED" for label in f["labels"])
    assert finding["interpretation"]["derived_interval"] is None  # nothing to derive from: no dispersion reported
    assert any("Confidence interval" in m for m in finding["interpretation"]["missing"])


def test_case12_derived_interval_is_separate_and_labelled(db, client):
    rid = result_id(db, "NCT04184622", "15 mg", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72)
    body = client.get(f"/api/findings/{rid}").json()
    assert body["finding"]["ci_low"] is None  # the stored, reported interval stays empty
    derived = body["interpretation"]["derived_interval"]
    assert derived["kind"] == "INFERENCE" and "not reported by the source" in derived["note"]
    expected = ci_from_se(-22.5, 0.39)
    assert derived["ci_low"] == pytest.approx(expected["ci_low"]) and derived["ci_high"] == pytest.approx(expected["ci_high"])


def test_case12_registry_analysis_without_p_value_is_marked(db):
    study = db.scalar(select(Study).where(Study.nct_id == "NCT05822830"))  # SURMOUNT-5: registry posts CI, no p-value
    analysis = next(a for a in study.analyses if a.origin == "REGISTRY" and a.endpoint.role == "PRIMARY")
    dto = analysis_dto(analysis)
    assert (dto["estimate"], dto["ci_low"], dto["ci_high"]) == (-6.5, -8.1, -4.9)
    assert dto["p_value_text"] is None and dto["p_value_status"] == "NOT_REPORTED" and "p_value" in dto["missing_fields"]
    assert "p-value not reported" in interpret_analysis(dto)["observed"]


def test_case12_zero_filling_is_detected(db):
    row = db.scalar(select(OutcomeResult).where(OutcomeResult.ci_low.is_(None)))
    row.ci_low = 0.0  # one limit silently filled
    db.flush()
    assert not v.v12_missing_values_stay_missing(db).passed


def test_case12_curation_schema_rejects_numbers_without_supporting_quote():
    base = {"endpoint": "e", "arm": "tirzepatide 15 mg", "source": "pubmed:1"}
    ResultDef.model_validate({**base, "value": -20.9, "quote": "was −20.9% with 15 mg"})
    with pytest.raises(Exception, match="not present in the locator quote"):
        ResultDef.model_validate({**base, "value": -20.9, "quote": "was −21.9% with 15 mg"})
    with pytest.raises(Exception, match="not present in the locator quote"):
        ResultDef.model_validate({**base, "value": -20.9, "ci_low": -21.9, "ci_high": -19.9, "quote": "was −20.9% with 15 mg"})
    with pytest.raises(Exception, match="has no value"):
        ResultDef.model_validate({**base, "quote": "weight fell"})
    with pytest.raises(Exception, match="both limits"):
        ResultDef.model_validate({**base, "value": -20.9, "ci_low": -21.9, "quote": "−20.9% (−21.9"})


# ---- Comparability classifications (acceptance demonstrations A–C)


def test_within_trial_randomized_comparison_reports_the_registry_analysis(db):
    a = get_finding(db, result_id(db, "NCT04184622", "5 mg", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72))
    b = get_finding(db, result_id(db, "NCT04184622", "placebo", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72))
    comparison = compare_findings(db, a, b)
    assert comparison.classification == "DIRECT RANDOMIZED COMPARISON" and comparison.same_study
    analysis = comparison.reported_analyses[0]
    assert (analysis["estimate"], analysis["ci_low"], analysis["ci_high"]) == (-13.5, -14.6, -12.5)
    assert comparison.descriptive_difference["ci"] is None


def test_retatrutide_vs_tirzepatide_is_not_head_to_head(db):
    reta = get_finding(db, result_id(db, "NCT05929066", "12 mg", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=80))
    tirz = get_finding(db, result_id(db, "NCT04184622", "15 mg", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=72))
    assert (reta.value, tirz.value) == (-25.0, -20.9)
    comparison = compare_findings(db, reta, tirz)
    assert comparison.classification == "DESCRIPTIVE CROSS-STUDY COMPARISON"
    assert not comparison.reported_analyses
    assert [t["display_name"] for t in comparison.head_to_head_trials] == ["TRIUMPH-5"] and comparison.head_to_head_trials[0]["has_results"] is False
    assert any("superior" in s for s in comparison.prohibited_statements) and any("statistically significant" in s for s in comparison.prohibited_statements)
    assert comparison.descriptive_difference["ci_status"] == "NOT_AVAILABLE" and "not an estimate of comparative efficacy" in comparison.descriptive_difference["note"]
    assert any("TRIUMPH-5" in r for r in comparison.requirements)


def test_different_diabetes_populations_are_not_comparable(db):
    elora = get_finding(db, result_id(db, "NCT06603571", "9 mg + tirzepatide 15", estimand="EFFICACY"))
    tirz = get_finding(db, result_id(db, "NCT04184622", "15 mg", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72))
    comparison = compare_findings(db, elora, tirz)
    assert comparison.classification == "NOT SUITABLE FOR EFFICACY COMPARISON"
    assert any(f.key == "diabetes_status" and f.assessment == "BLOCKING" for f in comparison.factors)
    assert comparison.descriptive_difference is None


def test_similar_placebo_controlled_trials_are_only_partially_comparable(db):
    orfo = get_finding(db, result_id(db, "NCT05869903", "36 mg", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72))
    tirz = get_finding(db, result_id(db, "NCT04184622", "15 mg", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72))
    comparison = compare_findings(db, orfo, tirz)
    assert comparison.classification == "PARTIALLY COMPARABLE"
    assert any("superior" in s for s in comparison.prohibited_statements)


def test_unknown_estimand_gives_insufficient_evidence(db):
    sema = get_finding(db, result_id(db, "NCT05822830", "semaglutide", tier="REGISTRY_RESULTS"))  # SURMOUNT-5 registry: estimand not determinable
    tirz = get_finding(db, result_id(db, "NCT04184622", "15 mg", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72))
    assert sema.estimand_label == "NOT_STATED"
    assert compare_findings(db, sema, tirz).classification == "INSUFFICIENT EVIDENCE"


def test_every_classification_gives_reasons_not_just_a_label(db, client):
    for preset in client.get("/api/compare/presets").json():
        body = client.post("/api/compare", json={"a": preset["a"], "b": preset["b"]}).json()["comparison"]
        assert body["reasons"] and body["factors"] and body["permitted_statements"], preset["title"]
        assert all(f["explanation"] for f in body["factors"])
    classes = {p["classification"] for p in client.get("/api/compare/presets").json()}
    assert {"DIRECT RANDOMIZED COMPARISON", "PARTIALLY COMPARABLE", "DESCRIPTIVE CROSS-STUDY COMPARISON", "NOT SUITABLE FOR EFFICACY COMPARISON"} <= classes


# ---- Number formatting must never alter a value


def test_formatting_never_drops_significant_zeros():
    from incretinos.engines.findings import _fmt

    assert _fmt(80.0, 0) == "80" and _fmt(100.0, 0) == "100" and _fmt(40, 0) == "40" and _fmt(0.0) == "0"
    assert _fmt(-20.0) == "-20" and _fmt(-20.9) == "-20.9" and _fmt(1.20) == "1.2" and _fmt(0.84) == "0.84" and _fmt(630.0, 0) == "630"
    assert _fmt(None) == "not reported"


def test_statement_time_points_match_stored_values(db):
    """Every rendered statement names exactly the stored assessment week."""
    from incretinos.models import OutcomeResult

    checked = 0
    for row in db.scalars(select(OutcomeResult).where(OutcomeResult.timepoint_weeks.is_not(None)).limit(600)):
        statement = build_finding(db, row).statement
        assert f"at week {row.timepoint_weeks:.0f} " in statement, (row.id, row.timepoint_weeks, statement[:160])
        checked += 1
    assert checked > 300
    triumph = get_finding(db, result_id(db, "NCT05929066", "12 mg", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=80))
    assert "at week 80 " in triumph.statement and "-25%" in triumph.statement


# =========================================================================== Session 2: comparability audit (C1–C8, WEB-12, XS-12)
#
# Results are located by their scientific description (trial, arm, endpoint title, estimand, tier, week),
# never by id. Names introduced in Session 2 are imported inside the tests, so that each test fails on its
# own (rather than the whole module failing to import) when run against the code it was written to correct.


def _pick(db, nct: str, *, category: str = "WEIGHT_PCT_CHANGE", arm: str | None = None, arm_is: str | None = None, title: str | None = None, cls: str | None = None,
          estimand: str | None = None, tier: str | None = None, weeks: float | None = None, baseline: str | None = None):
    """The finding for the first stored result matching every stated attribute (arm = substring, arm_is = exact)."""
    study = db.scalar(select(Study).where(Study.nct_id == nct))
    assert study is not None, f"{nct} not seeded"
    for r in sorted(study.results, key=lambda x: x.id):
        if r.endpoint.category != category or r.value is None:
            continue
        if arm is not None and arm.lower() not in r.arm_label.lower():
            continue
        if arm_is is not None and arm_is.lower() != r.arm_label.lower():
            continue
        if title is not None and title.lower() not in r.endpoint.title.lower():
            continue
        if cls is not None and cls.lower() != (r.class_title or "").lower():
            continue
        if estimand is not None and (r.estimand.label if r.estimand else "NOT_STATED") != estimand:
            continue
        if tier is not None and r.tier != tier:
            continue
        if weeks is not None and r.timepoint_weeks != weeks:
            continue
        if baseline is not None and baseline.lower() not in (r.baseline_definition or "").lower():
            continue
        return build_finding(db, r)
    raise AssertionError(f"No result for {nct} / {category} / {arm or arm_is} / {title} / {estimand} / {tier} / {weeks}")


def _factor(comparison, key: str):
    return next(f for f in comparison.factors if f.key == key)


SM1, SM3, SM4, SM_OSA, ATTAIN1, TRIUMPH1, ACHIEVE1, ACHIEVE3, ACHIEVE5, AWARD11, BELIEVE, RETA_P2, BIOEQ = (
    "NCT04184622", "NCT04657016", "NCT04660643", "NCT05412004", "NCT05869903", "NCT05929066", "NCT05971940", "NCT06045221", "NCT06109311", "NCT03495102", "NCT05616013", "NCT04881760", "NCT06440980",
)


# ---- C1: "direct randomized" needs two different arms of one randomization


def test_c1_registry_and_curated_record_of_one_arm_are_not_a_treatment_comparison(db):
    registry = _pick(db, ATTAIN1, arm="36 mg", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72)
    release = _pick(db, ATTAIN1, arm="36 mg", estimand="EFFICACY", tier="PRESS_RELEASE", weeks=72)
    comparison = compare_findings(db, registry, release)
    assert comparison.classification == "NOT SUITABLE FOR EFFICACY COMPARISON"  # was DIRECT RANDOMIZED COMPARISON with a "difference" of 0.05
    assert registry.arm_id != release.arm_id and registry.arm_group_key == release.arm_group_key  # two arm rows, one randomized group
    assert comparison.relationship == "SAME_ARM_TWO_REPORTS"
    assert "not a treatment comparison" in comparison.headline and "not a treatment comparison" in _factor(comparison, "randomization").explanation
    assert comparison.descriptive_difference is None and comparison.reported_analyses == []
    assert not any("randomized against each other" in s for s in comparison.permitted_statements)
    assert any("same arm" in s for s in comparison.prohibited_statements)


def test_c1_capsule_dose_and_its_documented_tablet_equivalent_are_one_arm(db):
    capsule = _pick(db, ATTAIN1, arm="36 mg once daily", estimand="TREATMENT_REGIMEN", weeks=72)
    tablet = _pick(db, ATTAIN1, arm="FOUNDAYO 17.2", weeks=72)
    comparison = compare_findings(db, capsule, tablet)
    assert not comparison.classification.startswith("DIRECT") and comparison.descriptive_difference is None  # was DIRECT with a "difference" of −0.1
    assert comparison.relationship == "SAME_ARM_TWO_REPORTS"
    assert "documented exposure-equivalent pair" in _factor(comparison, "randomization").explanation
    # A different dose of the same drug is a different arm: 36 mg capsule versus the label's 9 mg tablet (= 12 mg capsule).
    other = _pick(db, ATTAIN1, arm="FOUNDAYO 9 mg", weeks=72)
    assert compare_findings(db, capsule, other).relationship == "RANDOMIZED_ARMS_OF_ONE_TRIAL"


def test_c1_surmount4_registry_arm_and_its_publication_label_are_one_arm(db):
    registry = _pick(db, SM4, arm_is="Tirzepatide MTD", tier="REGISTRY_RESULTS", weeks=88, baseline="randomization")
    release = _pick(db, SM4, arm="continued", tier="PRESS_RELEASE", weeks=88, baseline="week 36")
    comparison = compare_findings(db, registry, release)
    assert not comparison.classification.startswith("DIRECT") and comparison.relationship == "SAME_ARM_TWO_REPORTS" and comparison.descriptive_difference is None


def test_c1_arms_of_separately_randomized_substudies_are_not_direct(db):
    tirzepatide_no_pap = _pick(db, SM_OSA, arm="Tirzepatide MTD_GPI1")
    placebo_pap = _pick(db, SM_OSA, arm="Placebo_GPI2")
    placebo_no_pap = _pick(db, SM_OSA, arm="Placebo_GPI1")
    across = compare_findings(db, tirzepatide_no_pap, placebo_pap)
    assert across.same_study and not across.classification.startswith("DIRECT")
    assert across.relationship == "SAME_REGISTRATION_NOT_RANDOMIZED_AGAINST_EACH_OTHER" and across.reported_analyses == []
    assert "not randomized against each other" in _factor(across, "randomization").explanation
    assert any("superior" in s for s in across.prohibited_statements)
    within = compare_findings(db, tirzepatide_no_pap, placebo_no_pap)
    assert within.classification == "DIRECT RANDOMIZED COMPARISON" and within.relationship == "RANDOMIZED_ARMS_OF_ONE_TRIAL"


def test_c1_unresolved_arm_identity_is_not_called_direct(db):
    # 'retatrutide 12 mg' (publication) and '12 mg LY3437943 (2 mg)' (registry) may be one arm; four registry arms could be it.
    curated = _pick(db, RETA_P2, arm_is="retatrutide 12 mg", weeks=48)
    registry = _pick(db, RETA_P2, arm="12 mg LY3437943", title="Mean Percent Change", weeks=48)
    comparison = compare_findings(db, curated, registry)
    assert comparison.classification == "INSUFFICIENT EVIDENCE" and comparison.relationship == "SAME_TRIAL_ARM_IDENTITY_UNRESOLVED"
    assert comparison.descriptive_difference is None


def test_c1_arm_labels_are_read_for_substudy_period_and_dose_markers():
    from incretinos.engines.findings import arm_dose_tokens, arm_stratum

    assert arm_stratum("Tirzepatide MTD_GPI1") == "SUBSTUDY GPI1" != arm_stratum("Placebo_GPI2") and arm_stratum("Eloralintide (YSA1)") == "SUBSTUDY YSA1"
    assert arm_stratum("Tirzepatide (lead-in)") == "LEAD-IN" and arm_stratum("Retatrutide Extension Period") == "EXTENSION" and arm_stratum("LY3537031 Period 1") == "PERIOD 1"
    for switch in ("retatrutide 4 mg to MTD", "placebo to retatrutide MTD", "Retatrutide Dose 1 to Retatrutide Dose 2", "Retatrutide Dose 1 to Placebo"):
        assert arm_stratum(switch) == "SWITCH SEQUENCE", switch
    for main in ("15 mg Tirzepatide", "Placebo", "Placebo to Match", "Tirzepatide titrated to MTD", "Tirzepatide (LY3298176)", "tirzepatide MTD (continued)", "placebo (switched)",
                 "orforglipron maximum tolerated dose (up to 36 mg capsule, stated as equivalent to 17.2 mg tablet)", "4 mg LY3437943 (2 mg)"):
        assert arm_stratum(main) == "", main  # no sub-study or period marker: the main randomization
    assert arm_dose_tokens("3/6/9 mg Eloralintide") == (3.0, 6.0, 9.0) != arm_dose_tokens("9 mg Eloralintide") and arm_dose_tokens("eloralintide 6-9 mg (dose escalation)") == arm_dose_tokens("6/9 mg Eloralintide")
    assert arm_dose_tokens("Retatrutide Dose 2") == () and arm_dose_tokens("12 milligram (mg) LY3502970") == arm_dose_tokens("12 mg LY3502970") == (12.0,)


def test_c1_sweep_direct_only_for_distinct_arms_of_one_randomization_and_one_quantity(db):
    """Every pair of stored percent-weight-change results within a trial, and a spread of pairs across trials."""
    import itertools

    from incretinos.engines.findings import unit_family
    from incretinos.models import Endpoint

    rows = list(db.scalars(select(OutcomeResult).join(Endpoint, OutcomeResult.endpoint_id == Endpoint.id).where(Endpoint.category == "WEIGHT_PCT_CHANGE").order_by(OutcomeResult.id)))
    findings = [build_finding(db, r) for r in rows]
    direct = same_arm = 0
    for a, b in itertools.combinations(findings, 2):
        if a.study_id != b.study_id:
            continue
        c = compare_findings(db, a, b)
        if a.arm_group_key is not None and a.arm_group_key == b.arm_group_key:
            same_arm += 1
            assert not c.classification.startswith("DIRECT") and c.descriptive_difference is None, (a.result_id, b.result_id)
        if c.classification.startswith("DIRECT"):
            direct += 1
            assert a.arm_group_key != b.arm_group_key and a.randomization_stratum == b.randomization_stratum, (a.result_id, b.result_id)
            assert a.baseline_class == b.baseline_class and a.timepoint_weeks == b.timepoint_weeks, (a.result_id, b.result_id)
            assert unit_family(a.unit) == unit_family(b.unit) and a.endpoint_definition.threshold == b.endpoint_definition.threshold, (a.result_id, b.result_id)
            assert a.estimand_label == b.estimand_label, (a.result_id, b.result_id)
            assert all(x["pair_orientation"]["treatment_is"] in ("A", "B") for x in c.reported_analyses)
        if c.descriptive_difference is not None:
            assert c.descriptive_difference["ci"] is None and c.descriptive_difference["ci_status"] == "NOT_AVAILABLE"
    assert direct > 100 and same_arm > 100
    cross = 0
    sample = findings[::6]
    for a, b in itertools.combinations(sample, 2):
        if a.study_id == b.study_id:
            continue
        c = compare_findings(db, a, b)
        cross += 1
        assert not c.classification.startswith("DIRECT") and c.reported_analyses == [], (a.result_id, b.result_id)
        assert c.descriptive_difference is None or c.descriptive_difference["ci"] is None
    assert cross > 500


# ---- C2: the endpoint category is a family, not a definition


def test_c2_systolic_and_diastolic_pressure_are_different_endpoints(db):
    sbp = _pick(db, SM3, category="BLOOD_PRESSURE", title="Systolic", arm="Placebo")
    dbp = _pick(db, SM3, category="BLOOD_PRESSURE", title="Diastolic", arm="Tirzepatide")
    comparison = compare_findings(db, sbp, dbp)
    assert comparison.classification == "NOT SUITABLE FOR EFFICACY COMPARISON" and comparison.descriptive_difference is None
    endpoint = _factor(comparison, "endpoint")
    assert endpoint.assessment == "BLOCKING" and "systolic blood pressure" in endpoint.explanation and "diastolic blood pressure" in endpoint.explanation
    assert comparison.reported_analyses == []  # the SBP analysis is not quoted for an SBP-versus-DBP pair


@pytest.mark.parametrize(
    ("nct", "category", "a", "b", "blocking"),
    [
        (AWARD11, "PHARMACOKINETICS", {"title": "Cmax"}, {"title": "AUC"}, {"endpoint", "unit"}),
        (SM_OSA, "OTHER", {"title": "Change From Baseline in Apnea", "arm": "Tirzepatide MTD_GPI1"}, {"title": "Percent Change From Baseline in Apnea", "arm": "Placebo_GPI1"}, {"unit"}),
        (BELIEVE, "BODY_COMPOSITION", {"title": "Total Body Fat Mass in Kilograms", "arm_is": "30 mg/kg Bimagrumab"}, {"title": "Percent Change From Baseline for Fat Mass by DXA at Week 48", "arm_is": "10/30 mg/kg Bimagrumab"}, {"unit"}),
        (SM1, "PHYSICAL_FUNCTION", {"title": "SF-36v2", "arm": "Placebo"}, {"title": "IWQOL-Lite-CT", "arm": "15 mg"}, {"endpoint"}),
        (SM3, "WEIGHT_RESPONDER", {"title": "5% Body Weight Reduction", "arm": "Placebo"}, {"title": "Maintain", "arm": "Tirzepatide"}, {"endpoint"}),
    ],
)
def test_c2_unrelated_endpoints_of_one_category_are_never_differenced(db, nct, category, a, b, blocking):
    fa, fb = _pick(db, nct, category=category, **a), _pick(db, nct, category=category, **b)
    assert fa.category == fb.category == category
    comparison = compare_findings(db, fa, fb)
    assert comparison.classification == "NOT SUITABLE FOR EFFICACY COMPARISON", comparison.classification
    assert comparison.descriptive_difference is None and comparison.reported_analyses == []
    assert blocking <= {f.key for f in comparison.factors if f.assessment == "BLOCKING"}


def test_c2_endpoint_definition_reads_measurement_threshold_and_unit():
    from incretinos.engines.findings import endpoint_definition, unit_family

    maintain = endpoint_definition("WEIGHT_RESPONDER", "Percentage of Participants Who Maintain ≥80% of the Body Weight Lost During Intensive Lifestyle Program", None, None, "percentage of participants")
    five = endpoint_definition("WEIGHT_RESPONDER", "Percentage of Participants With Greater Than or Equal to (≥) 5% Body Weight Reduction", None, None, "Percentage of participants")
    assert maintain.threshold == (">=80%",) and five.threshold == (">=5%",) and maintain.unit_family == five.unit_family == "PERCENT_OF_PARTICIPANTS"
    # The class names the component of a multi-component measure; a stored threshold that contradicts it is not used.
    classed = endpoint_definition("WEIGHT_RESPONDER", "Percentage of Participants Who Achieve ≥5%, ≥10%, ≥15%, ≥20% Body Weight Reduction From Baseline", "≥10% body weight reduction from baseline", 20.0, "percentage of participants")
    assert classed.threshold == (">=10%",) and not classed.threshold_ambiguous
    assert endpoint_definition("WEIGHT_RESPONDER", "Participants Who Achieve ≥5%, ≥10% Body Weight Reduction", None, None, "%").threshold_ambiguous
    assert endpoint_definition("BLOOD_PRESSURE", "Change From Baseline in SBP, DBP", "DBP", None, "mmHg").measure == ("diastolic blood pressure",)
    assert endpoint_definition("LIPIDS", "Percent Change From Baseline in Very Low-Density Lipoprotein (VLDL) Cholesterol", None, None, "percent change").measure == ("VLDL cholesterol",)
    assert endpoint_definition("LIPIDS", "Percent Change From Baseline in Non-High-Density Lipoprotein (Non-HDL) Cholesterol", None, None, "percent change").measure == ("non-HDL cholesterol",)
    assert unit_family("millimeters of mercury (mmHg)") == unit_family("mm Hg") == "MMHG" and unit_family("Kilograms (Kg)") == unit_family("kg") == "KG"
    assert unit_family("percent change") != unit_family("percentage of participants") and unit_family("kg/m^2") != unit_family("kg") and unit_family(None) is None


# ---- C3: the quoted between-arm analysis is the analysis of this endpoint, estimand, time point and arm pair


def test_c3_no_analysis_is_quoted_when_none_matches_the_estimand(db):
    high = _pick(db, SM1, arm="15 mg", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=72)
    placebo = _pick(db, SM1, arm="placebo", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=72)
    comparison = compare_findings(db, high, placebo)
    assert comparison.classification == "DIRECT RANDOMIZED COMPARISON" and comparison.descriptive_difference["value"] == pytest.approx(-17.8)
    assert comparison.reported_analyses == []  # the registry efficacy-estimand analysis (−20.1) is not the analysis of these treatment-regimen values
    efficacy = next(x for x in comparison.other_analyses if x["estimate"] == -20.1)
    assert any("estimand" in reason for reason in efficacy["mismatch"])
    assert any("no between-arm analysis of this endpoint, estimand and time point is stored" in s for s in comparison.permitted_statements)
    assert not any("-20.1" in s for s in comparison.permitted_statements)
    assert any("statistically significant" in s for s in comparison.prohibited_statements)


def test_c3_analysis_matches_baseline_and_responder_threshold(db):
    # SURMOUNT-4 change from study entry (week 0): the from-randomization analysis (−21.4) is a different quantity.
    a = _pick(db, SM4, arm_is="Tirzepatide MTD", tier="REGISTRY_RESULTS", weeks=88, baseline="Baseline (Week 0)")
    b = _pick(db, SM4, arm_is="Placebo", tier="REGISTRY_RESULTS", weeks=88, baseline="Baseline (Week 0)")
    comparison = compare_findings(db, a, b)
    assert [x["estimate"] for x in comparison.reported_analyses] == [-16.4]
    assert -21.4 in [x["estimate"] for x in comparison.other_analyses]
    # SURMOUNT-1 ≥20% responders: the ≥5% odds ratio (75.48) is not quoted.
    a = _pick(db, SM1, category="WEIGHT_RESPONDER", title="≥20%", arm="15 mg")
    b = _pick(db, SM1, category="WEIGHT_RESPONDER", title="≥20%", arm="Placebo")
    comparison = compare_findings(db, a, b)
    assert [x["estimate"] for x in comparison.reported_analyses] == [150.59]
    assert all(x["mismatch"] for x in comparison.other_analyses) and 75.48 in [x["estimate"] for x in comparison.other_analyses]


def test_c3_treatment_and_comparator_are_named_and_the_sign_is_never_flipped(db):
    high = _pick(db, SM1, arm="15 mg", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72)
    placebo = _pick(db, SM1, arm="placebo", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72)
    forward, reverse = compare_findings(db, high, placebo), compare_findings(db, placebo, high)
    for comparison, treatment_is in ((forward, "A"), (reverse, "B")):
        analysis = comparison.reported_analyses[0]
        assert analysis["estimate"] == -20.1 and (analysis["treatment_label"], analysis["comparator_label"]) == ("15 mg Tirzepatide", "Placebo")
        assert analysis["pair_orientation"]["treatment_is"] == treatment_is and analysis["pair_orientation"]["direction_established"]
        assert any("-20.1" in s and "15 mg Tirzepatide versus Placebo" in s for s in comparison.permitted_statements)
    assert forward.descriptive_difference["value"] == pytest.approx(-20.1) and reverse.descriptive_difference["value"] == pytest.approx(20.1)
    assert reverse.descriptive_difference["orientation"] == "A minus B" and reverse.reported_analyses[0]["pair_orientation"]["same_orientation_as_descriptive_difference"] is False
    assert any("not re-signed" in s for s in reverse.permitted_statements)


# ---- C4 and C7: a stored analysis is read only as far as its fields allow


def _dto(**overrides) -> dict:
    base = {"id": 0, "estimate": -2.0, "ci_low": -3.0, "ci_high": -1.0, "ci_level": 95.0, "ci_sides": "TWO_SIDED", "param_type": "LS Mean Difference", "unit": "kg",
            "treatment_label": "Drug", "comparator_label": "Placebo", "p_value_text": None, "role": "PRIMARY", "multiplicity_controlled": "YES", "hypothesis_type": "SUPERIORITY",
            "ni_margin": None, "ni_met": "NOT_STATED", "superiority_tested": "NOT_STATED", "superiority_met": "NOT_STATED", "tier": "PEER_REVIEWED", "ci_status": "REPORTED",
            "p_value_status": "NOT_REPORTED", "estimand_label": "EFFICACY", "labels": [], "source": None, "locator_quote": None, "verification_status": "QUOTE_ANCHORED",
            "direction": {"established": True, "status": "STATED_IN_CURATED_SOURCE", "basis": "stated"}}
    return {**base, **overrides}


def test_c7_null_value_follows_the_stated_scale():
    difference = interpret_analysis(_dto(estimate=0.95, ci_low=0.8, ci_high=1.1))  # a difference: 0 is outside 0.8–1.1
    assert difference["effect_scale"] == "DIFFERENCE" and difference["null_value"] == 0 and difference["ci_excludes_no_difference"] is True
    ratio = interpret_analysis(_dto(estimate=0.95, ci_low=0.8, ci_high=1.1, param_type="Hazard Ratio (HR)"))  # a ratio: 1 is inside
    assert ratio["effect_scale"] == "RATIO" and ratio["null_value"] == 1 and ratio["ci_excludes_no_difference"] is False and not ratio["establishes"]
    assert any("includes 1" in s for s in ratio["does_not_establish"])


def test_c7_bioequivalence_ratios_stored_as_differences_are_not_read_against_zero(db):
    study = db.scalar(select(Study).where(Study.nct_id == BIOEQ))
    ratios = [a for a in study.analyses if a.unit == "ratio"]
    assert len(ratios) >= 12
    for analysis in ratios:
        dto = analysis_dto(analysis)
        reading = interpret_analysis(dto)
        assert dto["effect_scale"]["scale"] == "NOT_ESTABLISHED" and dto["effect_scale"]["null_value"] is None, analysis.id  # 'difference' by name, 'ratio' by unit
        assert reading["ci_excludes_no_difference"] is None and not reading["establishes"], analysis.id
        assert not any(s.startswith("A confirmatory conclusion") for s in reading["does_not_establish"]), analysis.id  # the old reading: "the interval excludes no difference, but…"
        assert any("scale" in m for m in reading["missing"])
        assert "90% CI" in reading["observed"] if analysis.ci_low is not None else "confidence interval not reported" in reading["observed"]
    assert sum(a.ci_level == 90 for a in ratios) >= 11


def test_c4_interval_with_invalid_or_missing_level_supports_no_inference(db):
    study = db.scalar(select(Study).where(Study.nct_id == ACHIEVE3))
    bad = next(a for a in study.analyses if a.ci_level is not None and a.ci_level < 50)  # the registry record posts -3.99 as the level
    dto = analysis_dto(bad)
    reading = interpret_analysis(dto)
    assert dto["ci_level_status"] == "INVALID_LEVEL" and any(label["code"] == "CI_LEVEL_INVALID" for label in dto["labels"])
    assert reading["ci_excludes_no_difference"] is None and reading["ci_level"] is None and not reading["establishes"]
    assert "% CI" not in reading["observed"] and "not a valid percentage" in reading["observed"]
    unknown = interpret_analysis(_dto(ci_level=None))
    assert unknown["ci_excludes_no_difference"] is None and not unknown["establishes"] and "confidence level not stated" in unknown["observed"]
    assert any("confidence level" in s for s in unknown["does_not_establish"])


def test_c4_stated_confidence_level_and_sidedness_are_used():
    ninety = interpret_analysis(_dto(ci_level=90.0))
    assert "90% CI" in ninety["observed"] and ninety["ci_excludes_no_difference"] is True
    assert not ninety["establishes"] and any("90%" in s and "conventional" in s for s in ninety["does_not_establish"])
    one_sided = interpret_analysis(_dto(ci_level=97.5, ci_sides="ONE_SIDED"))
    assert "one-sided 97.5% CI" in one_sided["observed"] and not one_sided["establishes"]
    conventional = interpret_analysis(_dto())
    assert conventional["establishes"] and "95% confidence interval excludes 0" in conventional["establishes"][0]


def test_c4_direction_is_stated_only_when_established(db):
    award = db.scalar(select(Study).where(Study.nct_id == AWARD11))
    # The registry posts the groups as "1.5 mg, 3 mg" with estimate −0.17, while the arm values give 1.5 mg − 3 mg = +0.18.
    # The registry adapter now takes the comparator from the registry's arm role (1.5 mg is the ACTIVE_COMPARATOR arm), so
    # the stored contrast is 3 mg versus 1.5 mg and the arm values confirm it. Nothing was re-signed on the way.
    stored = next(a for a in award.analyses if a.endpoint.category == "HBA1C_CHANGE" and {a.treatment_label, a.comparator_label} == {"Dulaglutide 1.5 mg", "Dulaglutide 3 mg"} and a.estimate == -0.17)
    assert (stored.treatment_label, stored.comparator_label, stored.direction_basis) == ("Dulaglutide 3 mg", "Dulaglutide 1.5 mg", "REGISTRY_ARM_TYPE")
    assert stored.group_labels == ["Dulaglutide 1.5 mg", "Dulaglutide 3 mg"]  # the posted order is kept beside the assigned direction
    repaired = analysis_dto(stored)
    assert repaired["direction"]["status"] == "VERIFIED_AGAINST_ARM_VALUES" and repaired["estimate"] == -0.17
    assert interpret_analysis(repaired)["observed"].startswith("Dulaglutide 3 mg versus Dulaglutide 1.5 mg")
    # No stored analysis contradicts its arm values any more (62 did while the posted order was stored as the direction).
    statuses = {analysis_dto(a)["direction"]["status"] for study in db.scalars(select(Study)) for a in study.analyses}
    assert "CONTRADICTED_BY_ARM_VALUES" not in statuses and "VERIFIED_AGAINST_ARM_VALUES" in statuses
    # The rule itself: read in the posted order (as it used to be stored), the same record is contradicted by its arm
    # values, and then no direction is stated. Changed in memory only; nothing is flushed.
    with db.no_autoflush:
        mirrored = stored
        mirrored.treatment_label, mirrored.comparator_label = mirrored.comparator_label, mirrored.treatment_label
        mirrored.treatment_arm_id, mirrored.comparator_arm_id = mirrored.comparator_arm_id, mirrored.treatment_arm_id
        dto = analysis_dto(mirrored)
        assert dto["direction"]["status"] == "CONTRADICTED_BY_ARM_VALUES" and dto["direction"]["established"] is False
        assert any(label["code"] == "DIRECTION_NOT_ESTABLISHED" for label in dto["labels"])
        reading = interpret_analysis(dto)
        assert reading["observed"].startswith("Between Dulaglutide 1.5 mg and Dulaglutide 3 mg") and " versus " not in reading["observed"]
        assert reading["direction_established"] is False and any("Which group the estimate favours" in s for s in reading["does_not_establish"])
        assert dto["estimate"] == -0.17  # reported as stored; nothing is re-signed
    db.expire(mirrored)
    sm1 = db.scalar(select(Study).where(Study.nct_id == SM1))
    primary = next(a for a in sm1.analyses if a.origin == "REGISTRY" and a.estimate == -20.1)
    dto = analysis_dto(primary)
    assert dto["direction"]["status"] == "VERIFIED_AGAINST_ARM_VALUES" and dto["effect_scale"]["scale"] == "DIFFERENCE"
    assert interpret_analysis(dto)["observed"].startswith("15 mg Tirzepatide versus Placebo")


# ---- C5: follow-up and lead-in baselines


def test_c5_eight_weeks_more_follow_up_is_a_major_difference_for_weight(db):
    reta = _pick(db, TRIUMPH1, arm_is="retatrutide 12 mg", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=80)
    tirz = _pick(db, SM1, arm="15 mg", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=72)
    duration = _factor(compare_findings(db, reta, tirz), "duration")
    assert duration.assessment == "MAJOR_DIFFERENCE" and "8 weeks" in duration.explanation and "still changing" in duration.explanation


def test_c5_baseline_class_recognises_lead_in_run_in_and_withdrawal_designs():
    from incretinos.engines.findings import baseline_class

    assert baseline_class({}, "Baseline") == "STUDY_BASELINE" and baseline_class(None, None) == "STUDY_BASELINE"
    assert baseline_class({"comparator_type": "PLACEBO"}, "baseline (randomization, week 0)") == "STUDY_BASELINE"
    assert baseline_class({"lead_in": "12-week intensive lifestyle intervention before randomization"}, "Baseline") == "LEAD_IN_DESIGN_BASELINE_UNSPECIFIED"
    assert baseline_class({"run_in": "4-week placebo run-in"}, None) == "LEAD_IN_DESIGN_BASELINE_UNSPECIFIED"
    assert baseline_class({"randomized_withdrawal": True}, "Randomization (Week 36)") == "RANDOMIZATION_AFTER_LEAD_IN"
    assert baseline_class({"randomized_withdrawal": True}, "Baseline (Week 0)") == "STUDY_ENTRY_INCLUDING_LEAD_IN"
    assert baseline_class({"lead_in": "x"}, "randomization") == "RANDOMIZATION_AFTER_LEAD_IN"
    assert baseline_class({"lead_in": "12-week lifestyle lead-in"}, "baseline (randomization, week 0)") == "RANDOMIZATION_AFTER_LEAD_IN"
    assert baseline_class({"lead_in": "12-week lifestyle lead-in"}, "Baseline (Week 0)") == "LEAD_IN_DESIGN_BASELINE_UNSPECIFIED"  # week 0 may be the randomization here
    assert baseline_class({"randomized_withdrawal": True}, "study entry (week 0)") == "STUDY_ENTRY_INCLUDING_LEAD_IN"
    assert baseline_class({}, "Randomization (Week 36)") == "RANDOMIZATION_AFTER_LEAD_IN"  # stated in the text even without a curated design feature
    assert baseline_class({}, "baseline (week 0) of the extension subset") == "SUBSET_BASELINE"


def test_c5_change_after_a_lead_in_is_never_differenced_with_a_change_from_study_baseline(db):
    after_lead_in = _pick(db, SM3, arm="Tirzepatide")
    plain = _pick(db, SM1, arm="15 mg", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72)
    assert after_lead_in.baseline_class == "LEAD_IN_DESIGN_BASELINE_UNSPECIFIED" and plain.baseline_class == "STUDY_BASELINE"
    assert any(label["code"] == "LEAD_IN_DESIGN" for label in after_lead_in.labels)
    comparison = compare_findings(db, after_lead_in, plain)
    assert comparison.classification == "NOT SUITABLE FOR EFFICACY COMPARISON" and comparison.descriptive_difference is None
    assert _factor(comparison, "baseline").assessment == "BLOCKING"
    within = compare_findings(db, after_lead_in, _pick(db, SM3, arm="Placebo"))  # the trial's own randomized contrast is unaffected
    assert within.classification == "DIRECT RANDOMIZED COMPARISON" and _factor(within, "baseline").assessment == "MATCH"


def test_c5_no_difference_is_formed_across_a_baseline_that_is_not_described(db):
    orfo = _pick(db, ATTAIN1, arm="36 mg once daily", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=72)  # the curated row does not describe its baseline
    tirz = _pick(db, SM1, arm="15 mg", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=72)
    assert orfo.baseline_definition is None and tirz.baseline_definition is not None
    comparison = compare_findings(db, orfo, tirz)
    assert _factor(comparison, "baseline").assessment == "UNKNOWN" and comparison.classification == "DESCRIPTIVE CROSS-STUDY COMPARISON"
    assert comparison.descriptive_difference is None and any(r.startswith("No arithmetic difference is given") and "baseline" in r for r in comparison.reasons)


# ---- C6: unknown is unknown


def test_c6_unknown_population_factors_are_not_called_similar(db):
    a = _pick(db, ACHIEVE1, arm="36 mg", weeks=40)
    b = _pick(db, ACHIEVE5, arm="36 mg", weeks=40)
    comparison = compare_findings(db, a, b)
    assert {_factor(comparison, k).assessment for k in ("baseline_weight", "baseline_bmi", "age")} == {"UNKNOWN"}
    assert comparison.classification == "DESCRIPTIVE CROSS-STUDY COMPARISON"  # was PARTIALLY COMPARABLE with "similar … populations"
    assert any("Not established as similar" in r and "mean age" in r for r in comparison.reasons)
    assert not any("similar" in s for s in comparison.permitted_statements)
    dose = _factor(comparison, "dose")  # orforglipron has two formulations on record and neither arm records which
    assert dose.assessment == "UNKNOWN" and "more than one formulation" in dose.explanation


def test_c6_partially_comparable_states_what_matched_and_what_is_unknown(db):
    orfo = _pick(db, ATTAIN1, arm="36 mg", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72)
    tirz = _pick(db, SM1, arm="15 mg", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72)
    comparison = compare_findings(db, orfo, tirz)
    assert comparison.classification == "PARTIALLY COMPARABLE"
    text = " ".join(comparison.permitted_statements)
    assert "similar endpoint definitions, estimands, populations" not in text
    assert "match on the stored evidence:" in text and "mean baseline body weight" in text and "mean age" in text
    assert "not established as similar" in text and "mean baseline bmi" in text.split("not established as similar")[1]
    assert not guard_statement(text, cross_trial=True)
    # Remove the stored population summary of one trial: the same pair can no longer be called partially comparable.
    orfo.population_summary = {}
    stripped = compare_findings(db, orfo, tirz)
    assert stripped.classification == "DESCRIPTIVE CROSS-STUDY COMPARISON" and any("body size" in r for r in stripped.reasons)


# ---- C8: the language guard


@pytest.mark.parametrize(
    "text",
    [
        "Retatrutide is superior to tirzepatide.",
        "Without doubt, retatrutide is superior to tirzepatide.",
        "There is no question that retatrutide is superior to tirzepatide.",
        "Retatrutide was not only effective but significantly greater in weight loss than tirzepatide.",
        "Retatrutide achieved larger weight loss than tirzepatide (p<0.05).",
        "Retatrutide reduced weight more than tirzepatide and the difference was significant.",
        "Retatrutide is clearly the stronger drug, exceeding tirzepatide by 4.1 percentage points.",
        "The difference between retatrutide and tirzepatide was statistically significant.",
        "Retatrutide outperformed tirzepatide.",
        "Retatrutide is more effective than tirzepatide.",
        "Retatrutide is better than tirzepatide for weight loss.",
        "Although the trials were not head-to-head, retatrutide is superior to tirzepatide.",
        "It is no surprise that retatrutide is superior to tirzepatide.",
        "Retatrutide is superior to tirzepatide and was not associated with more nausea.",
        "The weight loss was significantly larger with retatrutide.",
    ],
)
def test_c8_cross_trial_claims_are_flagged_whatever_negation_word_stands_nearby(text):
    assert guard_statement(text, cross_trial=True), text


@pytest.mark.parametrize(
    "text",
    [
        "Superiority was not demonstrated.",
        "The difference must not be described as statistically significant; no valid test supports it.",
        "Do not state or imply that retatrutide 12 mg is superior (or inferior) to tirzepatide 15 mg on the basis of these two findings.",
        "No statistically significant difference can be claimed across trials.",
        "These values do not show that retatrutide is superior to tirzepatide.",
        "There is no evidence that retatrutide is superior to tirzepatide.",
        "It is not known whether retatrutide is more effective than tirzepatide.",
        "Retatrutide is not superior, better, or more effective than tirzepatide on this evidence.",
        "Neither superiority nor a statistically significant difference may be claimed.",
        "A significant difference between the two drugs was not observed.",
        "The difference was non-significant.",
        "The two values may be tabulated side by side with their study context and the methodological differences listed here.",
        "ACHIEVE-4 reported the incidence of clinically significant or severe hypoglycaemia.",
    ],
)
def test_c8_honest_negative_statements_are_not_flagged(text):
    assert not guard_statement(text, cross_trial=True), text


def test_c8_guard_covers_noninferiority_and_keeps_earlier_behaviour():
    ni = {"hypothesis_type": "NON_INFERIORITY", "superiority_met": "NOT_STATED"}
    assert guard_statement("Orforglipron outperformed insulin glargine on MACE-4.", **ni)
    assert guard_statement("Orforglipron was more effective than insulin glargine.", **ni)
    assert not guard_statement("Orforglipron was non-inferior to insulin glargine; superiority was not demonstrated.", **ni)
    assert not guard_statement("Report counts cannot be read as incidence and do not show that the drug causes the event.", data_family="SPONTANEOUS")
    assert not guard_statement("DXA lean mass decreased; this is not a direct measure of muscle loss.", lean_mass=True)
    assert guard_statement("Participants lost muscle on tirzepatide, without doubt.", lean_mass=True)


def test_c8_statements_the_engine_itself_permits_pass_the_guard(db, client):
    for preset in client.get("/api/compare/presets").json():
        body = client.post("/api/compare", json={"a": preset["a"], "b": preset["b"]}).json()["comparison"]
        if body["classification"].startswith("DIRECT"):
            continue  # a within-trial analysis may legitimately report a p-value
        assert not guard_statement(" ".join(body["permitted_statements"]), cross_trial=True), preset["title"]


# ---- WEB-12: worked comparisons are resolved by description and checked against their titles


def test_web12_presets_are_two_different_results_and_are_what_their_titles_say(db, client):
    presets = {p["title"]: p for p in client.get("/api/compare/presets").json()}
    assert len(presets) == 8 and all(p["a"] != p["b"] and p["verified"] for p in presets.values())
    elora = presets["EloraTZP 9 mg + 15 mg vs tirzepatide 15 mg within the Phase 2b trial"]
    assert (elora["resolved"]["a"]["arm"], elora["resolved"]["b"]["arm"]) == ("eloralintide 9 mg + tirzepatide 15 mg", "tirzepatide 15 mg")
    assert elora["relationship"] == "RANDOMIZED_ARMS_OF_ONE_TRIAL" and elora["classification"] == "DIRECT RANDOMIZED COMPARISON"
    expected = {
        "Tirzepatide 15 mg vs placebo within SURMOUNT-1": ("DIRECT RANDOMIZED COMPARISON", "RANDOMIZED_ARMS_OF_ONE_TRIAL"),
        "Retatrutide 12 mg (TRIUMPH-1) vs tirzepatide 15 mg (SURMOUNT-1)": ("DESCRIPTIVE CROSS-STUDY COMPARISON", "DIFFERENT_TRIALS"),
        "ATTAIN-1 36 mg: efficacy estimand vs treatment-regimen estimand": ("NOT SUITABLE FOR EFFICACY COMPARISON", "SAME_ARM_TWO_REPORTS"),
        "Orforglipron 36 mg (ATTAIN-1) vs tirzepatide 15 mg (SURMOUNT-1)": ("PARTIALLY COMPARABLE", "DIFFERENT_TRIALS"),
        "SURMOUNT-4 (from randomization at week 36) vs SURMOUNT-1 (from baseline)": ("NOT SUITABLE FOR EFFICACY COMPARISON", "DIFFERENT_TRIALS"),
        "EloraTZP Phase 2b (T2D, 48 weeks) vs tirzepatide 15 mg in SURMOUNT-1 (no T2D, 72 weeks)": ("NOT SUITABLE FOR EFFICACY COMPARISON", "DIFFERENT_TRIALS"),
        "Tirzepatide vs semaglutide within SURMOUNT-5": ("DIRECT RANDOMIZED COMPARISON", "RANDOMIZED_ARMS_OF_ONE_TRIAL"),
    }
    for title, (classification, relationship) in expected.items():
        assert (presets[title]["classification"], presets[title]["relationship"]) == (classification, relationship), title
    for p in presets.values():  # every resolved pair is re-read from the database and matches what the preset says it resolved to
        for side in ("a", "b"):
            finding = get_finding(db, p[side])
            described = p["resolved"][side]
            assert (finding.study_name, finding.arm_label, finding.estimand_label, finding.timepoint_weeks, finding.tier) == (
                described["study"], described["arm"], described["estimand"], described["timepoint_weeks"], described["tier"]), p["title"]
            assert finding.study_name.split()[0] in p["title"] or finding.study_name in p["title"], p["title"]
    assert client.post("/api/compare", json={"a": elora["a"], "b": elora["a"]}).status_code == 422  # a result beside itself is refused


def test_web12_a_preset_that_is_no_longer_what_its_title_says_is_not_offered(db):
    from incretinos.api.main import PRESETS, _resolve_preset

    spec = next(s for s in PRESETS if s.title.startswith("EloraTZP 9 mg + 15 mg vs tirzepatide 15 mg"))
    assert _resolve_preset(db, spec) is not None
    study = db.scalar(select(Study).where(Study.nct_id == "NCT06603571"))
    for r in study.results:  # the trial's tirzepatide-alone arm no longer has an efficacy-estimand weight result
        if r.arm is not None and r.arm.drug_slug == "tirzepatide" and r.endpoint.category == "WEIGHT_PCT_CHANGE":
            r.value = None
    db.flush()
    assert _resolve_preset(db, spec) is None  # not silently replaced by the combination arm (the old substring match did that)


# ---- XS-12: one display rule, no winner by tier


def test_xs12_display_rule_never_looks_at_the_evidence_tier():
    from incretinos.engines.findings import DISPLAY_RULE, display_lead, display_order

    label = {"id": 3, "tier": "REGULATORY_LABEL", "ci_low": None, "ci_high": None, "dispersion_value": None, "n_analyzed": None, "verification_status": "QUOTE_ANCHORED"}
    release = {"id": 1, "tier": "PRESS_RELEASE", "ci_low": None, "ci_high": None, "dispersion_value": None, "n_analyzed": None, "verification_status": "QUOTE_ANCHORED"}
    article = {"id": 2, "tier": "PEER_REVIEWED", "ci_low": -12.0, "ci_high": -10.4, "dispersion_value": None, "n_analyzed": None, "verification_status": "PENDING_HUMAN_REVIEW"}
    assert [x["id"] for x in display_order([label, release, article])] == [2, 1, 3]  # reported interval first, then the record stored first
    for tiers in (("REGULATORY_LABEL", "PRESS_RELEASE"), ("PRESS_RELEASE", "REGULATORY_LABEL")):  # swapping tiers changes nothing
        assert [x["id"] for x in display_order([{**label, "tier": tiers[0]}, {**release, "tier": tiers[1]}])] == [1, 3]
    rejected = {**article, "id": 9, "verification_status": "REJECTED"}
    assert display_lead([rejected, label]) == (label, []) and display_lead([rejected]) == (None, [])
    assert "Display choice, not a judgement of correctness" in DISPLAY_RULE and "Evidence tier is not used" in DISPLAY_RULE


def test_xs12_every_source_of_one_quantity_is_returned_with_its_tier(db, client):
    from incretinos.engines import benefit_risk
    from incretinos.engines.findings import DISPLAY_RULE

    profiles = benefit_risk.arm_profiles(db, ATTAIN1, estimand="TREATMENT_REGIMEN")
    top = next(a for a in profiles["arms"] if a["drug_slug"] == "orforglipron" and a["dose_amount"] == 36)
    entry = top["efficacy"]["weight_pct"]
    sources = [entry, *entry["other_sources"]]
    assert {s["tier"] for s in sources} == {"PEER_REVIEWED", "REGULATORY_REVIEW", "REGULATORY_LABEL"}  # journal, FDA review and label all returned
    assert entry["ci_low"] is not None and all(o["ci_low"] is None for o in entry["other_sources"])  # the lead is the one with a reported interval
    assert entry["sources_report_same_value"] is False and sorted({s["value"] for s in sources}) == [-11.2, -11.1]
    assert entry["display_choice"] == DISPLAY_RULE and entry["alternatives"] == 2
    body = client.get(f"/api/estimands?study={ATTAIN1}").json()
    assert body["display_rule"] == DISPLAY_RULE
    cell = next(r for r in body["rows"] if r["category"] == "WEIGHT_PCT_CHANGE" and r["arm"] == "orforglipron 36 mg" and r["timepoint_weeks"] == 72 and "TREATMENT_REGIMEN" in r["by_estimand"])
    assert len(cell["by_estimand"]["TREATMENT_REGIMEN"]) >= 2  # all sources stay listed in the cell
    assert cell["display_lead"]["TREATMENT_REGIMEN"] == entry["result_id"]  # and the chart lead is the same record on both surfaces


# ---- Evidence a reviewer rejected takes no part in comparisons, profiles or worked examples


def test_rejected_evidence_is_excluded_from_comparisons_and_profiles(db):
    from incretinos.api.main import PRESETS, _resolve_preset
    from incretinos.engines import benefit_risk

    high = _pick(db, SM1, arm="15 mg", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72)
    placebo = _pick(db, SM1, arm="placebo", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72)
    assert compare_findings(db, high, placebo).classification == "DIRECT RANDOMIZED COMPARISON"
    row = db.get(OutcomeResult, high.result_id)
    # A rejection exists only as the outcome of an attested review decision: the ORM refuses to assign the status
    # (models.HumanStatusWriteError), so the decision is recorded through the review lifecycle. Nothing is committed;
    # the session fixture rolls it back.
    with pytest.raises(HumanStatusWriteError):
        row.verification_status = "REJECTED"
    recorded = record_decision(db, evidence_type="result", row_id=row.id, decision="REJECT", reviewer="Test Reviewer", attested=True, expected_fingerprint=row.fingerprint,
                               rationale="The registry value belongs to another analysis population than the row states.")
    assert recorded.status_after == "REJECTED"
    # The comparison engine's own check: a finding built from the rejected row (this session still holds the object,
    # as inspection code would) is refused.
    rejected_finding = get_finding(db, high.result_id)
    assert rejected_finding is not None and rejected_finding.verification_status == "REJECTED"
    rejected = compare_findings(db, rejected_finding, placebo)
    assert rejected.classification == "NOT SUITABLE FOR EFFICACY COMPARISON" and rejected.descriptive_difference is None
    assert _factor(rejected, "review_status").assessment == "BLOCKING"
    profiles = benefit_risk.arm_profiles(db, SM1, estimand="EFFICACY")
    assert "weight_pct" not in next(a for a in profiles["arms"] if a["key"] == "tirzepatide|15|")["efficacy"]  # no other efficacy-estimand value is substituted
    assert _resolve_preset(db, next(s for s in PRESETS if s.title == "Tirzepatide 15 mg vs placebo within SURMOUNT-1")) is None
    # And the session-level rule in front of it: once the session no longer holds the object, analytic code cannot
    # load the row at all; only code that opts in to see rejected evidence can.
    db.expunge_all()
    assert get_finding(db, high.result_id) is None
    with rejected_visible(db):
        assert get_finding(db, high.result_id).verification_status == "REJECTED"
