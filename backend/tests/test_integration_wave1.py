"""Guarantees that exist only because several Session 2 branches were merged.

Each test holds two or more pieces of work together: the review lifecycle with the automated checks,
the data repair with the evidence identity, the read-only request session with the review router, the
three schema revisions with one another. Tests that belong to one branch alone stay in that branch's
own test module.
"""

from __future__ import annotations

from collections import Counter

import pytest
from sqlalchemy import func, select

from incretinos.api import auth
from incretinos.api import main as api_main
from incretinos.api.routers import review as review_router
from incretinos.models import (
    REVIEWABLE_MODELS,
    EvidenceVersion,
    HumanStatusWriteError,
    ParticipantFlow,
    StatisticalAnalysis,
    Study,
)
from incretinos.review import checks
from incretinos.review.lifecycle import CHANGE_FLAG, reconcile
from incretinos.review.policy import risk_level

CHECKED_TYPES = ("result", "analysis", "adverse_event", "claim")
RISK_ORDER = ("HIGH", "MEDIUM", "LOW", "NONE")


def _automated(flags: list[dict] | None) -> list[dict]:
    """Stored flags without the ones the lifecycle adds itself (identity collisions, changed since last ingestion)."""
    return [f for f in flags or [] if not str(f["code"]).startswith("IDENTITY_") and f["code"] != CHANGE_FLAG]


# --------------------------------------------------------------------------- review lifecycle x automated checks


def test_reconcile_stores_what_the_checks_report_for_every_record(seeded, db):
    """The flags and the risk level on each row and on its current version are exactly what review.checks returns."""
    assert seeded["review"]["checks"] == "RAN"
    found = checks.evaluate_all(db)
    assert set(found) == {(t, row_id) for t in CHECKED_TYPES for (row_id,) in db.execute(select(REVIEWABLE_MODELS[t].id))}  # one entry per record
    seen_levels: Counter[str] = Counter()
    for evidence_type in CHECKED_TYPES:
        model = REVIEWABLE_MODELS[evidence_type]
        for row_id, stored, risk in db.execute(select(model.id, model.check_flags, model.risk_level)):
            expected = [f.as_dict() for f in found[(evidence_type, row_id)]]
            assert _automated(stored) == expected, (evidence_type, row_id)
            if len(stored) == len(expected):  # no flag of the lifecycle's own on this row: the level is the checks' level
                assert risk == risk_level(found[(evidence_type, row_id)]), (evidence_type, row_id)
            assert risk == (min((f["severity"] for f in stored), key=RISK_ORDER.index) if stored else "NONE"), (evidence_type, row_id)
            seen_levels[risk] += 1
            for flag in stored:
                assert set(flag) == {"code", "severity", "explanation", "fields"} and flag["explanation"]
                if flag["code"] in checks.CHECKS:
                    assert flag["severity"] == checks.CHECKS[flag["code"]]["severity"]
    assert seen_levels["HIGH"] > 0 and seen_levels["MEDIUM"] > 0 and seen_levels["LOW"] > 0 and seen_levels["NONE"] > 0
    # The version index the review list is served from holds the same flags and level as the row.
    versions = {(v.evidence_type, v.evidence_key): v for v in db.scalars(select(EvidenceVersion).where(EvidenceVersion.is_current.is_(True)))}
    for evidence_type, model in REVIEWABLE_MODELS.items():
        for key, stored, risk in db.execute(select(model.evidence_key, model.check_flags, model.risk_level)):
            version = versions[(evidence_type, key)]
            assert (version.check_flags, version.risk_level) == (stored, risk), (evidence_type, key)
            if evidence_type not in CHECKED_TYPES:  # mechanisms, indications and equivalences are not covered by the checks
                assert stored == [] and risk == "NONE"
    # Nothing has changed since the first load, so no record says it changed.
    assert not [1 for model in REVIEWABLE_MODELS.values() for (flags,) in db.execute(select(model.check_flags)) if any(f["code"] == CHANGE_FLAG for f in flags or [])]


def test_the_review_api_returns_the_flags_and_filters_and_sorts_by_risk(client, db):
    stored: Counter[str] = Counter()
    for model in REVIEWABLE_MODELS.values():
        stored.update({level: n for level, n in db.execute(select(model.risk_level, func.count()).group_by(model.risk_level))})
    first = client.get("/api/review/evidence", params={"limit": 1}).json()
    assert first["facets"]["risk_level"] == dict(stored) and sum(stored.values()) == first["total"]
    for level in RISK_ORDER:
        page = client.get("/api/review/evidence", params={"risk_level": level, "limit": 200}).json()
        assert page["total"] == stored.get(level, 0) and all(r["risk_level"] == level for r in page["rows"])
        assert all((r["n_flags"] > 0) == (level != "NONE") and len(r["flag_codes"]) == len(set(r["flag_codes"])) for r in page["rows"])

    def everything(**params) -> list[dict]:
        out: list[dict] = []
        while True:
            body = client.get("/api/review/evidence", params={**params, "limit": 500, "offset": len(out)}).json()
            out += body["rows"]
            if not body["rows"] or len(out) >= body["total"]:
                return out

    # Sorting by risk puts HIGH first and is the exact reverse when descending, over the whole list.
    by_risk = everything(evidence_type="analysis", sort="risk")
    ranks = [RISK_ORDER.index(r["risk_level"]) for r in by_risk]
    assert len(by_risk) == db.scalar(select(func.count()).select_from(StatisticalAnalysis)) and ranks == sorted(ranks) and ranks[0] == 0 and ranks[-1] == 3
    assert [r["evidence_key"] for r in everything(evidence_type="analysis", sort="risk", descending=True)] == [r["evidence_key"] for r in by_risk][::-1]
    # Filters combine: HIGH curated results only.
    high_curated = everything(evidence_type="result", origin="CURATED", risk_level="HIGH")
    assert high_curated and all(r["origin"] == "CURATED" and r["risk_level"] == "HIGH" and r["evidence_type"] == "result" for r in high_curated)
    # The dossier gives every flag with its code, fixed severity, explanation and the fields it concerns.
    detail = client.get(f"/api/review/evidence/by-key/result/{high_curated[0]['evidence_key']}").json()
    assert detail["risk_level"] == "HIGH" and [f["code"] for f in detail["check_flags"]] == high_curated[0]["flag_codes"]
    for flag in detail["check_flags"]:
        assert flag["severity"] == checks.CHECKS[flag["code"]]["severity"] and flag["explanation"] and isinstance(flag["fields"], list)
    assert detail["check_flags"][0]["severity"] == "HIGH"  # ordered worst first
    assert client.get("/api/review/evidence", params={"risk_level": "SEVERE"}).status_code == 422


def test_a_second_reconcile_changes_nothing(seeded, db):
    """With the real checks installed: no row, no version, no flag, no status and no change event moves on a second pass."""
    assert seeded["review"]["changed"] == 0 and seeded["evidence_changes"] == []
    report = reconcile(db)
    assert report.checks == "RAN" and report.rows == seeded["review"]["rows"]
    assert (report.rows_updated, report.new_versions, report.first_seen_keys, report.changed_total, report.transitions_total, report.disappeared_keys) == (0, 0, 0, 0, 0, 0)
    assert report.change_event_ids == [] and report.orphaned_decisions == [] and report.statuses == seeded["review"]["statuses"]
    db.rollback()  # reconcile moved last_seen_at inside this session only


def test_duplicate_flags_and_evidence_key_collisions_agree_on_the_seed(db):
    """The checks' duplicate key and the evidence key answer the same question: can the stored labels tell two rows apart?

    The six lipid analyses of SURMOUNT-4 share endpoint, groups, parameter and method; the registry's own description
    of each analysis separates them. It is stored (data repair), it is in the evidence key (review lifecycle) and it
    is in the checks' duplicate key, so neither mechanism reports them.
    """
    study = db.scalar(select(Study).where(Study.nct_id == "NCT04660643"))
    lipid = [a for a in study.analyses if a.origin == "REGISTRY" and a.endpoint.title.startswith("Percent Change From Randomization in Lipid Parameters")]
    assert len(lipid) == 6 and len({a.group_description for a in lipid}) == 6 and len({a.evidence_key for a in lipid}) == 6
    assert not [f for a in lipid for f in a.check_flags if f["code"].startswith(("DUPLICATE_", "IDENTITY_"))]
    duplicate_flagged = set()
    for evidence_type in CHECKED_TYPES:
        model = REVIEWABLE_MODELS[evidence_type]
        for key, flags in db.execute(select(model.evidence_key, model.check_flags)):
            if any(f["code"].startswith("DUPLICATE_") for f in flags or []):
                duplicate_flagged.add((evidence_type, key))
    collided = {(v.evidence_type, v.evidence_key) for v in db.scalars(select(EvidenceVersion).where(EvidenceVersion.is_current.is_(True), EvidenceVersion.collision_level > 0))}
    assert duplicate_flagged == {k for k in collided if k[0] in CHECKED_TYPES} == set()


# --------------------------------------------------------------------------- data repair x review lifecycle


def test_new_analysis_columns_are_in_the_fingerprint_and_only_the_description_is_in_the_key(db):
    """group_labels, direction_basis and class_title are signed off with the row; changing them asks for a recheck."""
    from incretinos.review import identity

    records = {r.row_id: r for r in identity.load_records(db, identity.latest_snapshots(db)) if r.evidence_type == "analysis"}
    stated = [a for a in db.scalars(select(StatisticalAnalysis)) if a.origin == "REGISTRY" and a.group_labels and a.class_title]
    assert stated, "no registry analysis with a class title in the seed"
    row = stated[0]
    record = records[row.id]
    assert record.snapshot["group_labels"] == row.group_labels and record.snapshot["direction_basis"] == row.direction_basis
    assert record.snapshot["class_title"] == row.class_title and record.snapshot["group_description"] == row.group_description
    assert record.evidence_key == row.evidence_key and record.fingerprint == row.fingerprint  # what is stored is what is derived
    with db.no_autoflush:
        before = (record.natural_key, record.fingerprint)
        for name, value in (("direction_basis", "NOT_STATED"), ("class_title", "another class"), ("group_labels", list(reversed(row.group_labels)))):
            original = getattr(row, name)
            setattr(row, name, value)
            db.flush()
            changed = next(r for r in identity.load_records(db, identity.latest_snapshots(db)) if r.evidence_type == "analysis" and r.row_id == row.id)
            assert changed.natural_key == before[0] and changed.fingerprint != before[1], name  # same identity, new content
            setattr(row, name, original)
        row.group_description = "another analysis of the same outcome"
        db.flush()
        rekeyed = next(r for r in identity.load_records(db, identity.latest_snapshots(db)) if r.evidence_type == "analysis" and r.row_id == row.id)
        assert rekeyed.natural_key != before[0]  # the registry's own description says WHICH analysis this is
    db.rollback()


def test_participant_flow_carries_only_the_mechanical_status_and_cannot_be_given_a_human_one(db):
    statuses = dict(db.execute(select(ParticipantFlow.verification_status, func.count()).group_by(ParticipantFlow.verification_status)).all())
    assert set(statuses) == {"MACHINE_EXTRACTED"} and sum(statuses.values()) > 1000
    assert ParticipantFlow.__table__.c.verification_status.type.length >= len("SOURCE_CHANGED_RECHECK_REQUIRED")
    row = db.scalar(select(ParticipantFlow).limit(1))
    for status in ("HUMAN_VERIFIED", "REJECTED"):
        with pytest.raises(HumanStatusWriteError):
            row.verification_status = status
    db.rollback()


# --------------------------------------------------------------------------- hardening x review lifecycle


def test_every_router_uses_the_one_reviewer_check():
    """The byte-wise token comparison protects the evidence decision route because it is the same function object."""
    assert api_main.require_reviewer is auth.require_reviewer is review_router.require_reviewer
    decisions = next(r for r in review_router.router.routes if r.path.endswith("/decisions"))

    def calls(dependant) -> set:
        return {dependant.call} | {c for d in dependant.dependencies for c in calls(d)}

    assert auth.require_reviewer in calls(decisions.dependant) and decisions.methods == {"POST"}
    for route in review_router.router.routes:  # every other route of the router is a GET and therefore cannot commit
        assert route.methods == ({"POST"} if route is decisions else {"GET"}), route.path


def test_the_three_session_2_revisions_form_one_line_in_the_agreed_order():
    from alembic.script import ScriptDirectory

    from incretinos.db import alembic_config

    scripts = ScriptDirectory.from_config(alembic_config())
    assert [(s.revision, s.down_revision) for s in scripts.walk_revisions()] == [
        ("17814f6d32bf", "9b4e73774dde"),  # foreign-key indexes (hardening)
        ("9b4e73774dde", "65bce81e7f45"),  # evidence review lifecycle
        ("65bce81e7f45", "10d1699882ea"),  # data integrity
        ("10d1699882ea", None),
    ]


# --------------------------------------------------------------------------- claims x review lifecycle x comparability


def test_the_assistant_labels_evidence_with_the_real_statuses_and_never_as_human_verified(db):
    """Every evidence block names the stored status of each record behind it, in the vocabulary of the review lifecycle."""
    from incretinos.assistant.answer import ask
    from incretinos.review.policy import STATUS_LABEL, STORED_STATUSES

    questions = (
        "What evidence differentiates retatrutide from tirzepatide?",
        "Which outcomes in EloraTZP are exploratory?",
        "What would be required to compare SURMOUNT-1 and TRIUMPH-1 rigorously?",
        "Which results depend on hypothetical treatment continuation?",
        "Which programs have prospective cardiovascular outcomes trials?",
    )
    seen: Counter[str] = Counter()
    for question in questions:
        answer = ask(db, question).as_dict()
        kinds = {block["kind"] for block in answer["blocks"]}
        assert "VERIFIED_EVIDENCE" not in kinds and kinds <= {"REPORTED_EVIDENCE", "INTERPRETATION", "UNAVAILABLE", "HYPOTHESIS", "REFUSAL", "MODEL_SUMMARY"}
        for block in answer["blocks"]:
            for record in block["evidence"] or []:
                if record["verification_status"] is not None:
                    assert record["verification_status"] in STORED_STATUSES and record["verification_status"] != "CURATED_PENDING_REVIEW"
                    seen[record["verification_status"]] += 1
            verification = block["verification"]
            if verification is not None:
                assert verification["human_verified"] is False and set(verification["statuses"]) <= STORED_STATUSES
                assert verification["label"] in STATUS_LABEL.values() or verification["label"]
                # The stored status of every record is the one the review API reports for it.
                for record in block["evidence"]:
                    if record["type"] == "result":
                        assert db.scalar(select(REVIEWABLE_MODELS["result"].verification_status).where(REVIEWABLE_MODELS["result"].id == record["id"])) == record["verification_status"]
    assert {"MACHINE_EXTRACTED", "QUOTE_ANCHORED", "PENDING_HUMAN_REVIEW"} <= set(seen) and "HUMAN_VERIFIED" not in seen and "REJECTED" not in seen


def test_the_assistant_leads_with_the_shared_display_rule_not_with_the_evidence_tier(db):
    """ATTAIN-1 reports its treatment-regimen weight change in the label (no interval) and in the publication (with one).

    The answer leads with the highest-dose arm and, of the sources reporting it, with the one the shared display rule
    puts first (a reported interval), which is what the estimand inspector marks as its lead as well.
    """
    from incretinos.assistant.answer import _best_weight_result
    from incretinos.engines.findings import display_lead

    study = db.scalar(select(Study).where(Study.nct_id == "NCT05869903"))
    lead = _best_weight_result(db, study, "TREATMENT_REGIMEN")
    assert (lead.tier, lead.value, lead.ci_low, lead.ci_high, lead.arm.dose_amount) == ("PEER_REVIEWED", -11.2, -12.0, -10.4, 36)
    label_rows = [r for r in study.results if r.tier == "REGULATORY_LABEL" and r.endpoint.category == "WEIGHT_PCT_CHANGE" and r.estimand is not None and r.estimand.label == "TREATMENT_REGIMEN" and r.scope == "ARM"]
    assert label_rows and all(r.ci_low is None for r in label_rows)  # the tier that used to win has no interval
    assert display_lead([lead, *label_rows])[0] is lead
    # One estimand throughout when none is requested: never the larger of two estimands' values.
    for nct in ("NCT05929066", "NCT04184622", "NCT05931367"):
        chosen = _best_weight_result(db, db.scalar(select(Study).where(Study.nct_id == nct)))
        assert chosen is not None and chosen.estimand is not None and chosen.estimand.label == "TREATMENT_REGIMEN", nct
