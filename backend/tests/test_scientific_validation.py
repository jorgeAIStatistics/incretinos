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
    LabelRecord,
    OutcomeResult,
    StatisticalAnalysis,
    Study,
)

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
