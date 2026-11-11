"""Guarantees that exist only because the Session 2 P0 fix branches were merged (second integration wave).

Each test holds two or more fix branches together: the stored arm roles (data fix) with the engines and the API
(comparability, frontend); the reviewer identity (lifecycle fix) with the API metadata the workbench reads; the
request-body limits (hardening fix) with the lifecycle's request bodies; the claim verifier (claims fix) with the
analyses' internal consistency (comparability fix); the exported report with the comparison's own reasons.
Tests that belong to one branch alone stay in that branch's own test module.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from incretinos.api import limits
from incretinos.api.main import DecisionRequest, GapReview
from incretinos.api.routers.review import EvidenceDecisionRequest
from incretinos.assistant import benchmark
from incretinos.assistant.answer import check_citation_fidelity
from incretinos.assistant.claims import Vocabulary
from incretinos.assistant.facts import fact_from_analysis
from incretinos.assistant.verifier import effect_supported, internally_inconsistent, superiority_supported
from incretinos.config import get_settings
from incretinos.engines.comparability import compare_findings
from incretinos.engines.findings import analysis_dto, build_finding, get_finding
from incretinos.models import OutcomeResult, StatisticalAnalysis, Study, StudyArm
from incretinos.reports.export import comparison_report_markdown
from incretinos.vocab import ARM_ROLE_LABEL, ArmRole, is_placebo_arm

# --------------------------------------------------------------------------- stored arm roles x engines x API


def test_a_treatment_sequence_is_never_a_placebo_arm_or_a_dose(db):
    arms = db.scalars(select(StudyArm)).all()
    assert all(a.arm_role in ARM_ROLE_LABEL for a in arms)  # every arm carries a stored role after the seed
    sequences = [a for a in arms if a.arm_role == ArmRole.SEQUENCE.value]
    assert sequences, "the seed holds placebo/active switch arms (BELIEVE)"
    for arm in sequences:
        assert arm.arm_type == "SEQUENCE", arm.label
        if arm.origin == "REGISTRY":
            assert arm.registered_arm_type == "PLACEBO_COMPARATOR", arm.label  # the type as registered is kept apart
        assert not arm.is_placebo and arm.drug_slug is None and arm.dose_amount is None, arm.label
        assert not is_placebo_arm(arm)
    # The engines' placebo test agrees with the stored role for every arm on the seed.
    for arm in arms:
        assert is_placebo_arm(arm) == (arm.arm_role == ArmRole.PLACEBO.value and bool(arm.is_placebo)), (arm.id, arm.label)
    # A stored flag never outranks the stored role or type.
    assert not is_placebo_arm(SimpleNamespace(is_placebo=True, arm_role="SEQUENCE", arm_type="PLACEBO_COMPARATOR"))
    assert not is_placebo_arm(SimpleNamespace(is_placebo=True, arm_role=None, arm_type="SEQUENCE"))
    assert not is_placebo_arm(SimpleNamespace(is_placebo=True, arm_role="UNCLASSIFIED", arm_type="PLACEBO_COMPARATOR"))
    assert is_placebo_arm(SimpleNamespace(is_placebo=True, arm_role=None, arm_type="PLACEBO_COMPARATOR"))  # a database stored before roles


def test_arm_roles_are_on_findings_benefit_risk_and_study_arms(client, db):
    # The sequence arm with the most results linked to it (BELIEVE's placebo/bimagrumab switch arm on the seed).
    linked = {arm_id: n for arm_id, n in db.execute(select(OutcomeResult.arm_id, func.count()).group_by(OutcomeResult.arm_id)) if arm_id is not None}
    sequence = max(db.scalars(select(StudyArm).where(StudyArm.arm_role == ArmRole.SEQUENCE.value)), key=lambda a: (linked.get(a.id, 0), -a.id))
    assert linked.get(sequence.id), "a sequence arm with results on the seed"
    study = db.get(Study, sequence.study_id)
    detail = client.get(f"/api/studies/{study.nct_id}").json()
    stored = {a.id: a.arm_role for a in study.arms}
    assert {a["id"]: a["arm_role"] for a in detail["arms"]} == stored
    assert all(a["arm_role_label"] == ARM_ROLE_LABEL[a["arm_role"]] for a in detail["arms"])

    # Findings carry the stored role of their arm; a sequence arm's finding is not a placebo finding.
    rows = db.scalars(select(OutcomeResult).where(OutcomeResult.study_id == study.id, OutcomeResult.arm_id.is_not(None))).all()
    assert rows
    for row in rows:
        finding = build_finding(db, row)
        assert finding.arm_role == row.arm.arm_role and finding.arm_role_label == ARM_ROLE_LABEL[row.arm.arm_role]
        if row.arm.arm_role == ArmRole.SEQUENCE.value:
            assert not finding.is_placebo and finding.dose_amount is None
    listed = client.get("/api/findings", params={"study": study.nct_id, "limit": 200}).json()["rows"]
    assert listed and all(f["arm_role"] in ARM_ROLE_LABEL and f["arm_role_label"] == ARM_ROLE_LABEL[f["arm_role"]] for f in listed if f["arm_id"] is not None)
    assert any(f["arm_role"] == ArmRole.SEQUENCE.value and not f["is_placebo"] for f in listed)

    # Benefit-risk arm profiles: every profile states its role; a sequence is never the placebo profile or a dose.
    profile = client.get(f"/api/benefit-risk/{study.nct_id}")
    assert profile.status_code == 200, profile.text[:300]
    arms = profile.json()["arms"]
    assert arms and all(a["arm_role"] in ARM_ROLE_LABEL and a["arm_role_label"] == ARM_ROLE_LABEL[a["arm_role"]] for a in arms)
    for a in arms:
        if a["arm_role"] == ArmRole.SEQUENCE.value:
            assert not a["is_placebo"] and a["dose_amount"] is None, a["label"]
        if a["is_placebo"]:
            assert a["arm_role"] == ArmRole.PLACEBO.value, a["label"]


# --------------------------------------------------------------------------- reviewer identity x workbench


def test_meta_and_review_summary_state_how_the_reviewer_is_named(client, monkeypatch):
    settings = get_settings()
    assert client.get("/api/meta").json()["reviewer_identity"] == client.get("/api/review/summary").json()["reviewer_identity"] == "SHARED_TOKEN_SELF_DECLARED"
    assert settings.reviewer_identity_mode == "SHARED_TOKEN_SELF_DECLARED"
    monkeypatch.setattr(settings, "reviewers_file", "/nonexistent/reviewers.json")
    assert settings.reviewer_identity_mode == "PERSONAL_TOKENS"  # the workbench then sends no typed name
    monkeypatch.setattr(settings, "reviewers_file", None)
    monkeypatch.setattr(settings, "reviewer_token", None)
    assert settings.reviewer_identity_mode == "READ_ONLY"


# --------------------------------------------------------------------------- body limits x lifecycle request bodies


def test_the_largest_legitimate_review_bodies_fit_the_reviewer_limit():
    """The lifecycle's request schemas, filled to their bounds with multi-byte text and pretty-printed, fit the limit."""
    settings = get_settings()
    text = "Ω—é"  # 2, 3 and 2 bytes in UTF-8
    def fill(n: int) -> str:
        return (text * (n // len(text) + 1))[:n]
    evidence = {
        "decision": "PROPOSE_CORRECTION",
        "reviewer": fill(120),
        "rationale": fill(4000),
        "attest_human_review": True,
        "expected_fingerprint": "f" * 64,
        "proposed_correction": {"changes": {f"{i:02d}" + fill(198): fill(2000) for i in range(40)}, "supporting_quote": fill(4000), "note": fill(2000)},
    }
    gap = {"review_status": "CONFIRMED", "reviewer_note": fill(2000), "clinical_importance": 5, "reviewer": fill(120)}
    queue = {"decision": "APPROVE", "reviewer": fill(120), "rationale": fill(2000)}
    for model, body, path, method in (
        (EvidenceDecisionRequest, evidence, "/api/review/evidence/result/1/decisions", "POST"),
        (GapReview, gap, "/api/gaps/1", "PATCH"),
        (DecisionRequest, queue, "/api/review-queue/1/decision", "POST"),
    ):
        model.model_validate(body)  # a body the schema accepts
        size = len(json.dumps(body, indent=2, ensure_ascii=False).encode())
        limit = limits.body_limit(method, path, has_reviewer_token=True, cfg=settings)
        assert limit == settings.max_review_body_bytes and size <= limit, (path, size, limit)


# --------------------------------------------------------------------------- claim verifier x analysis consistency


def test_an_internally_inconsistent_analysis_supports_no_significance_superiority_or_direction_claim(db):
    inconsistent = []
    for analysis in db.scalars(select(StatisticalAnalysis)):
        status = analysis_dto(analysis)["internal_consistency"]["status"]
        fact = fact_from_analysis(analysis)
        assert fact.internal_consistency == status
        if status not in ("CONSISTENT", "NOT_CHECKABLE"):
            inconsistent.append(analysis.id)
            assert internally_inconsistent(fact) and "internally inconsistent" in internally_inconsistent(fact)
            assert effect_supported(fact) == (False, internally_inconsistent(fact))
            assert superiority_supported(fact)[0] is False
    assert len(inconsistent) >= 3  # the registry records whose interval contradicts their own p-value or estimate

    # The same wording, on the same analysis, is accepted while the record agrees with itself and refused when it does not.
    label_etd = {"kind": "analysis", "nct": "NCT04184622", "category": "WEIGHT_PCT_CHANGE", "treatment_contains": "15 mg", "comparator_contains": "placebo", "tier": "REGULATORY_LABEL"}
    fact = benchmark.resolve_evidence(db, label_etd)
    assert fact.internal_consistency in ("CONSISTENT", "NOT_CHECKABLE") and effect_supported(fact)[0]
    broken = replace(fact, internal_consistency="P_VALUE_DISAGREES_WITH_INTERVAL", internal_consistency_detail="The stored record is internally inconsistent: test.")
    vocab = Vocabulary.from_session(db)
    sentence = "Tirzepatide 15 mg reduced body weight by 17.8 percentage points more than placebo in SURMOUNT-1 at week 72 [C1]."

    def check(f):
        return check_citation_fidelity(sentence, [{"id": "C1", "text": f.text, "source_ids": f.source_ids, "fact": f}], vocabulary=vocab)

    assert check(fact)["passed"], check(fact)["problems"]
    refused = check(broken)
    assert not refused["passed"] and "internally inconsistent" in json.dumps(refused["problems"])
    assert effect_supported(broken)[0] is False and superiority_supported(replace(broken, superiority_met="YES"))[0] is False


# --------------------------------------------------------------------------- comparability x report export


def test_the_report_states_the_comparisons_own_reason_when_no_difference_is_formed(db):
    """Two arms at different assessment weeks: no A - B is formed, and the report says why in the comparison's words."""
    rows = db.scalars(select(OutcomeResult).where(OutcomeResult.value.is_not(None), OutcomeResult.arm_id.is_not(None), OutcomeResult.scope == "ARM")).all()
    by_key: dict[tuple, list[OutcomeResult]] = {}
    for r in rows:
        by_key.setdefault((r.study_id, r.endpoint.category, r.unit, r.arm.drug_slug, r.arm.dose_amount), []).append(r)
    pair = None
    for group in by_key.values():
        weeks = {r.timepoint_weeks: r for r in group if r.timepoint_weeks is not None}
        if len(weeks) > 1:
            first, last = min(weeks), max(weeks)
            if last - first > 1:
                pair = (weeks[first], weeks[last])
                break
    assert pair is not None
    comparison = compare_findings(db, get_finding(db, pair[0].id), get_finding(db, pair[1].id))
    assert comparison.descriptive_difference is None
    markdown = comparison_report_markdown(db, pair[0].id, pair[1].id)
    section = markdown.split("## Descriptive difference", 1)[1].split("##", 1)[0]
    assert "the classification does not permit" not in section
    expected = next((r for r in comparison.reasons if r.startswith("No arithmetic difference is given")), comparison.reasons[0])
    assert "Not computed." in section and expected.split(":")[0] in section


# --------------------------------------------------------------------------- claims benchmark x held-out set v2


def test_a_drug_status_selector_may_name_the_drug_by_slug(db):
    """heldout-v2 writes ``slug:`` where the runner reads ``drug:``; both name the same drug, and two different names are refused."""
    by_drug = benchmark.resolve_evidence(db, {"kind": "drug_status", "drug": "tirzepatide"})
    by_slug = benchmark.resolve_evidence(db, {"kind": "drug_status", "slug": "tirzepatide"})
    assert by_slug.text == by_drug.text and by_slug.regulatory_status == by_drug.regulatory_status
    with pytest.raises(benchmark.BenchmarkError):
        benchmark.resolve_evidence(db, {"kind": "drug_status", "drug": "tirzepatide", "slug": "retatrutide"})
    with pytest.raises(benchmark.BenchmarkError):
        benchmark.resolve_evidence(db, {"kind": "drug_status", "slug": "no-such-drug"})
