"""Data integrity: stored evidence sits where its own source text says it belongs.

These tests exist because of defects found in the Session 2 audit — values that were all "in their quote"
and still wrong: ≥10/15/20 % responder values under a "≥5 %" title, fat mass under lean mass, a contrast stored
in the reverse of the direction the registry states, two arms merged into one, a placebo arm attributed to a
drug, mg/kg stored as mg, most posted serious adverse-event terms dropped. Each test fails on the old behaviour.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from incretinos.config import get_settings
from incretinos.curation import loader
from incretinos.curation.loader import CurationError
from incretinos.curation.schema import (
    AdverseEventDef,
    AnalysisDef,
    EndpointDef,
    EstimandDef,
    ResultDef,
    StudyEvidenceFile,
    ae_kind_for_term,
    number_in_quote,
    number_tokens,
    quote_numbers,
)
from incretinos.db import Base, make_engine
from incretinos.engines.benefit_risk import arm_profiles
from incretinos.engines.findings import analysis_dto, build_finding
from incretinos.engines.interpretation import interpret_analysis
from incretinos.ingestion.ctgov import (
    CTGovAdapter,
    analysis_class,
    multiplicity_from_comment,
    stated_direction,
)
from incretinos.ingestion.openfda import OpenFDALabelAdapter
from incretinos.ingestion.transport import FixtureTransport, TransportError
from incretinos.models import (
    AdverseEvent,
    Endpoint,
    Estimand,
    EvidenceSource,
    OutcomeResult,
    ParticipantFlow,
    StatisticalAnalysis,
    Study,
    StudyArm,
)
from incretinos.vocab import (
    DirectionBasis,
    arm_drug_slug,
    arm_identity_key,
    categorize_endpoint,
    match_arm_label,
    norm_arm_label,
    parse_single_dose,
    parse_single_dose_mg,
    parse_timeframe,
    percent_thresholds,
    to_float,
)

from .conftest import FIXTURES

BACKEND = Path(__file__).resolve().parent.parent
NOTES_DIR = BACKEND.parent / "research" / "web-notes"


def _raw(nct: str) -> dict:
    return json.loads((FIXTURES / "ctgov" / "studies" / f"{nct}.json").read_text())


def _study(db, nct: str) -> Study:
    study = db.scalar(select(Study).where(Study.nct_id == nct))
    assert study is not None, f"{nct} not seeded"
    return study


@pytest.fixture(scope="module")
def converter():
    """The notes converter, imported from scripts/ (it is a script, not a package module)."""
    spec = importlib.util.spec_from_file_location("notes_to_curated", BACKEND / "scripts" / "notes_to_curated.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["notes_to_curated"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def converted(converter) -> dict:
    return converter.convert()


@pytest.fixture()
def scratch(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'scratch.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


# --------------------------------------------------------------------------- curated responders (CUR-04, ING-01, XS-01)


def test_no_curated_responder_row_names_another_threshold_than_its_endpoint(db):
    rows = db.execute(
        select(OutcomeResult, Endpoint).join(Endpoint, OutcomeResult.endpoint_id == Endpoint.id).where(OutcomeResult.origin == "CURATED", Endpoint.category == "WEIGHT_RESPONDER")
    ).all()
    assert len(rows) >= 80, "expected the curated responder rows of ATTAIN-1 and ATTAIN-2"
    named_in_quote = 0
    for result, endpoint in rows:
        assert endpoint.threshold is not None, (result.id, endpoint.title)
        assert percent_thresholds(endpoint.title) == {endpoint.threshold}, endpoint.title
        named = percent_thresholds(result.locator_quote)
        if named:
            named_in_quote += 1
            assert endpoint.threshold in named, (result.id, result.value, endpoint.title, result.locator_quote)
    assert named_in_quote >= 24  # the FDA-label rows print their threshold in the quote


def test_fda_label_responder_values_sit_under_their_own_threshold(db):
    """ATTAIN-1, FOUNDAYO 17.2 mg: 71.5 / 54.5 / 35.9 / 18.4 % are ≥5 / ≥10 / ≥15 / ≥20 %, not four values of "≥5 %"."""
    study = _study(db, "NCT05869903")
    label = {
        r.endpoint.threshold: r.value
        for r in study.results
        if r.origin == "CURATED" and r.endpoint.category == "WEIGHT_RESPONDER" and r.source.key == "openfda-label:foundayo" and r.arm_label.startswith("FOUNDAYO 17.2 mg")
    }
    assert label == {5.0: 71.5, 10.0: 54.5, 15.0: 35.9, 20.0: 18.4}
    finding = build_finding(db, next(r for r in study.results if r.origin == "CURATED" and r.value == 18.4 and r.source.key == "openfda-label:foundayo"))
    assert "≥20% weight reduction of 18.4%" in finding.statement and "≥5%" not in finding.statement


def test_one_endpoint_arm_estimand_source_and_time_has_one_curated_value(db):
    groups: dict[tuple, set] = defaultdict(set)
    for r in db.scalars(select(OutcomeResult).where(OutcomeResult.origin == "CURATED", OutcomeResult.scope == "ARM")):
        groups[(r.study_id, r.endpoint_id, r.arm_id, r.estimand_id, r.source_id, r.timepoint_weeks, r.class_title, r.baseline_definition)].add(r.value)
    assert not {k: v for k, v in groups.items() if len(v) > 1}


def test_every_curated_row_sits_under_the_endpoint_its_research_note_names(db, converter):
    """Replays the audit's measurement: join each curated row to its research note; the endpoint must be the note's."""
    manual = {s["url"]: s["key"] for s in yaml.safe_load((converter.DATA_DIR / "sources.yaml").read_text())["sources"] if s.get("url")}
    notes: dict[tuple, set[str]] = defaultdict(set)
    for path in sorted(NOTES_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        sources = {s["id"]: s for s in data.get("sources", [])}
        for f in data.get("findings", []):
            src = sources.get(f.get("source_id"))
            key = converter.source_key(src, manual) if src else None
            if key and f.get("nct"):
                value = f.get("value") if isinstance(f.get("value"), int | float) else None
                notes[(f["nct"], (f.get("arm") or "").strip(), value, f.get("range_low"), f.get("range_high"), (f.get("locator_quote") or "").strip(), key)].add((f.get("endpoint") or "").strip()[:300])
    matched = 0
    for r in db.scalars(select(OutcomeResult).where(OutcomeResult.origin == "CURATED")):
        titles = notes.get((r.study.nct_id, r.arm_label, r.value, r.value_low, r.value_high, r.locator_quote, r.source.key))
        if titles:
            matched += 1
            assert r.endpoint.title in titles, (r.id, r.value, r.endpoint.title, titles)
    assert matched >= 240


def test_body_composition_compartments_are_not_mixed(db):
    study = _study(db, "NCT05616013")  # BELIEVE
    rows = [r for r in study.results if r.origin == "CURATED" and r.endpoint.category == "BODY_COMPOSITION"]
    fat = [r for r in rows if "fat mass" in r.endpoint.title.lower()]
    lean = [r for r in rows if "lean mass" in r.endpoint.title.lower()]
    assert fat and lean and not {r.endpoint_id for r in fat} & {r.endpoint_id for r in lean}
    # The fat-mass range (-33.7 % to -24.7 %) used to be rendered as a lean-mass result with the lean-mass caveat.
    fat_range = next(r for r in rows if r.value_low == -33.7 and r.value_high == -24.7)
    assert "fat mass" in fat_range.endpoint.title.lower() and "lean" not in fat_range.endpoint.title.lower()
    finding = build_finding(db, fat_range)
    assert not finding.is_body_composition_lean and not any(label["code"] == "LEAN_MASS" for label in finding.labels)
    for r in lean:
        assert build_finding(db, r).is_body_composition_lean


def test_pk_ratios_keep_auc_and_cmax_apart(db):
    study = _study(db, "NCT06440980")
    curated = [a for a in study.analyses if a.origin == "CURATED"]
    auc = [a for a in curated if "AUC" in a.endpoint.title]
    cmax = [a for a in curated if "Cmax" in a.endpoint.title]
    assert len(auc) == 6 and len(cmax) == 6 and not {a.endpoint_id for a in auc} & {a.endpoint_id for a in cmax}
    # CUR-11: the interval "(0.9560, 1.0705)" is in the quote and is stored (it was dropped as "not in the quote").
    restored = next(a for a in cmax if a.estimate == 1.01 and "1.01 (0.9560, 1.0705)" in a.locator_quote)
    assert (restored.ci_low, restored.ci_high, restored.ci_level) == (0.956, 1.0705, 90)
    assert "1.01 (0.9560, 1.0705)" in restored.locator_quote and "not entered" not in (restored.notes or "")


def test_surmount4_exploratory_whole_study_values_are_not_under_the_primary_endpoint(db):
    study = _study(db, "NCT04660643")
    entry = next(r for r in study.results if r.origin == "CURATED" and r.value == -26.0)
    assert entry.endpoint.title == "percent change in body weight from study entry" and entry.endpoint.role == "EXPLORATORY"


# --------------------------------------------------------------------------- converter


def test_converter_gives_distinct_endpoints_distinct_keys(converter):
    def key(text: str, unit: str = "% of participants", weeks: float = 72) -> str:
        return converter.endpoint_key(converter.endpoint_definition(text, unit, unit, weeks, "KEY_SECONDARY", "YES", 50.0))

    responder = [key(f"proportion of participants with >={t}% body-weight reduction") for t in (5, 10, 15, 20)]
    assert len(set(responder)) == 4 and all(f"ge{t}pct" in k for t, k in zip((5, 10, 15, 20), responder, strict=True))
    assert key("percent change in total body lean mass by DXA (LS mean)", "%", 48) != key("percent change in total body fat mass by DXA (LS mean)", "%", 48)
    assert key("percent change in body weight from randomization", "%", 88) != key("percent change in body weight from study entry", "%", 88)
    assert key("ratio of geometric LS means, steady-state AUC over dosing interval (tablet/capsule)", "ratio", None) != key(
        "ratio of geometric LS means, steady-state Cmax (tablet/capsule)", "ratio", None
    )
    # symbols that a slug throws away still separate endpoints
    assert key("participants with HbA1c <7%") != key("participants with HbA1c ≤7%")


def test_converter_raises_on_a_key_collision(converter):
    endpoints: dict[str, dict] = {}
    five = converter.endpoint_definition("proportion of participants with >=5% body-weight reduction", "% of participants", "% of participants", 72, "KEY_SECONDARY", "YES", 60.0)
    twenty = converter.endpoint_definition("proportion of participants with >=20% body-weight reduction", "% of participants", "% of participants", 72, "KEY_SECONDARY", "NO", 6.4)
    converter.register_endpoint(endpoints, "weight-responder-w72", five)
    converter.register_endpoint(endpoints, "weight-responder-w72", five)  # the same endpoint again is not a collision
    with pytest.raises(converter.EndpointKeyCollision, match="two different endpoints"):
        converter.register_endpoint(endpoints, "weight-responder-w72", twenty)
    assert endpoints["weight-responder-w72"]["threshold"] == 5.0  # the first endpoint was not overwritten


def test_converter_fails_loudly_when_its_key_function_folds_endpoints(converter, monkeypatch):
    """The whole conversion stops — nothing is written — if two endpoints of a study would share a key."""
    monkeypatch.setattr(converter, "endpoint_key", lambda definition: converter.slug(f"{definition['category']}-w{definition.get('timepoint_weeks')}", 80))
    with pytest.raises(converter.EndpointKeyCollision):
        converter.convert()


def test_generated_files_are_what_the_converter_produces(converter, converted):
    """The committed generated files are in sync with the notes and the converter: no hand edit, no stale file."""
    studies = converter.DATA_DIR / "studies"
    on_disk = {p.name.split(".")[0] for p in studies.glob("*.generated.yaml")}
    assert on_disk == set(converted["documents"])
    for nct, doc in converted["documents"].items():
        assert yaml.safe_load((studies / f"{nct}.generated.yaml").read_text()) == doc, nct
    assert yaml.safe_load((converter.DATA_DIR / "claims.yaml").read_text()) == {"claims": converted["claims"]}
    assert yaml.safe_load((converter.DATA_DIR / "sources.research.yaml").read_text()) == {"sources": converted["sources"]}
    for doc in converted["documents"].values():
        keys = [e["key"] for e in doc["endpoints"]]
        assert len(keys) == len(set(keys))
        identities = [converter.endpoint_identity(e) for e in doc["endpoints"]]
        assert len(identities) == len(set(identities))


def test_converter_rejects_what_the_source_text_does_not_settle(converted):
    rejected = "\n".join(converted["rejected"])
    # ACHIEVE-3 press release: "14.6 lbs (6.7%; 12 mg) ..." prints no minus sign and no loss wording.
    for finding in ("F389", "F391", "F393", "F395"):
        assert f"orforglipron:{finding}: result rejected — sign not established by the quote" in rejected
    achieve3 = converted["documents"]["NCT06045221"]["results"]
    assert not [r for r in achieve3 if r["source"].startswith("web:eli-lilly-an-2025-09-17") and r.get("value") in (-6.7, -9.2, -3.7, -5.3) and "lbs (" in r["quote"]]
    # "lost an average of 64.4 lbs (25.9%)" states the loss in words: kept, and the row says how its sign is known.
    triumph = next(r for r in converted["documents"]["NCT05929066"]["results"] if r.get("value") == -25.9)
    assert triumph["sign_basis"] == "DIRECTION_WORDING" and "without a minus sign" in triumph["notes"]


# --------------------------------------------------------------------------- schema validation


BASE_RESULT = {"endpoint": "e", "arm": "tirzepatide 15 mg", "source": "pubmed:1"}


def test_number_tokens_handles_trailing_zeros():
    assert "0.9560" in number_tokens(0.956) and "0.956" in number_tokens(0.956)
    assert "1.20" in number_tokens(1.2) and "1.0705" in number_tokens(1.0705)
    assert number_in_quote(0.956, "24 mg 14.5 mg 0.98 (0.9316, 1.0217) 1.01 (0.9560, 1.0705)")
    assert number_in_quote(1.0705, "(0.9560, 1.0705)") and number_in_quote(1.2, "95% CI 0·59-1·20") and number_in_quote(2539, "2,539 participants")
    assert not number_in_quote(0.956, "0.95601") and not number_in_quote(12.4, "112.4 kg") and not number_in_quote(12.4, "12.45%")
    AnalysisDef.model_validate(
        {"endpoint": "e", "treatment": "tablet 14.5 mg", "comparator": "capsule 24 mg", "param_type": "Ratio of geometric LS means", "estimate": 1.01, "ci_low": 0.956, "ci_high": 1.0705, "source": "pubmed:1", "quote": "1.01 (0.9560, 1.0705)"}
    )


def test_sign_must_be_the_sign_the_quote_prints():
    assert quote_numbers("−5.5% vs 14.0% (95% CI, −21.2% to −17.7%); HR 0.59-1.20; GLP-1") == [(5.5, -1), (14.0, 1), (95.0, 1), (21.2, -1), (17.7, -1), (0.59, 1), (1.2, 1), (1.0, 1)]
    ResultDef.model_validate({**BASE_RESULT, "value": -20.9, "quote": "was −20.9% with 15 mg"})
    ResultDef.model_validate({**BASE_RESULT, "value": 14.0, "quote": "-5.5% with tirzepatide vs 14.0% with placebo"})
    with pytest.raises(ValidationError, match="stored as negative but its quote prints 6.7 without a minus sign"):
        ResultDef.model_validate({**BASE_RESULT, "value": -6.7, "quote": "14.6 lbs (6.7%; 12 mg)"})
    with pytest.raises(ValidationError, match="needs a quote that states the loss or reduction in words"):
        ResultDef.model_validate({**BASE_RESULT, "value": -6.7, "quote": "14.6 lbs (6.7%; 12 mg)", "sign_basis": "DIRECTION_WORDING"})
    ResultDef.model_validate({**BASE_RESULT, "value": -25.9, "quote": "lost an average of 64.4 lbs (25.9%)", "sign_basis": "DIRECTION_WORDING"})
    with pytest.raises(ValidationError, match="stored as positive but its quote prints it with a minus sign"):
        ResultDef.model_validate({**BASE_RESULT, "value": 20.9, "quote": "was −20.9% with 15 mg"})
    with pytest.raises(ValidationError, match="a ratio is positive"):
        AnalysisDef.model_validate({"endpoint": "e", "treatment": "a", "comparator": "b", "param_type": "Hazard ratio", "estimate": -0.84, "source": "pubmed:1", "quote": "HR -0.84"})


def test_a_confidence_interval_must_be_the_interval_of_its_value():
    quote = "-5.5% (95% CI, -6.1 to -4.9) with 5 mg and -9.0% (95% CI, -10.0 to -8.1) with 15 mg"
    ResultDef.model_validate({**BASE_RESULT, "value": -9.0, "ci_low": -10.0, "ci_high": -8.1, "quote": quote})
    with pytest.raises(ValidationError, match="lies outside its confidence interval"):  # the other arm's interval
        ResultDef.model_validate({**BASE_RESULT, "value": -9.0, "ci_low": -6.1, "ci_high": -4.9, "quote": quote})
    with pytest.raises(ValidationError, match="out of order"):
        ResultDef.model_validate({**BASE_RESULT, "value": -9.0, "ci_low": -8.1, "ci_high": -10.0, "quote": quote})
    with pytest.raises(ValidationError, match="lies outside its confidence interval"):
        AnalysisDef.model_validate({"endpoint": "e", "treatment": "a", "comparator": "b", "param_type": "Hazard ratio", "estimate": 0.43, "ci_low": 0.59, "ci_high": 1.20, "source": "pubmed:1", "quote": "HR 0.84 (0.59 to 1.20); HR 0.43 (0.25 to 0.75)"})


def test_adverse_event_measure_kind_must_agree_with_its_term():
    assert ae_kind_for_term("incidence of dysesthesia (adverse event incidence; NOT discontinuation)").value == "PARTICIPANTS_WITH_EVENT"
    assert ae_kind_for_term("discontinuation of study treatment for any reason (NOT an adverse-event incidence)").value == "TREATMENT_DISCONTINUATION_ANY_REASON"
    assert ae_kind_for_term("treatment discontinuation due to adverse events (percent of participants)").value == "TREATMENT_DISCONTINUATION_DUE_TO_AE"
    row = {"arm": "retatrutide 12 mg", "term": "dysesthesia", "percent": 20.9, "source": "pubmed:1", "quote": "Dysesthesia occurred in 8.8% and 20.9%"}
    AdverseEventDef.model_validate({**row, "category": "OTHER", "measure_kind": "PARTICIPANTS_WITH_EVENT"})
    with pytest.raises(ValidationError, match="does not describe a discontinuation"):
        AdverseEventDef.model_validate({**row, "category": "DISCONTINUATION", "measure_kind": "TREATMENT_DISCONTINUATION_DUE_TO_AE"})
    any_reason = {"arm": "placebo", "term": "discontinuation of study treatment for any reason", "percent": 29.9, "source": "pubmed:1", "quote": "vs. 29.9% with placebo", "category": "DISCONTINUATION"}
    AdverseEventDef.model_validate({**any_reason, "measure_kind": "TREATMENT_DISCONTINUATION_ANY_REASON"})
    with pytest.raises(ValidationError, match="says any reason"):
        AdverseEventDef.model_validate({**any_reason, "measure_kind": "TREATMENT_DISCONTINUATION_DUE_TO_AE"})
    with pytest.raises(ValidationError, match="describes a discontinuation but the measure kind is an event incidence"):
        AdverseEventDef.model_validate({**any_reason, "category": "OTHER", "measure_kind": "PARTICIPANTS_WITH_EVENT"})
    with pytest.raises(ValidationError, match="needs category DISCONTINUATION"):
        AdverseEventDef.model_validate({**any_reason, "category": "OTHER", "measure_kind": "TREATMENT_DISCONTINUATION_ANY_REASON"})
    with pytest.raises(ValidationError, match="not a percentage of participants"):
        AdverseEventDef.model_validate({**row, "percent": 120.9, "quote": "120.9", "category": "OTHER", "measure_kind": "PARTICIPANTS_WITH_EVENT"})


def test_curated_adverse_event_rows_carry_the_kind_their_term_describes(db):
    curated = list(db.scalars(select(AdverseEvent).where(AdverseEvent.origin == "CURATED")))
    for ae in curated:
        assert ae.measure_kind == ae_kind_for_term(ae.term).value, (ae.id, ae.term, ae.measure_kind)
    dysesthesia = [ae for ae in curated if ae.term == "dysesthesia"]
    assert len(dysesthesia) == 7 and {(ae.measure_kind, ae.category) for ae in dysesthesia} == {("PARTICIPANTS_WITH_EVENT", "OTHER")}
    any_reason = [ae for ae in curated if "any reason" in ae.term]
    assert len(any_reason) == 8 and {ae.measure_kind for ae in any_reason} == {"TREATMENT_DISCONTINUATION_ANY_REASON"}


def test_responder_endpoint_needs_its_threshold_and_a_matching_quote():
    endpoint = {"key": "r5", "title": "proportion of participants with >=5% body-weight reduction", "role": "KEY_SECONDARY", "category": "WEIGHT_RESPONDER", "unit": "% of participants"}
    with pytest.raises(ValidationError, match="has no threshold"):
        EndpointDef.model_validate(endpoint)
    with pytest.raises(ValidationError, match="not the threshold its title names"):
        EndpointDef.model_validate({**endpoint, "threshold": 10})
    good = {**endpoint, "threshold": 5}
    row = {"endpoint": "r5", "arm": "FOUNDAYO 17.2 mg", "unit": "% of patients", "source": "openfda-label:foundayo"}
    StudyEvidenceFile.model_validate({"study": "NCT05869903", "endpoints": [good], "results": [{**row, "value": 71.5, "quote": "% of patients who lost ≥5% body weight 26.8 59.6 63.1 71.5"}]})
    with pytest.raises(ValidationError, match="the quote names the threshold.*≥20 %.*≥5 % responder endpoint"):
        StudyEvidenceFile.model_validate({"study": "NCT05869903", "endpoints": [good], "results": [{**row, "value": 18.4, "quote": "% of patients who lost ≥20% body weight 2.9 5.9 8.9 18.4"}]})
    with pytest.raises(ValidationError, match="two different values"):
        StudyEvidenceFile.model_validate(
            {"study": "NCT05869903", "endpoints": [good], "results": [{**row, "value": 71.5, "quote": "26.8 59.6 63.1 71.5"}, {**row, "value": 54.5, "quote": "13 32.5 39.8 54.5"}]}
        )
    with pytest.raises(ValidationError, match="defined twice"):
        StudyEvidenceFile.model_validate({"study": "NCT05869903", "endpoints": [good, {**good, "title": "proportion of participants with >=5% weight reduction (other)"}]})
    with pytest.raises(ValidationError, match="not a proportion of participants"):
        StudyEvidenceFile.model_validate({"study": "NCT05869903", "endpoints": [good], "results": [{**row, "value": 171.5, "quote": "171.5"}]})


def test_unit_must_follow_the_endpoint_category():
    pct = {"key": "w", "title": "percent change in body weight from baseline", "role": "PRIMARY", "category": "WEIGHT_PCT_CHANGE", "unit": "%"}
    row = {"endpoint": "w", "arm": "tirzepatide MTD", "source": "openfda-label:zepbound"}
    StudyEvidenceFile.model_validate({"study": "NCT04660643", "endpoints": [pct], "results": [{**row, "value": -20.9, "quote": "-20.9%"}]})
    with pytest.raises(ValidationError, match="magnitude of reduction"):
        EndpointDef.model_validate({**pct, "unit": "% reduction"})
    with pytest.raises(ValidationError, match="magnitude of reduction"):
        StudyEvidenceFile.model_validate({"study": "NCT04660643", "endpoints": [pct], "results": [{**row, "value": 20.9, "unit": "% reduction", "quote": "average body weight loss of 20.9%"}]})
    with pytest.raises(ValidationError, match="does not belong to category WEIGHT_PCT_CHANGE"):
        StudyEvidenceFile.model_validate({"study": "NCT04660643", "endpoints": [pct], "results": [{**row, "value": -20.9, "unit": "kg", "quote": "-20.9"}]})


def test_a_reduction_reported_as_a_positive_number_is_not_stored_among_signed_changes(db):
    """Zepbound label: "average body weight loss of 20.9%" — kept as reported, outside the signed-change categories."""
    study = _study(db, "NCT04660643")
    lead_in = next(r for r in study.results if r.origin == "CURATED" and r.value == 20.9)
    assert lead_in.endpoint.category == "OTHER" and lead_in.unit == "% reduction" and "loss of 20.9%" in lead_in.locator_quote
    signed = [r for r in db.scalars(select(OutcomeResult).join(Endpoint).where(OutcomeResult.origin == "CURATED", Endpoint.category.in_(["WEIGHT_PCT_CHANGE", "WEIGHT_ABS_CHANGE"])))]
    assert signed and not [r.id for r in signed if "reduction" in (r.unit or "").lower() or "loss" in (r.unit or "").lower()]


def test_stored_negative_values_are_printed_negative_or_say_how_the_sign_is_known(db):
    from incretinos.curation.schema import has_direction_wording, printed_signs

    unsigned = []
    for r in db.scalars(select(OutcomeResult).where(OutcomeResult.origin == "CURATED", OutcomeResult.value < 0)):
        if printed_signs(r.value, r.locator_quote) == {1}:
            unsigned.append(r)
            assert has_direction_wording(r.locator_quote) and "minus sign" in (r.notes or ""), (r.id, r.value, r.locator_quote)
    assert len(unsigned) == 4  # TRIUMPH-1 (3) and the retatrutide T2D trial (1): "lost an average of ... (25.9%)"


# --------------------------------------------------------------------------- estimands (CUR-07, ING-08)


def test_estimand_stated_by_source_needs_a_quote_that_names_it():
    base = {"key": "efficacy@web:x", "label": "EFFICACY", "description": "d", "source": "web:x"}
    with pytest.raises(ValidationError, match="has no quote"):
        EstimandDef.model_validate(base)
    with pytest.raises(ValidationError, match="does not name the EFFICACY estimand"):
        EstimandDef.model_validate({**base, "quote": "-7.8% (-8.0 kg; -17.6 lbs)"})
    EstimandDef.model_validate({**base, "quote": "using the efficacy estimand"})
    EstimandDef.model_validate({**base, "basis": "INFERRED_BY_CURATOR"})
    with pytest.raises(ValidationError, match="STATED_BY_SOURCE .quoted. or INFERRED_BY_CURATOR"):
        EstimandDef.model_validate({**base, "basis": "INFERRED_FROM_ANALYSIS_DESCRIPTION"})
    endpoint = {"key": "w", "title": "percent change in body weight from baseline", "role": "PRIMARY", "category": "WEIGHT_PCT_CHANGE", "unit": "%"}
    with pytest.raises(ValidationError, match="not inherited from another one"):
        StudyEvidenceFile.model_validate(
            {
                "study": "NCT05869903",
                "estimands": [{**base, "quote": "using the efficacy estimand"}],
                "endpoints": [endpoint],
                "results": [{"endpoint": "w", "estimand": "efficacy@web:x", "arm": "36 mg", "value": -12.4, "source": "openfda-label:foundayo", "quote": "-12.4"}],
            }
        )


def test_no_row_inherits_an_estimand_record_from_another_source(db):
    for model in (OutcomeResult, StatisticalAnalysis):
        rows = db.execute(select(model.id, model.source_id, Estimand.source_id).join(Estimand, model.estimand_id == Estimand.id)).all()
        assert rows and not [r for r in rows if r[1] != r[2]], model.__tablename__


def test_estimands_stated_by_source_carry_a_quote_found_in_that_source(db):
    stated = list(db.scalars(select(Estimand).where(Estimand.origin == "CURATED", Estimand.basis == "STATED_BY_SOURCE")))
    assert len(stated) >= 5
    for estimand in stated:
        assert estimand.quote and "estimand" in estimand.quote.lower(), (estimand.id, estimand.quote)
        assert loader._quote_in_source(db, estimand.source, estimand.quote), (estimand.id, estimand.source.key)
    # A curated label that no checked quote supports is the curator's attribution, and is shown as one.
    curator = list(db.scalars(select(Estimand).where(Estimand.origin == "CURATED", Estimand.basis == "INFERRED_BY_CURATOR")))
    assert curator and not db.scalar(select(func.count()).select_from(Estimand).where(Estimand.origin == "CURATED", Estimand.basis.notin_(["STATED_BY_SOURCE", "INFERRED_BY_CURATOR"])))
    row = db.scalar(select(OutcomeResult).where(OutcomeResult.estimand_id == curator[0].id))
    finding = build_finding(db, row)
    assert any(label["code"] == "ESTIMAND_CURATOR" for label in finding.labels) and "curator's attribution" in finding.statement


def test_registry_estimand_quote_occurs_in_the_result_it_is_attached_to(db):
    checked = 0
    for r in db.scalars(select(OutcomeResult).where(OutcomeResult.origin == "REGISTRY")):
        assert r.estimand is not None and r.estimand.origin == "REGISTRY"
        own_text = f"{r.analysis_population or ''} {r.analysis_method or ''}".lower()
        if r.estimand.quote:
            assert r.estimand.quote.lower() in own_text, (r.id, r.estimand.quote)
            checked += 1
        for strategy in r.estimand.strategies:  # e.g. the rescue-therapy strategy: only on outcomes whose text states it
            assert strategy.quote and strategy.quote.lower() in own_text, (r.id, strategy.event, strategy.quote)
    assert checked >= 400


# --------------------------------------------------------------------------- registry analyses (ING-02)


def test_believe_contrasts_follow_the_registrys_stated_direction(db):
    study = _study(db, "NCT05616013")
    posted = _raw("NCT05616013")["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
    titles = {g["id"]: g["title"] for g in posted["groups"]}
    stored = {a.source_location: a for a in study.analyses if a.origin == "REGISTRY"}
    assert len(posted["analyses"]) == 8
    for index, analysis in enumerate(posted["analyses"]):
        row = stored[f"resultsSection.outcomeMeasuresModule.outcomeMeasures[0].analyses[{index}]"]
        description = analysis["groupDescription"]  # e.g. "Bima 30mg/kg + Sema 2.4mg vs Placebo"
        assert description.endswith("vs Placebo")
        assert row.group_description == description and row.direction_basis == DirectionBasis.GROUP_DESCRIPTION.value
        assert row.group_labels == [titles[g] for g in analysis["groupIds"]]  # posted order kept
        # The reference group: placebo through week 40, then bimagrumab at weeks 52 and 64 — a sequence arm, not stored as placebo.
        assert row.comparator_label == "Placebo/30 mg/kg Bimagrumab" and row.comparator_arm.arm_role == "SEQUENCE" and not row.comparator_arm.is_placebo
        assert row.treatment_label != row.comparator_label and not row.treatment_arm.is_placebo
        assert row.estimate == float(analysis["paramValue"])
    first = stored["resultsSection.outcomeMeasuresModule.outcomeMeasures[0].analyses[0]"]
    assert first.treatment_label == "30 mg/kg Bimagrumab + 2.4 mg Semaglutide" and first.estimate == -14.5
    assert interpret_analysis(analysis_dto(first))["observed"].startswith("30 mg/kg Bimagrumab + 2.4 mg Semaglutide versus Placebo/30 mg/kg Bimagrumab")


def test_stated_direction_is_read_only_from_an_unambiguous_contrast():
    placebo, combo, mono = "Placebo/30 mg/kg Bimagrumab", "30 mg/kg Bimagrumab + 2.4 mg Semaglutide", "30 mg/kg Bimagrumab"
    assert stated_direction("Bima 30mg/kg + Sema 2.4mg vs Placebo", [placebo, combo]) == (1, 0)
    assert stated_direction("Bima 30mg/kg vs Placebo", [placebo, mono]) == (1, 0)  # "Bima 30mg/kg" fits both groups; "Placebo" settles it
    assert stated_direction("Placebo vs Bima 30mg/kg", [placebo, mono]) == (0, 1)  # the registry's order, whatever it is
    assert stated_direction("Total Cholesterol", ["Placebo", "Tirzepatide MTD"]) is None
    assert stated_direction("Drug A vs Drug B", ["5 mg Tirzepatide", "Placebo"]) is None  # names neither group
    assert stated_direction(None, ["Placebo", "Tirzepatide MTD"]) is None


def test_every_registry_analysis_records_how_its_direction_is_known(db):
    rows = list(db.scalars(select(StatisticalAnalysis).where(StatisticalAnalysis.origin == "REGISTRY")))
    assert len(rows) == 684
    by_basis = Counter(a.direction_basis for a in rows)
    assert set(by_basis) <= {b.value for b in DirectionBasis} and by_basis[DirectionBasis.GROUP_DESCRIPTION.value] == 8
    for a in rows:
        assert a.group_labels and len(a.group_labels) == 2 and {a.treatment_label, a.comparator_label} == set(a.group_labels)
        if a.direction_basis == DirectionBasis.ARM_TYPE.value:
            assert a.comparator_arm.arm_type in ("PLACEBO_COMPARATOR", "ACTIVE_COMPARATOR") and "registry arm type" in a.comment
        elif a.direction_basis == DirectionBasis.PLACEBO_GROUP.value:
            assert a.comparator_arm.is_placebo and not a.treatment_arm.is_placebo
        elif a.direction_basis == DirectionBasis.NOT_STATED.value:
            assert [a.treatment_label, a.comparator_label] == a.group_labels and "reference_group" in a.missing_fields
    for a in db.scalars(select(StatisticalAnalysis).where(StatisticalAnalysis.origin == "CURATED")):
        assert a.direction_basis == DirectionBasis.CURATED.value and a.group_labels == [x for x in (a.treatment_label, a.comparator_label) if x]


def test_analyses_of_a_multi_class_outcome_are_distinguishable(db):
    study = _study(db, "NCT04660643")  # SURMOUNT-4, lipids: six analyses of one outcome
    lipids = [a for a in study.analyses if a.origin == "REGISTRY" and a.source_location.startswith("resultsSection.outcomeMeasuresModule.outcomeMeasures[8].")]
    assert {a.class_title: a.estimate for a in lipids} == {
        "Total Cholesterol": -5.54, "LDL Cholesterol": -6.57, "HDL Cholesterol": 3.2, "VLDL Cholesterol": -19.7, "Triglycerides": -20.6, "FFA": -10.8,
    }
    assert "[HDL Cholesterol]" in interpret_analysis(analysis_dto(next(a for a in lipids if a.class_title == "HDL Cholesterol")))["observed"]
    seen: Counter = Counter()
    for a in db.scalars(select(StatisticalAnalysis).where(StatisticalAnalysis.origin == "REGISTRY")):
        seen[(a.study_id, a.endpoint_id, a.treatment_label, a.comparator_label, a.param_type, a.hypothesis_type, a.class_title, a.group_description)] += 1
    assert not {k: n for k, n in seen.items() if n > 1}
    assert analysis_class("Physical Component Score - Values represented under LS Mean are model-based estimates", ["Physical Component Score", "Mental Component Score"]) == "Physical Component Score"
    assert analysis_class("Primary CV endpoint", ["Number of participants"]) is None


def test_multiplicity_is_yes_only_on_an_affirmative_statement(db):
    for negated in (
        "No adjustments for multiplicity were made.",
        "Analysis was performed without multiplicity adjustment.",
        "This endpoint was not included in the graphical testing procedure.",
        "Not controlled for type I error.",
    ):
        assert multiplicity_from_comment(negated).value == "NO", negated
    assert multiplicity_from_comment("Multiplicity adjustments using the graphical approach were performed in order to control the overall Type I error rate.").value == "YES"
    for silent in (None, "", "ANCOVA model with treatment (Type III sum of squares) as covariate.", "Non-inferiority margin of 0.3%."):
        assert multiplicity_from_comment(silent).value == "NOT_STATED"
    yes = list(db.scalars(select(StatisticalAnalysis).where(StatisticalAnalysis.origin == "REGISTRY", StatisticalAnalysis.multiplicity_controlled == "YES")))
    assert len(yes) == 8 and {a.study.nct_id for a in yes} == {"NCT01394952"}  # REWIND, as posted


# --------------------------------------------------------------------------- arms (ING-03, ING-08)


def test_nct06643728_arms_are_distinct(db):
    posted = _raw("NCT06643728")["protocolSection"]["armsInterventionsModule"]["armGroups"]
    study = _study(db, "NCT06643728")
    arms = {a.label: a for a in study.arms if a.origin == "REGISTRY"}
    assert len(posted) == 12 and set(arms) == {a["label"] for a in posted}
    for group in posted:
        assert arms[group["label"]].arm_type == group["type"]
    tirzepatide = arms["Part A: Bimagrumab Placebo + Tirzepatide Dose 2"]
    bimagrumab = arms["Part A: Bimagrumab Dose 2 + Tirzepatide Placebo"]
    assert tirzepatide.id != bimagrumab.id
    assert (tirzepatide.arm_type, tirzepatide.drug_slug) == ("ACTIVE_COMPARATOR", "tirzepatide")
    assert (bimagrumab.arm_type, bimagrumab.drug_slug) == ("EXPERIMENTAL", "bimagrumab")
    double_placebo = arms["Part A: Bimagrumab Placebo + Tirzepatide Placebo"]
    assert double_placebo.is_placebo and double_placebo.drug_slug == "placebo"


def test_arm_labels_that_differ_in_substance_are_never_matched():
    assert arm_identity_key("36 mg-1 LY3502970") == arm_identity_key("36 Mg-1 LY3502970") == arm_identity_key("36 mg -1 LY3502970")
    assert norm_arm_label("5 mg Tirzepatide") == norm_arm_label("Tirzepatide 5 mg") == norm_arm_label("5 milligrams (mg) Tirzepatide")
    for a, b in (
        ("Part A: Bimagrumab Placebo + Tirzepatide Dose 2", "Part A: Bimagrumab Dose 2 + Tirzepatide Placebo"),
        ("Tirzepatide then Placebo", "Placebo then Tirzepatide"),
        ("Orforglipron 12 mg QD", "Orforglipron 12 mg QW"),
        ("Tirzepatide 5 mg Dose 2", "Tirzepatide 2 mg Dose 5"),
        ("30 mg/kg Bimagrumab", "30 mg Bimagrumab"),
    ):
        assert norm_arm_label(a) != norm_arm_label(b) and arm_identity_key(a) != arm_identity_key(b), (a, b)
        assert match_arm_label(a, [b]) is None
    arms = ["6 mg Orforglipron QD", "12 mg Orforglipron QD", "Placebo", "Orforglipron 12 mg QW"]
    assert match_arm_label("Orforglipron 6 mg", arms) == 0  # no frequency given: the only 6 mg arm
    assert match_arm_label("orforglipron 6 mg once daily", arms) == 0
    assert match_arm_label("Orforglipron 12 mg", arms) is None  # QD or QW? ambiguous, so attached to neither
    assert match_arm_label("placebo", arms) == 2


def test_no_placebo_arm_is_attributed_to_a_drug(db):
    arms = list(db.scalars(select(StudyArm)))
    placebo_typed = [a for a in arms if a.arm_type == "PLACEBO_COMPARATOR"]
    assert len(placebo_typed) >= 30
    for arm in placebo_typed:
        assert arm.is_placebo and arm.drug_slug == "placebo" and arm.dose_amount is None, (arm.study.nct_id, arm.label, arm.drug_slug)
    assert not [(a.study.nct_id, a.label, a.drug_slug) for a in arms if a.is_placebo and a.drug_slug != "placebo"]
    believe = next(a for a in _study(db, "NCT05616013").arms if a.label == "Placebo/30 mg/kg Bimagrumab")
    # Registered as the placebo comparator but switched to bimagrumab at week 52: neither a bimagrumab arm (as it was
    # first stored, carrying 67 results) nor a placebo arm (as the data repair stored it) — a sequence arm.
    assert not believe.is_placebo and believe.drug_slug is None and believe.registered_arm_type == "PLACEBO_COMPARATOR" and believe.arm_type == "SEQUENCE"
    assert arm_drug_slug("Part A: Bimagrumab Placebo + Tirzepatide Placebo") == ("placebo", True)
    assert arm_drug_slug("Part A: Bimagrumab Placebo + Tirzepatide Dose 2") == ("tirzepatide", False)
    assert arm_drug_slug("placebo to match tirzepatide") == ("placebo", True)
    assert arm_drug_slug("Placebo + 2.4 mg Semaglutide") == ("semaglutide", False)


def test_weight_based_doses_keep_their_unit(db):
    assert parse_single_dose("30 mg/kg Bimagrumab") == (30.0, "mg/kg") and parse_single_dose_mg("30 mg/kg Bimagrumab") is None
    assert parse_single_dose_mg("5 mg Tirzepatide") == 5.0 and parse_single_dose("10 milligrams (mg) Tirzepatide") == (10.0, "mg")
    for several in ("10/30 mg/kg Bimagrumab", "Cohort 2A: 1/3/6 mg LY3502970 Capsule QD (Home Dosing)", "Pooled 6 mg/12 mg/36 mg Orforglipron", "eloralintide 6-9 mg (dose escalation)", "4 mg LY3437943 (2 mg)"):
        assert parse_single_dose(several) is None, several
    believe = {a.label: a for a in _study(db, "NCT05616013").arms}
    assert (believe["30 mg/kg Bimagrumab"].dose_amount, believe["30 mg/kg Bimagrumab"].dose_unit) == (30.0, "mg/kg")
    assert believe["10/30 mg/kg Bimagrumab"].dose_amount is None  # 10 then 30 mg/kg is not "30"
    for arm in db.scalars(select(StudyArm).where(StudyArm.dose_amount.is_not(None))):
        assert arm.dose_unit, arm.label
        if "mg/kg" in arm.label.lower():
            assert arm.dose_unit == "mg/kg", (arm.label, arm.dose_unit)


def test_number_randomized_comes_from_the_period_the_group_entered(db):
    surmount4 = {a.label: a.n_randomized for a in _study(db, "NCT04660643").arms if a.origin == "REGISTRY"}
    assert surmount4 == {"Tirzepatide (lead-in)": 783, "Tirzepatide MTD": 335, "Placebo": 335}  # randomized arms were stored as 0
    assert not db.scalar(select(func.count()).select_from(StudyArm).where(StudyArm.n_randomized == 0))
    # a group that is not part of a period has no "0 of 0" row for it
    assert not db.scalar(select(func.count()).select_from(AdverseEvent).where(AdverseEvent.origin == "REGISTRY", AdverseEvent.n_at_risk == 0))
    assert not db.scalar(select(func.count()).select_from(ParticipantFlow).where(ParticipantFlow.n_started == 0))


def test_reingest_rebuilds_arms_and_estimands_from_the_current_record(scratch):
    adapter = CTGovAdapter(FixtureTransport(FIXTURES))
    nct = "NCT05822830"  # SURMOUNT-5
    adapter.ingest_study(scratch, nct)
    study = scratch.scalar(select(Study).where(Study.nct_id == nct))
    before = {a.label: (a.id, a.n_randomized, a.description) for a in scratch.scalars(select(StudyArm).where(StudyArm.study_id == study.id))}
    estimands_before = scratch.scalar(select(func.count()).select_from(Estimand))
    raw = _raw(nct)
    flow_group = raw["resultsSection"]["participantFlowModule"]["groups"][0]
    label = flow_group["title"]
    assert before[label][1] is not None

    changed = copy.deepcopy(raw)
    changed["protocolSection"]["statusModule"]["lastUpdatePostDateStruct"] = {"date": "2099-01-01", "type": "ACTUAL"}
    for arm in changed["protocolSection"]["armsInterventionsModule"]["armGroups"]:
        if arm["label"] == label:
            arm["description"] = "A corrected arm description."
    started = next(m for m in changed["resultsSection"]["participantFlowModule"]["periods"][0]["milestones"] if m["type"] == "STARTED")
    for achievement in started["achievements"]:
        if achievement["groupId"] == flow_group["id"]:
            achievement["numSubjects"] = str(before[label][1] - 100)
    removed = changed["protocolSection"]["armsInterventionsModule"]["armGroups"][-1]["label"]
    assert removed != label
    changed["protocolSection"]["armsInterventionsModule"]["armGroups"][-1]["label"] = "A renamed arm"

    adapter.ingest_study(scratch, nct, raw_override=json.dumps(changed).encode())
    after = {a.label: (a.id, a.n_randomized, a.description) for a in scratch.scalars(select(StudyArm).where(StudyArm.study_id == study.id))}
    assert after[label] == (before[label][0], before[label][1] - 100, "A corrected arm description.")  # same row, new values
    assert "A renamed arm" in after
    assert scratch.scalar(select(func.count()).select_from(Estimand)) == estimands_before  # rebuilt, not accumulated
    assert scratch.scalar(select(func.count()).select_from(StudyArm).where(StudyArm.study_id == study.id, StudyArm.n_randomized == 0)) == 0


def test_an_arm_dropped_by_the_registry_keeps_no_registry_values(scratch):
    adapter = CTGovAdapter(FixtureTransport(FIXTURES))
    nct = "NCT05822830"
    adapter.ingest_study(scratch, nct)
    study = scratch.scalar(select(Study).where(Study.nct_id == nct))
    arms = list(scratch.scalars(select(StudyArm).where(StudyArm.study_id == study.id).order_by(StudyArm.id)))
    kept, dropped = arms[0], arms[1]
    source = scratch.scalar(select(EvidenceSource))
    endpoint = Endpoint(study_id=study.id, origin="CURATED", kind="CURATED", title="a curated endpoint")
    scratch.add(endpoint)
    scratch.flush()
    scratch.add(OutcomeResult(study_id=study.id, endpoint_id=endpoint.id, arm_id=kept.id, origin="CURATED", arm_label=kept.label, value=1.0, source_id=source.id, tier="PEER_REVIEWED", verification_status="PENDING_HUMAN_REVIEW"))
    scratch.flush()
    changed = _raw(nct)
    changed["protocolSection"]["statusModule"]["lastUpdatePostDateStruct"] = {"date": "2099-01-01", "type": "ACTUAL"}
    renames = {kept.label: "Renamed arm one", dropped.label: "Renamed arm two"}
    text = json.dumps(changed)
    for old, new in renames.items():
        text = text.replace(json.dumps(old), json.dumps(new))
    adapter.ingest_study(scratch, nct, raw_override=text.encode())
    scratch.expire_all()
    labels = {a.label: a for a in scratch.scalars(select(StudyArm).where(StudyArm.study_id == study.id))}
    assert dropped.label not in labels  # nothing points at it: removed
    survivor = labels[kept.label]  # curated evidence still points at it: kept, but no longer a registry arm
    assert survivor.origin == "CURATED" and survivor.n_randomized is None and survivor.description is None and survivor.arm_type == "CURATED"
    assert scratch.scalar(select(OutcomeResult.arm_id).where(OutcomeResult.origin == "CURATED")) == survivor.id


def test_arm_annotation_must_name_a_registered_arm(db):
    study = _study(db, "NCT05872620")
    capsule = {a.label: a.formulation_key for a in study.arms if a.origin == "REGISTRY" and a.arm_type == "EXPERIMENTAL"}
    assert capsule == {"6 mg Orforglipron QD": "orforglipron-capsule", "12 mg Orforglipron QD": "orforglipron-capsule", "36 mg Orforglipron QD": "orforglipron-capsule"}
    file = StudyEvidenceFile.model_validate({"study": "NCT05872620", "arms": [{"label": "48 mg Orforglipron QD", "formulation": "orforglipron-capsule"}]})
    with pytest.raises(CurationError, match="does not name an arm of this study"):
        loader.apply_arm_annotations(db, study, file)


# --------------------------------------------------------------------------- adverse events and participant flow


def test_every_posted_serious_adverse_event_term_is_stored(db):
    """ATTAIN-1 posts 146 serious terms; 125 of them used to be dropped by a frequency filter."""
    module = _raw("NCT05869903")["resultsSection"]["adverseEventsModule"]
    assert len(module["seriousEvents"]) == 146
    study = _study(db, "NCT05869903")
    titles = {g["id"]: g["title"] for g in module["eventGroups"]}
    stored = {(ae.source_location, ae.arm_label): ae for ae in study.adverse_events if ae.origin == "REGISTRY" and ae.source_location.startswith("resultsSection.adverseEventsModule.seriousEvents[")}
    assert len({loc for loc, _ in stored}) == 146
    for index, event in enumerate(module["seriousEvents"]):
        for stat in event["stats"]:
            row = stored[(f"resultsSection.adverseEventsModule.seriousEvents[{index}]", titles[stat["groupId"]])]
            assert (row.term, row.organ_system, row.n_affected, row.n_at_risk, row.is_serious) == (event["term"], event["organSystem"], stat["numAffected"], stat["numAtRisk"], True)
            assert row.verification_status == "MACHINE_EXTRACTED" and row.measure_kind == "PARTICIPANTS_WITH_EVENT"
    assert {"Acute myocardial infarction", "Atrial flutter"} <= {ae.term for ae in stored.values()}  # dropped before: under 5 participants


def test_serious_and_other_event_tables_are_complete_for_every_study(db):
    posted_serious = posted_other = 0
    for path in sorted((FIXTURES / "ctgov" / "studies").glob("NCT*.json")):
        module = json.loads(path.read_text()).get("resultsSection", {}).get("adverseEventsModule")
        if not module:
            continue
        study = db.scalar(select(Study).where(Study.nct_id == path.stem))
        if study is None:
            continue
        groups = {g["id"] for g in module.get("eventGroups", [])}
        for key in ("seriousEvents", "otherEvents"):
            posted = sum(1 for e in module.get(key, []) for s in e.get("stats", []) if s.get("groupId") in groups)
            stored = db.scalar(select(func.count()).select_from(AdverseEvent).where(AdverseEvent.study_id == study.id, AdverseEvent.origin == "REGISTRY", AdverseEvent.source_location.like(f"resultsSection.adverseEventsModule.{key}[%")))
            assert stored == posted, (path.stem, key, stored, posted)
            if key == "seriousEvents":
                posted_serious += len(module.get(key, []))
            else:
                posted_other += len(module.get(key, []))
    assert posted_serious == 4155 and posted_other > 300  # 3,248 of the 4,155 serious terms were not stored before


def test_serious_event_rows_do_not_replace_the_incidence_of_the_same_term(db):
    """'Vomiting' is in both registry tables. The tolerability profile reads the event incidence, not the serious subset."""
    study = _study(db, "NCT04184622")
    vomiting = [ae for ae in study.adverse_events if ae.origin == "REGISTRY" and ae.term == "Vomiting" and ae.arm_label == "5 mg Tirzepatide"]
    assert {ae.is_serious for ae in vomiting} == {True, False}
    non_serious = next(ae for ae in vomiting if not ae.is_serious)
    profile = next(a for a in arm_profiles(db, "NCT04184622")["arms"] if a["label"] == "5 mg Tirzepatide")
    assert profile["tolerability"]["vomiting"]["ae_id"] == non_serious.id and profile["tolerability"]["vomiting"]["n_affected"] == non_serious.n_affected


def test_withdrawal_reasons_are_captured_with_counts_and_denominators(db):
    module = _raw("NCT05869903")["resultsSection"]["participantFlowModule"]
    study = _study(db, "NCT05869903")
    titles = {g["id"]: g["title"] for g in module["groups"]}
    period = module["periods"][0]
    started = {a["groupId"]: int(a["numSubjects"]) for m in period["milestones"] if m["type"] == "STARTED" for a in m["achievements"]}
    rows = {(f.label, f.arm_label): f for f in db.scalars(select(ParticipantFlow).where(ParticipantFlow.study_id == study.id, ParticipantFlow.kind == "WITHDRAWAL_REASON"))}
    assert len(period["dropWithdraws"]) == 10 and len(rows) == 40
    for drop in period["dropWithdraws"]:
        for reason in drop["reasons"]:
            row = rows[(drop["type"], titles[reason["groupId"]])]
            assert (row.n_participants, row.n_started) == (int(reason["numSubjects"]), started[reason["groupId"]])
            assert row.verification_status == "MACHINE_EXTRACTED" and row.tier == "REGISTRY_RESULTS" and row.period_title == "Overall Study"
    assert (rows[("Withdrawal by Subject", "Placebo")].n_participants, rows[("Withdrawal by Subject", "Placebo")].n_started) == (89, 949)
    assert rows[("Death", "36 mg Orforglipron")].n_participants == 0  # a posted zero is a zero
    milestones = {(f.label, f.arm_label): f.n_participants for f in db.scalars(select(ParticipantFlow).where(ParticipantFlow.study_id == study.id, ParticipantFlow.kind == "MILESTONE"))}
    assert milestones[("Received at Least One Dose of Study Drug", "Placebo")] == 948 and milestones[("COMPLETED", "Placebo")] == 768
    # The safety views still read study withdrawal due to adverse events from the adverse-event table, as a separate kind.
    withdrawal = next(ae for ae in study.adverse_events if ae.measure_kind == "STUDY_WITHDRAWAL_DUE_TO_AE" and ae.arm_label == "Placebo")
    assert (withdrawal.n_affected, withdrawal.n_at_risk) == (10, 949)


def test_participant_flow_is_complete_for_every_study(db):
    total = 0
    for path in sorted((FIXTURES / "ctgov" / "studies").glob("NCT*.json")):
        module = json.loads(path.read_text()).get("resultsSection", {}).get("participantFlowModule")
        study = db.scalar(select(Study).where(Study.nct_id == path.stem))
        if not module or study is None:
            continue
        expected = 0
        for period in module.get("periods", []):
            started = {a["groupId"]: to_float(a.get("numSubjects")) for m in period.get("milestones", []) if m.get("type") == "STARTED" for a in m.get("achievements", [])}
            groups = {g["id"] for g in module.get("groups", [])}
            part = {g for g in groups if started.get(g) != 0}  # only a posted zero takes a group out of a period
            expected += sum(1 for d in period.get("dropWithdraws", []) for r in d.get("reasons", []) if r.get("groupId") in part)
        stored = db.scalar(select(func.count()).select_from(ParticipantFlow).where(ParticipantFlow.study_id == study.id, ParticipantFlow.kind == "WITHDRAWAL_REASON"))
        assert stored == expected, (path.stem, stored, expected)
        total += stored
    assert total > 500


def test_safety_terms_never_turn_an_unreported_count_into_zero(client):
    body = client.get("/api/safety/terms", params={"nct": "NCT04184622"}).json()
    terms = body["terms"]
    assert len({(t["term"], t["measure_kind"]) for t in terms}) == len(terms)  # one entry per term and measure kind
    discontinuation = next(t for t in terms if t["measure_kind"] == "TREATMENT_DISCONTINUATION_DUE_TO_AE")
    assert discontinuation["max_participants_affected"] is None  # the publication reports percentages, no count
    assert not [t for t in terms if t["max_participants_affected"] == 0 and t["measure_kind"] == "TREATMENT_DISCONTINUATION_DUE_TO_AE"]
    vomiting = next(t for t in terms if t["term"] == "Vomiting")
    assert vomiting["tables"] == ["SERIOUS", "NOT_SERIOUS"] and vomiting["is_serious"] is None
    cvot = client.get("/api/safety/terms", params={"nct": "NCT04255433"}).json()
    assert cvot["truncated"] and cvot["total_terms"] > 1000 and len(cvot["terms"]) == 300  # the cut is stated, not silent
    assert len(client.get("/api/safety/terms", params={"nct": "NCT04255433", "limit": 5000}).json()["terms"]) == cvot["total_terms"]


# --------------------------------------------------------------------------- missing is not zero, fallbacks are not values (ING-13)


def test_parsers_do_not_turn_missing_or_ambiguous_text_into_a_number():
    assert to_float("1,5") is None and to_float("12,34") is None  # could be a decimal comma: not 15, not 1234
    assert to_float("1,234") == 1234.0 and to_float("1,234,567.5") == 1234567.5 and to_float("NA") is None
    assert parse_timeframe("Baseline, Week 72") == (72.0, "Baseline") and parse_timeframe("Randomization (Week 36) to Week 88")[0] == 88.0
    assert parse_timeframe("Baseline (Week 0), Week 88")[0] == 88.0 and parse_timeframe("Week 0, Week 116")[0] == 116.0
    for several in (
        "Predose: Week 0, 1, 4, 12, 24, 30; Postdose: Week 2, 8, 16, 20, 36",  # was 2
        "Baseline, Week 24, Week 48",  # was 48 for every class
        "Week 4, Week 12, Week 36, Week 52",
        "Week 8, 16, and 36, at 1 to 24 hours, 24 to 96 hours, or 120 to 168 hours postdose",
        "Baseline through Week 176, Baseline through Week 190",
    ):
        assert parse_timeframe(several)[0] is None, several


def test_results_of_a_multi_time_frame_have_no_single_time_point(db):
    study = _study(db, "NCT04867785")  # retatrutide phase 2 (T2D): PK sampled at eleven weeks, stored as "week 2"
    pk = [r for r in study.results if r.origin == "REGISTRY" and r.timeframe_text and r.timeframe_text.startswith("Predose: Week 0, 1, 4")]
    assert pk and all(r.timepoint_weeks is None and "timepoint_weeks" in r.missing_fields for r in pk)


def test_responder_thresholds_are_read_from_registry_titles(db):
    assert categorize_endpoint("Percentage of Participants With Greater Than or Equal to (≥) 5% Body Weight Reduction")[1] == 5.0
    assert categorize_endpoint("Percentage of Participants Who Achieved Weight Loss of >=10%")[1] == 10.0
    assert categorize_endpoint("proportion of patients with >=20% body-weight reduction") == (categorize_endpoint("x ≥20% body weight reduction")[0], 20.0)
    # several thresholds in one title: none of them is the endpoint's (it used to take the last one, 20)
    assert categorize_endpoint("Percentage of Participants Who Achieve ≥5%, ≥10%, ≥15%, ≥20% Body Weight Reduction From Baseline")[1] is None
    assert categorize_endpoint("Percentage of Participants Who Maintain ≥80% of the Body Weight Lost")[1] is None
    multi = db.scalar(select(Endpoint).where(Endpoint.origin == "REGISTRY", Endpoint.kind == "RESULTS", Endpoint.title.like("Percentage of Participants Who Achieve ≥5%, ≥10%, ≥15%, ≥20%%")))
    assert multi is not None and multi.threshold is None
    row = db.scalar(select(OutcomeResult).where(OutcomeResult.endpoint_id == multi.id, OutcomeResult.class_title.like("%5%%")))
    statement = build_finding(db, row).statement
    assert row.class_title in statement and "≥20% weight reduction" not in statement


class _NoTotalTransport(FixtureTransport):
    def get(self, service, path, params=None, *, fixture_key=None):
        return json.dumps({"meta": {"disclaimer": "…"}, "results": []}).encode()


def test_label_search_without_a_total_is_an_error_not_zero_labels(tmp_path):
    with pytest.raises(TransportError, match="number of label records is unknown"):
        OpenFDALabelAdapter(_NoTotalTransport(tmp_path)).count_labels_for_generic("RETATRUTIDE")
    assert OpenFDALabelAdapter(FixtureTransport(FIXTURES)).count_labels_for_generic("RETATRUTIDE") == 0  # recorded 404: a definite zero


# --------------------------------------------------------------------------- nothing is human-verified by any of this


def test_no_seeded_evidence_is_human_verified(db):
    for model in (OutcomeResult, StatisticalAnalysis, AdverseEvent, ParticipantFlow):
        statuses = {s for (s,) in db.execute(select(model.verification_status).distinct())}
        assert "HUMAN_VERIFIED" not in statuses, model.__tablename__
    assert {s for (s,) in db.execute(select(ParticipantFlow.verification_status).distinct())} == {"MACHINE_EXTRACTED"}
    assert get_settings().curated_dir.is_dir()


# =========================================================================== Session 2 P0 data fixes (review DR-*, CK-*)


# --------------------------------------------------------------------------- DR-1: one source table, one endpoint record


def test_rows_of_one_source_table_share_one_endpoint_record(db):
    """Role and multiplicity are not endpoint identity: placebo, low-dose and comparator rows stay with the active arms."""
    # ATTAIN-1, Foundayo label "% of patients who lost ≥20% body weight 2.9 5.9 8.9 18.4": four arms, one endpoint record.
    attain1 = _study(db, "NCT05869903")
    label20 = [r for r in attain1.results if r.origin == "CURATED" and r.source.key == "openfda-label:foundayo" and r.endpoint.category == "WEIGHT_RESPONDER" and r.endpoint.threshold == 20.0]
    assert sorted(r.value for r in label20) == [2.9, 5.9, 8.9, 18.4] and len({r.endpoint_id for r in label20}) == 1
    # The label's primary-endpoint table: placebo -2.1 with the three Foundayo doses.
    label_pct = [r for r in attain1.results if r.origin == "CURATED" and r.source.key == "openfda-label:foundayo" and r.endpoint.category == "WEIGHT_PCT_CHANGE"]
    assert -2.1 in {r.value for r in label_pct} and len({r.endpoint_id for r in label_pct if r.estimand_id == label_pct[0].estimand_id}) == 1
    # TRIUMPH-2: the 4 mg arm is not split off; EloraTZP: the tirzepatide comparator and monotherapies sit with the combinations.
    for nct, arms in (("NCT05929079", 4), ("NCT06603571", 9)):
        rows = [r for r in _study(db, nct).results if r.origin == "CURATED" and r.endpoint.category == "WEIGHT_PCT_CHANGE" and r.source.tier == "PRESS_RELEASE"]
        by_endpoint: dict[int, set] = defaultdict(set)
        for r in rows:
            by_endpoint[r.endpoint_id].add(r.arm_label)
        assert max(len(v) for v in by_endpoint.values()) >= arms, (nct, by_endpoint)
    # Curated endpoints of a study are never two records of one (title, category, threshold, unit, time point).
    seen = Counter((e.study_id, e.title, e.category, e.threshold, e.unit, e.timepoint_weeks) for e in db.scalars(select(Endpoint).where(Endpoint.origin == "CURATED", Endpoint.kind == "CURATED")))
    hand = {(e.study_id, e.title) for e in db.scalars(select(Endpoint).where(Endpoint.origin == "CURATED")) if e.title[:1].isupper()}  # hand-curated titles
    assert not [k for k, n in seen.items() if n > 1 and (k[0], k[1]) not in hand]


def test_endpoint_role_and_multiplicity_are_settled_over_all_rows_of_the_endpoint(converter):
    endpoint = {"title": "t", "role": "SECONDARY", "multiplicity_controlled": "YES"}

    def entry(arm, role, mult, active=True, quote="q"):
        return {"role": role, "multiplicity_controlled": mult, "active": active, "quote": quote, "arm": arm, "row": {"notes": None}}

    agree = [entry("9 mg", "PRIMARY", "YES"), entry("12 mg", "PRIMARY", "YES"), entry("placebo", "NOT_STATED", "NOT_STATED", active=False)]
    assert converter.settle_endpoint_status("NCT0", endpoint, agree) == [] and (endpoint["role"], endpoint["multiplicity_controlled"]) == ("PRIMARY", "YES")
    # the active arms disagree (5.5 mg not in the controlled hierarchy): NOT_STATED, each row keeps its note's statement
    split = [entry("5.5 mg", "KEY_SECONDARY", "NO"), entry("9 mg", "KEY_SECONDARY", "YES"), entry("17.2 mg", "KEY_SECONDARY", "YES")]
    lines = converter.settle_endpoint_status("NCT0", endpoint, split)
    assert endpoint["multiplicity_controlled"] == "NOT_STATED" and endpoint["role"] == "KEY_SECONDARY"
    assert len(lines) == 1 and "needs human curation" in lines[0]
    assert "not controlled (NO)" in split[0]["row"]["notes"] and "controlled (YES)" in split[1]["row"]["notes"]
    # a role the quote itself names settles a disagreement, but only if it is one of the recorded roles
    named = [entry("4 mg", "KEY_SECONDARY", "YES"), entry("9 mg", "PRIMARY", "YES", quote="For the primary endpoint, participants taking 9 mg lost …")]
    converter.settle_endpoint_status("NCT0", endpoint, named)
    assert endpoint["role"] == "PRIMARY" and "KEY_SECONDARY" in named[0]["row"]["notes"]


# --------------------------------------------------------------------------- DR-2: a share of participants is not a mean change


BELIEVE_RESPONDER = "Percentage of Participants With Reduction in Body Weight ≥ 5%, ≥ 10% and ≥15% at Week 48"


def test_participant_shares_are_never_filed_as_mean_change():
    assert categorize_endpoint(BELIEVE_RESPONDER, "percentage of participants") == (categorize_endpoint("≥5% body weight reduction")[0], None)
    assert categorize_endpoint(BELIEVE_RESPONDER)[0].value == "WEIGHT_RESPONDER"
    for title in (
        "Percentage of Participants With Reduction in Waist Circumference Greater Than or Equal to (≥) 5 cm at Week 48",
        "Percentage of Participants With Body Mass Index (BMI) Categories at Baseline and Week 48",
        "Percentage of Participants With Reduction in Fat Mass ≥ 5% ≥ 10% ≥ 15% by DXA at Week 48",
        "Percentage of Participants Achieving HbA1c ≤6.5% and ≥10% Weight Reduction",  # a composite target
    ):
        assert categorize_endpoint(title, "percentage of participants")[0].value == "OTHER", title
    # a mean share of an earlier loss is not a change in body weight either
    assert categorize_endpoint("Percent Maintenance of Body Weight Reduction Achieved in SURMOUNT-5")[0].value == "OTHER"
    assert categorize_endpoint("Percentage of Body Weight Reduction Achieved at Week 80 that is Maintained at Week 116")[0].value == "OTHER"
    # still a change when it is one
    assert categorize_endpoint("Percent Change From Baseline in Body Weight", "percent change")[0].value == "WEIGHT_PCT_CHANGE"
    assert categorize_endpoint("Change From Baseline in Waist Circumference", "centimeters")[0].value == "WAIST_CHANGE"


def test_no_stored_share_of_participants_sits_in_a_measurement_category(db):
    from incretinos.vocab import MEASUREMENT_CATEGORIES, counts_participants

    measurement = {c.value for c in MEASUREMENT_CATEGORIES}
    wrong = [
        (e.study.nct_id, e.category, e.title, e.unit)
        for e in db.scalars(select(Endpoint).where(Endpoint.category.in_(measurement)))
        if counts_participants(e.title, e.unit)
    ]
    assert not wrong
    believe = [r for r in _study(db, "NCT05616013").results if r.origin == "REGISTRY" and r.endpoint.title == BELIEVE_RESPONDER]
    assert believe and {r.endpoint.category for r in believe} == {"WEIGHT_RESPONDER"}
    # benefit-risk no longer reads the responder shares as percent change in body weight
    profiles = arm_profiles(db, "NCT05616013")
    weight = [a["efficacy"]["weight_pct"] for a in profiles["arms"] if "weight_pct" in a["efficacy"]]
    assert all("participants" not in (w.get("unit") or "") for w in weight)


# --------------------------------------------------------------------------- DR-4 / WEB-06: stored arm roles


def test_switch_arm_is_a_sequence_not_a_placebo_arm(db):
    from incretinos.engines.safety import arm_role

    believe = next(a for a in _study(db, "NCT05616013").arms if a.label == "Placebo/30 mg/kg Bimagrumab")
    assert (believe.is_placebo, believe.drug_slug, believe.dose_amount) == (False, None, None)
    assert (believe.arm_type, believe.registered_arm_type, believe.arm_role) == ("SEQUENCE", "PLACEBO_COMPARATOR", "SEQUENCE")
    assert "followed by" in believe.dose_text and believe.n_randomized
    assert arm_role(believe, {})["arm_role"] == "SEQUENCE"
    profiles = arm_profiles(db, "NCT05616013")
    assert not [a["label"] for a in profiles["arms"] if a["is_placebo"] and "Bimagrumab" in a["label"]]
    triumph6 = next(a for a in _study(db, "NCT06859268").arms if a.label == "Retatrutide Dose 1 to Placebo")
    assert (triumph6.arm_role, triumph6.is_placebo) == ("SEQUENCE", False)
    # a curated "placebo to retatrutide MTD" arm is a sequence too, not a retatrutide arm
    curated = next(a for a in _study(db, "NCT05929066").arms if a.label == "placebo to retatrutide MTD")
    assert (curated.arm_role, curated.drug_slug, curated.is_placebo) == ("SEQUENCE", None, False)


def test_every_arm_stores_the_role_the_shared_rule_gives(db):
    from incretinos.vocab import classify_arm_role

    arms = list(db.scalars(select(StudyArm)))
    assert arms and all(a.arm_role for a in arms)
    for arm in arms:
        roles = {link.drug.slug: link.role for link in arm.study.drug_links}
        role, basis = classify_arm_role(arm_type=arm.arm_type, is_placebo=arm.is_placebo, drug_slug=arm.drug_slug, label=arm.label, drug_roles=roles)
        assert (arm.arm_role, arm.arm_role_basis) == (role.value, basis), (arm.study.nct_id, arm.label)
        if arm.arm_role == "PLACEBO":
            assert arm.is_placebo and arm.drug_slug == "placebo"
    # WEB-06: in EloraTZP the tirzepatide-alone arms are the active comparator, the combinations the study drug
    elora = {a.label: a.arm_role for a in _study(db, "NCT06603571").arms}
    assert elora["Tirzepatide Dose 3"] == "ACTIVE_COMPARATOR" and elora["tirzepatide 15 mg"] == "ACTIVE_COMPARATOR"
    assert elora["LY3841136 Dose 3 + Tirzepatide Dose 3"] == "STUDY_DRUG" and elora["Placebo"] == "PLACEBO"


# --------------------------------------------------------------------------- DR-5: dose lists and titrations


def test_dose_lists_and_titrations_are_not_one_dose(db):
    for label in ("tirzepatide MTD (10 or 15 mg)", "15 mg or MTD - Tirzepatide", "semaglutide MTD (1.7 or 2.4 mg)", "retatrutide 4 mg to MTD", "up to 36 mg orforglipron", "tirzepatide 10 mg and 15 mg", "10, 15 mg"):
        assert parse_single_dose(label) is None, label
    for label, dose in (("Placebo + 2.4 mg Semaglutide", (2.4, "mg")), ("Group 2, 10 mg Tirzepatide", (10.0, "mg")), ("FOUNDAYO 5.5 mg once daily", (5.5, "mg"))):
        assert parse_single_dose(label) == dose, label
    surmount5 = {a.label: a for a in _study(db, "NCT05822830").arms}
    assert surmount5["15 mg or MTD - Tirzepatide"].dose_amount is None and surmount5["tirzepatide MTD (10 or 15 mg)"].dose_amount is None
    triumph1 = {a.label: a.dose_amount for a in _study(db, "NCT05929066").arms}
    assert triumph1["retatrutide 4 mg to MTD"] is None and triumph1["retatrutide 4 mg"] == 4.0


# --------------------------------------------------------------------------- DR-6: source labels attach to the registered arm


def test_source_labels_attach_to_the_one_registered_arm_of_that_drug_and_dose(db):
    achieve3 = _study(db, "NCT06045221")
    for analysis in (a for a in achieve3.analyses if a.origin == "CURATED"):
        for arm in (analysis.treatment_arm, analysis.comparator_arm):
            assert arm.origin == "REGISTRY", (analysis.treatment_label, analysis.comparator_label, arm.label)
    sema7 = next(a for a in achieve3.analyses if a.origin == "CURATED" and a.comparator_label.startswith("oral semaglutide 7 mg"))
    assert sema7.comparator_arm.label == "7 mg Semaglutide" and "attached to the registered arm '7 mg Semaglutide'" in sema7.notes
    # Foundayo label dosages: the documented bioequivalent dose of the ATTAIN-1 capsule arms
    attain1 = _study(db, "NCT05869903")
    label55 = [r for r in attain1.results if r.origin == "CURATED" and r.arm_label.startswith("FOUNDAYO 5.5 mg")]
    assert label55 and {r.arm.label for r in label55} == {"6 mg Orforglipron"} and all("documented bioequivalent" in r.notes for r in label55)
    believe = next(r for r in _study(db, "NCT05616013").results if r.origin == "CURATED" and r.arm_label == "semaglutide 2.4 mg")
    assert believe.arm.label == "Placebo + 2.4 mg Semaglutide"
    # never: a pooled or titrated group, or a label without one single dose
    assert loader.registered_arm_for(db, _study(db, "NCT04881760"), "retatrutide 4 mg (combined)") == (None, None)
    assert loader.registered_arm_for(db, _study(db, "NCT05929066"), "retatrutide 4 mg to MTD") == (None, None)
    assert loader.registered_arm_for(db, attain1, "FOUNDAYO 14.5 mg once daily") == (None, None)  # no such arm
    for arm in db.scalars(select(StudyArm).where(StudyArm.origin == "CURATED")):
        mapped, _ = loader.registered_arm_for(db, arm.study, arm.label)
        assert mapped is None, (arm.study.nct_id, arm.label, mapped.label)  # every curated-only arm has no unique registered twin


# --------------------------------------------------------------------------- DR-7: a reported number is stored once


def test_achieve4_publication_counts_are_stored_once(db, converted):
    achieve4 = _study(db, "NCT05803421")
    rows = [r for r in achieve4.results if r.origin == "CURATED" and r.source.key == "pubmed:42815506"]
    counts = Counter(r.value for r in rows if r.endpoint.category in ("MACE", "MORTALITY"))
    assert counts == {1.4: 1, 3.2: 1, 4.2: 1, 5.0: 1}, counts  # MACE-4 and all-cause death, each once
    # No curated value is stored more often than its quote prints it (per study).
    per_quote = Counter((r.study_id, r.locator_quote, r.value) for r in db.scalars(select(OutcomeResult).where(OutcomeResult.origin == "CURATED", OutcomeResult.value.is_not(None))))
    printed = {k: sum(1 for magnitude, _ in quote_numbers(k[1]) if abs(magnitude - abs(k[2])) < 1e-9) for k in per_quote}
    assert not [k for k, n in per_quote.items() if n > printed[k]]
    assert "orforglipron:F429: result not generated — the hand-curated file NCT05803421.yaml already holds this row" in "\n".join(converted["rejected"])


# --------------------------------------------------------------------------- DR-8: bioequivalence ratios


def test_bioequivalence_ratios_are_ratios_of_geometric_means(db):
    from incretinos.review.checks import evaluate_all

    rows = [a for a in _study(db, "NCT06440980").analyses if a.origin == "CURATED"]
    assert len(rows) == 12
    assert {(a.param_type, a.ci_level) for a in rows} == {("Ratio of geometric least-squares means", 90.0)}
    flags = evaluate_all(db)
    assert not [f for a in rows for f in flags[("analysis", a.id)] if f.code == "ANALYSIS_SCALE_AMBIGUOUS"]


# --------------------------------------------------------------------------- DR-9: A1C


def test_a1c_change_is_an_hba1c_change(db):
    assert categorize_endpoint("change in A1C from baseline", "%")[0].value == "HBA1C_CHANGE"
    rows = [r for r in _study(db, "NCT06603571").results if r.origin == "CURATED" and "A1C" in r.endpoint.title]
    assert len(rows) == 9 and {r.endpoint.category for r in rows} == {"HBA1C_CHANGE"}


# --------------------------------------------------------------------------- DR-10: missing STARTED is not zero


def test_unreadable_started_count_keeps_the_group_with_a_null_denominator(scratch):
    nct = "NCT05822830"
    raw = _raw(nct)
    period = raw["resultsSection"]["participantFlowModule"]["periods"][0]
    started = next(m for m in period["milestones"] if m["type"] == "STARTED")
    group = started["achievements"][0]["groupId"]
    started["achievements"][0]["numSubjects"] = "NA"
    expected = sum(1 for d in period.get("dropWithdraws", []) for r in d.get("reasons", []) if r.get("groupId") == group)
    assert expected
    CTGovAdapter(FixtureTransport(FIXTURES)).ingest_study(scratch, nct, raw_override=json.dumps(raw).encode())
    title = next(g["title"] for g in raw["resultsSection"]["participantFlowModule"]["groups"] if g["id"] == group)
    rows = list(scratch.scalars(select(ParticipantFlow).where(ParticipantFlow.arm_label == title, ParticipantFlow.period_index == 0, ParticipantFlow.kind == "WITHDRAWAL_REASON")))
    assert len(rows) == expected
    assert all(r.n_started is None and "n_started" in r.missing_fields for r in rows)


# --------------------------------------------------------------------------- CK-1 / CK-2: checks read stored fields


def test_reference_check_reads_the_stored_direction_basis(db):
    from incretinos.review.checks import evaluate_all

    flags = evaluate_all(db)
    assumed = [a for a in db.scalars(select(StatisticalAnalysis).where(StatisticalAnalysis.origin == "REGISTRY")) if any(f.code == "ANALYSIS_REFERENCE_FROM_ARM_ROLE" for f in flags[("analysis", a.id)])]
    assert len(assumed) > 50  # the 62 flags had fallen to 0 when a comment phrase changed
    assert {a.direction_basis for a in assumed} <= {DirectionBasis.ARM_TYPE.value, DirectionBasis.PLACEBO_GROUP.value}
    stated = [a for a in db.scalars(select(StatisticalAnalysis)) if a.direction_basis == DirectionBasis.GROUP_DESCRIPTION.value]
    assert stated and not [a for a in stated if any(f.code.startswith("ANALYSIS_REFERENCE") for f in flags[("analysis", a.id)])]


def test_a_stored_threshold_that_contradicts_title_and_quote_is_flagged(db):
    from incretinos.review.checks import evaluate

    row = next(r for r in _study(db, "NCT05869903").results if r.origin == "CURATED" and r.source.key == "openfda-label:foundayo" and r.value == 18.4)
    assert row.endpoint.threshold == 20.0 and not {f.code for f in evaluate(db, "result", row)} & {"ENDPOINT_THRESHOLD_CONTRADICTED", "ENDPOINT_THRESHOLD_VS_TITLE"}
    row.endpoint.threshold = 10.0  # rolled back by the fixture
    db.flush()
    found = {f.code for f in evaluate(db, "result", row)}
    assert {"ENDPOINT_THRESHOLD_CONTRADICTED", "ENDPOINT_THRESHOLD_VS_TITLE"} <= found


# --------------------------------------------------------------------------- F22: non-inferiority conclusions


def test_ni_met_is_set_only_from_a_quote_that_states_it(db, converter):
    for nct in ("NCT06045221", "NCT04255433"):  # ACHIEVE-3, SURPASS-CVOT: the stored text gives p-values, not the conclusion
        rows = [a for a in _study(db, nct).analyses if a.origin == "CURATED" and a.hypothesis_type == "NON_INFERIORITY"]
        assert rows and all(a.ni_met == "NOT_STATED" and "no quote from the cited source states in words" in a.notes for a in rows), nct
    achieve4 = next(a for a in _study(db, "NCT05803421").analyses if a.origin == "CURATED" and a.hypothesis_type == "NON_INFERIORITY")
    assert achieve4.ni_met == "YES"
    hand = yaml.safe_load((converter.DATA_DIR / "studies" / "NCT05803421.yaml").read_text())
    assert any("demonstrating non-inferiority" in q for a in hand["analyses"] for q in a.get("extra_quotes", []))
    row = {"quote": "(hazard ratio, 0.92; 95.3% confidence interval, 0.83 to 1.01; P = 0.003 for noninferiority)"}
    converter.settle_ni_met(row)
    assert "ni_met" not in row and "NOT_STATED" in row["notes"]
    row = {"quote": "tirzepatide was noninferior to dulaglutide (hazard ratio, 0.92)"}
    converter.settle_ni_met(row)
    assert row["ni_met"] == "YES"


# --------------------------------------------------------------------------- BR-4: SURMOUNT-4 assessment week


def test_surmount4_default_week_is_the_reported_assessment_week(db):
    study = _study(db, "NCT04660643")
    assert (study.design_features["treatment_weeks"], study.design_features["randomized_period_weeks"], study.design_features["lead_in_weeks"]) == (88, 52, 36)
    primary = _raw("NCT04660643")["protocolSection"]["outcomesModule"]["primaryOutcomes"][0]["timeFrame"]
    assert "Week 88" in primary and "Week 36" in primary
    profiles = arm_profiles(db, "NCT04660643")
    assert profiles["selection"]["weight_pct"]["timepoint_weeks"] == 88
    assert [a for a in profiles["arms"] if "weight_pct" in a["efficacy"]] and not profiles["messages"]


# --------------------------------------------------------------------------- WEB-17: reported text of curated values


def test_curated_values_keep_their_reported_text(db):
    assert loader.reported_text(-25.0, "and -25.0% (12-mg dose)") == "-25.0"
    assert loader.reported_text(4.2, "57 (4·2%) of 1358") == "4.2"
    assert loader.reported_text(0.956, "1.01 (0.9560, 1.0705)") == "0.9560"
    assert loader.reported_text(-6.7, "lost 6.7%") == "-6.7"  # sign stated in words: printed digits, stored sign
    assert loader.reported_text(None, "x") is None
    rows = list(db.scalars(select(OutcomeResult).where(OutcomeResult.origin == "CURATED", OutcomeResult.value.is_not(None))))
    assert rows and all(r.value_text is not None and float(r.value_text) == r.value for r in rows)
    trailing = [r for r in rows if r.value_text.endswith("0") and "." in r.value_text]
    assert trailing  # e.g. "-25.0", which used to become "-25"
    for analysis in db.scalars(select(StatisticalAnalysis).where(StatisticalAnalysis.origin == "CURATED", StatisticalAnalysis.estimate.is_not(None))):
        assert float(analysis.estimate_text) == analysis.estimate
