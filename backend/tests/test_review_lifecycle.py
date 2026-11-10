"""Evidence-level review lifecycle: identity, decisions, effective status, reconcile, reset, ledger.

Everything that mutates runs on its own temporary database. One small database (two studies, a cut
of the curated files, the real fixtures) is seeded once per module through the real pipeline; each
test works on a copy of it. Decisions are recorded by a simulated reviewer on those copies only.
Where a test changes a value (a registry payload, a curated entry, a label text) the change is a
labelled perturbation in a temporary directory, never a statement about the real source.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from incretinos.config import get_settings
from incretinos.db import Base, get_sessionmaker, make_engine, reset_derived_data, reset_engine
from incretinos.engines.validation import run_all
from incretinos.ingestion.pipeline import Pipeline
from incretinos.ingestion.transport import FixtureTransport
from incretinos.models import (
    REVIEWABLE_MODELS,
    AdverseEvent,
    AuditEvent,
    ChangeEvent,
    EvidenceClaim,
    EvidenceVersion,
    HumanStatusWriteError,
    OutcomeResult,
    ReviewDecision,
    ReviewQueueItem,
    SourceSnapshot,
    StatisticalAnalysis,
    Study,
)
from incretinos.review import ledger as ledger_module
from incretinos.review.lifecycle import (
    APPROVAL_CARRIED_FORWARD,
    APPROVED,
    CHANGED_SINCE_APPROVAL,
    CHANGED_SINCE_REJECTION,
    NO_DECISION,
    REJECTED_IN_REVIEW,
    SUPPORT_NOT_FOUND,
    DecisionFact,
    InvalidDecision,
    attach_decisions,
    effective_status,
    reconcile,
    record_decision,
)
from incretinos.review.policy import STORED_STATUSES, rationale_problem, reviewer_problem

from .conftest import FIXTURES

BACKEND = Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "incretinos"
SURMOUNT_1, SURMOUNT_5 = "NCT04184622", "NCT05822830"
TOKEN = {"X-Reviewer-Token": "test-reviewer-token"}
REVIEWER = "Dr. Test Reviewer"
RATIONALE = "Compared the stored value, arm and endpoint with the cited source table."
HUMAN_VERIFIED, REJECTED, RECHECK = "HUMAN_VERIFIED", "REJECTED", "SOURCE_CHANGED_RECHECK_REQUIRED"
MACHINE, ANCHORED, PENDING = "MACHINE_EXTRACTED", "QUOTE_ANCHORED", "PENDING_HUMAN_REVIEW"
PRIMARY_TITLE = "Percent Change From Baseline in Body Weight (Primary Treatment Period)"  # SURMOUNT-1, first posted outcome measure
REGISTRY_FIXTURE = f"ctgov/studies/{SURMOUNT_1}.json"
LABEL_FIXTURE = "openfda/label/zepbound.json"


# --------------------------------------------------------------------------- the pure core: one test per truth-table row

T0 = datetime(2026, 1, 1, tzinfo=UTC)
FP, OLD_FP = "f" * 64, "0" * 64
H1, H2 = "a" * 64, "b" * 64


def fact(decision: str, *, fingerprint: str = FP, minutes: int = 0, actor: str = "HUMAN", attested: bool = True, source: str | None = H1, **kw: Any) -> DecisionFact:
    return DecisionFact(decision=decision, fingerprint=fingerprint, actor_kind=actor, attested=attested, decided_at=T0 + timedelta(minutes=minutes),
                        id=minutes + 1, source_content_hash=source, **kw)


def derive(decisions: list[DecisionFact], *, mechanical: str = ANCHORED, structured: bool = False, source: str | None = H1):
    return effective_status(mechanical=mechanical, decisions=decisions, fingerprint=FP, structured=structured, source_content_hash=source)


def test_row_1_no_decisive_decision_keeps_the_mechanical_status():
    for mechanical in (MACHINE, ANCHORED, PENDING):
        outcome = derive([], mechanical=mechanical)
        assert (outcome.status, outcome.reason, outcome.decision) == (mechanical, NO_DECISION, None)
    assert derive([], mechanical="CURATED_PENDING_REVIEW").status == PENDING  # the legacy alias is accepted as input only
    with pytest.raises(ValueError):
        derive([], mechanical=HUMAN_VERIFIED)  # a human status can never be passed off as the mechanical one


def test_row_2_approval_of_the_current_fingerprint_on_the_same_source_version():
    outcome = derive([fact("APPROVE")], mechanical=PENDING)  # even a row that could not be anchored mechanically
    assert (outcome.status, outcome.reason) == (HUMAN_VERIFIED, APPROVED) and outcome.decision.decision == "APPROVE"


def test_row_3_approval_carries_forward_for_a_structured_row_when_the_source_has_a_new_version():
    outcome = derive([fact("APPROVE", source=H1)], mechanical=MACHINE, structured=True, source=H2)
    assert (outcome.status, outcome.reason, outcome.carried_forward) == (HUMAN_VERIFIED, APPROVAL_CARRIED_FORWARD, True)


def test_row_4_approval_carries_forward_for_a_curated_row_whose_quote_is_still_anchored():
    outcome = derive([fact("APPROVE", source=H1)], mechanical=ANCHORED, source=H2)
    assert (outcome.status, outcome.reason) == (HUMAN_VERIFIED, APPROVAL_CARRIED_FORWARD)


def test_row_5_approval_lapses_when_the_quote_is_no_longer_found_in_the_new_source_version():
    outcome = derive([fact("APPROVE", source=H1)], mechanical=PENDING, source=H2)
    assert (outcome.status, outcome.reason) == (RECHECK, SUPPORT_NOT_FOUND)


def test_row_6_approval_of_an_older_fingerprint_requires_a_recheck():
    for source in (H1, H2):
        outcome = derive([fact("APPROVE", fingerprint=OLD_FP)], mechanical=MACHINE, structured=True, source=source)
        assert (outcome.status, outcome.reason) == (RECHECK, CHANGED_SINCE_APPROVAL)


@pytest.mark.parametrize("decision", ["REJECT", "FLAG_WRONG_ATTRIBUTION"])
def test_row_7_rejection_of_the_current_fingerprint_holds_whatever_the_source_does(decision):
    for source, mechanical, structured in ((H1, MACHINE, True), (H2, MACHINE, True), (H2, ANCHORED, False), (None, PENDING, False)):
        outcome = derive([fact(decision)], mechanical=mechanical, structured=structured, source=source)
        assert (outcome.status, outcome.reason) == (REJECTED, REJECTED_IN_REVIEW)


@pytest.mark.parametrize("decision", ["REJECT", "FLAG_WRONG_ATTRIBUTION"])
def test_row_8_rejection_of_an_older_fingerprint_requires_a_recheck_never_a_mechanical_status(decision):
    outcome = derive([fact(decision, fingerprint=OLD_FP)], mechanical=MACHINE, structured=True)
    assert (outcome.status, outcome.reason) == (RECHECK, CHANGED_SINCE_REJECTION)
    assert outcome.status not in (MACHINE, ANCHORED, PENDING)


@pytest.mark.parametrize("request_kind", ["REQUEST_INFO", "PROPOSE_CORRECTION"])
def test_row_9_requests_never_change_the_status_and_stay_open_until_a_later_decisive_decision(request_kind):
    alone = derive([fact(request_kind)], mechanical=ANCHORED)
    assert alone.status == ANCHORED and [d.decision for d in alone.open_requests] == [request_kind]
    after_approval = derive([fact("APPROVE", minutes=0), fact(request_kind, minutes=5)])
    assert after_approval.status == HUMAN_VERIFIED and len(after_approval.open_requests) == 1
    answered = derive([fact(request_kind, minutes=0), fact("REJECT", minutes=5)])
    assert answered.status == REJECTED and answered.open_requests == ()


@pytest.mark.parametrize("decision", ["APPROVE", "REJECT", "FLAG_WRONG_ATTRIBUTION"])
def test_row_10_a_non_human_or_unattested_decision_has_no_effect(decision):
    for bogus in (fact(decision, actor="AUTOMATION"), fact(decision, actor="SYSTEM"), fact(decision, attested=False), fact(decision, actor=None, attested=None)):
        assert derive([bogus], mechanical=PENDING).status == PENDING
        # ... and it cannot override a real decision recorded earlier.
        assert derive([fact("REJECT", minutes=0), DecisionFact(**{**_as_kwargs(bogus), "decided_at": T0 + timedelta(minutes=9), "id": 99})]).status == REJECTED


def _as_kwargs(f: DecisionFact) -> dict[str, Any]:
    return {name: getattr(f, name) for name in DecisionFact.__dataclass_fields__}


def test_the_latest_decisive_decision_wins():
    assert derive([fact("APPROVE", minutes=0), fact("REJECT", minutes=1)]).status == REJECTED
    assert derive([fact("REJECT", minutes=0), fact("APPROVE", minutes=1)]).status == HUMAN_VERIFIED
    assert derive([fact("APPROVE", minutes=0, fingerprint=OLD_FP), fact("APPROVE", minutes=1)]).status == HUMAN_VERIFIED
    assert derive([fact("APPROVE", minutes=1, fingerprint=OLD_FP), fact("APPROVE", minutes=0)]).status == RECHECK


def test_decisions_follow_a_row_whose_key_changed_without_its_content_changing():
    """Evidence keys of rows that share a natural key change when a sibling appears or disappears."""
    natural = "n" * 64
    rejected = fact("REJECT", evidence_type="result", evidence_key=natural, natural_key=natural)  # recorded when the row was alone
    rows = [("result", "k1" * 32, natural, FP), ("result", "k2" * 32, natural, OLD_FP)]  # now two rows share the natural key
    attached, orphans = attach_decisions(rows, [rejected])
    assert attached[("result", "k1" * 32)] == (rejected,) and orphans == []  # same content: still its decision -> stays REJECTED
    assert effective_status(mechanical=ANCHORED, decisions=attached[("result", "k1" * 32)], fingerprint=FP, structured=False, source_content_hash=H1).status == REJECTED
    # The other row has no decision of its own: it inherits the detached one as "made on an earlier version" -> recheck, not mechanical.
    assert effective_status(mechanical=ANCHORED, decisions=attached[("result", "k2" * 32)], fingerprint=OLD_FP, structured=False, source_content_hash=H1).status == RECHECK

    own = fact("APPROVE", minutes=3, fingerprint=OLD_FP, evidence_type="result", evidence_key="k2" * 32, natural_key=natural)
    attached, _ = attach_decisions(rows, [rejected, own])
    assert attached[("result", "k2" * 32)] == (own,)  # a row with its own history does not inherit a sibling's detached decision

    gone = fact("APPROVE", evidence_type="result", evidence_key="zz" * 32, natural_key="m" * 64)
    attached, orphans = attach_decisions(rows, [gone])
    assert attached == {} and orphans == [gone]  # evidence no longer exists: an orphan, reported and kept


# --------------------------------------------------------------------------- vocabulary and guard rails


def test_rationale_must_say_something():
    assert rationale_problem(RATIONALE) is None
    assert rationale_problem("Matches Table 2 of the label, 15 mg column.") is None
    for filler in ("", "ok", "looks good", "approved approved approved approved approved", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "ok ok ok ok ok ok ok ok ok ok",
                   "Approve recorded from the review queue.", "Reject recorded from the review queue", "12345 67890 12345 67890 12345"):
        assert rationale_problem(filler), filler


def test_reviewer_name_must_identify_a_person():
    assert reviewer_problem(REVIEWER) is None
    for bad in ("", " ", "  ", "x", "42", "system", "API", "Reviewer", "automation"):
        assert reviewer_problem(bad), bad


def test_the_orm_refuses_to_assign_a_human_status():
    """No loader, fixture or script can mark a row verified or rejected by assignment."""
    for status in (HUMAN_VERIFIED, REJECTED):
        for model in REVIEWABLE_MODELS.values():
            with pytest.raises(HumanStatusWriteError):
                model(verification_status=status)
        row = OutcomeResult(verification_status=MACHINE)
        with pytest.raises(HumanStatusWriteError):
            row.verification_status = status
    assert OutcomeResult(verification_status=RECHECK).verification_status == RECHECK  # other statuses are ordinary values


_MODEL_NAMES = {model.__name__ for model in REVIEWABLE_MODELS.values()}


def _callee(call: ast.Call) -> str:
    return call.func.id if isinstance(call.func, ast.Name) else call.func.attr if isinstance(call.func, ast.Attribute) else ""


def _status_writers() -> tuple[dict[str, list[str]], dict[str, set[str]]]:
    """(every place in the package that can store a verification status, every module that names a human status).

    A write is: constructing a reviewable model with ``verification_status=``, a ``.values(verification_status=...)``
    or ``.values({"verification_status": ...})`` statement, an attribute assignment, or ``setattr(..., "verification_status", ...)``.
    Building a DTO that happens to have a field of that name is not a write.
    """
    writes: dict[str, list[str]] = {}
    mentions: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        name = path.relative_to(PACKAGE).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                callee = _callee(node)
                if callee in _MODEL_NAMES | {"values"} and any(k.arg == "verification_status" for k in node.keywords):
                    writes.setdefault(name, []).append(f"{callee}(verification_status=...) at line {node.lineno}")
                if callee == "values" and any(isinstance(a, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "verification_status" for k in a.keys) for a in node.args):
                    writes.setdefault(name, []).append(f"values({{'verification_status': ...}}) at line {node.lineno}")
                if callee == "setattr" and any(isinstance(a, ast.Constant) and a.value == "verification_status" for a in node.args):
                    writes.setdefault(name, []).append(f"setattr at line {node.lineno}")
            if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Attribute) and t.attr == "verification_status" for t in targets):
                    writes.setdefault(name, []).append(f"attribute assignment at line {node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr in ("HUMAN_VERIFIED", "REJECTED"):
                mentions.setdefault(name, set()).add(node.attr)
            if isinstance(node, ast.Constant) and node.value in ("HUMAN_VERIFIED", "REJECTED"):
                mentions.setdefault(name, set()).add(node.value)
    return writes, mentions


def test_only_the_review_lifecycle_can_write_a_human_status():
    """Static proof that there is no second writer of HUMAN_VERIFIED or REJECTED anywhere in the codebase."""
    writes, mentions = _status_writers()
    # 1. Modules that store a verification status at all: the two loaders and the review lifecycle. Nothing else.
    assert set(writes) == {"ingestion/ctgov.py", "curation/loader.py", "review/lifecycle.py"}, writes
    # 2. The loaders never name HUMAN_VERIFIED or REJECTED, so whatever they store cannot be one of them
    #    (and the ORM refuses the assignment anyway: test_the_orm_refuses_to_assign_a_human_status).
    assert "ingestion/ctgov.py" not in mentions and "curation/loader.py" not in mentions, mentions
    # 3. The lifecycle writes with exactly two statements: the derived status, and the protective hold on rejected rows.
    lifecycle = (PACKAGE / "review" / "lifecycle.py").read_text(encoding="utf-8")
    assert lifecycle.count('verification_status=bindparam("b_status")') == 1 and lifecycle.count("values(verification_status=REJECTED)") == 1
    assert len(writes["review/lifecycle.py"]) == 2, writes["review/lifecycle.py"]
    # 4. No raw SQL in the package, the migrations or the scripts sets either status.
    for path in [*PACKAGE.rglob("*.py"), *(BACKEND / "alembic").rglob("*.py"), *(BACKEND / "scripts").rglob("*.py")]:
        compact = path.read_text(encoding="utf-8").replace(" ", "")
        for status in ("HUMAN_VERIFIED", "REJECTED"):
            assert f"verification_status='{status}'" not in compact and f'verification_status="{status}"' not in compact, f"{path}: raw SQL sets {status}"


def test_no_seed_or_fixture_ships_a_human_status(seeded, db):
    """The committed seed produces only mechanical statuses and no evidence decision."""
    for evidence_type, model in REVIEWABLE_MODELS.items():
        stored = {s for (s,) in db.execute(select(model.verification_status).distinct().execution_options(include_rejected=True))}
        assert stored <= {MACHINE, ANCHORED, PENDING}, (evidence_type, stored)
        assert db.scalar(select(func.count()).select_from(model).where(model.evidence_key.is_(None)).execution_options(include_rejected=True)) == 0
    assert db.scalar(select(func.count()).select_from(ReviewDecision).where(ReviewDecision.evidence_type.is_not(None))) == 0
    assert seeded["review"]["changed"] == 0 and seeded["evidence_changes"] == []  # a first seed reports no "changes"
    for path in [*FIXTURES.rglob("*.json"), *get_settings().curated_dir.rglob("*.yaml")]:
        content = path.read_text(encoding="utf-8")
        assert "HUMAN_VERIFIED" not in content and '"REJECTED"' not in content, path


def test_status_columns_are_wide_enough_for_every_status():
    longest = max(len(s) for s in STORED_STATUSES)
    assert longest == 31
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if column.name in ("verification_status", "base_verification_status", "effective_status", "mechanical_status", "status_before", "status_after"):
                assert column.type.length >= 40, f"{table.name}.{column.name}"
    assert Base.metadata.tables["review_decision"].c.decision.type.length >= len("FLAG_WRONG_ATTRIBUTION")


# --------------------------------------------------------------------------- the laboratory database


def _mini_curated(dst: Path) -> Path:
    """A cut of the real curated directory: two studies, their two drugs, and the claims that cite sources those need."""
    src = get_settings().curated_dir
    (dst / "studies").mkdir(parents=True)
    watch = yaml.safe_load((src / "watchlist.yaml").read_text(encoding="utf-8"))
    watch["studies"] = [s for s in watch["studies"] if s["nct"] in (SURMOUNT_1, SURMOUNT_5)]
    watch["discovery_terms"] = {}
    (dst / "watchlist.yaml").write_text(yaml.safe_dump(watch, allow_unicode=True, sort_keys=False), encoding="utf-8")
    drugs = yaml.safe_load((src / "drugs.yaml").read_text(encoding="utf-8"))
    drugs["drugs"] = [d for d in drugs["drugs"] if d["slug"] in ("tirzepatide", "semaglutide")]
    (dst / "drugs.yaml").write_text(yaml.safe_dump(drugs, allow_unicode=True, sort_keys=False), encoding="utf-8")
    claims = yaml.safe_load((src / "claims.yaml").read_text(encoding="utf-8"))
    claims["claims"] = [c for c in claims["claims"] if c["source"] in ("openfda-label:zepbound", "openfda-label:mounjaro")][:6]
    (dst / "claims.yaml").write_text(yaml.safe_dump(claims, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for name in [p.name for p in src.glob("sources*.yaml")] + ["web_anchors.json"]:
        shutil.copy(src / name, dst / name)
    for nct in (SURMOUNT_1, SURMOUNT_5):
        for path in (src / "studies").glob(f"{nct}*.yaml"):
            shutil.copy(path, dst / "studies" / path.name)
    return dst


@dataclass
class PatchedTransport(FixtureTransport):
    """Serves the committed fixtures, with labelled edits applied to chosen responses (a simulated source revision)."""

    patches: dict[str, Callable[[Any], None]] = field(default_factory=dict)

    def get(self, service, path, params=None, *, fixture_key=None):
        raw = super().get(service, path, params, fixture_key=fixture_key)
        for needle, patch in self.patches.items():
            if fixture_key and needle in fixture_key:
                document = json.loads(raw)
                patch(document)
                return json.dumps(document).encode()
        return raw


@dataclass
class Lab:
    db_path: Path
    curated: Path
    client: TestClient

    def session(self) -> Session:
        return get_sessionmaker()()

    def pipeline(self, session: Session, patches: dict[str, Callable[[Any], None]] | None = None) -> Pipeline:
        transport = PatchedTransport(FIXTURES, patches=patches or {})
        return Pipeline(session, get_settings(), transport=transport)

    def reseed(self, patches: dict[str, Callable[[Any], None]] | None = None):
        """A plain reseed through the real pipeline (what `incretinos seed` does)."""
        with self.session() as session:
            report = self.pipeline(session, patches).run(skip={"discover"})
            session.commit()
        assert not report.failures, report.failures
        return report

    def check_updates(self, patches: dict[str, Callable[[Any], None]] | None = None):
        """What `incretinos check-updates` and the scheduler do: re-fetch registry records and labels only."""
        with self.session() as session:
            pipeline = self.pipeline(session, patches)
            pipeline._run("ctgov", lambda: pipeline.ingest_studies(demo_change=False))
            pipeline._run("openfda-label", pipeline.ingest_labels)
            session.commit()
        assert not pipeline.report.failures, pipeline.report.failures
        return pipeline.report

    def rows(self, **params: Any) -> list[dict[str, Any]]:
        response = self.client.get("/api/review/evidence", params={"limit": 500, **params})
        assert response.status_code == 200, response.text
        return response.json()["rows"]

    def all_rows(self, **params: Any) -> list[dict[str, Any]]:
        """Every matching row, walking the pages (the list endpoint caps a page at 500)."""
        out: list[dict[str, Any]] = []
        while True:
            response = self.client.get("/api/review/evidence", params={**params, "limit": 500, "offset": len(out)})
            assert response.status_code == 200, response.text
            body = response.json()
            out += body["rows"]
            if not body["rows"] or len(out) >= body["total"]:
                assert len(out) == body["total"]
                return out

    def one(self, **params: Any) -> dict[str, Any]:
        rows = self.rows(**params)
        assert len(rows) == 1, f"{params}: {[r['label'] for r in rows]}"
        return rows[0]

    def by_key(self, row: dict[str, Any]) -> dict[str, Any]:
        response = self.client.get(f"/api/review/evidence/by-key/{row['evidence_type']}/{row['evidence_key']}")
        assert response.status_code == 200, response.text
        return response.json()

    def decide(self, row: dict[str, Any], decision: str, *, attest: bool = True, rationale: str = RATIONALE, reviewer: str = REVIEWER,
               headers: dict[str, str] | None = TOKEN, fingerprint: str | None = None, **extra: Any):
        current = self.by_key(row)
        body = {"decision": decision, "reviewer": reviewer, "rationale": rationale, "attest_human_review": attest,
                "expected_fingerprint": fingerprint or current["fingerprint"], **extra}
        return self.client.post(f"/api/review/evidence/{current['evidence_type']}/{current['id']}/decisions", json=body, headers=headers or {})

    def status(self, row: dict[str, Any]) -> str:
        return self.by_key(row)["status"]["verification_status"]

    # -- well-known rows of the laboratory database
    def registry_primary(self, arm: str) -> dict[str, Any]:
        """SURMOUNT-1, registry-posted primary outcome (percent change in body weight), one arm."""
        rows = [r for r in self.rows(nct=SURMOUNT_1, evidence_type="result", origin="REGISTRY", endpoint_category="WEIGHT_PCT_CHANGE")
                if r["label"] == f"SURMOUNT-1 — {PRIMARY_TITLE} — {arm}"]
        assert len(rows) == 1, [r["label"] for r in rows]
        return rows[0]

    def curated_result(self, arm: str, nct: str = SURMOUNT_1) -> dict[str, Any]:
        rows = [r for r in self.rows(nct=nct, evidence_type="result", origin="CURATED") if r["label"].endswith(f"— {arm}")]
        assert len(rows) == 1, [r["label"] for r in rows]
        return rows[0]

    def label_analysis(self, arm: str) -> dict[str, Any]:
        """SURMOUNT-1 difference from placebo as curated from the Zepbound label (full text stored)."""
        rows = [r for r in self.rows(nct=SURMOUNT_1, evidence_type="analysis", origin="CURATED", source_key="openfda-label:zepbound") if f"— {arm} vs placebo" in r["label"]]
        assert len(rows) == 1, [r["label"] for r in rows]
        return rows[0]

    def audit(self, action: str) -> list[dict[str, Any]]:
        body = self.client.get("/api/audit", params={"action": action, "limit": 1000}).json()
        assert body["total"] == len(body["rows"])  # one page holds every event of this action
        return body["rows"]

    def changes(self) -> list[dict[str, Any]]:
        """Every detected source change (the list endpoints return {total, limit, offset, rows})."""
        body = self.client.get("/api/changes", params={"limit": 1000}).json()
        assert body["total"] == len(body["rows"])
        return body["rows"]

    def fingerprint_state(self) -> dict[tuple[str, str], tuple[str, str]]:
        """(type, key) -> (fingerprint, status) for every reviewable row, read straight from the tables."""
        out = {}
        with self.session() as session:
            for evidence_type, model in REVIEWABLE_MODELS.items():
                for key, fingerprint, status in session.execute(select(model.evidence_key, model.fingerprint, model.verification_status).execution_options(include_rejected=True)):
                    out[(evidence_type, key)] = (fingerprint, status)
        return out


def _migrate_database(path: Path) -> None:
    """Create or upgrade a SQLite file by running the Alembic migrations against it."""
    run = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env={**os.environ, "INCRETINOS_DATABASE_URL": f"sqlite:///{path}"},
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stdout[-800:] + run.stderr[-800:]


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("review-lab")
    curated = _mini_curated(root / "curated")
    # The same schema path as the CLI, the container and conftest: the migrations, not create_all. The commands
    # under test (`seed --reset`, `init-db --reset`, `review reconcile`, `review import`) refuse a database that
    # carries no migration version.
    _migrate_database(root / "template.db")
    engine = make_engine(f"sqlite:///{root / 'template.db'}")
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    report = Pipeline(session, get_settings().model_copy(update={"curated_dir": curated}), offline=True).run(skip={"discover"})
    session.commit()
    session.close()
    engine.dispose()
    assert not report.failures, report.failures
    assert report.review["rows"] > 400 and report.review["changed"] == 0
    return root


@pytest.fixture()
def lab(template, tmp_path):
    """A private copy of the laboratory database and curated files, wired into the application for one test."""
    from incretinos.api.main import app

    db_path = tmp_path / "lab.db"
    shutil.copy(template / "template.db", db_path)
    curated = shutil.copytree(template / "curated", tmp_path / "curated")
    names = ("INCRETINOS_DATABASE_URL", "INCRETINOS_CURATED_DIR")
    saved = {name: os.environ.get(name) for name in names}
    os.environ["INCRETINOS_DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["INCRETINOS_CURATED_DIR"] = str(curated)
    get_settings.cache_clear()
    reset_engine()
    try:
        yield Lab(db_path, Path(curated), TestClient(app))
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()
        reset_engine()


def _edit_yaml(path: Path, edit: Callable[[dict], None]) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(document)
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _find_ids(payload: Any, key: str = "result_id") -> set[int]:
    """Every value stored under ``key`` anywhere in a JSON document."""
    found: set[int] = set()
    if isinstance(payload, dict):
        for name, value in payload.items():
            if name == key and isinstance(value, int):
                found.add(value)
            found |= _find_ids(value, key)
    elif isinstance(payload, list):
        for item in payload:
            found |= _find_ids(item, key)
    return found


# --------------------------------------------------------------------------- decisions attach to exact evidence


def test_a_decision_is_attached_to_the_exact_evidence_version_and_audited(lab):
    row = lab.registry_primary("15 mg Tirzepatide")
    detail = lab.by_key(row)
    assert detail["values"]["value"] == -22.5 and detail["status"]["verification_status"] == MACHINE and detail["reconciled"]
    audit_before = len(lab.audit("review.evidence_decision"))

    response = lab.decide(row, "APPROVE")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status_before"], body["status_after"], body["reason"], body["usable"]) == (MACHINE, HUMAN_VERIFIED, "APPROVED", True)
    assert body["evidence_key"] == row["evidence_key"] and body["fingerprint"] == detail["fingerprint"]

    with lab.session() as session:
        decision = session.scalar(select(ReviewDecision).where(ReviewDecision.uid == body["uid"]))
        stored = session.get(OutcomeResult, detail["id"])
        version = session.get(EvidenceVersion, decision.evidence_version_id)
        snapshot = session.get(SourceSnapshot, stored.source_snapshot_id)
        # The decision names the evidence object, the exact content, and the source version the reviewer saw.
        assert (decision.evidence_type, decision.evidence_key, decision.fingerprint) == ("result", stored.evidence_key, stored.fingerprint)
        assert (version.evidence_key, version.fingerprint, version.snapshot["value"]) == (stored.evidence_key, stored.fingerprint, -22.5)
        assert version.snapshot["arm"]["label"] == "15 mg Tirzepatide" and version.snapshot["endpoint"]["title"] == PRIMARY_TITLE
        assert decision.source_key == f"ctgov:{SURMOUNT_1}" and decision.source_content_hash == snapshot.content_hash and decision.source_snapshot_version == snapshot.version_no
        assert (decision.decision, decision.reviewer, decision.rationale, decision.attested, decision.actor_kind) == ("APPROVE", REVIEWER, RATIONALE, True, "HUMAN")
        assert (decision.status_before, decision.status_after) == (MACHINE, HUMAN_VERIFIED)
        # The mechanical status is kept beside the effective one.
        assert (stored.verification_status, stored.base_verification_status) == (HUMAN_VERIFIED, MACHINE)

    events = lab.audit("review.evidence_decision")
    assert len(events) == audit_before + 1
    event = events[0]
    assert event["actor"] == REVIEWER and event["target_type"] == "result" and event["target_id"] == row["evidence_key"]
    for name, expected in (("evidence_type", "result"), ("evidence_key", row["evidence_key"]), ("fingerprint", detail["fingerprint"]), ("study", SURMOUNT_1),
                           ("status_before", MACHINE), ("status_after", HUMAN_VERIFIED), ("decision", "APPROVE"), ("attested", True)):
        assert event["detail"][name] == expected, name
    assert "test-reviewer-token" not in json.dumps(events)  # the token is never stored

    after = lab.by_key(row)
    assert after["status"]["decided_by"]["reviewer"] == REVIEWER and after["status"]["base_verification_status"] == MACHINE
    assert [d["decision"] for d in after["decisions"]] == ["APPROVE"] and after["decisions"][0]["on_current_version"] and after["decisions"][0]["decisive"]
    assert any(e["action"] == "review.evidence_decision" for e in after["audit"])


def test_approving_one_row_verifies_only_that_row(lab):
    before = lab.fingerprint_state()
    row = lab.registry_primary("10 mg Tirzepatide")
    assert lab.decide(row, "APPROVE").status_code == 200
    after = lab.fingerprint_state()
    changed = {key for key in before if before[key] != after[key]}
    assert changed == {("result", row["evidence_key"])}
    assert after[("result", row["evidence_key"])] == (before[("result", row["evidence_key"])][0], HUMAN_VERIFIED)
    summary = lab.client.get("/api/review/summary").json()
    assert summary["by_status"][HUMAN_VERIFIED] == 1 and summary["decisions_recorded"] == 1
    assert [r.rule_id for r in _failed_rules(lab)] == []  # V13 and V16 accept a verified row that rests on a recorded decision


def _failed_rules(lab: Lab):
    with lab.session() as session:
        return [r for r in run_all(session) if not r.passed and r.rule_id in ("V13", "V16")]


@contextmanager
def _loaded_results():
    """Collect the primary key of every OutcomeResult the ORM materialises inside the block, whatever code asks for it."""
    loaded: set[int] = set()

    def on_load(target: OutcomeResult, _context: Any) -> None:
        loaded.add(target.id)

    event.listen(OutcomeResult, "load", on_load)
    event.listen(OutcomeResult, "refresh", lambda target, _context, _attrs: loaded.add(target.id), named=False)
    try:
        yield loaded
    finally:
        event.remove(OutcomeResult, "load", on_load)


ASSISTANT_QUESTION = "SURMOUNT-1 15 mg Tirzepatide percent change body weight week 72 efficacy estimand registry-posted results"


def _analytic_calls(client: TestClient, rid: int, other_id: int, source_id: int) -> dict[str, Callable[[], Any]]:
    """One call per analytic surface of the product. Each returns the HTTP response."""
    return {
        "findings list and picker": lambda: client.get("/api/findings", params={"study": SURMOUNT_1, "arm_level_only": False, "limit": 2000}),
        "finding detail": lambda: client.get(f"/api/findings/{rid}"),
        "comparison": lambda: client.post("/api/compare", json={"a": rid, "b": other_id}),
        "comparison presets": lambda: client.get("/api/compare/presets"),
        "study comparison": lambda: client.get("/api/compare/studies", params={"a": SURMOUNT_1, "b": SURMOUNT_5}),
        "estimand inspector": lambda: client.get("/api/estimands", params={"study": SURMOUNT_1}),
        "benefit-risk profiles": lambda: client.get(f"/api/benefit-risk/{SURMOUNT_1}", params={"estimand": "EFFICACY", "weeks": 72}),
        "benefit-risk utility": lambda: client.post("/api/benefit-risk/utility", json={"nct": SURMOUNT_1, "estimand": "EFFICACY"}),
        "assistant evidence cards": lambda: client.post("/api/assistant/ask", json={"question": ASSISTANT_QUESTION}),
        "gap list": lambda: client.get("/api/gaps"),
        "safety table": lambda: client.get("/api/safety/trial-ae", params={"nct": SURMOUNT_1}),
        "design workbench: endpoints": lambda: client.get(f"/api/design/endpoints/{SURMOUNT_1}"),
        "design workbench: coverage": lambda: client.get("/api/design/coverage"),
        "exported comparison report": lambda: client.get("/api/reports/comparison", params={"a": rid, "b": other_id}),
        "exported study report": lambda: client.get(f"/api/reports/study/{SURMOUNT_1}"),
        "overview": lambda: client.get("/api/overview"),
        "study list": lambda: client.get("/api/studies"),
        "drug page": lambda: client.get("/api/drugs/tirzepatide"),
        "source dependents": lambda: client.get(f"/api/sources/{source_id}"),
        "scientific validation": lambda: client.get("/api/validation"),
    }


def test_a_rejected_result_leaves_every_analytic_surface_and_stays_visible_for_inspection(lab):
    row = lab.registry_primary("15 mg Tirzepatide")
    placebo = lab.registry_primary("Placebo")
    rid, placebo_id = lab.by_key(row)["id"], lab.by_key(placebo)["id"]
    source_id = lab.by_key(row)["source"]["id"]
    client = lab.client
    calls = _analytic_calls(client, rid, placebo_id, source_id)
    uses_the_row = ("findings list and picker", "finding detail", "comparison", "comparison presets", "estimand inspector", "benefit-risk profiles",
                    "assistant evidence cards", "exported comparison report", "exported study report", "overview")

    # Before the decision the row feeds the analytic surfaces.
    before: dict[str, Any] = {}
    for name, call in calls.items():
        with _loaded_results() as loaded:
            before[name] = call()
        assert before[name].status_code == 200, (name, before[name].text[:300])
        if name in uses_the_row:
            assert rid in loaded, name
    assert rid in _find_ids(before["findings list and picker"].json()) and rid in _find_ids(before["estimand inspector"].json())
    assert rid in _find_ids(before["benefit-risk profiles"].json()) and any(rid in (p["a"], p["b"]) for p in before["comparison presets"].json())
    assert "-22.5" in before["exported study report"].text and "-22.5" in json.dumps(before["assistant evidence cards"].json())
    n_results_before = next(s for s in before["study list"].json()["rows"] if s["nct_id"] == SURMOUNT_1)["n_results"]
    counts_before = client.get("/api/meta").json()["counts"]

    response = lab.decide(row, "REJECT", rationale="The stored value is not the one posted for this arm in the registry table.")
    assert response.status_code == 200 and response.json()["status_after"] == REJECTED and response.json()["usable"] is False

    # After it, no analytic surface loads it, whatever the shape of its response.
    after: dict[str, Any] = {}
    for name, call in calls.items():
        with _loaded_results() as loaded:
            after[name] = call()
        assert rid not in loaded, f"{name} still reads the rejected result"
    assert after["finding detail"].status_code == 404 and after["comparison"].status_code == 404 and after["exported comparison report"].status_code == 404
    for name in set(calls) - {"finding detail", "comparison", "exported comparison report"}:
        assert after[name].status_code == 200, name
        assert rid not in _find_ids(after[name].json() if name != "exported study report" else {}), name
    assert not any(rid in (p["a"], p["b"]) for p in after["comparison presets"].json())
    arms = after["benefit-risk profiles"].json()["arms"]
    assert "-22.5" not in json.dumps(arms) and placebo_id in _find_ids(arms)  # the 15 mg arm lost this value; the other arms are intact
    assert "-22.5" not in after["exported study report"].text and "-22.5" not in json.dumps(after["assistant evidence cards"].json())
    assert next(s for s in after["study list"].json()["rows"] if s["nct_id"] == SURMOUNT_1)["n_results"] == n_results_before - 1
    meta = client.get("/api/meta").json()
    assert meta["counts"]["results"] == counts_before["results"] - 1 and meta["counts"]["results_rejected"] == 1
    assert meta["result_verification"][REJECTED] == 1 and meta["review"]["by_status"][REJECTED] == 1

    # Gap detection (run by every load) and direct engine access go through the same filtered session.
    from incretinos.engines.gaps import detect_gaps

    with lab.session() as session, _loaded_results() as loaded:
        assert detect_gaps(session)
        study = session.scalar(select(Study).where(Study.nct_id == SURMOUNT_1))
        assert rid not in {r.id for r in study.results} and session.get(OutcomeResult, rid) is None
        assert session.scalar(select(func.count()).select_from(OutcomeResult).where(OutcomeResult.id == rid)) == 0
        assert loaded and rid not in loaded
    assert [r.rule_id for r in _failed_rules(lab)] == []

    # Inspection surfaces still show it, labelled.
    with _loaded_results() as loaded:
        detail = lab.by_key(row)
        study_page = client.get(f"/api/studies/{SURMOUNT_1}").json()
    assert rid in loaded
    assert detail["id"] == rid and detail["values"]["value"] == -22.5
    assert detail["status"]["verification_status"] == REJECTED and detail["status"]["usable"] is False and detail["status"]["label"] == "Rejected in review"
    listed = lab.one(status=REJECTED)
    assert listed["evidence_key"] == row["evidence_key"] and listed["usable"] is False
    assert client.get(f"/api/review/evidence/result/{rid}").status_code == 200
    shown = [r for group in study_page["endpoint_groups"] for r in group["results"] if r["result_id"] == rid]
    assert len(shown) == 1 and shown[0]["verification_status"] == REJECTED and shown[0]["value"] == -22.5
    assert any(label["code"] == "REJECTED" and label["tone"] == "critical" for label in shown[0]["labels"])


def test_a_rejected_adverse_event_analysis_and_claim_are_hidden_too(lab):
    client = lab.client
    nausea = [r for r in lab.rows(nct=SURMOUNT_1, evidence_type="adverse_event", origin="REGISTRY", q="nausea") if r["label"] == "SURMOUNT-1 — Nausea — 15 mg Tirzepatide"]
    assert len(nausea) == 1
    ae_id = lab.by_key(nausea[0])["id"]
    table = client.get("/api/safety/trial-ae", params={"nct": SURMOUNT_1, "measure_kind": "PARTICIPANTS_WITH_EVENT", "term": "nausea"}).json()
    assert ae_id in {r["id"] for r in table["rows"]}
    assert lab.decide(nausea[0], "FLAG_WRONG_ATTRIBUTION", rationale="These counts belong to another results group of the registry record.").json()["status_after"] == REJECTED
    table = client.get("/api/safety/trial-ae", params={"nct": SURMOUNT_1, "measure_kind": "PARTICIPANTS_WITH_EVENT", "term": "nausea"}).json()
    assert ae_id not in {r["id"] for r in table["rows"]} and len(table["rows"]) >= 3  # safety table
    placebo_id = next(r["id"] for r in table["rows"] if r["arm_label"] == "Placebo")
    assert client.post("/api/stats/risk-contrast", json={"ae_a": ae_id, "ae_b": placebo_id}).status_code == 404

    analysis = lab.label_analysis("tirzepatide 15 mg")
    analysis_id = lab.by_key(analysis)["id"]
    assert client.get(f"/api/analyses/{analysis_id}").status_code == 200
    assert lab.decide(analysis, "REJECT", rationale="The interval stored for this comparison is not the one in the label table.").json()["status_after"] == REJECTED
    assert client.get(f"/api/analyses/{analysis_id}").status_code == 404
    assert analysis_id not in {a["id"] for a in client.get("/api/analyses", params={"study": SURMOUNT_1, "limit": 1500}).json()["rows"]}

    claim = lab.rows(evidence_type="claim", nct=SURMOUNT_1)[0]
    with lab.session() as session:
        claim_key = session.scalar(select(EvidenceClaim.key).where(EvidenceClaim.evidence_key == claim["evidence_key"]))
    assert lab.decide(claim, "REJECT", rationale="The quoted sentence does not support the statement as worded here.").json()["status_after"] == REJECTED
    with lab.session() as session:
        assert session.scalar(select(EvidenceClaim).where(EvidenceClaim.key == claim_key)) is None  # hidden from ordinary queries
    # The curated loader still finds the rejected claim (no duplicate key) and the rejection survives the reload.
    lab.reseed()
    with lab.session() as session:
        assert session.scalar(select(func.count()).select_from(EvidenceClaim).where(EvidenceClaim.key == claim_key).execution_options(include_rejected=True)) == 1
    assert lab.status(claim) == REJECTED and lab.status(analysis) == REJECTED and lab.status(nausea[0]) == REJECTED
    study_page = client.get(f"/api/studies/{SURMOUNT_1}").json()
    assert any(c["verification_status"] == REJECTED for c in study_page["claims"])  # the trial's own page still lists it, labelled


def test_request_info_and_proposed_correction_leave_the_status_alone(lab):
    row = lab.curated_result("tirzepatide 15 mg")
    before = lab.status(row)
    assert before == ANCHORED

    info = lab.decide(row, "REQUEST_INFO", attest=False, rationale="Which analysis population does the abstract use for this estimate?")
    assert info.status_code == 200 and (info.json()["status_before"], info.json()["status_after"]) == (ANCHORED, ANCHORED)

    correction = {"changes": {"ci_low": -21.9, "arm.dose_amount": 15}, "supporting_quote": "-20.9% (95% CI, -21.8 to -19.9) with 15-mg doses"}
    proposed = lab.decide(row, "PROPOSE_CORRECTION", attest=False, rationale="The lower confidence limit should be checked against the abstract.", proposed_correction=correction)
    assert proposed.status_code == 200 and proposed.json()["status_after"] == ANCHORED

    detail = lab.by_key(row)
    assert detail["status"]["verification_status"] == ANCHORED and detail["values"]["ci_low"] == -21.8  # the correction was NOT applied
    assert [d["decision"] for d in detail["status"]["open_requests"]] == ["PROPOSE_CORRECTION", "REQUEST_INFO"]
    stored_correction = next(d for d in detail["decisions"] if d["decision"] == "PROPOSE_CORRECTION")["proposed_correction"]
    assert stored_correction == {**correction, "note": None}  # stored verbatim with the reviewer's supporting quote
    assert all(not d["decisive"] for d in detail["decisions"])
    summary = lab.client.get("/api/review/summary").json()
    assert summary["open_requests_total"] == 2 and {r["decision"] for r in summary["open_requests"]} == {"REQUEST_INFO", "PROPOSE_CORRECTION"}
    assert lab.one(has_open_request=True)["evidence_key"] == row["evidence_key"]

    # Unknown field names are refused; a correction without the decision kind, or the kind without a correction, too.
    bad = lab.decide(row, "PROPOSE_CORRECTION", attest=False, rationale="The stored value looks wrong against the abstract text.",
                     proposed_correction={"changes": {"no_such_field": 1}, "supporting_quote": "some supporting passage"})
    assert bad.status_code == 422 and "no_such_field" in bad.json()["detail"]
    assert lab.decide(row, "PROPOSE_CORRECTION", attest=False, rationale="The stored value looks wrong against the abstract text.").status_code == 422
    assert lab.decide(row, "REQUEST_INFO", attest=False, proposed_correction=correction).status_code == 422

    # Requests survive a reseed and are answered by a later decisive decision.
    lab.reseed()
    assert lab.status(row) == ANCHORED and len(lab.by_key(row)["status"]["open_requests"]) == 2
    assert lab.decide(row, "APPROVE").json()["status_after"] == HUMAN_VERIFIED
    assert lab.by_key(row)["status"]["open_requests"] == [] and lab.client.get("/api/review/summary").json()["open_requests_total"] == 0


def test_a_stale_fingerprint_is_a_conflict(lab):
    row = lab.registry_primary("5 mg Tirzepatide")
    stale = lab.decide(row, "APPROVE", fingerprint="0" * 64)
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_fingerprint"] == lab.by_key(row)["fingerprint"] and "changed since it was opened" in stale.json()["detail"]["message"]
    assert lab.status(row) == MACHINE
    with lab.session() as session:
        assert session.scalar(select(func.count()).select_from(ReviewDecision)) == 0  # nothing was recorded
    malformed = lab.decide(row, "APPROVE", fingerprint="not-a-fingerprint")
    assert malformed.status_code == 422
    assert lab.client.post("/api/review/evidence/result/99999999/decisions", json={"decision": "APPROVE", "reviewer": REVIEWER, "rationale": RATIONALE,
                           "attest_human_review": True, "expected_fingerprint": "0" * 64}, headers=TOKEN).status_code == 404


def test_recording_a_decision_requires_the_reviewer_token(lab):
    row = lab.registry_primary("5 mg Tirzepatide")
    assert lab.decide(row, "APPROVE", headers=None).status_code == 401
    assert lab.decide(row, "APPROVE", headers={"X-Reviewer-Token": "wrong"}).status_code == 401
    settings = get_settings()
    original, settings.reviewer_token = settings.reviewer_token, None
    try:
        refused = lab.decide(row, "APPROVE")
        assert refused.status_code == 403 and "read-only" in refused.json()["detail"]
    finally:
        settings.reviewer_token = original
    assert lab.status(row) == MACHINE
    # Reading needs no token.
    assert lab.client.get("/api/review/evidence", params={"limit": 1}).status_code == 200 and lab.client.get("/api/review/summary").status_code == 200


def test_a_non_human_or_unattested_decision_can_never_verify_or_reject(lab):
    row = lab.registry_primary("5 mg Tirzepatide")
    detail = lab.by_key(row)
    for decision in ("APPROVE", "REJECT", "FLAG_WRONG_ATTRIBUTION"):
        unattested = lab.decide(row, decision, attest=False)
        assert unattested.status_code == 422 and "attestation" in unattested.json()["detail"]
    body = {"decision": "APPROVE", "reviewer": REVIEWER, "rationale": RATIONALE, "expected_fingerprint": detail["fingerprint"]}  # flag omitted altogether
    assert lab.client.post(f"/api/review/evidence/result/{detail['id']}/decisions", json=body, headers=TOKEN).status_code == 422
    assert lab.client.post(f"/api/review/evidence/result/{detail['id']}/decisions", json={**body, "attest_human_review": True, "actor_kind": "HUMAN"}, headers=TOKEN).status_code == 422  # not a client-settable field

    with lab.session() as session:
        for actor in ("AUTOMATION", "SYSTEM"):
            with pytest.raises(InvalidDecision):
                record_decision(session, evidence_type="result", row_id=detail["id"], decision="APPROVE", reviewer=REVIEWER, rationale=RATIONALE, attested=True,
                                expected_fingerprint=detail["fingerprint"], actor_kind=actor)
        # An automated actor may leave a request; it changes nothing.
        note = record_decision(session, evidence_type="result", row_id=detail["id"], decision="REQUEST_INFO", reviewer="Consistency Checker", rationale=RATIONALE,
                               attested=False, expected_fingerprint=detail["fingerprint"], actor_kind="AUTOMATION")
        assert note.status_after == MACHINE
        # Even a decision row forced into the table (bypassing the service) cannot verify: reconcile ignores it.
        version = session.scalar(select(EvidenceVersion).where(EvidenceVersion.evidence_key == detail["evidence_key"], EvidenceVersion.fingerprint == detail["fingerprint"]))
        for uid, actor, attested in (("forged-1", "AUTOMATION", True), ("forged-2", "HUMAN", False)):
            session.add(ReviewDecision(target_type="result", decision="APPROVE", reviewer="Somebody Else", rationale=RATIONALE, uid=uid, evidence_type="result",
                                       evidence_key=detail["evidence_key"], natural_key=version.natural_key, fingerprint=detail["fingerprint"], evidence_version_id=version.id,
                                       actor_kind=actor, attested=attested))
        session.commit()
        report = reconcile(session)
        session.commit()
        assert report.statuses.get(HUMAN_VERIFIED, 0) == 0 and report.transitions_total == 0
    assert lab.status(row) == MACHINE
    assert all(not d["decisive"] for d in lab.by_key(row)["decisions"])
    lab.reseed()
    assert lab.status(row) == MACHINE and lab.client.get("/api/review/summary").json()["by_status"][HUMAN_VERIFIED] == 0


def test_boilerplate_rationales_and_placeholder_reviewers_are_refused(lab):
    row = lab.registry_primary("5 mg Tirzepatide")
    for rationale in ("ok", "Approve recorded from the review queue.", "checked checked checked checked checked"):
        refused = lab.decide(row, "APPROVE", rationale=rationale)
        assert refused.status_code == 422, rationale
    for reviewer in ("  ", "system", "api"):
        assert lab.decide(row, "APPROVE", reviewer=reviewer).status_code == 422, reviewer
    assert lab.status(row) == MACHINE


def test_a_later_decision_supersedes_an_earlier_one_and_history_is_append_only(lab):
    row = lab.registry_primary("5 mg Tirzepatide")
    assert lab.decide(row, "REJECT", rationale="Entered against the wrong arm when first reviewed here.").json()["status_after"] == REJECTED
    assert lab.decide(row, "APPROVE", rationale="Rechecked against the registry table: the value is the 5 mg arm.").json()["status_after"] == HUMAN_VERIFIED
    detail = lab.by_key(row)
    assert [d["decision"] for d in detail["decisions"]] == ["APPROVE", "REJECT"] and detail["status"]["verification_status"] == HUMAN_VERIFIED
    assert lab.client.get(f"/api/findings/{detail['id']}").status_code == 200  # usable again
    with lab.session() as session:
        decision = session.scalar(select(ReviewDecision).order_by(ReviewDecision.id))
        decision.rationale = "rewritten after the fact"
        with pytest.raises(PermissionError):
            session.flush()
        session.rollback()
        session.delete(session.scalar(select(ReviewDecision).order_by(ReviewDecision.id)))
        with pytest.raises(PermissionError):
            session.flush()
        session.rollback()
        event = session.scalar(select(AuditEvent).order_by(AuditEvent.id))
        event.actor = "someone else"
        with pytest.raises(PermissionError):
            session.flush()


# --------------------------------------------------------------------------- the review list and dossier


def test_review_list_filters_sorts_paginates_and_counts(lab):
    client = lab.client
    everything = client.get("/api/review/evidence", params={"limit": 5}).json()
    total = everything["total"]
    assert total == everything["all_rows"] > 400 and len(everything["rows"]) == 5  # total is the match count, not the page size
    assert sum(everything["facets"]["evidence_type"].values()) == total and sum(everything["facets"]["status"].values()) == total
    assert set(everything["facets"]) == {"evidence_type", "status", "tier", "source_type", "drug", "risk_level", "endpoint_category", "origin", "missing_field"}

    # Pagination walks the whole result exactly once.
    pending = client.get("/api/review/evidence", params={"status": PENDING, "limit": 500}).json()
    seen: list[str] = []
    for offset in range(0, pending["total"] + 3, 3):
        page = client.get("/api/review/evidence", params={"status": PENDING, "limit": 3, "offset": offset}).json()
        assert page["total"] == pending["total"]
        seen += [r["evidence_key"] for r in page["rows"]]
    assert seen == [r["evidence_key"] for r in pending["rows"]] and len(set(seen)) == pending["total"] > 0

    # Each filter narrows; a facet reports what its own filter would return.
    for params, check in (
        ({"evidence_type": "analysis"}, lambda r: r["evidence_type"] == "analysis"),
        ({"evidence_type": ["claim", "mechanism"]}, lambda r: r["evidence_type"] in ("claim", "mechanism")),
        ({"drug": "semaglutide"}, lambda r: "semaglutide" in r["drugs"]),
        ({"nct": SURMOUNT_5}, lambda r: r["study_nct"] == SURMOUNT_5),
        ({"source_type": "PUBMED"}, lambda r: r["source_type"] == "PUBMED"),
        ({"tier": "REGULATORY_LABEL"}, lambda r: r["tier"] == "REGULATORY_LABEL"),
        ({"status": [PENDING, ANCHORED]}, lambda r: r["verification_status"] in (PENDING, ANCHORED)),
        ({"status": "CURATED_PENDING_REVIEW"}, lambda r: r["verification_status"] == PENDING),  # legacy alias accepted as input
        ({"endpoint_category": "WEIGHT_PCT_CHANGE"}, lambda r: r["endpoint_category"] == "WEIGHT_PCT_CHANGE"),
        ({"missing_field": "confidence_interval"}, lambda r: "confidence_interval" in r["missing_fields"]),
        ({"origin": "CURATED"}, lambda r: r["origin"] == "CURATED"),
        ({"q": "nausea"}, lambda r: "nausea" in r["label"].lower()),
        ({"q": "with 15-mg doses"}, lambda r: r["origin"] == "CURATED"),  # free text also searches the locator quote
    ):
        body = client.get("/api/review/evidence", params={**params, "limit": 500}).json()
        assert 0 < body["total"] < total and all(check(r) for r in body["rows"]), params
        assert body["total"] == len(body["rows"]) or body["total"] > 500
    # Risk level: the four levels partition the rows, and each filter returns what its facet announces.
    assert client.get("/api/review/evidence", params={"risk_level": ["HIGH", "MEDIUM", "LOW", "NONE"]}).json()["total"] == total
    for level in ("HIGH", "MEDIUM", "LOW", "NONE"):
        body = client.get("/api/review/evidence", params={"risk_level": level, "limit": 500}).json()
        assert body["total"] == everything["facets"]["risk_level"].get(level, 0) and all(r["risk_level"] == level for r in body["rows"])
    typed = client.get("/api/review/evidence", params={"evidence_type": "analysis", "origin": "CURATED"}).json()
    assert typed["facets"]["evidence_type"]["analysis"] == typed["total"] and typed["facets"]["evidence_type"]["result"] > 0  # other values of the own facet stay countable
    assert typed["facets"]["origin"] == {"CURATED": typed["total"], "REGISTRY": client.get("/api/review/evidence", params={"evidence_type": "analysis", "origin": "REGISTRY"}).json()["total"]}
    assert client.get("/api/review/evidence", params={"status": "NOT_A_STATUS"}).status_code == 422

    # Sorting. The two studies hold more rows than one page since every posted serious adverse-event term is
    # stored, so the orders are compared over the whole list, page by page, not over the first page only.
    by_status = lab.all_rows(sort="status")
    assert len(by_status) == total > 500
    assert by_status[0]["verification_status"] == PENDING and by_status[-1]["verification_status"] == MACHINE
    by_study = [r["study_name"] or "" for r in lab.all_rows(sort="study", nct=SURMOUNT_1) + lab.all_rows(sort="study", nct=SURMOUNT_5)]
    assert by_study == sorted(by_study)
    assert lab.rows(sort="risk")[0]["risk_level"] in ("HIGH", "MEDIUM", "LOW", "NONE")
    assert [r["evidence_key"] for r in lab.all_rows(sort="status", descending=True)] == [r["evidence_key"] for r in by_status][::-1]

    # Changed-since filters are empty on a database nothing has happened to.
    assert client.get("/api/review/evidence", params={"changed_since_last_seen": True}).json()["total"] == 0
    assert client.get("/api/review/evidence", params={"changed_since_review": True}).json()["total"] == 0


def test_the_dossier_gives_a_reviewer_everything_needed_to_decide(lab):
    registry = lab.by_key(lab.registry_primary("15 mg Tirzepatide"))
    assert registry["origin"] == "REGISTRY" and registry["anchoring"]["basis"] == "STRUCTURED_PAYLOAD"
    source = registry["source"]
    assert source["key"] == f"ctgov:{SURMOUNT_1}" and source["url"].endswith(SURMOUNT_1) and source["tier"] == "REGISTRY_RESULTS" and source["stores_full_text"]
    assert source["content_hash"] and source["snapshot_version"] == 1 and source["retrieved_at"] and source["license_note"]
    located = registry["registry"]
    assert located["resolved"] and located["location"].endswith("measurements[OG003]") and located["fragment"] == {"groupId": "OG003", "value": "-22.5", "spread": "0.39"}
    assert located["labels"]["measure_title"] == PRIMARY_TITLE and located["labels"]["group_title"] == "15 mg Tirzepatide"
    assert located["labels"]["unit"] and located["labels"]["analysis_population"] and located["labels"]["param_type"]
    assert registry["values"]["value"] == -22.5 and registry["values"]["dispersion_value"] == 0.39 and registry["values"]["n_analyzed"] == 625
    assert registry["confidence_interval"] == {"low": None, "high": None, "level": None, "sides": None, "status": "NOT_REPORTED"} and "confidence_interval" in registry["missing_fields"]
    assert registry["study"]["nct_id"] == SURMOUNT_1 and registry["population"]["diabetes_status"] == "NO_T2D" and registry["population"]["baseline_summary"]
    assert registry["arms"][0]["drug_slug"] == "tirzepatide" and registry["arms"][0]["dose_amount"] == 15 and registry["drugs"][0]["slug"] == "tirzepatide"
    assert registry["endpoint"]["role"] == "PRIMARY" and registry["endpoint"]["category"] == "WEIGHT_PCT_CHANGE" and registry["endpoint"]["multiplicity_controlled"] == "NOT_STATED"
    assert registry["time_point"]["weeks"] == 72 and registry["estimand"]["label"] == "EFFICACY" and registry["estimand"]["basis"] == "INFERRED_FROM_ANALYSIS_DESCRIPTION"
    assert isinstance(registry["estimand"]["strategies"], list)
    assert any(a["evidence_type"] == "analysis" and a["values"]["estimate"] is not None for a in registry["related_analyses"])
    assert registry["check_flags"] == [] and registry["risk_level"] == "NONE"
    assert len(registry["versions"]) == 1 and registry["versions"][0]["is_current"] and registry["versions"][0]["snapshot"]["value"] == -22.5
    assert registry["decisions"] == [] and registry["changed_since_review"] is None
    assert {"value", "ci_low", "arm.drug", "endpoint.title", "estimand.label"} <= set(registry["correctable_fields"])

    curated = lab.by_key(lab.curated_result("tirzepatide 15 mg"))
    assert curated["anchoring"]["basis"] == "VERIFIED_EXCERPT" and curated["anchoring"]["quote"].startswith("-20.9% (95% CI, -21.8 to -19.9)") and curated["registry"] is None
    assert curated["source"]["source_type"] == "PUBMED" and curated["source"]["stores_full_text"] is False
    assert curated["confidence_interval"] == {"low": -21.8, "high": -19.9, "level": 95.0, "sides": None, "status": "REPORTED"}
    assert curated["estimand"]["label"] == "TREATMENT_REGIMEN" and curated["estimand"]["basis"] == "STATED_BY_SOURCE"
    # The same quantity (study, arm, endpoint category, time point) from other sources is listed, not judged.
    others = curated["same_quantity_other_sources"]
    assert any(o["source_key"] == f"ctgov:{SURMOUNT_1}" and o["values"]["value"] == -22.5 for o in others)

    label_based = lab.by_key(lab.label_analysis("tirzepatide 15 mg"))
    assert label_based["anchoring"]["basis"] == "FULL_TEXT" and "-17.8 (-19.3, -16.3)" in label_based["anchoring"]["context"]
    assert label_based["source"]["stores_full_text"] and [a["role"] for a in label_based["arms"]] == ["treatment", "comparator"]
    assert label_based["confidence_interval"]["status"] == "REPORTED" and label_based["values"]["p_value_text"] == "<0.001"
    assert any(r["evidence_type"] == "result" for r in label_based["related_analyses"]) or label_based["related_analyses"] == []

    by_id = lab.client.get(f"/api/review/evidence/result/{registry['id']}").json()
    assert by_id["evidence_key"] == registry["evidence_key"] and by_id["fingerprint"] == registry["fingerprint"]
    assert lab.client.get("/api/review/evidence/by-key/result/" + "0" * 64).status_code == 404
    assert lab.client.get("/api/review/evidence/nonsense/1").status_code == 422
    for evidence_type in ("adverse_event", "claim", "mechanism", "indication"):
        sample = lab.rows(evidence_type=evidence_type)[0]
        assert lab.by_key(sample)["evidence_type"] == evidence_type


def test_reading_review_data_has_no_side_effects(lab):
    row = lab.registry_primary("15 mg Tirzepatide")

    def fingerprint_of_database() -> tuple:
        with sqlite3.connect(lab.db_path) as conn:
            counts = tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("audit_event", "review_decision", "evidence_version", "change_event", "review_queue_item"))
            return counts + conn.execute("SELECT MAX(last_seen_at), SUM(is_current) FROM evidence_version").fetchone() + (conn.execute("PRAGMA data_version").fetchone()[0],)

    before = fingerprint_of_database()
    lab.rows(status=PENDING)
    lab.by_key(row)
    lab.client.get(f"/api/review/evidence/result/{lab.by_key(row)['id']}")
    lab.client.get("/api/review/summary")
    lab.client.get("/api/review-queue")
    lab.client.get("/api/meta")
    lab.client.get(f"/api/studies/{SURMOUNT_1}")
    assert fingerprint_of_database() == before


def test_openapi_schema_describes_the_review_api():
    from incretinos.api.main import app

    spec = app.openapi()
    listing = spec["paths"]["/api/review/evidence"]["get"]
    assert {p["name"] for p in listing["parameters"]} >= {"evidence_type", "status", "drug", "nct", "source_type", "tier", "endpoint_category", "risk_level", "missing_field",
                                                           "changed_since_last_seen", "changed_since_review", "q", "sort", "limit", "offset"}
    assert listing["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/EvidenceListResponse")
    schemas = spec["components"]["schemas"]
    assert {"EvidenceDetail", "EvidenceListItem", "EvidenceDecisionRequest", "EvidenceDecisionResponse", "ReviewSummary", "SourceBlock", "AnchoringBlock", "RegistryBlock"} <= set(schemas)
    request = schemas["EvidenceDecisionRequest"]
    assert set(request["required"]) == {"decision", "reviewer", "rationale", "expected_fingerprint"} and request["additionalProperties"] is False
    assert set(request["properties"]["decision"]["enum"]) == {"APPROVE", "REJECT", "REQUEST_INFO", "FLAG_WRONG_ATTRIBUTION", "PROPOSE_CORRECTION"}
    decide = spec["paths"]["/api/review/evidence/{evidence_type}/{row_id}/decisions"]["post"]
    assert any(p["name"] == "x-reviewer-token" for p in decide["parameters"])


# --------------------------------------------------------------------------- persistence across rebuilds


def _decided_lab(lab: Lab) -> dict[str, dict[str, Any]]:
    """Approve one registry row, one curated row and one label-based analysis; reject one registry row; leave a request."""
    rows = {
        "approved_registry": lab.registry_primary("15 mg Tirzepatide"),
        "rejected_registry": lab.registry_primary("Placebo"),
        "approved_curated": lab.curated_result("tirzepatide 15 mg"),
        "approved_label": lab.label_analysis("tirzepatide 15 mg"),
        "requested": lab.curated_result("placebo"),
    }
    assert lab.decide(rows["approved_registry"], "APPROVE").json()["status_after"] == HUMAN_VERIFIED
    assert lab.decide(rows["rejected_registry"], "REJECT", rationale="The posted value for the placebo group is entered under the wrong outcome.").json()["status_after"] == REJECTED
    assert lab.decide(rows["approved_curated"], "APPROVE").json()["status_after"] == HUMAN_VERIFIED
    assert lab.decide(rows["approved_label"], "APPROVE").json()["status_after"] == HUMAN_VERIFIED
    assert lab.decide(rows["requested"], "REQUEST_INFO", attest=False, rationale="Is the placebo estimate from the same model as the active arms?").status_code == 200
    return rows


EXPECTED = {"approved_registry": HUMAN_VERIFIED, "rejected_registry": REJECTED, "approved_curated": HUMAN_VERIFIED, "approved_label": HUMAN_VERIFIED, "requested": ANCHORED}


def _assert_review_state(lab: Lab, rows: dict[str, dict[str, Any]], *, decisions: int = 5) -> None:
    for name, expected in EXPECTED.items():
        detail = lab.by_key(rows[name])  # the by-key link survives the rebuild although the primary key changed
        assert detail["status"]["verification_status"] == expected, name
        assert detail["fingerprint"] == rows[name]["fingerprint"], name
        assert len(detail["decisions"]) == 1 and detail["decisions"][0]["on_current_version"], name
    summary = lab.client.get("/api/review/summary").json()
    assert summary["by_status"][HUMAN_VERIFIED] == 3 and summary["by_status"][REJECTED] == 1 and summary["by_status"][RECHECK] == 0
    assert summary["decisions_recorded"] == decisions and summary["open_requests_total"] == 1 and summary["orphaned_decisions_total"] == 0
    assert summary["unreconciled_rows"] == {}
    rejected_id = lab.by_key(rows["rejected_registry"])["id"]
    assert lab.client.get(f"/api/findings/{rejected_id}").status_code == 404  # still unused after the rebuild
    assert rejected_id not in _find_ids(lab.client.get("/api/findings", params={"study": SURMOUNT_1, "arm_level_only": False, "limit": 2000}).json())
    assert [r.rule_id for r in _failed_rules(lab)] == []


def test_statuses_and_history_survive_a_reseed_a_reset_and_reopening_the_database(lab):
    rows = _decided_lab(lab)
    ids_before = {name: lab.by_key(row)["id"] for name, row in rows.items()}
    with lab.session() as session:
        audit_before = session.scalar(select(func.count()).select_from(AuditEvent))
        snapshots_before = session.scalar(select(func.count()).select_from(SourceSnapshot))
        versions_before = session.scalar(select(func.count()).select_from(EvidenceVersion))
    _assert_review_state(lab, rows)

    # 1. A plain reseed rebuilds every registry and curated row (new primary keys); the decisions are re-applied.
    report = lab.reseed()
    assert report.review["changed"] == 0 and report.review["transitions"] == 0 and report.review["new_versions"] == 0
    ids_after = {name: lab.by_key(row)["id"] for name, row in rows.items()}
    assert all(ids_after[name] != ids_before[name] for name in ("approved_registry", "rejected_registry", "approved_curated", "approved_label"))
    _assert_review_state(lab, rows)
    with lab.session() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "review.status_changed")) == 0  # no spurious transitions

    # 2. Closing and reopening the database.
    reset_engine()
    _assert_review_state(lab, rows)

    # 3. `seed --reset` through the command line: derived tables are emptied and rebuilt, review history is kept.
    from incretinos.cli import app as cli

    result = CliRunner().invoke(cli, ["seed", "--reset"])
    assert result.exit_code == 0, result.output[-2000:]
    report = json.loads(result.stdout)
    assert report["failures"] == [] and report["review"]["changed"] == 0 and report["review"]["transitions"] == 0 and report["review"]["orphaned_decisions"] == 0
    _assert_review_state(lab, rows)
    with lab.session() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) > audit_before  # the audit log was kept and appended to
        assert session.scalar(select(func.count()).select_from(SourceSnapshot)) == snapshots_before  # snapshots kept, none duplicated
        assert session.scalar(select(func.count()).select_from(EvidenceVersion)) == versions_before  # version history kept, nothing re-versioned
        assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "review.evidence_decision")) == 5
        assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "review.status_changed")) == 0

    # 4. `init-db --reset` alone empties the evidence tables and still keeps the ledger; reseeding restores the statuses.
    assert CliRunner().invoke(cli, ["init-db", "--reset"]).exit_code == 0
    with lab.session() as session:
        assert session.scalar(select(func.count()).select_from(OutcomeResult).execution_options(include_rejected=True)) == 0
        assert session.scalar(select(func.count()).select_from(ReviewDecision)) == 5
        assert session.scalar(select(func.count()).select_from(EvidenceVersion)) == versions_before
    status = CliRunner().invoke(cli, ["review", "status", "--json"])
    assert json.loads(status.stdout)["orphaned_decisions_total"] == 5  # nothing to attach to until the data is loaded again
    lab.reseed()
    _assert_review_state(lab, rows)


def test_a_rejected_row_is_not_restored_by_reseeding_and_is_held_between_load_steps(lab):
    row = lab.registry_primary("15 mg Tirzepatide")
    assert lab.decide(row, "REJECT", rationale="Value does not match the registry table for this results group.").json()["status_after"] == REJECTED
    observed: list[tuple[str, str | None]] = []

    with lab.session() as session:
        pipeline = lab.pipeline(session)
        original = pipeline.ingest_publications

        def spy() -> dict:
            # Runs right after the registry step committed its rebuilt rows and before the final reconcile.
            with lab.session() as other:
                observed.append(("mid-load status", other.scalar(select(OutcomeResult.verification_status).where(OutcomeResult.evidence_key.is_(None), OutcomeResult.value == -22.5,
                                 OutcomeResult.origin == "REGISTRY", OutcomeResult.arm_label == "15 mg Tirzepatide").execution_options(include_rejected=True))))
                study = other.scalar(select(Study).where(Study.nct_id == SURMOUNT_1))
                observed.append(("visible to analysis", str(any(r.value == -22.5 and r.arm_label == "15 mg Tirzepatide" and r.origin == "REGISTRY" for r in study.results))))
            return original()

        pipeline.ingest_publications = spy  # type: ignore[method-assign]
        report = pipeline.run(skip={"discover"})
        session.commit()
    assert not report.failures
    assert observed == [("mid-load status", REJECTED), ("visible to analysis", "False")]  # never exposed with a mechanical status
    assert lab.status(row) == REJECTED
    for _ in range(2):
        lab.reseed()
        assert lab.status(row) == REJECTED
    lab.check_updates()
    assert lab.status(row) == REJECTED
    assert lab.client.get(f"/api/findings/{lab.by_key(row)['id']}").status_code == 404
    with lab.session() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "review.status_changed")) == 0
    assert [r.rule_id for r in _failed_rules(lab)] == []


def _revise_primary_outcome(new_value: str) -> Callable[[dict], None]:
    """A labelled perturbation of the SURMOUNT-1 registry payload: the posted 15 mg primary value is revised."""

    def patch(document: dict) -> None:
        measure = document["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
        assert measure["title"] == PRIMARY_TITLE
        measurement = next(m for m in measure["classes"][0]["categories"][0]["measurements"] if m["groupId"] == "OG003")
        assert measurement["value"] == "-22.5"
        measurement["value"] = new_value
        document["protocolSection"]["statusModule"]["lastUpdatePostDateStruct"] = {"date": "2027-01-15", "type": "ACTUAL"}

    return patch


def test_a_revised_registry_value_forces_a_recheck_and_leaves_unaffected_approvals_verified(lab):
    changed_row = lab.registry_primary("15 mg Tirzepatide")
    untouched_row = lab.registry_primary("10 mg Tirzepatide")
    rejected_row = lab.registry_primary("Placebo")
    assert lab.decide(changed_row, "APPROVE").json()["status_after"] == HUMAN_VERIFIED
    assert lab.decide(untouched_row, "APPROVE").json()["status_after"] == HUMAN_VERIFIED
    assert lab.decide(rejected_row, "REJECT", rationale="Rejected while testing that a source revision cannot resurrect it.").json()["status_after"] == REJECTED

    report = lab.check_updates({REGISTRY_FIXTURE: _revise_primary_outcome("-20.5")})

    # The change is reported as a change of a reported value, with the old and the new value.
    [change] = [c for c in report.evidence_changes if c["evidence_key"] == changed_row["evidence_key"]]
    assert change["value_changed"] and (change["status_before"], change["status_after"]) == (HUMAN_VERIFIED, RECHECK) and change["source_changed"]
    assert {"field": "value", "before": -22.5, "after": -20.5} in change["changed_fields"] and {"field": "value_text", "before": "-22.5", "after": "-20.5"} in change["changed_fields"]
    assert len(report.evidence_changes) == 1  # nothing else in the record changed

    # A HIGH, reviewable change event with the field diff (it used to be LOW "registry revision date").
    events = lab.changes()
    event = next(e for e in events if e["source"]["key"] == f"ctgov:{SURMOUNT_1}")
    assert event["significance"] == "HIGH" and event["review_status"] == "OPEN"
    value_line = next(c for c in event["changes"] if c.get("evidence_key") == changed_row["evidence_key"] and c["field"] == "result.value")
    assert (value_line["before"], value_line["after"], value_line["significance"]) == (-22.5, -20.5, "HIGH") and "15 mg Tirzepatide" in value_line["label"]
    assert any(c["field"] == "last_update_posted" for c in event["changes"])  # merged with the registry's own protocol-level diff
    queue = lab.client.get("/api/review-queue", params={"item_type": "SOURCE_CHANGE"}).json()["rows"]
    assert any(i["target_id"] == event["id"] and i["priority"] == "HIGH" for i in queue)
    assert len([e for e in events if e["source"]["key"] == f"ctgov:{SURMOUNT_1}"]) == 1

    # The approved row whose value changed needs a recheck and offers the diff against the reviewed version.
    detail = lab.by_key(changed_row)
    assert detail["status"]["verification_status"] == RECHECK and detail["status"]["reason"] == "EVIDENCE_CHANGED_SINCE_APPROVAL" and detail["values"]["value"] == -20.5
    assert detail["fingerprint"] != changed_row["fingerprint"] and detail["source"]["snapshot_version"] == 2
    diff = {d["field"]: (d["before"], d["after"]) for d in detail["changed_since_review"]["diff"]}
    assert diff == {"value": (-22.5, -20.5), "value_text": ("-22.5", "-20.5")}
    assert [v["snapshot"]["value"] for v in detail["versions"]] == [-20.5, -22.5] and detail["versions"][0]["changed_fields"]
    assert detail["decisions"][0]["on_current_version"] is False
    assert lab.one(changed_since_review=True)["evidence_key"] == changed_row["evidence_key"]
    assert lab.one(changed_since_last_seen=True)["evidence_key"] == changed_row["evidence_key"]
    [transition] = lab.audit("review.status_changed")
    assert (transition["detail"]["from"], transition["detail"]["to"], transition["target_id"]) == (HUMAN_VERIFIED, RECHECK, changed_row["evidence_key"])

    # The unaffected approved row in the same study stays verified; the approval is carried forward and audited once.
    assert lab.status(untouched_row) == HUMAN_VERIFIED and lab.by_key(untouched_row)["status"]["reason"] == "APPROVAL_CARRIED_FORWARD"
    [carried] = lab.audit("review.approval_carried_forward")
    assert carried["target_id"] == untouched_row["evidence_key"] and carried["detail"]["to_snapshot_version"] == 2
    assert carried["detail"]["from_source_hash"] != carried["detail"]["to_source_hash"] and carried["detail"]["reviewer"] == REVIEWER
    assert lab.status(rejected_row) == REJECTED  # a source revision does not resurrect a rejected row

    # Nothing is reported twice.
    again = lab.check_updates({REGISTRY_FIXTURE: _revise_primary_outcome("-20.5")})
    assert again.evidence_changes == [] and len(lab.audit("review.approval_carried_forward")) == 1 and len(lab.audit("review.status_changed")) == 1
    assert len(lab.changes()) == len(events)

    # Approving the new version settles it; the registry going back to the earlier value is again a change.
    assert lab.decide(changed_row, "APPROVE", rationale="The revised registry value of -20.5 is what the record now posts.").json()["status_after"] == HUMAN_VERIFIED
    back = lab.check_updates()
    assert [c["evidence_key"] for c in back.evidence_changes] == [changed_row["evidence_key"]]
    assert lab.status(changed_row) == RECHECK and lab.by_key(changed_row)["values"]["value"] == -22.5
    assert [r.rule_id for r in _failed_rules(lab)] == []


def test_a_rejected_registry_row_whose_value_changes_needs_a_recheck_not_a_mechanical_status(lab):
    row = lab.registry_primary("15 mg Tirzepatide")
    assert lab.decide(row, "REJECT", rationale="The posted value looks like a transcription error in the registry.").json()["status_after"] == REJECTED
    lab.reseed({REGISTRY_FIXTURE: _revise_primary_outcome("-21.0")})
    detail = lab.by_key(row)
    assert detail["status"]["verification_status"] == RECHECK and detail["status"]["reason"] == "EVIDENCE_CHANGED_SINCE_REJECTION"
    assert detail["changed_since_review"]["decision"] == "REJECT" and {"field": "value", "before": -22.5, "after": -21.0} in detail["changed_since_review"]["diff"]


def test_a_changed_curated_value_forces_a_recheck(lab):
    row = lab.curated_result("tirzepatide 5 mg")
    sibling = lab.curated_result("tirzepatide 10 mg")
    assert lab.by_key(row)["values"]["value"] == -15.0
    assert lab.decide(row, "APPROVE").json()["status_after"] == HUMAN_VERIFIED
    assert lab.decide(sibling, "APPROVE").json()["status_after"] == HUMAN_VERIFIED

    def edit(document: dict) -> None:  # a labelled perturbation of the curated file: value and quote are changed together
        entry = next(r for r in document["results"] if r["arm"] == "tirzepatide 5 mg")
        assert entry["value"] == -15.0
        entry["value"] = -15.1
        entry["quote"] = entry["quote"].replace("-15.0%", "-15.1%")

    _edit_yaml(lab.curated / "studies" / f"{SURMOUNT_1}.generated.yaml", edit)
    report = lab.reseed()

    detail = lab.by_key(row)  # same evidence key: the quote of an unambiguous row is not part of its identity
    assert detail["values"]["value"] == -15.1 and detail["status"]["verification_status"] == RECHECK
    assert detail["status"]["base_verification_status"] == PENDING and detail["anchoring"]["basis"] == "NOT_ANCHORED"  # the edited quote is not a verified excerpt
    diff = {d["field"]: (d["before"], d["after"]) for d in detail["changed_since_review"]["diff"]}
    assert diff["value"] == (-15.0, -15.1) and diff["value_text"] == ("-15.0", "-15.1") and "quote" in diff and len(diff) == 3
    [change] = report.evidence_changes
    assert change["evidence_key"] == row["evidence_key"] and change["value_changed"] and change["source_changed"] is False
    event = next(e for e in lab.changes() if e["source"]["key"] == "pubmed:35658024")
    assert event["significance"] == "HIGH" and "no new version" in event["summary"]
    assert lab.status(sibling) == HUMAN_VERIFIED and lab.by_key(sibling)["status"]["reason"] == "APPROVED"  # untouched sibling keeps its approval


def _drop_from_label(needle: str, replacement: str) -> Callable[[dict], None]:
    """A labelled perturbation of the Zepbound label fixture: one figure in the clinical-studies text is altered."""

    def patch(document: dict) -> None:
        record = document["results"][0]
        changed = 0
        for section, value in record.items():
            if isinstance(value, list) and any(isinstance(v, str) and needle in v for v in value):
                record[section] = [v.replace(needle, replacement) if isinstance(v, str) else v for v in value]
                changed += 1
        assert changed, "the label text to perturb was not found"

    return patch


def test_a_quote_that_left_the_source_forces_a_recheck(lab):
    approved = lab.label_analysis("tirzepatide 15 mg")
    unreviewed = lab.label_analysis("tirzepatide 10 mg")
    rejected = lab.label_analysis("tirzepatide 5 mg")
    registry_row = lab.registry_primary("15 mg Tirzepatide")
    assert lab.by_key(approved)["anchoring"]["basis"] == "FULL_TEXT" and lab.status(unreviewed) == ANCHORED
    assert lab.decide(approved, "APPROVE").json()["status_after"] == HUMAN_VERIFIED
    assert lab.decide(rejected, "REJECT", rationale="This interval belongs to another dose column of the label table.").json()["status_after"] == REJECTED
    assert lab.decide(registry_row, "APPROVE").json()["status_after"] == HUMAN_VERIFIED
    quote = lab.by_key(approved)["anchoring"]["quote"]
    assert "-17.8 (-19.3, -16.3)" in quote

    # check-updates re-fetches labels but does not reload curated files: reconcile alone must notice (it used to stay anchored).
    lab.check_updates({LABEL_FIXTURE: _drop_from_label("-17.8 (-19.3, -16.3)", "-17.9 (-19.4, -16.4)")})
    detail = lab.by_key(approved)
    assert detail["fingerprint"] == approved["fingerprint"]  # the stored row itself did not change
    assert detail["status"]["verification_status"] == RECHECK and detail["status"]["reason"] == "SUPPORT_NOT_FOUND"
    assert detail["status"]["base_verification_status"] == PENDING and detail["anchoring"]["basis"] == "NOT_ANCHORED" and detail["source"]["snapshot_version"] == 2
    # An anchored row nobody reviewed drops to pending instead of staying "quote-anchored".
    assert lab.status(unreviewed) == PENDING and lab.by_key(unreviewed)["status"]["base_verification_status"] == PENDING
    assert lab.status(rejected) == REJECTED  # whatever the source does
    assert lab.status(registry_row) == HUMAN_VERIFIED  # another source entirely
    transitions = {(t["target_id"], t["detail"]["from"], t["detail"]["to"]) for t in lab.audit("review.status_changed")}
    assert (approved["evidence_key"], HUMAN_VERIFIED, RECHECK) in transitions and (unreviewed["evidence_key"], ANCHORED, PENDING) in transitions
    assert any(e["source"]["key"] == "openfda-label:zepbound" for e in lab.changes())  # the label change itself is on record

    # The label goes back to the version the reviewer saw: the approval of this exact content on this exact source counts again.
    lab.check_updates()
    assert lab.status(approved) == HUMAN_VERIFIED and lab.by_key(approved)["status"]["reason"] == "APPROVED"
    assert lab.status(unreviewed) == ANCHORED and lab.by_key(approved)["source"]["snapshot_version"] == 3
    transitions = {(t["target_id"], t["detail"]["from"], t["detail"]["to"]) for t in lab.audit("review.status_changed")}
    assert (approved["evidence_key"], RECHECK, HUMAN_VERIFIED) in transitions
    assert lab.audit("review.approval_carried_forward") == [] or all(e["target_id"] != approved["evidence_key"] for e in lab.audit("review.approval_carried_forward"))

    # A new label version that still contains the quote: the approval is carried forward to it, and that is audited.
    def append_sentence(document: dict) -> None:
        record = document["results"][0]
        record["indications_and_usage"] = [*record["indications_and_usage"], "Sentence added by a test to produce a new label version."]

    lab.check_updates({LABEL_FIXTURE: append_sentence})
    detail = lab.by_key(approved)
    assert detail["status"]["verification_status"] == HUMAN_VERIFIED and detail["status"]["reason"] == "APPROVAL_CARRIED_FORWARD" and detail["source"]["snapshot_version"] == 4
    assert detail["anchoring"]["basis"] == "FULL_TEXT"
    [carried] = [e for e in lab.audit("review.approval_carried_forward") if e["target_id"] == approved["evidence_key"]]
    assert carried["detail"]["basis"] == "quote found in the new source version" and carried["detail"]["to_snapshot_version"] == 4
    assert lab.status(rejected) == REJECTED and lab.status(unreviewed) == ANCHORED
    assert [r.rule_id for r in _failed_rules(lab)] == []


def test_a_decision_whose_evidence_disappears_is_surfaced_as_an_orphan_never_deleted(lab):
    row = lab.curated_result("tirzepatide 5 mg")
    assert lab.decide(row, "APPROVE").json()["status_after"] == HUMAN_VERIFIED

    def remove(document: dict) -> None:
        document["results"] = [r for r in document["results"] if r["arm"] != "tirzepatide 5 mg"]

    _edit_yaml(lab.curated / "studies" / f"{SURMOUNT_1}.generated.yaml", remove)
    report = lab.reseed()
    assert report.review["orphaned_decisions"] == 1 and report.review["disappeared_keys"] == 1
    summary = lab.client.get("/api/review/summary").json()
    [orphan] = summary["orphaned_decisions"]
    assert orphan["evidence_key"] == row["evidence_key"] and orphan["decision"] == "APPROVE" and orphan["reviewer"] == REVIEWER and "tirzepatide 5 mg" in orphan["label"]
    assert summary["by_status"][HUMAN_VERIFIED] == 0 and summary["decisions_recorded"] == 1
    assert lab.client.get(f"/api/review/evidence/by-key/result/{row['evidence_key']}").status_code == 404
    with lab.session() as session:
        assert session.scalar(select(func.count()).select_from(ReviewDecision)) == 1  # kept
        history = session.scalars(select(EvidenceVersion).where(EvidenceVersion.evidence_key == row["evidence_key"])).all()
        assert len(history) == 1 and history[0].is_current is False and history[0].snapshot["value"] == -15.0  # what was approved is still on record


def test_a_rejected_row_stays_rejected_when_its_key_changes_because_a_sibling_row_appears(lab):
    row = lab.curated_result("tirzepatide 5 mg")
    assert lab.decide(row, "REJECT", rationale="This estimate is for another estimand than the one the row states.").json()["status_after"] == REJECTED
    assert lab.by_key(row)["identity_collision"] is None

    def add_sibling(document: dict) -> None:
        # A second entry for the same study, source, endpoint, arm, estimand and time point (same numbers, a shorter quote):
        # the natural key no longer identifies one row, so both rows get a tie-breaker and the first row's key changes.
        entry = next(r for r in document["results"] if r["arm"] == "tirzepatide 5 mg")
        sibling = copy.deepcopy(entry)
        sibling["quote"] = "-15.0% (95% confidence interval [CI], -15.9 to -14.2)"
        document["results"].append(sibling)

    _edit_yaml(lab.curated / "studies" / f"{SURMOUNT_1}.generated.yaml", add_sibling)
    lab.reseed()
    pair = [r for r in lab.rows(nct=SURMOUNT_1, evidence_type="result", origin="CURATED") if r["label"].endswith("— tirzepatide 5 mg")]
    assert len(pair) == 2 and all(r["identity_collision"] and "IDENTITY_SHARED_NATURAL_KEY" in r["flag_codes"] and r["risk_level"] == "MEDIUM" for r in pair)
    assert row["evidence_key"] not in {r["evidence_key"] for r in pair}  # re-keyed
    original = next(r for r in pair if r["fingerprint"] == row["fingerprint"])
    added = next(r for r in pair if r["fingerprint"] != row["fingerprint"])
    detail = lab.by_key(original)
    assert detail["status"]["verification_status"] == REJECTED and detail["decisions"][0]["recorded_under_other_key"] is True  # same content: still rejected
    assert detail["identity_collision"]["level"] == 1 and "told apart only by their locator quote" in detail["identity_collision"]["explanation"]
    assert lab.status(added) == RECHECK  # never silently mechanical: a decision exists for this quantity
    summary = lab.client.get("/api/review/summary").json()
    assert summary["orphaned_decisions_total"] == 0 and summary["identity_collisions"] == 2
    assert lab.client.get(f"/api/findings/{detail['id']}").status_code == 404
    assert [r.rule_id for r in _failed_rules(lab)] == []


# --------------------------------------------------------------------------- reconcile, validation, ledger, migration, reset


def test_reconcile_is_idempotent(lab):
    _decided_lab(lab)

    def state() -> dict[str, Any]:
        with sqlite3.connect(lab.db_path) as conn:
            tables = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("audit_event", "review_decision", "evidence_version", "change_event", "review_queue_item")}
            versions = conn.execute("SELECT evidence_type, evidence_key, fingerprint, is_current, effective_status, mechanical_status, supersedes_id, first_seen_at, current_since "
                                    "FROM evidence_version ORDER BY id").fetchall()
        return {"tables": tables, "versions": versions, "rows": lab.fingerprint_state()}

    before = state()
    for _ in range(2):
        with lab.session() as session:
            report = reconcile(session)
            session.commit()
        assert (report.rows_updated, report.new_versions, report.changed_total, report.transitions_total, report.disappeared_keys) == (0, 0, 0, 0, 0)
        assert report.carried_forward == [] and report.change_event_ids == [] and not report.changed_anything
        assert report.statuses[HUMAN_VERIFIED] == 3 and report.statuses[REJECTED] == 1 and report.open_requests == 1
        assert state() == before
    assert CliRunner().invoke(__import__("incretinos.cli", fromlist=["app"]).app, ["review", "reconcile"]).exit_code == 0
    assert state() == before


def test_reconcile_is_fast_enough_on_the_full_database(seeded):
    """About 19,000 reviewable rows: a pass with nothing to change takes a few seconds at most."""
    session = get_sessionmaker()()
    try:
        report = reconcile(session)
        session.rollback()
    finally:
        session.close()
    assert report.rows > 15000 and report.rows_updated == 0 and report.new_versions == 0
    assert report.seconds < 10, report.seconds


def test_v16_catches_a_human_status_that_no_decision_supports(lab):
    row = lab.registry_primary("15 mg Tirzepatide")
    rid = lab.by_key(row)["id"]
    assert [r.rule_id for r in _failed_rules(lab)] == []
    for forced in (HUMAN_VERIFIED, REJECTED):
        with sqlite3.connect(lab.db_path) as conn:  # raw SQL: the one route the ORM guard cannot see
            conn.execute("UPDATE outcome_result SET verification_status = ? WHERE id = ?", (forced, rid))
        [failure] = _failed_rules(lab)
        assert failure.rule_id == "V16" and f"result {rid} is {forced} without an attested human decision" in failure.violations[0]
    with lab.session() as session:  # reconcile repairs it and audits nothing as a real transition
        report = reconcile(session)
        session.commit()
    assert report.rows_updated == 1 and lab.status(row) == MACHINE and [r.rule_id for r in _failed_rules(lab)] == []
    # The dangerous direction: the ledger says REJECTED but the row is stored as usable.
    assert lab.decide(row, "REJECT", rationale="Rejected so that validation has something to protect in this test.").json()["status_after"] == REJECTED
    with sqlite3.connect(lab.db_path) as conn:
        conn.execute("UPDATE outcome_result SET verification_status = 'MACHINE_EXTRACTED' WHERE id = ?", (rid,))
    [failure] = _failed_rules(lab)
    assert failure.rule_id == "V16" and "its decision history yields REJECTED" in failure.violations[0]
    # A legacy or unknown status value fails V13.
    with sqlite3.connect(lab.db_path) as conn:
        conn.execute("UPDATE outcome_result SET verification_status = 'CURATED_PENDING_REVIEW' WHERE id = ?", (rid,))
    assert "V13" in [r.rule_id for r in _failed_rules(lab)]


def test_ledger_export_and_import_reproduce_the_statuses_in_a_fresh_database(lab, template, tmp_path):
    rows = _decided_lab(lab)
    cli = __import__("incretinos.cli", fromlist=["app"]).app
    ledger_path = tmp_path / "ledger.json"
    exported = CliRunner().invoke(cli, ["review", "export", str(ledger_path)])
    assert exported.exit_code == 0 and "Wrote 5 decision(s)" in exported.stdout
    document = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert document["format"] == "incretinos-review-ledger" and document["n_decisions"] == 5
    first = document["decisions"][0]
    assert first["reviewer"] == REVIEWER and first["attested"] is True and first["actor_kind"] == "HUMAN" and first["snapshot"]["value"] == -22.5 and len(first["checksum"]) == 64
    assert "test-reviewer-token" not in ledger_path.read_text(encoding="utf-8")
    source_state = {key: value for key, value in lab.fingerprint_state().items() if value[1] in (HUMAN_VERIFIED, REJECTED)}
    assert len(source_state) == 4

    # A fresh database: the pristine template has the same evidence and no decisions.
    fresh = tmp_path / "fresh.db"
    shutil.copy(template / "template.db", fresh)
    os.environ["INCRETINOS_DATABASE_URL"] = f"sqlite:///{fresh}"
    get_settings.cache_clear()
    reset_engine()
    lab.db_path = fresh
    assert lab.client.get("/api/review/summary").json()["decisions_recorded"] == 0

    # Damaged or dishonest ledgers are refused as a whole.
    tampered = copy.deepcopy(document)
    tampered["decisions"][1]["decision"] = "APPROVE"  # was a rejection
    with lab.session() as session, pytest.raises(ledger_module.LedgerError, match="checksum"):
        ledger_module.import_ledger(session, tampered)
    unattested = copy.deepcopy(document)
    unattested["decisions"][0]["attested"] = False
    unattested["decisions"][0]["checksum"] = ledger_module._checksum(unattested["decisions"][0])
    with lab.session() as session, pytest.raises(ledger_module.LedgerError, match="attested decision by a human reviewer"):
        ledger_module.import_ledger(session, unattested)
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({**document, "format": "something-else"}), encoding="utf-8")
    refused = CliRunner().invoke(cli, ["review", "import", str(bad_file)])
    assert refused.exit_code == 1 and lab.client.get("/api/review/summary").json()["decisions_recorded"] == 0

    imported = CliRunner().invoke(cli, ["review", "import", str(ledger_path)])
    assert imported.exit_code == 0, imported.output
    outcome = json.loads(imported.stdout)
    assert (outcome["in_file"], outcome["imported"], outcome["already_present"], outcome["versions_created"]) == (5, 5, 0, 0)
    assert {key: value for key, value in lab.fingerprint_state().items() if value[1] in (HUMAN_VERIFIED, REJECTED)} == source_state
    _assert_review_state(lab, rows)
    detail = lab.by_key(rows["approved_registry"])
    assert detail["decisions"][0]["imported"] and detail["decisions"][0]["reviewer"] == REVIEWER and detail["decisions"][0]["rationale"] == RATIONALE
    assert len(lab.audit("review.ledger_imported")) == 1

    # Idempotent: importing the same file again adds nothing.
    again = json.loads(CliRunner().invoke(cli, ["review", "import", str(ledger_path)]).stdout)
    assert (again["imported"], again["already_present"]) == (0, 5)
    _assert_review_state(lab, rows)
    assert len(lab.audit("review.ledger_imported")) == 1
    lab.reseed()
    _assert_review_state(lab, rows)

    # A decision made on content this database does not hold is imported with what the reviewer saw, and asks for a recheck.
    older = copy.deepcopy(document["decisions"][0])
    older["uid"] = "00000000-0000-4000-8000-000000000001"
    older["snapshot"] = {**older["snapshot"], "value": -99.0}
    older["fingerprint"] = ledger_module.identity.fingerprint_of(older["snapshot"])
    older["created_at"] = datetime.now(UTC).isoformat()  # the most recent decisive decision for this evidence
    older["checksum"] = ledger_module._checksum(older)
    future = {**older, "uid": "00000000-0000-4000-8000-000000000002", "created_at": "2099-01-01T00:00:00+00:00"}
    future["checksum"] = ledger_module._checksum(future)
    with lab.session() as session, pytest.raises(ledger_module.LedgerError, match="dated in the future"):
        ledger_module.import_ledger(session, {**document, "n_decisions": 1, "decisions": [future]})
    with lab.session() as session:
        report = ledger_module.import_ledger(session, {**document, "n_decisions": 1, "decisions": [older]})
        session.commit()
    assert (report.imported, report.versions_created) == (1, 1)
    detail = lab.by_key(rows["approved_registry"])
    assert detail["status"]["verification_status"] == RECHECK and {"field": "value", "before": -99.0, "after": -22.5} in detail["changed_since_review"]["diff"]


def test_the_migration_upgrades_a_legacy_database(tmp_path):
    """Build a database at the previous revision, put legacy rows in it, upgrade, and check the result."""
    target = tmp_path / "legacy.db"
    env = {**os.environ, "INCRETINOS_DATABASE_URL": f"sqlite:///{target}"}

    def alembic(*args: str) -> subprocess.CompletedProcess:
        run = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env, capture_output=True, text=True)
        assert run.returncode == 0, run.stderr[-1500:]
        return run

    alembic("upgrade", "10d1699882ea")
    now = "2026-10-01 00:00:00.000000"
    with sqlite3.connect(target) as conn:
        assert "evidence_key" not in {row[1] for row in conn.execute("PRAGMA table_info(outcome_result)")}
        conn.execute("INSERT INTO evidence_source (id, key, source_type, tier, title, stores_full_text, classification, external_ids) VALUES (1, 'web:legacy', 'WEB', 'PRESS_RELEASE', 'Legacy source', 0, 'PUBLIC', '{}')")
        conn.execute("INSERT INTO study (id, nct_id, display_name, brief_title, has_results, conditions, countries, design_features) VALUES (1, 'NCT00000001', 'LEGACY-1', 'Legacy study', 0, '[]', '[]', '{}')")
        conn.execute("INSERT INTO endpoint (id, study_id, origin, kind, title, role, category, multiplicity_controlled, is_pooled_arms) VALUES (1, 1, 'CURATED', 'CURATED', 'weight change', 'PRIMARY', 'WEIGHT_PCT_CHANGE', 'NOT_STATED', 0)")
        for row_id, status in ((1, "CURATED_PENDING_REVIEW"), (2, "QUOTE_ANCHORED"), (3, "MACHINE_EXTRACTED")):
            conn.execute("INSERT INTO outcome_result (id, study_id, endpoint_id, origin, arm_label, scope, value, tier, verification_status, missing_fields, source_id) "
                         "VALUES (?, 1, 1, 'CURATED', 'arm', 'ARM', ?, 'PRESS_RELEASE', ?, '[]', 1)", (row_id, -1.0 * row_id, status))
        conn.execute("INSERT INTO statistical_analysis (id, study_id, endpoint_id, origin, hypothesis_type, ni_met, superiority_tested, superiority_met, multiplicity_controlled, tier, verification_status, missing_fields) "
                     "VALUES (1, 1, 1, 'CURATED', 'NOT_STATED', 'NOT_STATED', 'NOT_STATED', 'NOT_STATED', 'NOT_STATED', 'PRESS_RELEASE', 'CURATED_PENDING_REVIEW', '[]')")
        conn.execute("INSERT INTO adverse_event (id, study_id, origin, arm_label, scope, term, category, measure_kind, is_serious, tier, verification_status, missing_fields) "
                     "VALUES (1, 1, 'CURATED', 'arm', 'ARM', 'Nausea', 'OTHER', 'PARTICIPANTS_WITH_EVENT', 0, 'PRESS_RELEASE', 'CURATED_PENDING_REVIEW', '[]')")
        conn.execute("INSERT INTO evidence_claim (id, key, topic, statement, claim_kind, tier, verification_status, origin) VALUES (1, 'legacy:claim', 'DESIGN', 'A statement', 'DOCUMENTED', 'PRESS_RELEASE', 'CURATED_PENDING_REVIEW', 'CURATED')")
        conn.execute("INSERT INTO drug (id, slug, name, brand_names, is_combination, component_slugs, tier_group, regulatory_status) VALUES (1, 'legacy', 'Legacy', '[]', 0, '[]', 'CORE', 'UNKNOWN')")
        conn.execute("INSERT INTO drug_mechanism (id, drug_id, target, action, verification_status) VALUES (1, 1, 'GLP1R', 'AGONIST', 'CURATED_PENDING_REVIEW')")
        conn.execute("INSERT INTO indication (id, drug_id, text, status, jurisdiction, verification_status) VALUES (1, 1, 'An indication', 'APPROVED', 'US', 'CURATED_PENDING_REVIEW')")
        summary = {"bmi_mean": {"value": 38.0, "origin": "CURATED", "source_key": "web:legacy", "quote": "mean BMI 38.0", "verification": "CURATED_PENDING_REVIEW"},
                   "age_mean_years": {"value": 45.0, "origin": "REGISTRY", "location": "x"}}
        conn.execute("INSERT INTO population (id, study_id, diabetes_status, baseline, summary) VALUES (1, 1, 'NOT_STATED', '[]', ?)", (json.dumps(summary),))
        for item_id, item_type, status in ((1, "CURATED_CLAIM", "OPEN"), (2, "CURATED_CLAIM", "RESOLVED"), (3, "CURATED_CLAIM", "IN_REVIEW"), (4, "SOURCE_CHANGE", "OPEN")):
            conn.execute("INSERT INTO review_queue_item (id, item_type, target_type, target_id, title, reason, priority, status, payload, dedupe_key, created_at) "
                         "VALUES (?, ?, 'evidence_source', 1, 'Legacy item', 'reason', 'MEDIUM', ?, '{}', ?, ?)", (item_id, item_type, status, f"legacy:{item_id}", now))
        conn.execute("INSERT INTO review_decision (id, queue_item_id, target_type, target_id, decision, reviewer, rationale, created_at) VALUES (1, 2, 'evidence_source', 1, 'APPROVE', 'Earlier Reviewer', 'Recorded on the old queue', ?)", (now,))
        conn.commit()

    alembic("upgrade", "head")
    with sqlite3.connect(target) as conn:
        for table in ("outcome_result", "statistical_analysis", "adverse_event", "evidence_claim", "drug_mechanism", "indication", "exposure_equivalence"):
            columns = {row[1]: row[2] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert {"evidence_key", "fingerprint", "base_verification_status", "source_snapshot_id", "check_flags", "risk_level"} <= set(columns), table
            assert columns["verification_status"] == "VARCHAR(40)", (table, columns["verification_status"])
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE verification_status = 'CURATED_PENDING_REVIEW'").fetchone()[0] == 0, table
        assert [r[0] for r in conn.execute("SELECT verification_status FROM outcome_result ORDER BY id")] == [PENDING, ANCHORED, MACHINE]
        assert [r[0] for r in conn.execute("SELECT value FROM outcome_result ORDER BY id")] == [-1.0, -2.0, -3.0]  # data untouched
        for table in ("statistical_analysis", "adverse_event", "evidence_claim", "drug_mechanism", "indication"):
            assert conn.execute(f"SELECT verification_status FROM {table}").fetchone()[0] == PENDING, table
        migrated = json.loads(conn.execute("SELECT summary FROM population").fetchone()[0])
        assert migrated["bmi_mean"]["verification"] == PENDING and migrated["bmi_mean"]["value"] == 38.0 and migrated["age_mean_years"] == {"value": 45.0, "origin": "REGISTRY", "location": "x"}
        # Legacy curated queue items are retired, not deleted; the one that carried a decision keeps it as history.
        assert dict(conn.execute("SELECT id, status FROM review_queue_item")) == {1: "RETIRED", 2: "RESOLVED", 3: "RETIRED", 4: "OPEN"}
        assert conn.execute("SELECT queue_item_id, decision, reviewer, evidence_key FROM review_decision").fetchall() == [(2, "APPROVE", "Earlier Reviewer", None)]
        assert conn.execute("SELECT COUNT(*) FROM evidence_version").fetchone()[0] == 0
        # Nothing became human-verified or rejected by migrating.
        assert conn.execute("SELECT COUNT(*) FROM outcome_result WHERE verification_status IN ('HUMAN_VERIFIED', 'REJECTED')").fetchone()[0] == 0
        # The widened column takes the longest status.
        conn.execute("UPDATE outcome_result SET verification_status = 'SOURCE_CHANGED_RECHECK_REQUIRED' WHERE id = 3")
    check = subprocess.run([sys.executable, "-m", "alembic", "check"], cwd=BACKEND, env=env, capture_output=True, text=True)
    assert check.returncode == 0, check.stdout[-800:] + check.stderr[-800:]  # models and migrations agree

    # The upgraded database is usable by the application: reconcile keys the legacy rows.
    engine = make_engine(f"sqlite:///{target}")
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        session.execute(text("UPDATE outcome_result SET verification_status = 'MACHINE_EXTRACTED' WHERE id = 3"))
        report = reconcile(session)
        session.commit()
        assert report.rows == 8 and report.first_seen_keys == 8 and report.statuses.get(HUMAN_VERIFIED, 0) == 0
        assert session.scalar(select(func.count()).select_from(OutcomeResult).where(OutcomeResult.evidence_key.is_(None))) == 0
    engine.dispose()
    alembic("downgrade", "10d1699882ea")
    with sqlite3.connect(target) as conn:
        assert "evidence_key" not in {row[1] for row in conn.execute("PRAGMA table_info(outcome_result)")}
        assert conn.execute("SELECT verification_status FROM outcome_result WHERE id = 1").fetchone()[0] == "CURATED_PENDING_REVIEW"


def test_a_database_that_predates_the_lifecycle_is_reported_clearly(tmp_path):
    from incretinos.db import SchemaOutOfDate, create_all, ensure_schema_current

    target = tmp_path / "old.db"
    run = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "10d1699882ea"], cwd=BACKEND, env={**os.environ, "INCRETINOS_DATABASE_URL": f"sqlite:///{target}"},
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr[-800:]
    engine = make_engine(f"sqlite:///{target}")
    with pytest.raises(SchemaOutOfDate, match="alembic upgrade head"):
        ensure_schema_current(engine)
    with pytest.raises(SchemaOutOfDate):
        create_all(engine)
    engine.dispose()
    with sqlite3.connect(target) as conn:  # nothing was created on the way: the database can still be migrated
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'evidence_version'").fetchone()[0] == 0
    upgraded = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env={**os.environ, "INCRETINOS_DATABASE_URL": f"sqlite:///{target}"},
                              capture_output=True, text=True)
    assert upgraded.returncode == 0, upgraded.stderr[-800:]
    engine = make_engine(f"sqlite:///{target}")
    create_all(engine)  # now accepted
    engine.dispose()


def test_reset_keeps_review_history_and_wiping_it_needs_its_own_flag(lab, tmp_path):
    rows = _decided_lab(lab)
    cli = __import__("incretinos.cli", fromlist=["app"]).app
    with lab.session() as session:
        session.add(ReviewQueueItem(item_type="CURATED_CLAIM", target_type="evidence_source", target_id=1, title="legacy", reason="legacy", dedupe_key="curated:legacy"))
        gap_key = session.execute(text("SELECT key FROM evidence_gap WHERE drug_id IS NOT NULL LIMIT 1")).scalar()
        session.execute(text("UPDATE evidence_gap SET review_status = 'CONFIRMED', reviewer_note = 'kept by a reviewer' WHERE key = :k"), {"k": gap_key})
        session.commit()
        kept_before = {t: session.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in ("review_decision", "evidence_version", "audit_event", "source_snapshot", "change_event", "evidence_source")}

    # The wipe flag alone is refused; --reset alone keeps everything a person wrote.
    refused = CliRunner().invoke(cli, ["seed", "--wipe-review-history"])
    assert refused.exit_code != 0
    with lab.session() as session:
        removed = reset_derived_data(session)
        session.commit()
        assert removed["outcome_result"] > 0 and removed["study"] == 2 and "review_decision" not in removed and "audit_event" not in removed and "evidence_version" not in removed
        for table, count in kept_before.items():
            assert session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() == count, table
        assert session.execute(text("SELECT review_status, reviewer_note FROM evidence_gap WHERE key = :k"), {"k": gap_key}).one() == ("CONFIRMED", "kept by a reviewer")
        assert session.execute(text("SELECT COUNT(*) FROM study")).scalar() == 0 and session.execute(text("SELECT COUNT(*) FROM drug")).scalar() == 0
    lab.reseed()
    _assert_review_state(lab, rows)
    with lab.session() as session:
        assert session.execute(text("SELECT review_status, reviewer_note, drug_id IS NOT NULL FROM evidence_gap WHERE key = :k"), {"k": gap_key}).one() == ("CONFIRMED", "kept by a reviewer", 1)
        assert session.scalar(select(ReviewQueueItem.status).where(ReviewQueueItem.dedupe_key == "curated:legacy")) == "RETIRED"  # reconcile retires legacy items

    # Wiping review history is a separate, explicit flag; a ledger backup is written first.
    wiped = CliRunner().invoke(cli, ["seed", "--reset", "--wipe-review-history"])
    assert wiped.exit_code == 0, wiped.output[-1500:]
    backups = list(get_settings().cache_dir.parent.glob("review-ledger-before-wipe-*.json"))
    try:
        assert backups and json.loads(backups[-1].read_text(encoding="utf-8"))["n_decisions"] == 5
        summary = lab.client.get("/api/review/summary").json()
        assert summary["decisions_recorded"] == 0 and summary["by_status"][HUMAN_VERIFIED] == 0 and summary["by_status"][REJECTED] == 0
        # ... and the backup restores it.
        restored = CliRunner().invoke(cli, ["review", "import", str(backups[-1])])
        assert restored.exit_code == 0 and json.loads(restored.stdout)["imported"] == 5
        _assert_review_state(lab, rows)
    finally:
        for backup in backups:
            backup.unlink()


def test_review_status_command_reports_the_ledger(lab):
    _decided_lab(lab)
    cli = __import__("incretinos.cli", fromlist=["app"]).app
    result = CliRunner().invoke(cli, ["review", "status"])
    assert result.exit_code == 0
    assert "decisions recorded: 5" in result.stdout and "Human-verified" in result.stdout and "Open requests: 1" in result.stdout and "orphaned decisions: 0" in result.stdout
    as_json = json.loads(CliRunner().invoke(cli, ["review", "status", "--json"]).stdout)
    assert as_json["by_status"][REJECTED] == 1 and any(c["status"] == HUMAN_VERIFIED and c["drug"] == "tirzepatide" and c["evidence_type"] == "result" for c in as_json["counts"])


# --------------------------------------------------------------------------- the legacy queue


def test_queue_items_follow_a_state_machine_and_legacy_curated_items_are_refused(lab):
    client = lab.client
    with lab.session() as session:
        assert session.scalar(select(func.count()).select_from(ReviewQueueItem).where(ReviewQueueItem.item_type == "CURATED_CLAIM")) == 0  # no load creates them any more
        legacy = ReviewQueueItem(item_type="CURATED_CLAIM", target_type="evidence_source", target_id=1, title="legacy curated item", reason="legacy",
                                 payload={"source_key": "pubmed:35658024", "quote": "x"}, dedupe_key="curated:legacy-item", status="RETIRED")
        change = ReviewQueueItem(item_type="SOURCE_CHANGE", target_type="change_event", target_id=None, title="a source changed", reason="changed", dedupe_key="change:test")
        session.add_all([legacy, change])
        session.commit()
        legacy_id, change_id = legacy.id, change.id
    body = {"decision": "APPROVE", "reviewer": REVIEWER, "rationale": "Reviewed the change summary against the registry history."}

    refused = client.post(f"/api/review-queue/{legacy_id}/decision", json=body, headers=TOKEN)
    assert refused.status_code == 409 and "/api/review/evidence" in refused.json()["detail"]["evidence_review"] and "pubmed:35658024" in refused.json()["detail"]["evidence_review"]
    assert "retired" in refused.json()["detail"]["message"]

    assert client.post(f"/api/review-queue/{change_id}/decision", json={**body, "decision": "REOPEN"}, headers=TOKEN).status_code == 409  # nothing to reopen
    info = client.post(f"/api/review-queue/{change_id}/decision", json={**body, "decision": "NEEDS_INFO"}, headers=TOKEN)
    assert info.status_code == 200 and info.json()["status"] == "IN_REVIEW"
    done = client.post(f"/api/review-queue/{change_id}/decision", json=body, headers=TOKEN)
    assert done.status_code == 200 and (done.json()["previous_status"], done.json()["status"]) == ("IN_REVIEW", "RESOLVED")
    again = client.post(f"/api/review-queue/{change_id}/decision", json={**body, "decision": "REJECT"}, headers=TOKEN)
    assert again.status_code == 409 and "REOPEN" in again.json()["detail"]  # a resolved item accepts no contradictory decision
    reopened = client.post(f"/api/review-queue/{change_id}/decision", json={**body, "decision": "REOPEN", "rationale": "Reopened: the registry history shows a further change."}, headers=TOKEN)
    assert reopened.status_code == 200 and reopened.json()["status"] == "OPEN"
    assert client.post(f"/api/review-queue/{change_id}/decision", json={**body, "decision": "REJECT"}, headers=TOKEN).json()["status"] == "RESOLVED"
    assert client.post(f"/api/review-queue/{change_id}/decision", json={**body, "reviewer": "system"}, headers=TOKEN).status_code in (409, 422)
    with lab.session() as session:
        decisions = [d.decision for d in session.scalars(select(ReviewDecision).where(ReviewDecision.queue_item_id == change_id).order_by(ReviewDecision.id))]
        assert decisions == ["NEEDS_INFO", "APPROVE", "REOPEN", "REJECT"]
        assert session.scalar(select(func.count()).select_from(ReviewDecision).where(ReviewDecision.queue_item_id == legacy_id)) == 0
    # A queue decision never touches the verification status of evidence.
    assert lab.client.get("/api/review/summary").json()["by_status"][HUMAN_VERIFIED] == 0


def test_a_resolved_queue_item_does_not_suppress_a_later_occurrence(lab):
    from incretinos.ingestion.snapshots import enqueue_review

    with lab.session() as session:
        kwargs = dict(item_type="RECONCILIATION", target_type="study", target_id=1, title="primary publication missing", reason="not retrieved", dedupe_key="missing-primary:test")
        first = enqueue_review(session, **kwargs)
        session.flush()
        assert enqueue_review(session, **kwargs) is None  # still open: no duplicate
        first.status = "RESOLVED"
        session.flush()
        second = enqueue_review(session, **kwargs)
        session.flush()
        assert second is not None and second.id != first.id and second.status == "OPEN" and second.dedupe_key == "missing-primary:test"
        assert first.dedupe_key == f"missing-primary:test#closed:{first.id}" and first.status == "RESOLVED"  # history kept
        assert enqueue_review(session, **kwargs) is None
        session.rollback()


def test_registry_analyses_of_one_outcome_are_told_apart_by_their_group_description(seeded, db):
    """Per-class analyses of one outcome measure share groups, method and parameter; the registry's own label separates them."""
    study = db.scalar(select(Study).where(Study.nct_id == "NCT04660643"))
    lipid = [a for a in study.analyses if a.origin == "REGISTRY" and a.endpoint.title.startswith("Percent Change From Randomization in Lipid Parameters")]
    assert len(lipid) == 6 and len({a.evidence_key for a in lipid}) == 6
    versions = db.scalars(select(EvidenceVersion).where(EvidenceVersion.evidence_key.in_([a.evidence_key for a in lipid]))).all()
    assert {v.snapshot["group_description"] for v in versions} == {"Total Cholesterol", "LDL Cholesterol", "HDL Cholesterol", "VLDL Cholesterol", "Triglycerides", "FFA"}
    assert all(v.collision_level == 0 and v.natural_key == v.evidence_key for v in versions)
    # Keys are unique per table across the full seed, and natural-key collisions are exposed as flags, not hidden.
    for evidence_type, model in REVIEWABLE_MODELS.items():
        total = db.scalar(select(func.count()).select_from(model))
        assert db.scalar(select(func.count(func.distinct(model.evidence_key))).select_from(model)) == total, evidence_type
    collided = db.scalars(select(EvidenceVersion).where(EvidenceVersion.is_current.is_(True), EvidenceVersion.collision_level > 0)).all()
    assert all(any(str(f["code"]).startswith("IDENTITY_") for f in v.check_flags) and v.risk_level in ("MEDIUM", "HIGH") for v in collided)
    # What the reconcile reports and what the version index stores agree, per evidence type.
    assert seeded["review"]["key_collisions"] == dict(Counter(v.evidence_type for v in collided))
    # Before the data repair (revision 65bce81e7f45) 84 curated results and 12 curated analyses shared a natural
    # key with a sibling (responder values filed under another threshold's endpoint, AUC and Cmax under one
    # endpoint). The repair gave each its own endpoint, and the registry's groupDescription is now stored, so the
    # committed seed holds no row that its stored labels cannot tell apart. A new one is a data-quality regression.
    # That a collision, when one exists, is flagged and re-keyed is proved on a constructed sibling row in
    # test_a_rejected_row_stays_rejected_when_its_key_changes_because_a_sibling_row_appears.
    assert collided == [], sorted({v.label for v in collided})[:10]
    assert all(v.evidence_type in ("result", "analysis") and v.origin == "CURATED" for v in collided)  # registry rows need no tie-breaker


def test_evidence_keys_do_not_depend_on_primary_keys_values_or_positions(lab):
    """The same evidence gets the same key in an independently built database, and a changed value keeps the key."""
    row = lab.registry_primary("15 mg Tirzepatide")
    with lab.session() as session:
        before = dict(session.execute(select(StatisticalAnalysis.evidence_key, StatisticalAnalysis.fingerprint)).all())
        ae_keys = set(session.scalars(select(AdverseEvent.evidence_key)))
    lab.reseed({REGISTRY_FIXTURE: _shift_outcomes_and_revise})
    with lab.session() as session:
        after = dict(session.execute(select(StatisticalAnalysis.evidence_key, StatisticalAnalysis.fingerprint)).all())
        assert set(session.scalars(select(AdverseEvent.evidence_key))) == ae_keys
        change_events = session.scalars(select(ChangeEvent)).all()
    # An outcome measure inserted at the front shifts every JSON position; keys and fingerprints of the other rows do not move.
    assert before == after
    detail = lab.by_key(row)
    assert detail["values"]["value"] == -22.5 and detail["fingerprint"] == row["fingerprint"]
    assert detail["registry"]["location"].startswith("resultsSection.outcomeMeasuresModule.outcomeMeasures[1].")
    assert detail["registry"]["fragment"]["value"] == "-22.5"
    assert lab.client.get("/api/review/evidence", params={"changed_since_last_seen": True}).json()["total"] == 0
    assert all(not any(c.get("evidence_key") for c in e.changes) for e in change_events)  # no evidence row was reported as changed


def _shift_outcomes_and_revise(document: dict) -> None:
    """Insert a copy of an existing outcome measure, under a new title, in front of all others (positions shift by one)."""
    measures = document["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"]
    inserted = copy.deepcopy(measures[-1])
    inserted["title"] = "Inserted Outcome Measure (test perturbation)"
    inserted["analyses"] = []
    measures.insert(0, inserted)
    document["protocolSection"]["statusModule"]["lastUpdatePostDateStruct"] = {"date": "2027-02-01", "type": "ACTUAL"}


# --------------------------------------------------------------------------- hooks: automated checks, scheduler, commands, container start


def test_automated_check_flags_are_stored_when_the_checks_module_is_installed(lab, monkeypatch):
    """`review.checks.evaluate_all` is written on another track; reconcile calls it when present and never depends on it."""
    import types

    from incretinos.review.policy import CheckFlag

    row = lab.registry_primary("15 mg Tirzepatide")
    rid = lab.by_key(row)["id"]
    assert lab.by_key(row)["check_flags"] == [] and lab.by_key(row)["risk_level"] == "NONE"
    high_before = lab.all_rows(risk_level="HIGH")

    module = types.ModuleType("incretinos.review.checks")
    module.evaluate_all = lambda session: {("result", rid): [CheckFlag("TEST_ARM_MISMATCH", "HIGH", "Explains a risk; corrects nothing.", ("arm", "value"))]}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "incretinos.review.checks", module)
    with lab.session() as session:
        report = reconcile(session)
        session.commit()
    assert report.checks == "RAN" and report.rows_updated == 1 and report.new_versions == 0 and report.transitions_total == 0
    detail = lab.by_key(row)
    assert detail["check_flags"] == [{"code": "TEST_ARM_MISMATCH", "severity": "HIGH", "explanation": "Explains a risk; corrects nothing.", "fields": ["arm", "value"]}]
    assert detail["risk_level"] == "HIGH" and detail["values"]["value"] == -22.5 and detail["fingerprint"] == row["fingerprint"]  # a flag never changes evidence
    # The stand-in evaluated one row only. A row without an entry was not evaluated, so its stored flags (written by
    # the real checks module when the laboratory database was seeded) are kept: exactly one row changed above.
    high = lab.all_rows(risk_level="HIGH")
    assert all(r["risk_level"] == "HIGH" for r in high) and row["risk_level"] == "NONE"
    [flagged] = [r for r in high if r["evidence_key"] == row["evidence_key"]]
    assert flagged["flag_codes"] == ["TEST_ARM_MISMATCH"] and flagged["n_flags"] == 1
    assert {r["evidence_key"] for r in high} - {row["evidence_key"]} == {r["evidence_key"] for r in high_before}
    by_risk = lab.all_rows(sort="risk")
    order = [("HIGH", "MEDIUM", "LOW", "NONE").index(r["risk_level"]) for r in by_risk]
    assert order == sorted(order) and row["evidence_key"] in [r["evidence_key"] for r in by_risk[: len(high)]]
    with lab.session() as session:
        stored = session.get(OutcomeResult, rid)
        assert stored.risk_level == "HIGH" and stored.check_flags[0]["code"] == "TEST_ARM_MISMATCH" and stored.verification_status == MACHINE

    # A check that crashes is reported and audited; the load is not blocked and the stored flags are kept.
    def broken(session):
        raise RuntimeError("check failed")

    module.evaluate_all = broken  # type: ignore[attr-defined]
    with lab.session() as session:
        report = reconcile(session)
        session.commit()
    assert report.checks == "FAILED" and "RuntimeError: check failed" in report.checks_error and report.rows_updated == 0
    assert lab.by_key(row)["risk_level"] == "HIGH" and len(lab.audit("review.checks_failed")) == 1
    assert lab.reseed().review["checks"] == "FAILED" and lab.by_key(row)["risk_level"] == "HIGH"  # kept across a rebuild of the same content

    # Without the module nothing is flagged (and nothing breaks).
    monkeypatch.delitem(sys.modules, "incretinos.review.checks")
    with lab.session() as session:
        report = reconcile(session)
        session.commit()
    assert report.checks in ("NOT_INSTALLED", "RAN") and (report.checks == "RAN" or lab.by_key(row)["risk_level"] == "NONE")


def test_a_changed_row_carries_the_changed_since_last_ingestion_flag_with_the_previous_values(lab):
    """The flag is built by review.checks from the superseded version in evidence_version, and derived again on every pass."""
    changed_row = lab.registry_primary("15 mg Tirzepatide")
    untouched_row = lab.registry_primary("10 mg Tirzepatide")
    assert "CHANGED_SINCE_LAST_INGESTION" not in {code for r in lab.all_rows() for code in r["flag_codes"]}  # nothing has changed yet
    assert lab.by_key(changed_row)["risk_level"] == "NONE"

    lab.check_updates({REGISTRY_FIXTURE: _revise_primary_outcome("-20.5")})
    detail = lab.by_key(changed_row)
    [flag] = [f for f in detail["check_flags"] if f["code"] == "CHANGED_SINCE_LAST_INGESTION"]
    assert flag["severity"] == "MEDIUM" and set(flag["fields"]) == {"value", "value_text"}
    assert "value: -22.5 → -20.5" in flag["explanation"] and "2 field(s) differ from the previous ingestion" in flag["explanation"]
    assert detail["risk_level"] == "MEDIUM" and detail["values"]["value"] == -20.5
    # The flag names exactly the fields the version history records as changed.
    assert set(flag["fields"]) == {c["field"] for c in detail["versions"][0]["changed_fields"]}
    flagged = [r for r in lab.all_rows() if "CHANGED_SINCE_LAST_INGESTION" in r["flag_codes"]]
    assert [r["evidence_key"] for r in flagged] == [changed_row["evidence_key"]]  # no other row of the record changed
    assert lab.by_key(untouched_row)["check_flags"] == []
    with lab.session() as session:
        stored = session.scalar(select(OutcomeResult).where(OutcomeResult.evidence_key == changed_row["evidence_key"]))
        assert stored.risk_level == "MEDIUM" and [f["code"] for f in stored.check_flags] == ["CHANGED_SINCE_LAST_INGESTION"]
        version = session.scalar(select(EvidenceVersion).where(EvidenceVersion.evidence_key == changed_row["evidence_key"], EvidenceVersion.is_current.is_(True)))
        assert version.check_flags == stored.check_flags and version.supersedes_id is not None

    # Derived from stored history, so a second pass writes nothing and the flag stays while this version is current.
    with lab.session() as session:
        again = reconcile(session)
        session.commit()
    assert again.checks == "RAN" and again.rows_updated == 0 and again.new_versions == 0 and again.changed_total == 0
    assert lab.by_key(changed_row)["check_flags"] == detail["check_flags"]
    assert lab.check_updates({REGISTRY_FIXTURE: _revise_primary_outcome("-20.5")}).review["rows_updated"] == 0

    # The registry going back to the earlier value is again a change, described from the version it replaces.
    lab.check_updates()
    [back] = [f for f in lab.by_key(changed_row)["check_flags"] if f["code"] == "CHANGED_SINCE_LAST_INGESTION"]
    assert "value: -20.5 → -22.5" in back["explanation"] and lab.by_key(changed_row)["values"]["value"] == -22.5
    assert [r.rule_id for r in _failed_rules(lab)] == []


def test_the_scheduled_refresh_reconciles_and_reports_changed_evidence(lab, monkeypatch):
    from incretinos.ingestion import pipeline as pipeline_module
    from incretinos.ingestion.scheduler import refresh_tracked_sources

    row = lab.registry_primary("15 mg Tirzepatide")
    rejected = lab.registry_primary("Placebo")
    assert lab.decide(row, "APPROVE").json()["status_after"] == HUMAN_VERIFIED
    assert lab.decide(rejected, "REJECT", rationale="Rejected before the scheduled refresh rebuilds the registry rows.").json()["status_after"] == REJECTED
    patches: dict[str, Callable[[Any], None]] = {}
    monkeypatch.setattr(pipeline_module, "build_transport", lambda **_: PatchedTransport(FIXTURES, patches=patches))  # the scheduler asks for a live transport

    quiet = refresh_tracked_sources(get_settings())
    assert quiet["failures"] == [] and quiet["evidence_changes"] == [] and quiet["review"]["transitions"] == 0
    assert lab.status(row) == HUMAN_VERIFIED and lab.status(rejected) == REJECTED

    patches[REGISTRY_FIXTURE] = _revise_primary_outcome("-20.5")
    report = refresh_tracked_sources(get_settings())
    assert [c["evidence_key"] for c in report["evidence_changes"]] == [row["evidence_key"]] and report["evidence_changes"][0]["value_changed"]
    assert lab.status(row) == RECHECK and lab.status(rejected) == REJECTED
    assert any(e["significance"] == "HIGH" for e in lab.changes())
    gaps = lab.client.get("/api/gaps").json()["rows"]
    assert any(g["rule_id"] == "G13_RECORD_CHANGE" for g in gaps)  # the open change event is also raised as a gap by the same run


def test_check_updates_command_reports_review_state(lab):
    cli = __import__("incretinos.cli", fromlist=["app"]).app
    row = lab.registry_primary("15 mg Tirzepatide")
    assert lab.decide(row, "APPROVE").json()["status_after"] == HUMAN_VERIFIED
    result = CliRunner().invoke(cli, ["check-updates"])
    assert result.exit_code == 0, result.output[-1500:]
    out = json.loads(result.stdout)
    assert out["failures"] == [] and out["evidence_changes"] == [] and out["changes"] == []
    assert out["review"]["statuses"][HUMAN_VERIFIED] == 1 and out["review"]["transitions"] == 0 and out["review"]["changed"] == 0
    assert lab.status(row) == HUMAN_VERIFIED
    assert [r.rule_id for r in _failed_rules(lab)] == []  # (the other rules need studies this two-study database does not hold)


def test_container_start_reconciles_an_existing_database(lab, capsys):
    """After a schema upgrade the stored rows have no evidence key; the entry step keys them without reloading anything."""
    from incretinos import bootstrap

    row = lab.registry_primary("15 mg Tirzepatide")
    assert lab.decide(row, "APPROVE").json()["status_after"] == HUMAN_VERIFIED
    with sqlite3.connect(lab.db_path) as conn:
        n_before = conn.execute("SELECT COUNT(*) FROM outcome_result").fetchone()[0]
        for table in ("outcome_result", "statistical_analysis", "adverse_event", "evidence_claim", "drug_mechanism", "indication"):
            conn.execute(f"UPDATE {table} SET evidence_key = NULL, fingerprint = NULL, base_verification_status = NULL, source_snapshot_id = NULL")
        conn.execute("UPDATE outcome_result SET verification_status = 'MACHINE_EXTRACTED' WHERE verification_status = 'HUMAN_VERIFIED'")
    assert lab.client.get("/api/review/summary").json()["unreconciled_rows"]["result"] == n_before
    bootstrap.main()
    assert "Review state reconciled" in capsys.readouterr().out
    summary = lab.client.get("/api/review/summary").json()
    assert summary["unreconciled_rows"] == {} and summary["by_status"][HUMAN_VERIFIED] == 1
    assert lab.status(row) == HUMAN_VERIFIED
    with sqlite3.connect(lab.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM outcome_result").fetchone()[0] == n_before  # nothing was reloaded


def test_a_decision_on_a_row_that_was_never_reconciled_is_refused(lab):
    row = lab.registry_primary("5 mg Tirzepatide")
    detail = lab.by_key(row)
    with sqlite3.connect(lab.db_path) as conn:
        conn.execute("UPDATE outcome_result SET evidence_key = NULL, fingerprint = NULL WHERE id = ?", (detail["id"],))
    unkeyed = lab.client.get(f"/api/review/evidence/result/{detail['id']}").json()
    assert unkeyed["reconciled"] is False and unkeyed["evidence_key"] is None
    body = {"decision": "APPROVE", "reviewer": REVIEWER, "rationale": RATIONALE, "attest_human_review": True, "expected_fingerprint": detail["fingerprint"]}
    refused = lab.client.post(f"/api/review/evidence/result/{detail['id']}/decisions", json=body, headers=TOKEN)
    assert refused.status_code == 409 and "reconcile" in refused.json()["detail"]


def test_curated_baseline_entries_are_re_anchored_too(lab):
    """population.summary keeps a mechanical status per curated entry; reconcile keeps it honest (and never writes the legacy name)."""
    with lab.session() as session:
        from incretinos.models import Population

        population = session.scalar(select(Population).join(Study, Population.study_id == Study.id).where(Study.nct_id == SURMOUNT_1))
        quote = session.scalar(select(OutcomeResult.locator_quote).where(OutcomeResult.origin == "CURATED", OutcomeResult.arm_label == "tirzepatide 15 mg"))
        population.summary = {
            **(population.summary or {}),
            "anchored_entry": {"value": 1.0, "origin": "CURATED", "source_key": "pubmed:35658024", "quote": quote, "verification": "CURATED_PENDING_REVIEW"},
            "unanchored_entry": {"value": 2.0, "origin": "CURATED", "source_key": "pubmed:35658024", "quote": "a sentence that is not in the source", "verification": "QUOTE_ANCHORED"},
        }
        session.commit()
        report = reconcile(session)
        session.commit()
        assert report.population_entries_reanchored == 2
        summary = session.scalar(select(Population).where(Population.id == population.id)).summary
        assert summary["anchored_entry"]["verification"] == ANCHORED and summary["unanchored_entry"]["verification"] == PENDING
        assert summary["anchored_entry"]["value"] == 1.0 and all("verification" not in e for e in summary.values() if e.get("origin") == "REGISTRY")
        assert reconcile(session).population_entries_reanchored == 0


def test_a_change_of_supporting_text_alone_asks_for_a_recheck_only_where_a_review_exists(lab):
    """No value changed: reviewed rows need a recheck and are listed; unreviewed rows are versioned without a queue item each."""
    approved = lab.registry_primary("15 mg Tirzepatide")
    assert lab.decide(approved, "APPROVE").json()["status_after"] == HUMAN_VERIFIED

    def reword(document: dict) -> None:  # a labelled perturbation: the sponsor rewords the method description of the primary outcome
        measure = document["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
        assert measure["title"] == PRIMARY_TITLE
        measure["description"] = measure["description"] + " (Reworded by a test.)"
        document["protocolSection"]["statusModule"]["lastUpdatePostDateStruct"] = {"date": "2027-03-01", "type": "ACTUAL"}

    report = lab.check_updates({REGISTRY_FIXTURE: reword})
    changed = {c["evidence_key"]: c for c in report.evidence_changes}
    assert len(changed) == 4 and all(not c["value_changed"] for c in changed.values())  # the four arms of that outcome measure
    assert all([f["field"] for f in c["changed_fields"]] == ["analysis_method"] for c in changed.values())
    detail = lab.by_key(approved)
    assert detail["status"]["verification_status"] == RECHECK and detail["values"]["value"] == -22.5
    assert [d["field"] for d in detail["changed_since_review"]["diff"]] == ["analysis_method"]
    assert lab.client.get("/api/review/evidence", params={"changed_since_last_seen": True}).json()["total"] == 4
    event = next(e for e in lab.changes() if e["source"]["key"] == f"ctgov:{SURMOUNT_1}")
    listed = [c for c in event["changes"] if c.get("evidence_key")]
    assert {c["evidence_key"] for c in listed} == {approved["evidence_key"]} and listed[0]["field"] == "result.analysis_method" and listed[0]["significance"] == "MEDIUM"
    assert "3 further row(s) changed in label or classification only" in event["summary"] and event["significance"] != "HIGH"
    assert len(lab.audit("review.evidence_changed")) == 1
