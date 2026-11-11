"""Review lifecycle P0 fixes (Session 2): one test per reproduced defect, each failing before its fix.

Reuses the laboratory database of ``test_review_lifecycle`` (two studies, a cut of the curated files, the real
fixtures, seeded once per module; every test works on its own copy). Every change to a value, a title or a label
here is a labelled perturbation in a temporary directory, never a statement about the real source. Decisions are
recorded by a simulated reviewer on those copies only.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import StatementError
from typer.testing import CliRunner

from incretinos.config import get_settings
from incretinos.models import (
    AuditEvent,
    EvidenceClaim,
    EvidenceGap,
    OutcomeResult,
    ReviewDecision,
    ReviewQueueItem,
)
from incretinos.review import ledger as ledger_module
from incretinos.review import lifecycle
from incretinos.review.lifecycle import (
    CHANGED_SINCE_REJECTION,
    DecisionFact,
    aggregate_summaries,
    effective_status,
    reconcile,
    record_decision,
)
from incretinos.review.policy import is_usable, reviewer_problem

from .conftest import FIXTURES
from .test_review_lifecycle import (
    ANCHORED,
    HUMAN_VERIFIED,
    MACHINE,
    PRIMARY_TITLE,
    RATIONALE,
    RECHECK,
    REGISTRY_FIXTURE,
    REJECTED,
    REVIEWER,
    SURMOUNT_1,
    TOKEN,
    _edit_yaml,
    _failed_rules,
    lab,
    template,
)

#: The laboratory fixtures of test_review_lifecycle, made available here (pytest finds fixtures by name in the module).
SHARED_FIXTURES = (lab, template)
CLI = __import__("incretinos.cli", fromlist=["app"]).app
REJECT_WHY = "The stored value is not the one posted for this group in the cited table."


def _retitle_primary_outcome(document: dict) -> None:
    """A labelled perturbation: the sponsor retitles the first outcome measure (punctuation only, same measurement)."""
    measure = document["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
    assert measure["title"] == PRIMARY_TITLE
    measure["title"] = "Percent Change From Baseline in Body Weight - Primary Treatment Period"


def _retitle_placebo_ae_group(document: dict) -> None:
    """A labelled perturbation: the sponsor renames the placebo adverse-event group."""
    group = next(g for g in document["resultsSection"]["adverseEventsModule"]["eventGroups"] if g["id"] == "EG000")
    assert group["title"] == "Placebo"
    group["title"] = "Placebo QW"


def _findings_ids(lab) -> set[int]:
    body = lab.client.get("/api/findings", params={"study": SURMOUNT_1, "arm_level_only": False, "limit": 2000}).json()
    return {r["result_id"] for r in body["rows"]}


def _row_by_label(lab, label: str, **params: Any) -> dict[str, Any]:
    rows = [r for r in lab.rows(nct=SURMOUNT_1, **params) if r["label"] == label]
    assert len(rows) == 1, [r["label"] for r in lab.rows(nct=SURMOUNT_1, **params)][:20]
    return rows[0]


# --------------------------------------------------------------------------- F2: a rejection outlives a change of content


def test_f2_pure_core_a_rejection_of_an_older_version_holds_the_row_out_of_use():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    rejected = DecisionFact(decision="REJECT", fingerprint="0" * 64, actor_kind="HUMAN", attested=True, decided_at=t0, id=1)
    outcome = effective_status(mechanical=ANCHORED, decisions=[rejected], fingerprint="f" * 64, structured=False, source_content_hash=None)
    assert (outcome.status, outcome.reason, outcome.hold) == (RECHECK, CHANGED_SINCE_REJECTION, True)
    assert not is_usable(outcome.status, outcome.hold)
    approved = DecisionFact(decision="APPROVE", fingerprint="f" * 64, actor_kind="HUMAN", attested=True, decided_at=t0 + timedelta(minutes=1), id=2)
    after = effective_status(mechanical=ANCHORED, decisions=[rejected, approved], fingerprint="f" * 64, structured=False, source_content_hash=None)
    assert (after.status, after.hold) == (HUMAN_VERIFIED, False)  # only a new decision ends the exclusion


def test_f2_a_rejected_row_whose_notes_change_stays_excluded_until_a_reviewer_decides_again(lab):
    row = lab.curated_result("tirzepatide 5 mg")
    assert lab.decide(row, "REJECT", rationale=REJECT_WHY).json()["status_after"] == REJECTED

    def edit(document: dict) -> None:  # only the free-text note changes; the value does not
        entry = next(r for r in document["results"] if r["arm"] == "tirzepatide 5 mg")
        entry["notes"] = entry["notes"] + " (Note reworded by a test.)"

    _edit_yaml(lab.curated / "studies" / f"{SURMOUNT_1}.generated.yaml", edit)
    lab.reseed()
    detail = lab.by_key(row)
    assert detail["fingerprint"] != row["fingerprint"] and detail["values"]["value"] == -15.0
    status = detail["status"]
    assert (status["verification_status"], status["reason"], status["review_hold"], status["usable"]) == (RECHECK, CHANGED_SINCE_REJECTION, True, False)
    assert "excluded" in status["exclusion_reason"]
    new_id = detail["id"]
    assert new_id not in _findings_ids(lab) and lab.client.get(f"/api/findings/{new_id}").status_code == 404
    listed = lab.one(changed_since_review=True)
    assert listed["evidence_key"] == row["evidence_key"] and listed["usable"] is False and listed["review_hold"] is True
    with lab.session() as session:
        assert session.get(OutcomeResult, new_id) is None  # the choke point hides it from every ORM read
    assert lab.client.get("/api/review/summary").json()["held_total"] == 1
    assert [r.rule_id for r in _failed_rules(lab)] == []

    # A decision on the current version ends the hold.
    response = lab.decide(row, "APPROVE", rationale="The reworded note does not change the value; the value matches the abstract.")
    body = response.json()
    assert (body["status_before"], body["status_after"], body["review_hold"], body["usable"]) == (RECHECK, HUMAN_VERIFIED, False, True)
    assert lab.client.get(f"/api/findings/{new_id}").status_code == 200


# --------------------------------------------------------------------------- F1: decisions follow a relabel


def test_f1_a_registry_retitle_carries_a_rejection_to_the_relabelled_row_and_keeps_it_excluded(lab):
    rejected = lab.registry_primary("15 mg Tirzepatide")
    approved = lab.registry_primary("10 mg Tirzepatide")
    assert lab.decide(rejected, "REJECT", rationale=REJECT_WHY).json()["status_after"] == REJECTED
    assert lab.decide(approved, "APPROVE").json()["status_after"] == HUMAN_VERIFIED
    lab.check_updates({REGISTRY_FIXTURE: _retitle_primary_outcome})
    new_label = "SURMOUNT-1 — Percent Change From Baseline in Body Weight - Primary Treatment Period — 15 mg Tirzepatide"
    moved = _row_by_label(lab, new_label, evidence_type="result")
    assert moved["evidence_key"] != rejected["evidence_key"]
    detail = lab.by_key(moved)
    assert detail["values"]["value"] == -22.5
    assert (detail["status"]["verification_status"], detail["status"]["review_hold"], detail["status"]["usable"]) == (RECHECK, True, False)
    assert detail["decisions"][0]["recorded_under_other_key"] is True and detail["decisions"][0]["decision"] == "REJECT"
    assert detail["status"]["decided_by"]["evidence_key"] == rejected["evidence_key"]
    # The rejected value is in no analytic surface under its new identity.
    assert detail["id"] not in _findings_ids(lab) and lab.client.get(f"/api/findings/{detail['id']}").status_code == 404
    assert "-22.5" not in json.dumps(lab.client.get(f"/api/benefit-risk/{SURMOUNT_1}", params={"estimand": "EFFICACY", "weeks": 72}).json()["arms"])
    # The approval followed too, as a recheck (the reviewed title is not the current one); it stays usable.
    approved_now = lab.by_key(_row_by_label(lab, new_label.replace("15 mg", "10 mg"), evidence_type="result"))
    assert (approved_now["status"]["verification_status"], approved_now["status"]["review_hold"], approved_now["status"]["usable"]) == (RECHECK, False, True)
    # Nothing is orphaned; the link is audited with both keys and reported.
    summary = lab.client.get("/api/review/summary").json()
    assert summary["orphaned_decisions_total"] == 0 and summary["decisions_carried_by_anchor_total"] == 2
    carried = {c["from_evidence_key"]: c for c in summary["decisions_carried_by_anchor"]}
    assert carried[rejected["evidence_key"]]["to_evidence_key"] == moved["evidence_key"] and carried[rejected["evidence_key"]]["review_hold"] is True
    events = {e["detail"]["from_evidence_key"]: e for e in lab.audit("review.decision_carried_by_anchor")}
    assert events[rejected["evidence_key"]]["detail"]["to_evidence_key"] == moved["evidence_key"] and events[rejected["evidence_key"]]["detail"]["excluded"] is True
    assert [r.rule_id for r in _failed_rules(lab)] == []

    # The link is kept: a reseed and a second reconcile leave the row held, and audit nothing again.
    lab.reseed({REGISTRY_FIXTURE: _retitle_primary_outcome})
    with lab.session() as session:
        again = reconcile(session)
        session.commit()
    assert again.anchored_decisions == [] and not again.changed_anything
    assert lab.by_key(moved)["status"]["review_hold"] is True and len(lab.audit("review.decision_carried_by_anchor")) == 2

    # A reviewer decides on the relabelled row: the hold ends.
    body = lab.decide(moved, "REJECT", rationale="Still not the value posted for this group under the new outcome title.").json()
    assert (body["status_after"], body["review_hold"]) == (REJECTED, False)


def test_f1_a_renamed_adverse_event_group_carries_the_rejection_to_the_same_term_only(lab):
    nausea = [r for r in lab.rows(nct=SURMOUNT_1, evidence_type="adverse_event", origin="REGISTRY", q="nausea") if r["label"] == "SURMOUNT-1 — Nausea — Placebo"]
    assert len(nausea) == 1
    assert lab.decide(nausea[0], "REJECT", rationale="These counts belong to another results group of the registry record.").json()["status_after"] == REJECTED
    lab.check_updates({REGISTRY_FIXTURE: _retitle_placebo_ae_group})
    moved = [r for r in lab.rows(nct=SURMOUNT_1, evidence_type="adverse_event", origin="REGISTRY", q="nausea") if r["label"] == "SURMOUNT-1 — Nausea — Placebo QW"]
    assert len(moved) == 1
    detail = lab.by_key(moved[0])
    assert (detail["status"]["verification_status"], detail["status"]["review_hold"]) == (RECHECK, True)
    held = [r for r in lab.all_rows(nct=SURMOUNT_1, evidence_type="adverse_event") if r["review_hold"]]
    assert [r["evidence_key"] for r in held] == [moved[0]["evidence_key"]]  # one row, not every row with the same counts
    table = lab.client.get("/api/safety/trial-ae", params={"nct": SURMOUNT_1, "term": "nausea"}).json()
    assert detail["id"] not in {r["id"] for r in table["rows"]}


def test_f1_a_curated_reword_of_the_baseline_definition_carries_the_rejection(lab):
    row = lab.curated_result("tirzepatide 5 mg")
    assert lab.decide(row, "REJECT", rationale=REJECT_WHY).json()["status_after"] == REJECTED

    def edit(document: dict) -> None:
        entry = next(r for r in document["results"] if r["arm"] == "tirzepatide 5 mg")
        assert "randomization" in entry["baseline_definition"]
        entry["baseline_definition"] = entry["baseline_definition"].replace("randomization", "randomisation")

    _edit_yaml(lab.curated / "studies" / f"{SURMOUNT_1}.generated.yaml", edit)
    lab.reseed()
    moved = lab.curated_result("tirzepatide 5 mg")
    assert moved["evidence_key"] != row["evidence_key"]
    detail = lab.by_key(moved)
    assert (detail["status"]["verification_status"], detail["status"]["review_hold"]) == (RECHECK, True)
    assert lab.client.get(f"/api/findings/{detail['id']}").status_code == 404
    assert lab.client.get("/api/review/summary").json()["orphaned_decisions_total"] == 0


def test_f1_an_orphan_that_matches_nothing_is_raised_as_a_high_priority_queue_item_and_kept(lab):
    row = lab.curated_result("tirzepatide 5 mg")
    assert lab.decide(row, "REJECT", rationale=REJECT_WHY).json()["status_after"] == REJECTED
    path = lab.curated / "studies" / f"{SURMOUNT_1}.generated.yaml"
    original = path.read_text(encoding="utf-8")

    def edit(document: dict) -> None:  # the value changes as well as a label: no current row holds the rejected measurement
        entry = next(r for r in document["results"] if r["arm"] == "tirzepatide 5 mg")
        entry["baseline_definition"] = entry["baseline_definition"].replace("randomization", "randomisation")
        entry["value"] = -15.2
        entry["quote"] = entry["quote"].replace("-15.0%", "-15.2%")

    _edit_yaml(path, edit)
    lab.reseed()
    summary = lab.client.get("/api/review/summary").json()
    assert summary["orphaned_decisions_total"] == 1 and summary["orphaned_rejections_total"] == 1
    [orphan] = summary["orphaned_decisions"]
    assert orphan["evidence_key"] == row["evidence_key"] and orphan["excluded_value_may_be_in_use"] is True
    queue = lab.client.get("/api/review-queue", params={"item_type": "ORPHANED_DECISION"}).json()["rows"]
    assert len(queue) == 1 and queue[0]["priority"] == "HIGH" and queue[0]["target_id"] == orphan["decision_id"] and "may be in use again" in queue[0]["reason"]
    lab.reseed()  # raised once
    assert len(lab.client.get("/api/review-queue", params={"item_type": "ORPHANED_DECISION"}).json()["rows"]) == 1
    # The original entry comes back: the decision applies again and the item is retired, not deleted.
    path.write_text(original, encoding="utf-8")
    lab.reseed()
    assert lab.status(row) == REJECTED
    assert lab.client.get("/api/review-queue", params={"item_type": "ORPHANED_DECISION"}).json()["rows"] == []
    retired = lab.client.get("/api/review-queue", params={"item_type": "ORPHANED_DECISION", "status": "RETIRED"}).json()["rows"]
    assert len(retired) == 1 and lab.audit("review.orphan_resolved")


# --------------------------------------------------------------------------- F3: a rejected regulatory-status statement is shown nowhere


def _regulatory_claim(lab) -> dict[str, Any]:
    rows = [r for r in lab.rows(evidence_type="claim", drug="tirzepatide") if "Regulatory Status" in r["label"]]
    assert len(rows) == 1, [r["label"] for r in rows]
    return rows[0]


def test_f3_a_rejected_regulatory_status_statement_is_not_shown_on_any_surface(lab):
    client = lab.client
    claim = _regulatory_claim(lab)
    with lab.session() as session:
        drug_summary = session.execute(text("SELECT regulatory_summary FROM drug WHERE slug = 'tirzepatide'")).scalar()
    assert drug_summary and len(drug_summary) > 20
    before = client.get("/api/drugs/tirzepatide").json()
    assert before["regulatory_summary"] == drug_summary and before["regulatory_status_available"] is True

    assert lab.decide(claim, "REJECT", rationale="The approval statement cites the wrong label section and population.").json()["status_after"] == REJECTED

    def check(drug: dict[str, Any], where: str) -> None:
        assert drug["regulatory_status"] == "UNKNOWN", where
        assert drug["regulatory_summary"] is None and drug["regulatory_as_of"] is None and drug["regulatory_source"] is None, where
        assert drug["regulatory_status_available"] is False and "rejected in review" in drug["regulatory_status_note"], where

    check(client.get("/api/drugs/tirzepatide").json(), "drug profile")
    check(next(d for d in client.get("/api/drugs").json()["rows"] if d["slug"] == "tirzepatide"), "drug list")
    check(next(d for d in client.get("/api/overview").json()["drugs"] if d["slug"] == "tirzepatide"), "overview")
    for name in ("drug profile", "drug list", "overview"):
        assert drug_summary not in json.dumps({"p": client.get("/api/drugs/tirzepatide").json(), "l": client.get("/api/drugs").json(),
                                               "o": client.get("/api/overview").json()["drugs"]}), name
    # The review dossier's drug block (an inspection surface) shows no status either.
    dossier = lab.by_key(lab.registry_primary("15 mg Tirzepatide"))
    assert next(d for d in dossier["drugs"] if d["slug"] == "tirzepatide")["regulatory_status"] == "UNKNOWN"
    # The assistant's drug fact neither states nor cites it.
    from incretinos.assistant.facts import fact_from_drug
    from incretinos.models import Drug

    with lab.session() as session:
        drug = session.scalar(select(Drug).where(Drug.slug == "tirzepatide"))
        fact = fact_from_drug(session, drug)
        assert fact.regulatory_status is None and drug_summary not in fact.text and not fact.usable
    answer = client.post("/api/assistant/ask", json={"question": "What is the regulatory status of tirzepatide?"}).json()
    assert drug_summary not in json.dumps(answer)
    # Semaglutide's statement was not rejected: its status is shown.
    assert client.get("/api/drugs/semaglutide").json()["regulatory_status_available"] is True


def test_f3_a_held_regulatory_statement_stays_unavailable_after_its_text_changes(lab):
    claim = _regulatory_claim(lab)
    assert lab.decide(claim, "REJECT", rationale="The approval statement cites the wrong label section and population.").json()["status_after"] == REJECTED

    def edit(document: dict) -> None:
        drug = next(d for d in document["drugs"] if d["slug"] == "tirzepatide")
        drug["regulatory"]["summary"] = drug["regulatory"]["summary"] + " (Reworded by a test.)"

    _edit_yaml(lab.curated / "drugs.yaml", edit)
    lab.reseed()
    detail = lab.by_key(claim)
    assert (detail["status"]["verification_status"], detail["status"]["review_hold"]) == (RECHECK, True)
    profile = lab.client.get("/api/drugs/tirzepatide").json()
    assert profile["regulatory_status"] == "UNKNOWN" and profile["regulatory_summary"] is None


# --------------------------------------------------------------------------- F4: gaps never keep a rejected value


def test_f4_rejecting_a_value_refreshes_the_gaps_that_quote_it_in_the_same_transaction(db):
    gap = db.scalar(select(EvidenceGap).where(EvidenceGap.rule_id == "G12_SOURCE_CONFLICT", EvidenceGap.available_evidence.like("%-7.5 (peer reviewed%")))
    assert gap is not None, "the seeded database has the ATTAIN-1 6 mg source conflict"
    key = gap.key
    source_id = next(int(part.split("#")[1].rstrip(")")) for part in gap.available_evidence.split(";") if "-7.5 (" in part)
    result = db.scalar(select(OutcomeResult).where(OutcomeResult.value == -7.5, OutcomeResult.source_id == source_id, OutcomeResult.scope == "ARM"))
    assert result is not None and result.estimand is not None and result.estimand.label == "TREATMENT_REGIMEN"
    recorded = record_decision(db, evidence_type="result", row_id=result.id, decision="REJECT", reviewer=REVIEWER, rationale=REJECT_WHY, attested=True,
                               expected_fingerprint=result.fingerprint)
    assert recorded.status_after == REJECTED and recorded.gaps_refreshed is not None
    # Same transaction, not committed: the conflict existed only because of the rejected value, and it is gone.
    assert db.scalar(select(EvidenceGap).where(EvidenceGap.key == key)) is None
    assert not db.scalars(select(EvidenceGap).where(EvidenceGap.available_evidence.like(f"%source #{source_id})%"), EvidenceGap.available_evidence.like("%-7.5 (%"))).all()


# --------------------------------------------------------------------------- F5: concurrent decisions


def test_f5_concurrent_decisions_on_one_row_store_the_status_the_history_yields(lab, monkeypatch):
    row = lab.registry_primary("15 mg Tirzepatide")
    detail = lab.by_key(row)
    rid, fingerprint = detail["id"], detail["fingerprint"]
    original = lifecycle.decisions_for
    outcome: dict[str, Any] = {}
    calls = {"n": 0}

    def session_b() -> None:
        with lab.session() as other:
            try:
                recorded = record_decision(other, evidence_type="result", row_id=rid, decision="REJECT", reviewer="Dr. Second Reviewer",
                                           rationale="Rejected concurrently: the posted value belongs to another results group.", attested=True,
                                           expected_fingerprint=fingerprint)
                other.commit()
                outcome["b"] = recorded
            except Exception as exc:  # pragma: no cover - reported by the assertion below
                outcome["b_error"] = exc

    thread = threading.Thread(target=session_b)

    def interleave(*args: Any, **kwargs: Any):
        found = original(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1 and threading.current_thread() is threading.main_thread():
            # Session A has read the history. Session B now tries to record and commit a rejection.
            thread.start()
            time.sleep(0.8)
        return found

    monkeypatch.setattr(lifecycle, "decisions_for", interleave)
    with lab.session() as session_a:
        a = record_decision(session_a, evidence_type="result", row_id=rid, decision="APPROVE", reviewer=REVIEWER, rationale=RATIONALE, attested=True,
                            expected_fingerprint=fingerprint)
        session_a.commit()
    thread.join(timeout=30)
    assert "b_error" not in outcome, outcome.get("b_error")
    b = outcome["b"]
    # B waited for A: it saw A's approval, and its rejection is the latest decision.
    assert (a.status_before, a.status_after) == (MACHINE, HUMAN_VERIFIED)
    assert (b.status_before, b.status_after) == (HUMAN_VERIFIED, REJECTED)
    assert lab.status(row) == REJECTED and lab.by_key(row)["status"]["derived_status"] == REJECTED
    assert [r.rule_id for r in _failed_rules(lab)] == []


# --------------------------------------------------------------------------- F6 / F10: ledger import


def _ledger_entry(row_detail: dict[str, Any], decision: str, created_at: str, *, snapshot: dict[str, Any] | None = None, fingerprint: str | None = None,
                  key: str | None = None, reviewer: str = "Dr. Ledger Reviewer") -> dict[str, Any]:
    record = {
        "uid": f"test-{hashlib.sha256((created_at + decision + (key or '')).encode()).hexdigest()[:24]}", "evidence_type": row_detail["evidence_type"],
        "evidence_key": key or row_detail["evidence_key"], "natural_key": row_detail["natural_key"], "fingerprint": fingerprint or row_detail["fingerprint"],
        "decision": decision, "reviewer": reviewer, "rationale": "Checked the posted value against the registry results table.", "attested": True,
        "actor_kind": "HUMAN", "created_at": created_at, "source_key": None, "source_content_hash": None, "source_snapshot_version": None,
        "proposed_correction": None, "status_before": None, "status_after": None, "label": None, "study_nct": None, "snapshot": snapshot,
    }
    record["checksum"] = ledger_module._checksum(record)
    return record


def _ledger(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"format": "incretinos-review-ledger", "version": 1, "exported_at": datetime.now(UTC).isoformat(), "n_decisions": len(entries), "decisions": list(entries)}


def test_f6_an_imported_decision_with_a_non_utc_offset_is_ordered_by_its_real_instant(lab):
    row = lab.registry_primary("15 mg Tirzepatide")
    assert lab.decide(row, "REJECT", rationale=REJECT_WHY).json()["status_after"] == REJECTED
    detail = lab.by_key(row)
    rejected_at = datetime.fromisoformat(detail["decisions"][0]["created_at"].replace("Z", "+00:00"))
    rejected_at = rejected_at if rejected_at.tzinfo else rejected_at.replace(tzinfo=UTC)
    # An approval made one hour BEFORE the rejection, written with a +14:00 offset (its wall-clock text looks later).
    earlier = (rejected_at - timedelta(hours=1)).astimezone(__import__("datetime").timezone(timedelta(hours=14)))
    with lab.session() as session:
        report = ledger_module.import_ledger(session, _ledger(_ledger_entry(detail, "APPROVE", earlier.isoformat())), file_sha256="f6")
        session.commit()
    assert report.imported == 1 and lab.status(row) == REJECTED  # the later rejection still wins
    with lab.session() as session:
        stored = session.scalar(select(ReviewDecision.created_at).where(ReviewDecision.imported_from == "f6"))
    assert (stored.replace(tzinfo=UTC) if stored.tzinfo is None else stored) == earlier.astimezone(UTC)  # stored as the UTC instant
    # A date without an offset is refused: the order of decisions depends on it.
    with pytest.raises(ledger_module.LedgerError, match="no UTC offset"):
        ledger_module.validate_ledger(_ledger(_ledger_entry(detail, "APPROVE", "2026-01-01T10:00:00")))


def test_f6_a_new_decision_is_the_latest_even_after_importing_one_dated_slightly_ahead(lab):
    row = lab.registry_primary("15 mg Tirzepatide")
    detail = lab.by_key(row)
    ahead = (datetime.now(UTC) + timedelta(minutes=3)).isoformat()  # within the import tolerance for clock skew
    with lab.session() as session:
        ledger_module.import_ledger(session, _ledger(_ledger_entry(detail, "APPROVE", ahead)), file_sha256="ahead")
        session.commit()
    assert lab.status(row) == HUMAN_VERIFIED
    response = lab.decide(row, "REJECT", rationale=REJECT_WHY)
    assert response.status_code == 200 and response.json()["status_after"] == REJECTED and lab.status(row) == REJECTED
    [event] = [e for e in lab.audit("review.evidence_decision") if e["detail"]["decision"] == "REJECT"]
    assert "clock_adjusted_from" in event["detail"]


def test_f10_an_imported_decision_that_matches_no_version_is_kept_as_an_orphan_and_never_applied(lab):
    row = lab.registry_primary("15 mg Tirzepatide")
    detail = lab.by_key(row)
    # Same key, a fingerprint this database has never seen, and no snapshot: nothing says what was reviewed.
    stray = _ledger_entry(detail, "APPROVE", (datetime.now(UTC) - timedelta(days=1)).isoformat(), fingerprint="1" * 64)
    with lab.session() as session:
        report = ledger_module.import_ledger(session, _ledger(stray), file_sha256="stray")
        session.commit()
    assert report.matched == {"UNMATCHED": 1}
    assert lab.status(row) == MACHINE and lab.by_key(row)["status"]["reason"] == "NO_DECISION"  # not even a recheck: it was never applied
    summary = lab.client.get("/api/review/summary").json()
    [orphan] = summary["orphaned_decisions"]
    assert orphan["applicable"] is False and orphan["evidence_key"] == row["evidence_key"]
    with lab.session() as session:
        stored = session.scalar(select(ReviewDecision).where(ReviewDecision.imported_from == "stray"))
        assert (stored.ledger_match, stored.identity_basis, stored.evidence_version_id) == ("UNMATCHED", "IMPORTED_UNRECORDED", None)
    # A snapshot that does not hash to its fingerprint refuses the whole file before anything is written.
    forged = _ledger_entry(detail, "REJECT", (datetime.now(UTC) - timedelta(days=1)).isoformat(), snapshot={"value": 1.0}, fingerprint="2" * 64)
    with lab.session() as session, pytest.raises(ledger_module.LedgerError, match="does not hash"):
        ledger_module.import_ledger(session, _ledger(forged), file_sha256="forged")


def test_f10_and_sec03_a_ledger_with_nan_is_refused(lab, tmp_path):
    detail = lab.by_key(lab.registry_primary("15 mg Tirzepatide"))
    entry = _ledger_entry(detail, "PROPOSE_CORRECTION", (datetime.now(UTC) - timedelta(days=1)).isoformat())
    entry["proposed_correction"] = {"changes": {"value": float("nan")}, "supporting_quote": "a quote from the source"}
    path = tmp_path / "nan.json"
    path.write_text(json.dumps(_ledger(entry)), encoding="utf-8")  # json.dumps writes the bare token NaN
    result = CliRunner().invoke(CLI, ["review", "import", str(path)])
    assert result.exit_code == 1 and "NaN" in result.output


# --------------------------------------------------------------------------- F7: the estimand's definition is part of what was approved


def test_f7_an_approval_lapses_when_the_estimand_definition_changes(lab):
    row = lab.curated_result("tirzepatide 15 mg")
    assert lab.decide(row, "APPROVE").json()["status_after"] == HUMAN_VERIFIED
    snapshot = lab.by_key(row)["snapshot"]["estimand"]
    assert snapshot["strategies"] == [["TREATMENT_DISCONTINUATION", "TREATMENT_POLICY", snapshot["strategies"][0][2]]] and snapshot["description"]

    def edit(document: dict) -> None:  # a labelled perturbation: the estimand now uses a hypothetical strategy
        estimand = document["estimands"][0]
        estimand["strategies"][0]["strategy"] = "HYPOTHETICAL"
        estimand["description"] = "Effect had all participants remained on treatment (hypothetical strategy)."

    _edit_yaml(lab.curated / "studies" / f"{SURMOUNT_1}.generated.yaml", edit)
    lab.reseed()
    detail = lab.by_key(row)
    assert detail["status"]["verification_status"] == RECHECK and detail["status"]["reason"] == "EVIDENCE_CHANGED_SINCE_APPROVAL"
    diff = {d["field"] for d in detail["changed_since_review"]["diff"]}
    assert {"estimand.description", "estimand.strategies"} <= diff


# --------------------------------------------------------------------------- F8 / SEC-04: reviewer identity


@pytest.mark.parametrize("name", ["system.", "sys tem", "sys​tem", "automation-bot", "n/a", "xx", " SYSTEM ", "Review System", "a1"])
def test_f8_reserved_and_placeholder_reviewer_names_are_refused_after_normalisation(name):
    assert reviewer_problem(name) is not None


@pytest.mark.parametrize("name", ["Dr. Test Reviewer", "Li Wu", "Dr. Abbott", "María José Núñez"])
def test_f8_real_names_are_accepted(name):
    assert reviewer_problem(name) is None


@pytest.mark.parametrize("attest", ["yes", 1, "true"])
def test_f8_the_attestation_must_be_the_boolean_true(lab, attest):
    row = lab.registry_primary("15 mg Tirzepatide")
    response = lab.decide(row, "APPROVE", attest=attest)
    assert response.status_code == 422 and lab.status(row) == MACHINE


def test_f8_shared_token_decisions_are_labelled_self_declared(lab):
    row = lab.registry_primary("15 mg Tirzepatide")
    body = lab.decide(row, "APPROVE").json()
    assert body["identity_basis"] == "SELF_DECLARED_SHARED_TOKEN" and body["reviewer"] == REVIEWER
    assert lab.by_key(row)["decisions"][0]["identity_basis"] == "SELF_DECLARED_SHARED_TOKEN"
    assert lab.decide(row, "REJECT", reviewer="sys​tem", rationale=REJECT_WHY).status_code == 422


@pytest.fixture()
def reviewers_file(lab, tmp_path, monkeypatch):
    token = "personal-token-of-dr-jane-doe-0123456789"
    path = tmp_path / "reviewers.json"
    path.write_text(json.dumps({"Dr. Jane Doe": hashlib.sha256(token.encode()).hexdigest(),
                                "Dr. John Roe": hashlib.sha256(b"another-personal-token-abcdefghij").hexdigest()}), encoding="utf-8")
    monkeypatch.setenv("INCRETINOS_REVIEWERS_FILE", str(path))
    get_settings.cache_clear()
    yield {"X-Reviewer-Token": token}
    get_settings.cache_clear()


def test_f8_with_personal_tokens_the_reviewer_name_comes_from_the_token(lab, reviewers_file):
    row = lab.registry_primary("15 mg Tirzepatide")
    current = lab.by_key(row)
    url = f"/api/review/evidence/result/{current['id']}/decisions"
    body = {"decision": "APPROVE", "rationale": RATIONALE, "attest_human_review": True, "expected_fingerprint": current["fingerprint"]}
    # The shared development token no longer writes.
    assert lab.client.post(url, json={**body, "reviewer": REVIEWER}, headers=TOKEN).status_code == 401
    # A name that is not the token's own is refused.
    impostor = lab.client.post(url, json={**body, "reviewer": "Dr. John Roe"}, headers=reviewers_file)
    assert impostor.status_code == 422 and lab.status(row) == MACHINE
    response = lab.client.post(url, json=body, headers=reviewers_file)
    assert response.status_code == 200, response.text
    assert response.json()["reviewer"] == "Dr. Jane Doe" and response.json()["identity_basis"] == "AUTHENTICATED_REVIEWER_TOKEN"
    decision = lab.by_key(row)["decisions"][0]
    assert (decision["reviewer"], decision["identity_basis"]) == ("Dr. Jane Doe", "AUTHENTICATED_REVIEWER_TOKEN")
    assert lab.client.get("/api/review/summary").json()["reviewer_identity"] == "PERSONAL_TOKENS"
    # The gap-triage route follows the same rules.
    gap_id = lab.client.get("/api/gaps").json()["rows"][0]["id"]
    assert lab.client.patch(f"/api/gaps/{gap_id}", json={"review_status": "IN_REVIEW", "reviewer": "Dr. John Roe"}, headers=reviewers_file).status_code == 422
    triaged = lab.client.patch(f"/api/gaps/{gap_id}", json={"review_status": "IN_REVIEW"}, headers=reviewers_file)
    assert triaged.status_code == 200
    [event] = lab.audit("gap.reviewed")
    assert event["actor"] == "Dr. Jane Doe" and event["detail"]["identity_basis"] == "AUTHENTICATED_REVIEWER_TOKEN"


def test_sec04_gap_triage_refuses_reserved_names_and_never_erases_a_note(lab):
    gap_id = lab.client.get("/api/gaps").json()["rows"][0]["id"]
    for name in ("system", "  ", "12", "api", "System "):
        assert lab.client.patch(f"/api/gaps/{gap_id}", json={"review_status": "CONFIRMED", "reviewer": name}, headers=TOKEN).status_code == 422, name
    noted = lab.client.patch(f"/api/gaps/{gap_id}", json={"review_status": "CONFIRMED", "reviewer": REVIEWER, "reviewer_note": "Confirmed against the label."}, headers=TOKEN)
    assert noted.status_code == 200 and noted.json()["reviewer_note"] == "Confirmed against the label."
    status_only = lab.client.patch(f"/api/gaps/{gap_id}", json={"review_status": "IN_REVIEW", "reviewer": REVIEWER}, headers=TOKEN)
    assert status_only.status_code == 200 and status_only.json()["reviewer_note"] == "Confirmed against the label."
    assert lab.client.patch(f"/api/gaps/{gap_id}", json={"review_status": "IN_REVIEW", "reviewer": REVIEWER, "reviewer_note": "  "}, headers=TOKEN).status_code == 422
    replaced = lab.client.patch(f"/api/gaps/{gap_id}", json={"review_status": "CONFIRMED", "reviewer": REVIEWER, "reviewer_note": "Second note."}, headers=TOKEN)
    assert replaced.json()["reviewer_note"] == "Second note."
    events = lab.audit("gap.reviewed")
    assert events[0]["detail"]["before"]["reviewer_note"] == "Confirmed against the label." and events[0]["detail"]["after"]["reviewer_note"] == "Second note."
    with lab.session() as session:  # every note stays in the append-only decision history
        notes = session.scalars(select(ReviewDecision.rationale).where(ReviewDecision.target_type == "evidence_gap").order_by(ReviewDecision.id)).all()
        assert "Confirmed against the label." in notes and "Second note." in notes
        assert set(session.scalars(select(ReviewDecision.identity_basis).where(ReviewDecision.target_type == "evidence_gap"))) == {"SELF_DECLARED_SHARED_TOKEN"}


# --------------------------------------------------------------------------- F9: repairs are audited


def test_f9_reconcile_audits_every_status_it_repairs_and_check_mode_reports_it(lab):
    row = lab.registry_primary("15 mg Tirzepatide")
    other = lab.registry_primary("10 mg Tirzepatide")
    rid, other_id = lab.by_key(row)["id"], lab.by_key(other)["id"]
    with sqlite3.connect(lab.db_path) as conn:  # raw SQL: the route the ORM guards cannot see
        conn.execute("UPDATE outcome_result SET verification_status = 'HUMAN_VERIFIED' WHERE id = ?", (rid,))
        conn.execute("UPDATE outcome_result SET review_hold = 1 WHERE id = ?", (other_id,))
    check = CliRunner().invoke(CLI, ["review", "reconcile", "--check"])
    assert check.exit_code == 1 and json.loads(check.stdout)["status_repairs"] == 2
    assert lab.status(row) == HUMAN_VERIFIED  # the dry run changed nothing
    with lab.session() as session:
        report = reconcile(session)
        session.commit()
    assert report.repairs_total == 2 and {r["row_id"] for r in report.repairs} == {rid, other_id}
    repairs = {e["target_id"]: e for e in lab.audit("review.status_repaired")}
    forged = repairs[row["evidence_key"]]["detail"]
    assert forged["before"] == {"verification_status": HUMAN_VERIFIED, "review_hold": False} and forged["after"] == {"verification_status": MACHINE, "review_hold": False}
    assert forged["kind"] == "UNSUPPORTED_HUMAN_STATUS"
    assert repairs[other["evidence_key"]]["detail"]["before"]["review_hold"] is True
    assert lab.status(row) == MACHINE and CliRunner().invoke(CLI, ["review", "reconcile", "--check"]).exit_code == 0


def test_f9_the_orm_refuses_to_set_the_hold(lab):
    from incretinos.models import HumanStatusWriteError

    with lab.session() as session:
        result = session.scalar(select(OutcomeResult).limit(1))
        with pytest.raises(HumanStatusWriteError):
            result.review_hold = False


# --------------------------------------------------------------------------- F12: check-updates reports every step


def test_f12_check_updates_reports_the_registry_steps_rekeying(lab, tmp_path, monkeypatch):
    fixtures = shutil.copytree(FIXTURES, tmp_path / "fixtures")
    path = fixtures / REGISTRY_FIXTURE
    document = json.loads(path.read_text(encoding="utf-8"))
    _retitle_primary_outcome(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("INCRETINOS_FIXTURE_DIR", str(fixtures))
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(CLI, ["check-updates"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 0, result.output[-1500:]
    review = json.loads(result.stdout)["review"]
    assert [s["step"] for s in review["steps"]] == ["ctgov", "openfda-label"]
    ctgov = review["steps"][0]
    assert ctgov["first_seen_keys"] >= 4 and ctgov["disappeared_keys"] >= 4
    assert review["first_seen_keys"] == sum(s["first_seen_keys"] for s in review["steps"]) and review["disappeared_keys"] >= 4


def test_f12_aggregate_summaries_sums_events_and_keeps_the_final_state():
    total = aggregate_summaries([("a", {"rows": 5, "new_versions": 2, "orphaned_decisions": 1, "change_events": [1]}),
                                 ("b", {"rows": 6, "new_versions": 1, "orphaned_decisions": 0, "change_events": [2]})])
    assert (total["rows"], total["new_versions"], total["orphaned_decisions"], total["change_events"]) == (6, 3, 0, [1, 2])


# --------------------------------------------------------------------------- SEC-03: proposed corrections are JSON-safe and bounded


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "x" * 2001])
def test_sec03_a_proposed_correction_must_be_finite_and_bounded(lab, value):
    row = lab.registry_primary("15 mg Tirzepatide")
    current = lab.by_key(row)
    raw = json.dumps({"decision": "PROPOSE_CORRECTION", "reviewer": REVIEWER, "rationale": RATIONALE, "attest_human_review": False,
                      "expected_fingerprint": current["fingerprint"],
                      "proposed_correction": {"changes": {"analysis_method": value}, "supporting_quote": "the passage in the source"}})
    response = lab.client.post(f"/api/review/evidence/result/{current['id']}/decisions", content=raw, headers={**TOKEN, "Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    with lab.session() as session:
        assert session.scalar(select(func.count()).select_from(ReviewDecision)) == 0


def test_sec03_json_columns_refuse_nan_at_the_database_boundary(lab):
    with lab.session() as session:
        item = ReviewQueueItem(item_type="TEST", target_type="test", target_id=None, title="t", reason="r", payload={"x": math.nan})
        session.add(item)
        with pytest.raises(StatementError, match="not JSON compliant"):
            session.flush()
        session.rollback()


def test_new_review_columns_never_mark_anything_human_in_a_fresh_seed(lab):
    with lab.session() as session:
        assert session.scalar(select(func.count()).select_from(EvidenceClaim).where(EvidenceClaim.review_hold.is_(True)).execution_options(include_rejected=True)) == 0
        assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "review.status_repaired")) == 0
    assert Path(lab.db_path).is_file()
