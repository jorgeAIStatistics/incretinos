"""Hardening: read-only public mode, honest lists, bounded identifiers, inert exports, failure isolation.

Several tests here walk the application's route table instead of naming routes, so a route added later is
covered automatically. When such a test fails on a new route, the message says what to register:

* a GET route, or a POST/PATCH route that does not require the reviewer token, needs sample input in
  ``PATH_VALUES`` / ``QUERY_SAMPLES`` / ``BODY_SAMPLES`` below, so the suite can prove that it writes nothing;
* an integer path or query parameter needs an upper bound (use ``PathId`` / ``QueryId`` from ``api.main``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import event, func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from incretinos import config as config_module
from incretinos.api import limits
from incretinos.api.main import MAX_DB_ID, MAX_OFFSET, app, require_reviewer
from incretinos.config import Settings, describe_settings_sources, env_files, get_settings
from incretinos.db import (
    Base,
    ReadOnlyRequestError,
    get_db,
    get_engine,
    get_sessionmaker,
    make_engine,
    request_may_write,
)
from incretinos.engines import conflicts
from incretinos.ingestion.pipeline import Pipeline
from incretinos.ingestion.transport import FixtureTransport
from incretinos.models import (
    AdverseEvent,
    AuditEvent,
    EvidenceGap,
    EvidenceSource,
    IngestionRun,
    OutcomeResult,
    ReviewQueueItem,
    SourceSnapshot,
    StatisticalAnalysis,
    Study,
)
from incretinos.reports.export import (
    _cell,
    _code,
    _item,
    _link,
    _md,
    comparison_report_markdown,
    study_report_markdown,
)

from .conftest import FIXTURES, result_id

TOKEN = {"X-Reviewer-Token": "test-reviewer-token"}
BACKEND = Path(__file__).resolve().parents[1]
HUGE = 10**24
WRITE_VERBS = {"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "TRUNCATE", "CREATE", "DROP", "ALTER"}


# --------------------------------------------------------------------------- helpers


def _row_counts() -> dict[str, int]:
    """Rows in every table, read through a fresh session so that only committed state is counted."""
    session = get_sessionmaker()()
    try:
        return {table.name: session.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables}
    finally:
        session.close()


@contextmanager
def _recorded_writes() -> Iterator[list[str]]:
    """Every data-changing SQL statement sent to the application's engine while the block runs."""
    statements: list[str] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        words = statement.lstrip().split(None, 1)
        if words and words[0].upper() in WRITE_VERBS:
            statements.append(" ".join(statement.split())[:160])

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


@contextmanager
def _counted_queries() -> Iterator[list[int]]:
    counter = [0]

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        counter[0] += 1

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


def _requires_reviewer(route: APIRoute) -> bool:
    def walk(dependant) -> bool:
        return dependant.call is require_reviewer or any(walk(d) for d in dependant.dependencies)

    return any(walk(d) for d in route.dependant.dependencies)


def _flatten_routes(routes) -> Iterator[APIRoute]:
    """Every APIRoute, including those of an included router.

    Recent FastAPI keeps ``include_router`` lazy: ``app.routes`` then holds one router entry instead of a copy of
    each route, and a walk over APIRoute instances alone would silently skip the evidence review router.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            assert not route.include_context.prefix, "an included router with its own prefix needs the prefix applied here"
            yield from _flatten_routes(route.original_router.routes)


def _api_routes() -> list[tuple[str, APIRoute]]:
    found = [(method, route) for route in _flatten_routes(app.routes) for method in sorted(route.methods - {"HEAD", "OPTIONS"})]
    # The walk must see exactly the operations the application serves: a route this helper cannot reach would be a
    # route no guard below is applied to. The OpenAPI document is built by FastAPI itself, independently of this walk.
    documented = {(method.upper(), path) for path, operations in app.openapi()["paths"].items() for method in operations}
    assert {(method, route.path) for method, route in found} == documented, sorted(documented ^ {(m, r.path) for m, r in found})
    return found


def _required_query_parameters(method: str, path: str) -> set[str]:
    operation = app.openapi()["paths"][path][method.lower()]
    return {p["name"] for p in operation.get("parameters", []) if p["in"] == "query" and p.get("required")}


@pytest.fixture(scope="module")
def samples(seeded) -> dict:
    """Identifiers of real seeded rows, looked up by what they are (ids differ between seeds)."""
    session = get_sessionmaker()()
    try:
        sm1 = session.scalar(select(Study).where(Study.nct_id == "NCT04184622"))
        nausea = {ae.arm_label: ae.id for ae in sm1.adverse_events if ae.term == "Nausea" and ae.origin == "REGISTRY" and ae.measure_kind == "PARTICIPANTS_WITH_EVENT"}
        return {
            "a": (a := result_id(session, "NCT05929066", "12 mg", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=80)),
            "a_key": session.scalar(select(OutcomeResult.evidence_key).where(OutcomeResult.id == a)),
            "b": result_id(session, "NCT04184622", "15 mg", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=72),
            "analysis": session.scalar(select(StatisticalAnalysis.id).order_by(StatisticalAnalysis.id).limit(1)),
            "source": session.scalar(select(EvidenceSource.id).where(EvidenceSource.key == "ctgov:NCT04184622")),
            "gap": session.scalar(select(EvidenceGap.id).order_by(EvidenceGap.id).limit(1)),
            "item": session.scalar(select(ReviewQueueItem.id).order_by(ReviewQueueItem.id).limit(1)),
            "ae_a": nausea["15 mg Tirzepatide"],
            "ae_b": nausea["Placebo"],
            # A source-conflict group (ATTAIN-1, 36 mg, week 72): its identifier is derived from the natural key, not row ids.
            "group": conflicts.all_groups(session, [session.scalar(select(Study.id).where(Study.nct_id == "NCT05869903"))])[0].group_id,
        }
    finally:
        session.close()


# Sample input for the route sweep. Path parameters are filled in by name.
def _path_values(samples: dict) -> dict[str, object]:
    return {"nct": "NCT04184622", "slug": "tirzepatide", "result_id": samples["a"], "analysis_id": samples["analysis"], "source_id": samples["source"], "gap_id": samples["gap"], "item_id": samples["item"],
            # The evidence review router: one stored result, addressed by row id and by its evidence key.
            "evidence_type": "result", "row_id": samples["a"], "evidence_key": samples["a_key"],
            # The source-conflict router: one group of rows from several sources.
            "group_id": samples.get("group", "NCT00000000-0000000000000000")}


def _query_samples(samples: dict) -> dict[str, dict[str, object]]:
    return {
        "/api/harmonize": {"formulation": "orforglipron-capsule", "amount": 20, "target": "foundayo-tablet"},
        "/api/compare/studies": {"a": "NCT04184622", "b": "NCT05929066"},
        "/api/estimands": {"study": "NCT05869903"},
        "/api/safety/terms": {"nct": "NCT04184622"},
        "/api/design/eligibility-compare": {"a": "NCT07321886", "b": "NCT07392190"},
        "/api/reports/comparison": {"a": samples["a"], "b": samples["b"]},
    }


def _body_samples(samples: dict) -> dict[tuple[str, str], dict]:
    return {
        ("POST", "/api/compare"): {"a": samples["a"], "b": samples["b"]},
        ("POST", "/api/benefit-risk/utility"): {"nct": "NCT04184622"},
        ("POST", "/api/benefit-risk/dose-scenario"): {"nct": "NCT04184622", "shares": {"tirzepatide|5|": 0.5, "tirzepatide|15|": 0.5}},
        ("POST", "/api/stats/risk-contrast"): {"ae_a": samples["ae_a"], "ae_b": samples["ae_b"]},
        ("POST", "/api/safety/disproportionality"): {"drug_event": 50, "drug_total": 1000, "event_total": 500, "all_total": 100000},
        ("POST", "/api/design/power"): {"design": "two_means", "delta": 5, "sd": 10},
        ("POST", "/api/design/simulate"): {"n_per_arm": 50, "mean_treatment": -10, "mean_control": -2, "sd": 8, "n_sims": 200},
        ("POST", "/api/design/sensitivity"): {"delta": 5, "sd": 10, "n_randomized_per_group": 100},
        ("POST", "/api/assistant/ask"): {"question": "Which outcomes in EloraTZP are exploratory?"},
    }


def _sweep(client: TestClient, samples: dict, *, headers: dict | None = None, only_protected: bool = False) -> tuple[list[tuple[str, str, int, bool]], list[str]]:
    """Call every route once. Returns (method, path, status, protected) per call and the routes that lack sample input."""
    path_values, query_samples, body_samples = _path_values(samples), _query_samples(samples), _body_samples(samples)
    calls, unregistered = [], []
    for method, route in _api_routes():
        protected = _requires_reviewer(route)
        if only_protected and not protected:
            continue
        names = re.findall(r"{(\w+)(?::[^}]*)?}", route.path)
        unknown = [n for n in names if n not in path_values]
        required_query = _required_query_parameters(method, route.path)
        query = query_samples.get(route.path, {})
        body = body_samples.get((method, route.path))
        if not protected:
            if unknown:
                unregistered.append(f"{method} {route.path}: add path value(s) for {unknown} to _path_values")
                continue
            if required_query - set(query):
                unregistered.append(f"{method} {route.path}: add required query parameter(s) {sorted(required_query - set(query))} to _query_samples")
                continue
            if method != "GET" and body is None:
                unregistered.append(f"{method} {route.path}: add a valid request body to _body_samples, or make the route require the reviewer token")
                continue
        url = route.path
        for name in names:
            url = re.sub(r"{" + name + r"(?::[^}]*)?}", str(path_values.get(name, 1)), url)
        response = client.request(method, url, params=query, json=body if method != "GET" else None, headers=headers)
        calls.append((method, route.path, response.status_code, protected))
    return calls, unregistered


# --------------------------------------------------------------------------- F03: read-only in fact


def test_read_only_deployment_writes_nothing(client, samples, monkeypatch):
    """No token configured: every GET and every unauthenticated POST answers, and not one row changes."""
    monkeypatch.setattr(get_settings(), "reviewer_token", None)
    before = _row_counts()
    with _recorded_writes() as writes:
        # The valid token is sent on purpose: with no token configured it must not unlock anything either.
        calls, unregistered = _sweep(client, samples, headers=TOKEN)
    assert not unregistered, "Routes without sample input (see the module docstring):\n" + "\n".join(unregistered)
    assert len(calls) >= 47
    wrong = [f"{m} {p} -> {status}" for m, p, status, protected in calls if status != (403 if protected else 200)]
    assert not wrong, "Expected 200 from open routes and 403 from reviewer routes:\n" + "\n".join(wrong)
    assert writes == [], f"A read-only deployment sent data-changing SQL: {writes[:5]}"
    assert _row_counts() == before


def test_requests_without_the_reviewer_token_write_nothing(client, samples):
    """Token configured, but the caller has none: open routes still write nothing, reviewer routes answer 401."""
    assert get_settings().reviewer_token
    before = _row_counts()
    with _recorded_writes() as writes:
        calls, unregistered = _sweep(client, samples)
        wrong_token, _ = _sweep(client, samples, headers={"X-Reviewer-Token": "not-the-token"}, only_protected=True)
    assert not unregistered, "\n".join(unregistered)
    wrong = [f"{m} {p} -> {status}" for m, p, status, protected in calls + wrong_token if status != (401 if protected else 200)]
    assert not wrong, "\n".join(wrong)
    assert any(protected for *_, protected in calls), "the sweep found no reviewer-only route to check"
    assert writes == [] and _row_counts() == before


def test_no_get_route_requires_a_body_or_is_declared_with_a_writing_method():
    """The sweep above treats GET as read-only by contract; state changes belong to POST/PATCH behind the token."""
    for method, route in _api_routes():
        if method != "GET" and not _requires_reviewer(route):
            # An open non-GET route is a calculation over the request body. It must not take a database id in its path.
            assert "{" not in route.path, f"{method} {route.path} is open to anyone and addresses a stored row: require the reviewer token or use GET"


def _scratch_app() -> FastAPI:
    scratch = FastAPI()

    def write_and_commit(db: Session = Depends(get_db)) -> dict:
        db.add(AuditEvent(actor="hardening-test", action="test.write", detail={}))
        db.commit()
        return {"ok": True}

    scratch.add_api_route("/write", write_and_commit, methods=["GET", "POST"])
    return scratch


def _test_rows() -> int:
    session = get_sessionmaker()()
    try:
        return session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "test.write"))
    finally:
        session.close()


def test_request_session_cannot_commit_without_the_token(seeded, monkeypatch):
    """The guarantee does not rest on each route behaving: the session of such a request refuses to commit."""
    scratch = TestClient(_scratch_app(), raise_server_exceptions=True)
    for method, headers in (("GET", None), ("POST", None), ("POST", {"X-Reviewer-Token": "wrong"}), ("GET", TOKEN)):
        with pytest.raises(ReadOnlyRequestError):
            scratch.request(method, "/write", headers=headers)
    assert TestClient(_scratch_app(), raise_server_exceptions=False).post("/write").status_code == 500
    monkeypatch.setattr(get_settings(), "reviewer_token", None)
    with pytest.raises(ReadOnlyRequestError):  # no token configured: even the right header value writes nothing
        scratch.post("/write", headers=TOKEN)
    assert _test_rows() == 0


def test_request_session_commits_for_a_reviewer_post(seeded):
    """Positive control for the guard and for the write detector: the same route does write with the token."""
    scratch = TestClient(_scratch_app())
    try:
        with _recorded_writes() as writes:
            assert scratch.post("/write", headers=TOKEN).status_code == 200
        assert _test_rows() == 1 and any(w.upper().startswith("INSERT INTO AUDIT_EVENT") for w in writes)
    finally:
        # Audit rows are append-only through the ORM (the review lifecycle refuses to update or delete one), so the
        # scratch row this test wrote is removed with a statement on the table, never through a mapped object.
        session = get_sessionmaker()()
        with pytest.raises(PermissionError):
            session.delete(session.scalars(select(AuditEvent).where(AuditEvent.action == "test.write")).first())
            session.flush()
        session.rollback()
        session.execute(AuditEvent.__table__.delete().where(AuditEvent.__table__.c.action == "test.write"))
        session.commit()
        session.close()
    assert _test_rows() == 0


def test_write_boundary_is_method_and_token():
    assert request_may_write("POST", "test-reviewer-token") and request_may_write("patch", "test-reviewer-token")
    for method in ("GET", "HEAD", "OPTIONS", "get"):
        assert not request_may_write(method, "test-reviewer-token")
    assert not request_may_write("POST", None) and not request_may_write("POST", "") and not request_may_write("POST", "test-reviewer-token ")


def test_anonymous_activity_goes_to_the_application_log_not_the_audit_trail(client, samples, caplog):
    session = get_sessionmaker()()
    anonymous_before = session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action.in_(["comparison.run", "assistant.question", "report.exported"])))
    session.close()
    with caplog.at_level("INFO", logger="incretinos.usage"):
        assert client.post("/api/compare", json={"a": samples["a"], "b": samples["b"]}).status_code == 200
        assert client.get("/api/reports/comparison", params={"a": samples["a"], "b": samples["b"]}).status_code == 200
        assert client.post("/api/assistant/ask", json={"question": "Which outcomes in EloraTZP are exploratory?"}).status_code == 200
    logged = " | ".join(record.getMessage() for record in caplog.records if record.name == "incretinos.usage")
    assert "comparison.run" in logged and "report.exported" in logged and "assistant.question" in logged
    assert anonymous_before == 0
    audit = client.get("/api/audit", params={"limit": 1000}).json()
    assert not [e for e in audit["rows"] if e["actor"] == "api"], "anonymous API activity must not appear in the audit trail"


# --------------------------------------------------------------------------- F05: reviewer token header


def _evidence_decision(client, samples) -> dict:
    """A decision body that would be accepted with the right token: an attested approval of the current version."""
    fingerprint = client.get(f"/api/review/evidence/result/{samples['a']}").json()["fingerprint"]
    return {"decision": "APPROVE", "reviewer": "Test Reviewer", "rationale": "Checked the value, interval and arm against the cited publication table.",
            "attest_human_review": True, "expected_fingerprint": fingerprint}


@pytest.mark.parametrize(
    "header",
    [b"caf\xe9", "café".encode(), b"\xff\xfe\xfd", b"test-reviewer-token\xe9", b"\x80" * 4000, b" ", b"Bearer test-reviewer-token", ("x" * 100_000).encode()],
    ids=["latin-1", "utf-8", "invalid-utf-8", "token-plus-byte", "long-high-bytes", "blank", "bearer-prefix", "100kB"],
)
def test_malformed_reviewer_token_is_401_never_500(client, samples, header):
    before = _row_counts()
    decision = {"decision": "NEEDS_INFO", "reviewer": "Test Reviewer", "rationale": "Header robustness check."}
    responses = [
        client.patch(f"/api/gaps/{samples['gap']}", json={"review_status": "CONFIRMED", "reviewer": "Test Reviewer"}, headers={"X-Reviewer-Token": header}),
        client.post(f"/api/review-queue/{samples['item']}/decision", json=decision, headers={"X-Reviewer-Token": header}),
        # The evidence review router shares the one reviewer check (api/auth.py), so the same holds for an evidence decision.
        client.post(f"/api/review/evidence/result/{samples['a']}/decisions", json=_evidence_decision(client, samples), headers={"X-Reviewer-Token": header}),
    ]
    assert [r.status_code for r in responses] == [401, 401, 401], [r.text[:120] for r in responses]
    assert _row_counts() == before


def test_reviewer_token_comparison_handles_any_text():
    settings = Settings(_env_file=None, reviewer_token="clé-secrète-ß")
    assert settings.reviewer_token_matches("clé-secrète-ß")  # as text
    assert settings.reviewer_token_matches("clé-secrète-ß".encode().decode("latin-1"))  # as an ASGI server hands over UTF-8 header bytes
    for wrong in (None, "", "cle-secrete-ss", "clé-secrète-ß ", "\udcff", "\x00", "clé"):
        assert not settings.reviewer_token_matches(wrong)
    assert not Settings(_env_file=None, reviewer_token=None).reviewer_token_matches("anything")
    assert not Settings(_env_file=None, reviewer_token="").reviewer_token_matches("")


def test_no_token_configured_is_403_whatever_the_header(client, samples, monkeypatch):
    monkeypatch.setattr(get_settings(), "reviewer_token", None)
    body = _evidence_decision(client, samples)
    before = _row_counts()
    for header in (None, TOKEN, {"X-Reviewer-Token": b"caf\xe9"}):
        response = client.patch(f"/api/gaps/{samples['gap']}", json={"review_status": "CONFIRMED", "reviewer": "Test Reviewer"}, headers=header)
        assert response.status_code == 403 and "read-only" in response.json()["detail"]
        decision = client.post(f"/api/review/evidence/result/{samples['a']}/decisions", json=body, headers=header)
        assert decision.status_code == 403 and "read-only" in decision.json()["detail"]
    assert _row_counts() == before


# --------------------------------------------------------------------------- F08: list endpoints

# Every endpoint that returns a page of stored rows, with query parameters that select more than one page.
LIST_ENDPOINTS: dict[str, dict[str, object]] = {
    "/api/drugs": {},
    "/api/studies": {},
    "/api/discovered": {},
    "/api/findings": {},
    "/api/analyses": {},
    "/api/gaps": {},
    "/api/sources": {},
    "/api/changes": {},
    "/api/audit": {},
    "/api/review-queue": {"status": ""},
    "/api/safety/trial-ae": {"nct": "NCT04184622"},
    "/api/safety/terms": {"nct": "NCT04184622"},
    "/api/conflicts": {},
}
# The only responses that are a bare JSON array: fixed lists written in the code, not rows from the database.
FIXED_LISTS = {"/api/compare/presets", "/api/assistant/examples"}


def _row_key(row: dict) -> str:
    return json.dumps(row, sort_keys=True, default=str)


@pytest.mark.parametrize("path", sorted(LIST_ENDPOINTS))
def test_list_endpoint_reports_the_true_total_and_pages_through_everything(client, path):
    params = LIST_ENDPOINTS[path]
    first = client.get(path, params={**params, "limit": 3}).json()
    assert {"total", "limit", "offset", "rows"} <= set(first), sorted(first)
    total = first["total"]
    assert first["limit"] == 3 and first["offset"] == 0 and len(first["rows"]) == min(3, total)
    assert total >= 1, f"{path} has no rows in the seeded database, so the test proves nothing"

    # Walk the whole list in pages of 3 rows (capped, so the largest tables are sampled from both ends instead).
    step = 3
    offsets = list(range(0, total, step)) if total <= 150 else [*range(0, 30, step), *range(total - 31, total, step)]
    seen: list[str] = []
    for offset in offsets:
        page = client.get(path, params={**params, "limit": step, "offset": offset}).json()
        assert page["total"] == total and page["offset"] == offset
        assert len(page["rows"]) == min(step, total - offset), (path, offset)
        seen += [_row_key(r) for r in page["rows"]]
    assert len(seen) == len(set(seen)), f"{path}: the same row came back on two pages, so the order is not stable"
    if total <= 150:
        assert len(seen) == total
        everything = client.get(path, params={**params, "limit": 500}).json()
        assert everything["total"] == total and [_row_key(r) for r in everything["rows"]] == seen, f"{path}: pages do not concatenate to the full list"

    past_the_end = client.get(path, params={**params, "limit": 3, "offset": total}).json()
    assert past_the_end["rows"] == [] and past_the_end["total"] == total


@pytest.mark.parametrize("path", sorted(LIST_ENDPOINTS))
def test_list_endpoint_bounds_limit_and_offset(client, path):
    params = LIST_ENDPOINTS[path]
    for bad in ({"limit": 0}, {"limit": -1}, {"limit": 10**9}, {"offset": -1}, {"offset": MAX_OFFSET + 1}, {"offset": HUGE}, {"limit": "many"}):
        assert client.get(path, params={**params, **bad}).status_code == 422, (path, bad)
    assert client.get(path, params={**params, "offset": MAX_OFFSET}).json()["rows"] == []


def test_total_is_the_number_of_matches_not_the_page_size(client, db):
    """The original defect: /api/findings said "total": 400 while 1,853 results matched."""
    body = client.get("/api/findings").json()
    arm_level = [r for r in db.scalars(select(OutcomeResult)) if r.scope == "ARM" and " minus " not in r.arm_label and not (r.arm and r.arm.is_pooled)]
    assert body["total"] == len(arm_level) > len(body["rows"]) == body["limit"] == 400
    assert client.get("/api/findings", params={"arm_level_only": "false", "limit": 1}).json()["total"] == db.scalar(select(func.count()).select_from(OutcomeResult))
    assert client.get("/api/analyses", params={"limit": 1}).json()["total"] == db.scalar(select(func.count()).select_from(StatisticalAnalysis))
    assert client.get("/api/sources", params={"limit": 1}).json()["total"] == db.scalar(select(func.count()).select_from(EvidenceSource))
    assert client.get("/api/audit", params={"limit": 1}).json()["total"] == db.scalar(select(func.count()).select_from(AuditEvent))
    assert client.get("/api/safety/trial-ae", params={"limit": 1}).json()["total"] == db.scalar(select(func.count()).select_from(AdverseEvent))
    queue = client.get("/api/review-queue", params={"limit": 1}).json()
    assert queue["total"] == db.scalar(select(func.count()).select_from(ReviewQueueItem).where(ReviewQueueItem.status == "OPEN")) and len(queue["rows"]) == 1
    # The term list used to stop at 300 without saying so; one seeded study has more than twice that.
    # One entry per term and measure kind (a term stored under two categories, or in both registry event tables, used
    # to be listed twice); its categories are listed on the entry.
    distinct_terms: dict[str, dict[tuple[str, str], set[str]]] = {}
    for nct, term, kind, category in db.execute(select(Study.nct_id, AdverseEvent.term, AdverseEvent.measure_kind, AdverseEvent.category).join(Study, AdverseEvent.study_id == Study.id)):
        distinct_terms.setdefault(nct, {}).setdefault((term, kind), set()).add(category)
    nct, expected = max(distinct_terms.items(), key=lambda item: len(item[1]))
    assert len(expected) > 300
    default_page = client.get("/api/safety/terms", params={"nct": nct}).json()
    assert default_page["total"] == default_page["total_terms"] == len(expected) and default_page["truncated"] is True
    assert len(default_page["rows"]) == default_page["limit"] == 300 and default_page["terms"] == default_page["rows"]
    all_terms = client.get("/api/safety/terms", params={"nct": nct, "limit": 2000}).json()
    assert len(all_terms["rows"]) == len(expected) and all_terms["truncated"] is False
    assert {(t["term"], t["measure_kind"]): set(t["categories"]) for t in all_terms["rows"]} == expected


def test_filters_narrow_the_total_and_existing_fields_are_kept(client):
    everything = client.get("/api/findings", params={"limit": 2000}).json()
    for params in ({"drug": "tirzepatide"}, {"estimand": "NOT_STATED"}, {"estimand": "EFFICACY", "tier": "PEER_REVIEWED"}, {"study": "SURMOUNT-1", "category": "WEIGHT_PCT_CHANGE"}):
        filtered = client.get("/api/findings", params={**params, "limit": 2000}).json()
        assert 0 < filtered["total"] == len(filtered["rows"]) < everything["total"], params
        page = client.get("/api/findings", params={**params, "limit": 2, "offset": 1}).json()
        assert page["total"] == filtered["total"] and page["rows"] == filtered["rows"][1:3], params
    # Fields that existed before the envelope are still there.
    assert "category_labels" in everything
    assert {"note"} <= set(client.get("/api/discovered").json())
    assert {"by_type"} <= set(client.get("/api/sources").json())
    assert {"counts", "review_enabled"} <= set(client.get("/api/review-queue").json())
    assert {"weights", "default_weights", "nature_labels", "categories", "note"} <= set(client.get("/api/gaps").json())
    assert {"study", "terms"} <= set(client.get("/api/safety/terms", params={"nct": "NCT04184622"}).json())
    assert {"measure_kinds_present", "measure_kind_labels", "single_kind", "note"} <= set(client.get("/api/safety/trial-ae", params={"nct": "NCT04184622"}).json())
    source = client.get("/api/sources", params={"q": "NCT04184622"}).json()["rows"][0]
    detail = client.get(f"/api/sources/{source['id']}").json()
    assert detail["dependent_findings_total"] >= len(detail["dependent_findings"]) == min(60, detail["dependent_findings_total"])
    assert all(isinstance(s["content_stored"], bool) for s in detail["snapshots"]) and source["n_snapshots"] == len(detail["snapshots"])


def test_a_misspelt_filter_is_an_error_not_an_empty_list(client):
    for path, params in (("/api/findings", {"tier": "PEER_REVIWED"}), ("/api/findings", {"estimand": "EFFICACY "}), ("/api/findings", {"category": "WEIGHT"}), ("/api/analyses", {"category": "bogus"}),
                         ("/api/gaps", {"nature": "BOGUS"}), ("/api/sources", {"tier": "BOGUS"}), ("/api/safety/trial-ae", {"measure_kind": "BOGUS"})):
        response = client.get(path, params=params)
        assert response.status_code == 422 and "nknown" in response.text, (path, params, response.text[:120])
    for path, name in (("/api/findings", "drug"), ("/api/findings", "study"), ("/api/studies", "program"), ("/api/audit", "action"), ("/api/review-queue", "item_type"), ("/api/sources", "source_type")):
        assert client.get(path, params={name: "x" * 10_000}).status_code == 422, (path, name)


def test_every_open_get_returns_an_object_unless_it_is_a_fixed_list(client, samples):
    """A bare JSON array cannot say how many rows there are. New list endpoints must use the envelope."""
    calls_made = 0
    for method, route in _api_routes():
        if method != "GET" or _requires_reviewer(route) or "{" in route.path:
            continue
        response = client.get(route.path, params={**LIST_ENDPOINTS.get(route.path, {}), **_query_samples(samples).get(route.path, {})})
        if response.status_code != 200 or not response.headers["content-type"].startswith("application/json"):
            continue
        calls_made += 1
        body = response.json()
        if isinstance(body, list):
            assert route.path in FIXED_LISTS, f"{route.path} returns a bare array: return {{total, limit, offset, rows}} (see _page in api/main.py)"
        elif route.path in LIST_ENDPOINTS:
            assert {"total", "limit", "offset", "rows"} <= set(body)
    assert calls_made >= 20


def test_discovered_trials_without_a_completion_date_are_listed_last(client):
    rows = client.get("/api/discovered", params={"limit": 1000}).json()["rows"]
    dates = [r["primary_completion_date"] for r in rows]
    assert None in dates, "no trial without a completion date in the seed: the ordering rule is untested"
    known = [d for d in dates if d is not None]
    assert dates == known + [None] * (len(dates) - len(known)) and known == sorted(known, reverse=True)


def test_analyses_have_a_defined_order(client):
    rows = client.get("/api/analyses", params={"limit": 1500}).json()["rows"]
    keys = [(r["study_name"], r["endpoint_id"], r["id"]) for r in rows]
    by_study: dict[str, list] = {}
    for study, endpoint, analysis in keys:
        by_study.setdefault(study, []).append((endpoint, analysis))
    assert all(v == sorted(v) for v in by_study.values()), "within a study, analyses are ordered by endpoint and id"
    assert len(by_study) == len({k[0] for i, k in enumerate(keys) if i == 0 or keys[i - 1][0] != k[0]}), "each study's analyses are contiguous"


# --------------------------------------------------------------------------- F09: oversized identifiers


def test_oversized_identifiers_are_rejected_before_the_database(client, samples):
    gets = [f"/api/findings/{HUGE}", f"/api/analyses/{HUGE}", f"/api/sources/{HUGE}", f"/api/reports/comparison?a={HUGE}&b=1", f"/api/reports/comparison?a=1&b={HUGE}",
            f"/api/findings/{MAX_DB_ID + 1}", f"/api/sources/{2**63}", f"/api/analyses/{2**64}"]
    for path in gets:
        assert client.get(path).status_code == 422, path
    assert client.get(f"/api/findings/{MAX_DB_ID}").status_code == 404  # the largest id the database can hold is simply not on record
    assert client.get(f"/api/sources/{MAX_DB_ID}").status_code == 404
    for path in ("/api/findings/0", "/api/findings/-1", "/api/findings/abc", "/api/findings/1.5", "/api/findings/1e3"):
        assert client.get(path).status_code == 422, path
    posts = [("/api/compare", {"a": HUGE, "b": samples["b"]}), ("/api/compare", {"a": samples["a"], "b": 2**63}), ("/api/stats/risk-contrast", {"ae_a": HUGE, "ae_b": samples["ae_b"]}),
             ("/api/stats/risk-contrast", {"ae_a": samples["ae_a"], "ae_b": MAX_DB_ID + 1})]
    for path, body in posts:
        assert client.post(path, json=body).status_code == 422, (path, body)
    assert client.post("/api/compare", json={"a": MAX_DB_ID, "b": samples["b"]}).status_code == 404
    # Reviewer routes: reached only with the token, and then the identifier is checked before anything is written.
    before = _row_counts()
    assert client.patch(f"/api/gaps/{HUGE}", json={"review_status": "CONFIRMED", "reviewer": "Test Reviewer"}, headers=TOKEN).status_code == 422
    assert client.patch(f"/api/gaps/{MAX_DB_ID}", json={"review_status": "CONFIRMED", "reviewer": "Test Reviewer"}, headers=TOKEN).status_code == 404
    decision = {"decision": "NEEDS_INFO", "reviewer": "Test Reviewer", "rationale": "Identifier bounds check."}
    assert client.post(f"/api/review-queue/{HUGE}/decision", json=decision, headers=TOKEN).status_code == 422
    assert client.post(f"/api/review-queue/{MAX_DB_ID}/decision", json=decision, headers=TOKEN).status_code == 404
    assert _row_counts() == before


def test_every_integer_parameter_of_every_route_is_bounded():
    """An unbounded integer reaches the driver and overflows (500). Applies to routes added later as well."""
    unbounded = []
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                schema = parameter.get("schema", {})
                variants = schema.get("anyOf", [schema])
                for variant in variants:
                    if variant.get("type") == "integer" and "maximum" not in variant and "exclusiveMaximum" not in variant:
                        unbounded.append(f"{method.upper()} {path}: {parameter['in']} parameter '{parameter['name']}' has no upper bound (use PathId / QueryId, or Query(le=...))")
    assert not unbounded, "\n".join(unbounded)


# --------------------------------------------------------------------------- F11: Markdown export

HOSTILE = {
    "html": '<img src=x onerror=alert(1)><script>alert(2)</script></td><iframe src="//evil.example">',
    "heading": "Tirzepatide 15 mg\n\n# Injected heading\n## And another",
    "link": "[click here](javascript:alert(1)) ![pixel](https://evil.example/p.png) <https://evil.example/auto> [ref][1]",
    "table": "A | B | C |\n|---|---|\n| x | y",
    "code": "`rm -rf` ``` ~~~ ```python",
    "emphasis": "*bold* _under_ __strong__ ~~gone~~ ~sub~",
    "quote": "> quoted\n> more",
    "list": "- item\n+ item\n1. item",
    "definition": "[1]: https://evil.example/definition",
    "entity": "&lt;b&gt; &#60;i&#62; &amp;",
    "bidi": "22.5‮5.22‬",
    "url": "see https://evil.example/path and www.evil.example for more",
}


def _hostile_documents(db) -> tuple[str, str, dict[str, str]]:
    """Both report kinds, built from evidence whose every text field has been replaced by hostile input (rolled back afterwards)."""
    a = db.get(OutcomeResult, result_id(db, "NCT05929066", "12 mg", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=80))
    b = db.get(OutcomeResult, result_id(db, "NCT04184622", "15 mg", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=72))
    planted = {
        "study.display_name": HOSTILE["heading"], "study.brief_title": HOSTILE["html"], "study.phase": HOSTILE["emphasis"], "study.overall_status": HOSTILE["table"],
        "result.arm_label": HOSTILE["link"], "result.unit": HOSTILE["code"], "result.baseline_definition": HOSTILE["quote"], "result.locator_quote": HOSTILE["html"] + HOSTILE["heading"],
        "result.source_location": HOSTILE["code"] + "\n# heading in a location", "endpoint.title": HOSTILE["table"] + HOSTILE["definition"], "source.title": HOSTILE["link"] + "\n" + HOSTILE["heading"],
        "source.publisher": HOSTILE["list"], "source.url": "javascript:alert(document.domain)//\n# heading", "source.version_label": HOSTILE["entity"], "source.published_date": HOSTILE["bidi"],
        "population.diabetes_basis": HOSTILE["definition"],
    }
    for result in (a, b):
        result.arm_label, result.unit, result.baseline_definition = planted["result.arm_label"], planted["result.unit"], planted["result.baseline_definition"]
        result.locator_quote, result.source_location = planted["result.locator_quote"], planted["result.source_location"]
        result.endpoint.title = planted["endpoint.title"]
        result.source.title, result.source.publisher, result.source.url = planted["source.title"], planted["source.publisher"], planted["source.url"]
        result.source.version_label, result.source.published_date = planted["source.version_label"], planted["source.published_date"]
        study = result.study
        study.display_name, study.brief_title, study.phase, study.overall_status = planted["study.display_name"], planted["study.brief_title"], planted["study.phase"], planted["study.overall_status"]
        study.population.diabetes_basis = planted["population.diabetes_basis"]
        for link in study.publication_links:
            link.publication.title, link.publication.journal, link.publication.doi = HOSTILE["link"], HOSTILE["emphasis"], HOSTILE["html"]
        for analysis in study.analyses:
            analysis.treatment_label, analysis.comparator_label, analysis.p_value_text = HOSTILE["html"], HOSTILE["link"], HOSTILE["emphasis"]
    db.flush()
    return comparison_report_markdown(db, a.id, b.id), study_report_markdown(db, "NCT04184622"), planted


def test_reports_neutralise_hostile_source_text(db):
    comparison, study_report, _ = _hostile_documents(db)
    for name, document in (("comparison", comparison), ("study", study_report)):
        lines = document.split("\n")
        # No injected block: every heading is one the template writes.
        headings = [line for line in lines if re.match(r" {0,3}#{1,6}(\s|$)", line)]
        assert all(re.match(r"(# (Comparison|Evidence) report: |## |### (Why|Finding [AB] — ))", h) for h in headings), (name, headings)
        assert sum(h.startswith("# ") for h in headings) == 1, (name, [h for h in headings if h.startswith("# ")])
        assert not any("Injected heading" in h and h.lstrip().startswith("#") and not h.startswith(("# Comparison report", "# Evidence report", "### Finding")) for h in headings)
        quotes = [line for line in lines if re.match(r" {0,3}>", line)]
        assert quotes == [line for line in quotes if line.startswith("> **Scope.**")], (name, quotes)
        # No live HTML, link, image or autolink: every "<" that could open a tag was written as an entity.
        assert not re.search(r"<(?=[A-Za-z/!?])(?!https?://[^\s<>]+>)", document), (name, re.findall(r"<[A-Za-z/!?][^\n]{0,40}", document)[:5])
        assert "](" not in re.sub(r"\\\]", "", document) and "][" not in re.sub(r"\\\]", "", document), name
        assert "javascript:" not in re.sub(r"\\\]\(javascript:|^\s+javascript:alert.*$", "", document, flags=re.M) or "<javascript:" not in document
        assert "<javascript:" not in document
        # Table rows keep their column count: a pipe in source text never opens a new cell.
        for line in lines:
            if line.startswith("| "):
                cells = re.split(r"(?<!\\)\|", line.strip())[1:-1]
                assert len(cells) in (2, 5), (name, line[:160])
        assert "‮" not in document and "‬" not in document, name


def test_reports_render_without_injected_markup(db):
    """The same documents through a real CommonMark parser with tables, strikethrough and raw HTML enabled."""
    markdown_it = pytest.importorskip("markdown_it")
    comparison, study_report, _ = _hostile_documents(db)
    parser = markdown_it.MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])
    for name, document in (("comparison", comparison), ("study", study_report)):
        tokens = parser.parse(document)
        inline = [child for token in tokens if token.type == "inline" for child in (token.children or [])]
        kinds = {token.type for token in tokens} | {child.type for child in inline}
        assert not kinds & {"html_block", "html_inline", "image", "s_open", "fence", "code_block", "ordered_list_open"}, (name, sorted(kinds))
        links = [child.attrGet("href") for child in inline if child.type == "link_open"]
        assert all(re.fullmatch(r"https://(clinicaltrials\.gov/study/NCT\d{8}|pubmed\.ncbi\.nlm\.nih\.gov/\d+/?)", href or "") for href in links), (name, links)
        headings = [tokens[i + 1].content for i, token in enumerate(tokens) if token.type == "heading_open"]
        assert all(h.startswith(("Comparison report: ", "Evidence report: ", "Finding A — ", "Finding B — ", "Why")) or not h.startswith(("Injected", "And another", "heading")) for h in headings), (name, headings)
        assert len([t for t in tokens if t.type == "heading_open" and t.tag == "h1"]) == 1, name
        assert len([t for t in tokens if t.type == "blockquote_open"]) == 1, name  # the scope notice
        emphasised = [inline[i + 1].content for i, child in enumerate(inline) if child.type in ("em_open", "strong_open") and i + 1 < len(inline)]
        assert not [text for text in emphasised if text in ("bold", "under", "strong", "gone", "sub")], (name, emphasised)
        code = [child.content for child in inline if child.type == "code_inline"]
        assert all("\n" not in c for c in code), name
        # Hostile text is still there to be read, as text.
        text = " ".join(child.content for child in inline if child.type in ("text", "code_inline"))
        assert "<img src=x onerror=alert(1)>" in text and "Injected heading" in text and "[click here](javascript:alert(1))" in text, name


def test_escaping_changes_no_ordinary_scientific_text():
    """Numbers, p-values, intervals and vocabulary terms must read the same in the raw file as in the source."""
    for text in ("-20.9 % (95% CI -21.9 to -19.9)", "p <0.001", "P < .001", "difference, -19.4% [95% CI, -21.2% to -17.7%]; P < .001", "TREATMENT_REGIMEN", "ACTIVE_NOT_RECRUITING",
                 "HbA1c ≥7.0% and ≤10.5%", "BMI >=30 kg/m2", "mean (SD) 104.8 (22.1) kg", "Johnson & Johnson", "n = 2,539", "5 mg, 10 mg or 15 mg once weekly", "protocolSection.outcomesModule"):
        assert _md(text) == text and _cell(text) == text, text
    assert _item("In SURMOUNT-1 (NCT04184622; Phase 3), the 15 mg group had ...") == "In SURMOUNT-1 (NCT04184622; Phase 3), the 15 mg group had ..."
    assert _cell(None) == "—" and _cell("") == "—" and _cell("a|b") == "a\\|b" and _cell("line one\nline two") == "line one line two"
    assert _md("47.7* | 54.6*") == "47.7\\* | 54.6\\*"  # two asterisks in one quote used to italicise what lay between them
    assert _item("# heading") == "\\# heading" and _item("> quote") == "\\> quote" and _item("1. first") == "1\\. first" and _item("[x]: https://e.example") .startswith("\\[x")
    assert _code("a`b``c") == "```a`b``c```" and _code("`x`") == "`` `x` ``" and _code("") == "—" and _code("one\ntwo") == "`one two`"
    assert _link("https://doi.org/10.1016/S0140-6736(21)01324-6") == "<https://doi.org/10.1016/S0140-6736(21)01324-6>"
    for unsafe in ("javascript:alert(1)", "https://x.example/ <b>", "http://a b", "data:text/html,<script>", "ftp://x.example/f"):
        assert not _link(unsafe).startswith("<"), unsafe


def test_real_reports_still_cite_their_sources(client, samples):
    report = client.get("/api/reports/comparison", params={"a": samples["a"], "b": samples["b"]}).text
    assert "<https://pubmed.ncbi.nlm.nih.gov/42814954/>" in report and "<https://pubmed.ncbi.nlm.nih.gov/35658024/>" in report
    assert "TREATMENT_REGIMEN" in report and "\\_" not in report and "&lt;" not in report and "&amp;" not in report


# --------------------------------------------------------------------------- ING-09: ingestion failure isolation

GOOD, BROKEN, ALSO_GOOD = "NCT04184622", "NCT05822830", "NCT05869903"


@pytest.fixture()
def scratch(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'scratch.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


class _UnexpectedShapeTransport(FixtureTransport):
    """Serves the fixtures, except that one study comes back with a module set to null (an AttributeError in the parser)."""

    def get(self, service, path, params=None, *, fixture_key=None):
        content = super().get(service, path, params, fixture_key=fixture_key)
        if fixture_key and fixture_key.endswith(f"{BROKEN}.json"):
            raw = json.loads(content)
            raw["protocolSection"]["conditionsModule"] = None
            return json.dumps(raw).encode()
        return content


def _assert_broken_study_left_nothing(session, report) -> None:
    assert report.studies == [GOOD, ALSO_GOOD], "the report lists exactly the studies that are in the database"
    assert [s.nct_id for s in session.scalars(select(Study).order_by(Study.nct_id))] == sorted([GOOD, ALSO_GOOD])
    assert session.scalar(select(EvidenceSource).where(EvidenceSource.key == f"ctgov:{BROKEN}")) is None
    assert session.scalar(select(func.count()).select_from(SourceSnapshot).join(EvidenceSource).where(EvidenceSource.key == f"ctgov:{BROKEN}")) == 0
    assert all(session.scalar(select(func.count()).select_from(OutcomeResult).join(Study).where(Study.nct_id == nct)) > 0 for nct in (GOOD, ALSO_GOOD))
    failures = [f for f in report.failures if f["target"] == f"ctgov:{BROKEN}"]
    assert len(failures) == 1 and not [f for f in report.failures if f["target"] == "ctgov"], report.failures
    run = session.scalar(select(IngestionRun).where(IngestionRun.adapter == "ctgov"))
    # "rejections_held" is the review lifecycle's protective pass after the registry step of a full run (nothing was rejected here).
    assert run.status == "SUCCEEDED" and run.stats == {"studies": 2, "failed": 1, "changes": 0, "rejections_held": 0}, (run.status, run.stats, run.error)
    queued = session.scalars(select(ReviewQueueItem).where(ReviewQueueItem.item_type == "INGEST_FAILURE", ReviewQueueItem.title.like("Ingestion failed: ctgov%"))).all()
    assert [q.title for q in queued] == [f"Ingestion failed: ctgov:{BROKEN}"]


def test_unexpected_payload_fails_one_study_and_no_other(scratch):
    """Not a transport error, not a ValueError, not a KeyError: the kind of failure that used to roll back every study."""
    with pytest.raises(AttributeError):
        from incretinos.ingestion.ctgov import parse_study

        parse_study(json.loads(_UnexpectedShapeTransport(FIXTURES).get("ctgov", "/x", fixture_key=f"ctgov/studies/{BROKEN}.json")))
    pipeline = Pipeline(scratch, get_settings(), transport=_UnexpectedShapeTransport(FIXTURES))
    report = pipeline.run(only=[GOOD, BROKEN, ALSO_GOOD], skip={"pubmed", "labels", "faers", "discover"})
    _assert_broken_study_left_nothing(scratch, report)
    assert "AttributeError" in next(f["error"] for f in report.failures if f["target"] == f"ctgov:{BROKEN}")


def test_a_study_that_fails_after_its_rows_were_written_leaves_none(scratch, monkeypatch):
    """The failure happens after the registry record has been stored and flushed; the savepoint must take it all back."""
    from incretinos.curation import loader

    real = loader.apply_watch_study

    def fail_for_one(session, study, watch):
        if watch.nct == BROKEN:
            assert session.scalar(select(func.count()).select_from(OutcomeResult).where(OutcomeResult.study_id == study.id)) > 0  # rows exist at this point
            raise RuntimeError("database refused a value while the study was being written")
        return real(session, study, watch)

    monkeypatch.setattr(loader, "apply_watch_study", fail_for_one)
    pipeline = Pipeline(scratch, get_settings(), transport=FixtureTransport(FIXTURES))
    report = pipeline.run(only=[GOOD, BROKEN, ALSO_GOOD], skip={"pubmed", "labels", "faers", "discover"})
    _assert_broken_study_left_nothing(scratch, report)
    # Ingesting again without the fault brings the study in: nothing half-written blocks it.
    monkeypatch.setattr(loader, "apply_watch_study", real)
    again = Pipeline(scratch, get_settings(), transport=FixtureTransport(FIXTURES))
    again.ingest_studies(demo_change=False, only=[BROKEN])
    scratch.commit()
    assert again.report.studies == [BROKEN] and scratch.scalar(select(Study).where(Study.nct_id == BROKEN)) is not None


def test_a_step_that_is_rolled_back_is_not_listed_in_the_report(scratch, monkeypatch):
    """If a whole step fails, the report goes back to what it said before the step; item failures are kept and re-recorded."""
    pipeline = Pipeline(scratch, get_settings(), transport=_UnexpectedShapeTransport(FIXTURES))
    real = pipeline.ingest_studies

    def ingest_then_crash(**kwargs):
        real(**kwargs)
        assert pipeline.report.studies == [GOOD, ALSO_GOOD]
        raise MemoryError("the step dies after ingesting two studies and failing one")

    monkeypatch.setattr(pipeline, "ingest_studies", ingest_then_crash)
    report = pipeline.run(only=[GOOD, BROKEN, ALSO_GOOD], skip={"pubmed", "labels", "faers", "discover"})
    assert report.studies == [] and report.changes == []
    assert scratch.scalar(select(func.count()).select_from(Study)) == 0
    assert {f["target"] for f in report.failures} >= {f"ctgov:{BROKEN}", "ctgov"}
    run = scratch.scalars(select(IngestionRun).where(IngestionRun.adapter == "ctgov")).all()
    assert [r.status for r in run] == ["FAILED"] and "MemoryError" in run[0].error
    titles = {q.title for q in scratch.scalars(select(ReviewQueueItem).where(ReviewQueueItem.item_type == "INGEST_FAILURE"))}
    assert titles >= {f"Ingestion failed: ctgov:{BROKEN}", "Ingestion failed: ctgov"}


# --------------------------------------------------------------------------- ING-12: query counts

# Ceilings with room to spare. Before eager loading these endpoints issued 180-1,450 queries each.
QUERY_CEILINGS = {
    "/api/findings": 25,
    "/api/findings?limit=2000": 25,
    "/api/analyses?limit=1500": 20,
    "/api/studies": 15,
    "/api/drugs": 10,
    "/api/sources?limit=2000": 12,
    "/api/design/coverage": 20,
    "/api/compare/presets": 90,
    "/api/overview": 130,
    "/api/validation": 220,
    "/api/review-queue": 8,
    "/api/gaps": 8,
}


@pytest.mark.parametrize("path", sorted(QUERY_CEILINGS))
def test_read_endpoints_do_not_issue_a_query_per_row(client, path):
    with _counted_queries() as counter:
        assert client.get(path).status_code == 200
    assert counter[0] <= QUERY_CEILINGS[path], f"{path} issued {counter[0]} queries (ceiling {QUERY_CEILINGS[path]}): a per-row lookup has crept in"


# --------------------------------------------------------------------------- F21 / ING-14: one schema path, indexes


def test_models_and_migrations_describe_the_same_schema(seeded):
    """The test database was built by the migrations (conftest). Any difference from the models is drift."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    with get_engine().connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert differences == [], f"models.py and the migrations disagree; add a migration: {differences[:5]}"


def test_database_is_at_the_single_migration_head(seeded):
    from alembic.script import ScriptDirectory

    from incretinos.db import alembic_config, schema_state

    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    assert len(heads) == 1, f"several migration heads {heads}: give each revision one parent so that `alembic upgrade head` works"
    with get_engine().connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == heads[0]
    assert schema_state() == "migrated"


def test_foreign_key_indexes_from_the_migration_exist(seeded):
    inspector = inspect(get_engine())
    expected = {
        "adverse_event": {"arm_id"}, "change_event": {"from_snapshot_id", "to_snapshot_id"}, "outcome_result": {"arm_id", "estimand_id"},
        "statistical_analysis": {"estimand_id", "treatment_arm_id", "comparator_arm_id"}, "review_decision": {"queue_item_id"}, "study_version": {"snapshot_id"},
        "endpoint": {"source_id"}, "evidence_claim": {"source_id"}, "study": {"source_id"}, "exposure_equivalence": {"dose_a_id", "dose_b_id", "source_id"},
    }
    for table, columns in expected.items():
        leading = {index["column_names"][0] for index in inspector.get_indexes(table)}
        assert columns <= leading, f"{table}: no index leads with {sorted(columns - leading)}"


def _cli(*args: str, database: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "INCRETINOS_DATABASE_URL": f"sqlite:///{database}"}
    return subprocess.run([sys.executable, "-m", "incretinos.cli", *args], cwd=BACKEND, env=env, capture_output=True, text=True)


def test_cli_builds_the_schema_with_migrations_and_refuses_an_unversioned_database(tmp_path):
    import sqlite3

    fresh = tmp_path / "new" / "fresh.db"
    run = _cli("init-db", database=fresh)
    assert run.returncode == 0, run.stderr[-600:]
    with sqlite3.connect(fresh) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(Base.metadata.tables) | {"alembic_version"} <= tables
        assert connection.execute("SELECT count(*) FROM alembic_version").fetchone() == (1,)
    assert "configuration: environment variables only" in run.stderr  # the command says where its configuration came from

    legacy = tmp_path / "legacy.db"
    engine = make_engine(f"sqlite:///{legacy}")
    Base.metadata.create_all(engine)  # what an older build did: tables, but no migration version
    engine.dispose()
    refused = _cli("init-db", database=legacy)
    assert refused.returncode == 1 and "no migration version" in refused.stderr and "seed --reset --wipe-review-history" in refused.stderr
    assert "alembic stamp 10d1699882ea" in refused.stderr  # the way that keeps the contents is named as well
    assert _cli("seed", "--only", "NCT00000000", database=legacy).returncode == 1
    # --reset keeps review decisions, the audit log and snapshots, so it cannot be the command that throws away a
    # database it is unable to migrate: it is refused too, and the database is left exactly as it was.
    kept = _cli("init-db", "--reset", database=legacy)
    assert kept.returncode == 1 and "no migration version" in kept.stderr
    with sqlite3.connect(legacy) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(Base.metadata.tables) <= tables and "alembic_version" not in tables
    # Destroying it takes the explicit flag.
    rebuilt = _cli("init-db", "--reset", "--wipe-review-history", database=legacy)
    assert rebuilt.returncode == 0, rebuilt.stderr[-600:]
    with sqlite3.connect(legacy) as connection:
        assert connection.execute("SELECT count(*) FROM alembic_version").fetchone() == (1,)


def test_database_label_hides_the_password(monkeypatch):
    from incretinos import cli

    monkeypatch.setattr(get_settings(), "database_url", "postgresql+psycopg://incretinos:s3cret-pw@db.internal:5432/incretinos")
    assert "s3cret-pw" not in cli._database_label() and "db.internal" in cli._database_label()


# --------------------------------------------------------------------------- F15 / F17: configuration and headers


def test_env_file_is_never_read_from_the_working_directory(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("INCRETINOS_NCBI_EMAIL=from-working-directory@example.org\nINCRETINOS_REVIEWER_TOKEN=from-working-directory\n")
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("INCRETINOS_ENV_FILE")
    monkeypatch.delenv("INCRETINOS_REVIEWER_TOKEN")
    monkeypatch.setattr(config_module, "DEFAULT_ENV_FILES", (tmp_path / "repo" / ".env", tmp_path / "repo" / "backend" / ".env"))
    load = get_settings.__wrapped__

    assert env_files() == () and load().ncbi_email is None and load().reviewer_token is None
    assert "working directory is never searched" in describe_settings_sources()
    # An empty variable (what Docker Compose passes for anything left unset) is "not set", not an empty value.
    for name in ("INCRETINOS_OFFLINE", "INCRETINOS_REFRESH_HOURS", "INCRETINOS_LLM_PROVIDER", "INCRETINOS_DOCS_ENABLED", "INCRETINOS_RATE_LIMIT_PROXY_HOPS", "INCRETINOS_REVIEWER_TOKEN", "INCRETINOS_ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "")
    empty = load()
    assert (empty.offline, empty.refresh_hours, empty.llm_provider, empty.docs_enabled, empty.rate_limit_proxy_hops, empty.reviewer_token, empty.anthropic_api_key) == (True, 0.0, "none", False, 0, None, None)
    for name in ("INCRETINOS_OFFLINE", "INCRETINOS_REFRESH_HOURS", "INCRETINOS_LLM_PROVIDER", "INCRETINOS_DOCS_ENABLED", "INCRETINOS_RATE_LIMIT_PROXY_HOPS", "INCRETINOS_REVIEWER_TOKEN", "INCRETINOS_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name)

    (tmp_path / "repo" / "backend").mkdir(parents=True)
    (tmp_path / "repo" / ".env").write_text("INCRETINOS_NCBI_EMAIL=root@example.org\nINCRETINOS_NCBI_TOOL=from-root\n")
    assert load().ncbi_email == "root@example.org" and str(tmp_path / "repo" / ".env") in describe_settings_sources()
    (tmp_path / "repo" / "backend" / ".env").write_text("INCRETINOS_NCBI_EMAIL=backend@example.org\n")
    assert (load().ncbi_email, load().ncbi_tool) == ("backend@example.org", "from-root")  # backend/.env overrides the repository file
    monkeypatch.setenv("INCRETINOS_NCBI_EMAIL", "environment@example.org")
    assert load().ncbi_email == "environment@example.org"  # and the environment overrides both

    monkeypatch.delenv("INCRETINOS_NCBI_EMAIL")
    monkeypatch.setenv("INCRETINOS_ENV_FILE", str(elsewhere / ".env"))
    assert load().ncbi_email == "from-working-directory@example.org"  # an explicit path is honoured, wherever it is
    monkeypatch.setenv("INCRETINOS_ENV_FILE", "none")
    assert env_files() == () and load().ncbi_email is None and "INCRETINOS_ENV_FILE=none" in describe_settings_sources()
    monkeypatch.setenv("INCRETINOS_ENV_FILE", str(tmp_path / "missing.env"))
    with pytest.raises(FileNotFoundError):
        load()


def test_safe_defaults():
    defaults = Settings(_env_file=None, **{})
    fields = Settings.model_fields
    assert fields["reviewer_token"].default is None and fields["offline"].default is True and fields["llm_provider"].default == "none"
    assert fields["docs_enabled"].default is False and fields["rate_limit_enabled"].default is True and fields["rate_limit_proxy_hops"].default == 0
    assert fields["rate_limit_trusted_proxies"].default == "" and fields["rate_limit_query_per_minute"].default > 0
    assert fields["max_request_body_bytes"].default <= 64 * 1024 and fields["max_review_body_bytes"].default <= 256 * 1024
    assert fields["refresh_hours"].default == 0.0 and "*" not in fields["cors_origins"].default
    assert defaults.model_config.get("env_file") is None


def test_api_documentation_is_served_only_when_enabled(client):
    assert get_settings().docs_enabled is False
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
    # getattr: an included router is one entry of app.routes and has no path of its own.
    probe = "from incretinos.api.main import app; print(sorted(p for p in (getattr(r, 'path', '') for r in app.routes) if p and not p.startswith('/api')))"
    env = {**os.environ, "INCRETINOS_DOCS_ENABLED": "true"}
    enabled = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND, env=env, capture_output=True, text=True)
    assert enabled.returncode == 0 and all(p in enabled.stdout for p in ("/docs", "/redoc", "/openapi.json")), enabled.stdout + enabled.stderr[-400:]


def test_api_responses_carry_security_headers(client):
    responses = [client.get("/api/health"), client.get("/api/studies/NCT00000000"), client.get("/api/findings/abc"), client.get("/no-such-path"),
                 client.options("/api/compare", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"}),
                 client.options("/api/compare", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})]
    assert [r.status_code for r in responses] == [200, 404, 422, 404, 200, 400]
    for response in responses:
        assert response.headers["x-content-type-options"] == "nosniff" and response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer" and "default-src 'none'" in response.headers["content-security-policy"]
    assert "access-control-allow-origin" not in responses[-1].headers
    download = client.get("/api/reports/study/NCT04184622")
    assert download.headers["x-content-type-options"] == "nosniff" and download.headers["content-disposition"].startswith("attachment")


# --------------------------------------------------------------------------- rate limits


def test_token_bucket_allows_a_burst_then_refills():
    now = [1000.0]
    bucket = limits.TokenBucketLimiter(clock=lambda: now[0])
    assert [bucket.check("s", "client", 3) for _ in range(3)] == [0.0, 0.0, 0.0]
    wait = bucket.check("s", "client", 3)
    assert wait == pytest.approx(20.0)  # 3 per minute: the next request is available 20 s later
    assert bucket.check("s", "other-client", 3) == 0.0 and bucket.check("other-scope", "client", 3) == 0.0
    now[0] += 19.0
    assert bucket.check("s", "client", 3) == pytest.approx(1.0)
    now[0] += 1.0
    assert bucket.check("s", "client", 3) == 0.0 and bucket.check("s", "client", 3) > 0
    now[0] += 3600.0
    assert [bucket.check("s", "client", 3) for _ in range(4)][:3] == [0.0, 0.0, 0.0]  # never more than one full burst is saved up


def test_token_bucket_memory_is_bounded():
    now = [0.0]
    bucket = limits.TokenBucketLimiter(clock=lambda: now[0], max_keys=100)
    for i in range(1000):
        bucket.check("s", f"client-{i}", 5)
    assert len(bucket) <= 101  # 100 clients and one shared bucket for everyone beyond the cap
    assert sum(bucket.check("s", f"late-{i}", 5) > 0 for i in range(20)) >= 10, "clients beyond the cap share one budget"
    now[0] += 61.0
    assert bucket.check("s", "after-a-quiet-minute", 5) == 0.0 and len(bucket) <= 2


@pytest.fixture()
def enforced_limits(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_simulation_per_minute", 2)
    monkeypatch.setattr(settings, "rate_limit_assistant_per_minute", 1)
    monkeypatch.setattr(settings, "rate_limit_query_per_minute", 3)
    monkeypatch.setattr(settings, "rate_limit_proxy_hops", 0)
    monkeypatch.setattr(settings, "rate_limit_trusted_proxies", "")
    limits.limiter.reset()
    yield settings
    limits.limiter.reset()


SIMULATION = {"n_per_arm": 20, "mean_treatment": -5, "mean_control": 0, "sd": 5, "n_sims": 100}


def test_expensive_endpoints_answer_429_with_retry_after(client, enforced_limits, monkeypatch):
    assert [client.post("/api/design/simulate", json=SIMULATION).status_code for _ in range(2)] == [200, 200]
    limited = client.post("/api/design/simulate", json=SIMULATION)
    assert limited.status_code == 429 and 1 <= int(limited.headers["retry-after"]) <= 30 and "limit 2 per minute" in limited.json()["detail"]
    assert limited.headers["x-content-type-options"] == "nosniff"
    # The budgets are separate per endpoint, and other endpoints have none.
    question = {"question": "Which outcomes in EloraTZP are exploratory?"}
    assert client.post("/api/assistant/ask", json=question).status_code == 200
    again = client.post("/api/assistant/ask", json=question)
    assert again.status_code == 429 and int(again.headers["retry-after"]) >= 1
    assert all(client.post("/api/design/power", json={"design": "two_means", "delta": 5, "sd": 10}).status_code == 200 for _ in range(5))
    # A caller cannot buy a fresh budget by claiming to be someone else ...
    assert client.post("/api/design/simulate", json=SIMULATION, headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 429
    # ... not even with a hop count, as long as no proxy is trusted (SEC-02: the header is the caller's to write) ...
    monkeypatch.setattr(enforced_limits, "rate_limit_proxy_hops", 1)
    assert client.post("/api/design/simulate", json=SIMULATION, headers={"X-Forwarded-For": "198.51.100.7, 203.0.113.10"}).status_code == 429
    # ... and a trusted proxy is believed only on its own connections.
    monkeypatch.setattr(enforced_limits, "rate_limit_trusted_proxies", "10.0.0.0/24")
    assert client.post("/api/design/simulate", json=SIMULATION, headers={"X-Forwarded-For": "198.51.100.7, 203.0.113.10"}).status_code == 429
    proxy = TestClient(client.app, client=("10.0.0.5", 41000))
    assert proxy.post("/api/design/simulate", json=SIMULATION, headers={"X-Forwarded-For": "198.51.100.7, 203.0.113.9"}).status_code == 200
    assert proxy.post("/api/design/simulate", json=SIMULATION, headers={"X-Forwarded-For": "spoofed-by-client, 203.0.113.9"}).status_code == 200
    assert proxy.post("/api/design/simulate", json=SIMULATION, headers={"X-Forwarded-For": "another-spoof, 203.0.113.9"}).status_code == 429
    # A rejected request is rejected before validation or any work: an invalid body still gets 429.
    assert proxy.post("/api/design/simulate", json={"n_per_arm": 0}, headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 429


def test_rate_limits_can_be_switched_off(client, enforced_limits, monkeypatch):
    monkeypatch.setattr(enforced_limits, "rate_limit_simulation_per_minute", 0)
    assert all(client.post("/api/design/simulate", json=SIMULATION).status_code == 200 for _ in range(4))
    monkeypatch.setattr(enforced_limits, "rate_limit_simulation_per_minute", 1)
    monkeypatch.setattr(enforced_limits, "rate_limit_enabled", False)
    assert all(client.post("/api/design/simulate", json=SIMULATION).status_code == 200 for _ in range(4))


def test_the_two_expensive_open_routes_are_rate_limited():
    limited = {}
    for method, route in _api_routes():
        for dependency in route.dependant.dependencies:
            if dependency.call in (limits.limit_simulation, limits.limit_assistant):
                limited[(method, route.path)] = dependency.call
    assert limited.get(("POST", "/api/design/simulate")) is limits.limit_simulation
    assert limited.get(("POST", "/api/assistant/ask")) is limits.limit_assistant


# --------------------------------------------------------------------------- SEC-01 / SEC-05: request bodies and 422 reports


def _route_url(path: str, samples: dict) -> str:
    values = _path_values(samples)
    return re.sub(r"{(\w+)(?::[^}]*)?}", lambda m: str(values.get(m.group(1), 1)), path)


def _body_routes() -> list[tuple[str, APIRoute]]:
    return [(method, route) for method, route in _api_routes() if method in ("POST", "PUT", "PATCH")]


def test_every_route_with_a_body_has_a_size_limit_suited_to_it():
    """An open route is sized by the table in api/limits.py; a route not listed there must need the reviewer token."""
    settings = get_settings()
    for method, route in _body_routes():
        path = _route_url(route.path, {"a": 1, "analysis": 1, "source": 1, "gap": 1, "item": 1, "a_key": "k"})
        sized = any(m == method and p.fullmatch(path) for m, p, _ in limits.ROUTE_BODY_LIMITS)
        reviewer = any(m == method and p.fullmatch(path) for m, p in limits.REVIEW_BODY_ROUTES)
        assert sized != reviewer, f"{method} {route.path}: list it in exactly one of ROUTE_BODY_LIMITS / REVIEW_BODY_ROUTES"
        assert reviewer == _requires_reviewer(route), f"{method} {route.path}: only reviewer routes may accept the larger reviewer body"
        open_limit = limits.body_limit(method, path, has_reviewer_token=False, cfg=settings)
        assert open_limit <= settings.max_request_body_bytes <= 64 * 1024, (method, route.path, open_limit)
    # Every table entry names a route that exists, so a renamed route cannot silently lose its limit.
    served = {(method, route.path) for method, route in _body_routes()}
    for method, pattern, _ in limits.ROUTE_BODY_LIMITS:
        assert any(m == method and pattern.fullmatch(_route_url(p, {"a": 1, "analysis": 1, "source": 1, "gap": 1, "item": 1, "a_key": "k"})) for m, p in served), pattern.pattern


def _largest_valid_bodies() -> dict[str, dict]:
    """The largest body each open calculation route can legitimately need (UTF-8 text, the most entries allowed)."""
    big = -123456.78901234567
    return {
        "/api/compare": {"a": 2**31 - 1, "b": 2**31 - 2},
        "/api/stats/risk-contrast": {"ae_a": 2**31 - 1, "ae_b": 2**31 - 2},
        "/api/safety/disproportionality": {k: 10**9 for k in ("drug_event", "drug_total", "event_total", "all_total")},
        "/api/design/power": {"design": "noninferiority_hr_events", "alpha": 0.025, "power": 0.9, "ratio": 1.5, "delta": big, "sd": 1e5, "p1": 0.12345678901234, "p2": 0.23456789012345,
                              "margin": 1.3, "true_difference": big, "hazard_ratio": 0.9, "true_hazard_ratio": 1.0, "attrition": 0.15},
        "/api/design/simulate": {"n_per_arm": 20000, "mean_treatment": big, "mean_control": big, "sd": 9.87654321, "dropout_treatment": 0.25, "dropout_control": 0.25,
                                 "missing_handling": "JUMP_TO_REFERENCE", "n_sims": 200, "alpha": 0.05, "seed": 2**31 - 1},
        "/api/design/sensitivity": {"delta": big, "sd": 1e5, "n_randomized_per_group": 100000, "attrition_rates": [0.123456789012345] * 20, "alpha": 0.05,
                                    "mean_treatment": big, "mean_control": big, "missing_treatment": 0.5, "missing_control": 0.5, "deltas": [big] * 20},
        "/api/assistant/ask": {"question": "Ω—é" * 333},
        "/api/benefit-risk/utility": {"nct": "NCT04184622", "estimand": "TREATMENT_REGIMEN", "weeks": 72,
                                      "criteria": {f"{i:02d}" + "é" * 58: {"weight": 99.5, "best": big, "worst": big} for i in range(20)}},
        "/api/benefit-risk/dose-scenario": {"nct": "NCT04184622", "estimand": "TREATMENT_REGIMEN", "weeks": 72, "metric": "weight_pct",
                                            "shares": {f"{i:02d}" + "x" * 398: 0.02 for i in range(50)}, "non_persistent_share": 0.0, "non_persistent_value": big},
    }


def test_largest_legitimate_bodies_fit_their_limits(client):
    settings = get_settings()
    for path, body in _largest_valid_bodies().items():
        size = len(json.dumps(body, indent=2, ensure_ascii=False).encode())  # pretty-printed, as some clients send it
        assert size <= limits.body_limit("POST", path, has_reviewer_token=False, cfg=settings), (path, size)
        response = client.post(path, content=json.dumps(body, indent=2, ensure_ascii=False).encode(), headers={"content-type": "application/json"})
        assert response.status_code != 413, (path, response.text[:200])


def test_oversized_bodies_are_refused_with_413_before_they_are_read(client, samples):
    settings = get_settings()
    for method, route in _body_routes():
        url = _route_url(route.path, samples)
        for token in ({}, TOKEN):
            limit = limits.body_limit(method, url, has_reviewer_token=bool(token), cfg=settings)
            padding = b" " * (limit + 1 - len(b'{"a": 1}'))
            # Declared size over the limit: refused from the header alone.
            declared = client.request(method, url, content=b'{"a": 1}' + padding, headers={"content-type": "application/json", **token})
            assert declared.status_code == 413, (method, route.path, declared.status_code)
            assert f"{limit:,} bytes" in declared.json()["detail"] and declared.headers["x-content-type-options"] == "nosniff"

            # Streamed without a length (chunked): refused as well (how much is read is checked below, at the ASGI level).
            stream = iter([b'{"a": "', *([b"x" * 1024] * (limit // 1024 + 2)), b'"}'])
            streamed = client.request(method, url, content=stream, headers={"content-type": "application/json", **token})
            assert streamed.status_code == 413, (method, route.path, streamed.status_code)
    # The larger reviewer allowance needs the reviewer header; without it the open ceiling applies.
    review = _route_url("/api/review/evidence/{evidence_type}/{row_id}/decisions", samples)
    assert limits.body_limit("POST", review, has_reviewer_token=True, cfg=settings) == settings.max_review_body_bytes > settings.max_request_body_bytes
    assert limits.body_limit("POST", review, has_reviewer_token=False, cfg=settings) == settings.max_request_body_bytes
    # Any other request, a GET included, is held to the open ceiling.
    assert client.request("GET", "/api/health", content=b"x" * (settings.max_request_body_bytes + 1)).status_code == 413
    assert client.get("/api/health").status_code == 200
    bad_length = client.post("/api/compare", content=b'{"a": 1, "b": 2}', headers={"content-type": "application/json", "content-length": "12abc"})
    assert bad_length.status_code in (400, 413)


def test_a_body_over_the_limit_is_never_read_past_it():
    import asyncio

    def run(headers: list[tuple[bytes, bytes]]) -> tuple[int, list[str], list[dict]]:
        received, reached, sent = [0], [], []

        async def endless_body() -> dict:
            received[0] += 1
            return {"type": "http.request", "body": b"x" * 1024, "more_body": True}

        async def application(scope, receive, send) -> None:
            reached.append(scope["path"])

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/api/compare", "headers": [(b"content-type", b"application/json"), *headers]}
        asyncio.run(limits.RequestBodyLimitMiddleware(application)(scope, endless_body, send))
        return received[0], reached, sent

    # Streamed: the 4 KiB route stops reading at the fifth kibibyte, and the application never sees the request.
    received, reached, sent = run([(b"transfer-encoding", b"chunked")])
    assert received == 5 and reached == [] and sent[0]["status"] == 413
    # Declared too large: nothing is read at all.
    received, reached, sent = run([(b"content-length", b"1000000000")])
    assert received == 0 and reached == [] and sent[0]["status"] == 413


def test_body_limits_follow_the_settings(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "max_request_body_bytes", 2048)
    assert limits.body_limit("POST", "/api/benefit-risk/dose-scenario", has_reviewer_token=False, cfg=settings) == 2048  # the setting is a ceiling for every open route
    assert limits.body_limit("POST", "/api/compare", has_reviewer_token=False, cfg=settings) == 2048  # a route's own limit only ever tightens it
    monkeypatch.setattr(settings, "max_request_body_bytes", 4096)
    assert limits.body_limit("POST", "/api/compare", has_reviewer_token=False, cfg=settings) == 4096
    assert limits.body_limit("POST", "/api/assistant/ask", has_reviewer_token=False, cfg=settings) == 4096
    escaped = json.dumps({"question": "é" * 1000}).encode()  # \u00e9 escapes: 6 bytes a character
    assert client.post("/api/assistant/ask", content=escaped, headers={"content-type": "application/json"}).status_code == 413
    with pytest.raises(ValueError):
        Settings(_env_file=None, max_request_body_bytes=10)


def test_validation_errors_report_where_and_why_but_never_echo_the_input(client):
    marker = "ECHO-MARKER-"
    body = {"shares": {f"{marker}{i:05d}" + "y" * 200: "not a number" for i in range(200)}, "nct": "NCT04184622", "x": [marker] * 1000}
    raw = json.dumps(body).encode()
    assert len(raw) < get_settings().max_request_body_bytes
    response = client.post("/api/benefit-risk/dose-scenario", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert len(detail) == 21 and detail[-1]["type"] == "too_many_errors" and "further errors" in detail[-1]["msg"]
    assert all(set(item) == {"type", "loc", "msg"} for item in detail)
    assert len(response.content) < 12_000, len(response.content)  # was 3-5 times the request size
    assert all(len(str(part)) <= 81 for item in detail for part in item["loc"])  # a key from the body is named, truncated, once
    assert '"input"' not in response.text and '"ctx"' not in response.text and "not a number" not in response.text
    # The usual shape still says where and why (the web client prints loc and msg).
    simple = client.post("/api/design/power", json={"design": "two_means", "alpha": 0.9}).json()["detail"]
    assert simple == [{"type": "less_than", "loc": ["body", "alpha"], "msg": "Input should be less than 0.5"}]
    assert client.post("/api/compare", content=b'{"a": 1,', headers={"content-type": "application/json"}).json()["detail"][0]["type"] == "json_invalid"


LONE_SURROGATE_BODIES = [
    '{"question": "lone \\ud800 surrogate"}',
    '{"question": "lone low \\udc00 surrogate"}',
    '{"question": "reversed \\udc00\\ud800 pair"}',
    '{"question": "high at the end \\ud83d"}',
    '{"question": "high then text \\ud83dx"}',
    '{"\\ud800": 1, "a": 1, "b": 2}',
]


def test_lone_surrogates_are_refused_with_422_never_500(client, samples):
    plain = TestClient(client.app, raise_server_exceptions=False)
    for method, route in _body_routes():
        url = _route_url(route.path, samples)
        for body in LONE_SURROGATE_BODIES:
            for headers in ({"content-type": "application/json"}, {"content-type": "application/json", **TOKEN}):
                response = plain.request(method, url, content=body.encode(), headers=headers)
                assert response.status_code == 422, (method, route.path, body, response.status_code)
                assert response.json()["detail"][0]["type"] == "string_unicode"
    # Surrogate bytes written raw (not escaped) are refused the same way.
    assert plain.post("/api/assistant/ask", content=b'{"question": "raw ' + b"\xed\xa0\x80" + b' bytes"}', headers={"content-type": "application/json"}).status_code in (400, 422)
    # Valid text is untouched: a properly paired escape, and an escaped backslash followed by "ud800".
    for question in ('"Which outcomes in EloraTZP are exploratory? \\ud83d\\ude00"', '"Which outcomes in EloraTZP are exploratory? \\\\ud800"'):
        ok = plain.post("/api/assistant/ask", content=f'{{"question": {question}}}'.encode(), headers={"content-type": "application/json"})
        assert ok.status_code == 200, (question, ok.text[:200])


def test_lone_surrogate_detection():
    lone = limits.has_lone_surrogate
    for body in LONE_SURROGATE_BODIES:
        assert lone(body.encode()), body
    for body in ('{"q": "\\ud83d\\ude00"}', '{"q": "\\\\ud800"}', '{"q": "\\\\\\\\ud800"}', '{"q": "😀 plain"}', '{"q": "\\u00e9\\n\\""}', "not json at all", ""):
        assert not lone(body.encode()), body
    assert lone(b'{"q": "\\\\\\ud800"}')  # escaped backslash, then a real lone escape
    assert lone('{"q": "x"}'.encode("utf-16")) is False and lone('{"q": "\\ud800"}'.encode("utf-16"))


def test_the_422_report_encodes_whatever_the_error_holds():
    from incretinos.api.main import validation_detail

    errors = [{"type": "extra_forbidden", "loc": ("body", "\ud800" + "k" * 500, 3), "msg": "bad \udfff " + "m" * 1000, "input": "x" * 10**6, "ctx": {"error": ValueError("v")}}]
    detail = validation_detail(errors)
    encoded = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")  # what JSONResponse does; raised before
    assert b"xxxx" not in encoded and len(encoded) < 600
    assert detail[0]["loc"][0] == "body" and detail[0]["loc"][2] == 3 and detail[0]["loc"][1].startswith("\\ud800")


# --------------------------------------------------------------------------- SEC-02: who the client is


def test_forwarded_header_is_read_only_from_a_trusted_proxy(monkeypatch):
    cfg = Settings(_env_file=None)
    assert cfg.rate_limit_trusted_proxies == ""
    # Default: the socket peer, whatever the header says, from loopback (the same-host web proxy) or anywhere else.
    for peer in ("127.0.0.1", "::1", "10.0.0.5", "testclient"):
        assert limits.client_address(peer, ["203.0.113.1"], cfg) == peer
    assert limits.client_address(None, ["203.0.113.1"], cfg) == "unknown"
    hops = Settings(_env_file=None, rate_limit_proxy_hops=1)
    assert limits.client_address("127.0.0.1", ["203.0.113.1"], hops) == "127.0.0.1"  # a hop count alone trusts nobody

    trusted = Settings(_env_file=None, rate_limit_trusted_proxies="10.0.0.0/24, 192.0.2.7, 2001:db8::/32")
    assert limits.client_address("10.0.0.99", ["198.51.100.1, 203.0.113.5"], trusted) == "203.0.113.5"
    assert limits.client_address("10.0.0.99", ["198.51.100.1", "203.0.113.5, 192.0.2.7"], trusted) == "203.0.113.5"  # trusted hops are skipped
    assert limits.client_address("::ffff:10.0.0.3", ["203.0.113.5"], trusted) == "203.0.113.5"
    assert limits.client_address("2001:db8::1", ["203.0.113.6"], trusted) == "203.0.113.6"
    assert limits.client_address("10.0.0.99", [], trusted) == "10.0.0.99"
    assert limits.client_address("10.0.1.1", ["203.0.113.5"], trusted) == "10.0.1.1"  # outside the trusted network
    assert limits.client_address("127.0.0.1", ["203.0.113.5"], trusted) == "127.0.0.1"
    counted = Settings(_env_file=None, rate_limit_trusted_proxies="10.0.0.0/24", rate_limit_proxy_hops=2)
    assert limits.client_address("10.0.0.2", ["forged, 198.51.100.4, 10.0.0.1"], counted) == "198.51.100.4"
    assert limits.client_address("10.0.0.2", ["198.51.100.4"], counted) == "10.0.0.2"
    for bad in ("10.0.0.0/33", "proxy.internal", "10.0.0.1;"):
        with pytest.raises(ValueError):
            Settings(_env_file=None, rate_limit_trusted_proxies=bad)


def test_every_launcher_keeps_uvicorn_from_rewriting_the_client_address(monkeypatch):
    import uvicorn

    from incretinos import cli

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: seen.update(kwargs))
    cli.serve(host="127.0.0.1", port=8999, reload=False)
    assert seen.get("proxy_headers") is False
    assert "--no-proxy-headers" in (BACKEND / "Dockerfile").read_text().split("CMD", 1)[1]
    playwright = (BACKEND.parent / "frontend" / "playwright.config.ts").read_text()
    assert all("--no-proxy-headers" in line for line in playwright.splitlines() if "uvicorn " in line)


# --------------------------------------------------------------------------- SEC-12: budgets for the expensive reads


def test_expensive_reads_share_a_query_budget(client, enforced_limits):
    assert [client.get("/api/validation").status_code for _ in range(2)] == [200, 200]
    assert client.get("/api/findings", params={"limit": 1}).status_code == 200
    limited = client.get("/api/review/summary")
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1 and "limit 3 per minute" in limited.json()["detail"]
    assert limited.headers["x-content-type-options"] == "nosniff"
    # Cheap reads and the other budgets are not affected; another client has its own budget.
    assert client.get("/api/health").status_code == 200 and client.get("/api/drugs").status_code == 200
    assert TestClient(client.app, client=("198.51.100.20", 5000)).get("/api/validation").status_code == 200
    # A forged forwarded address does not buy a fresh budget.
    assert client.get("/api/safety/trial-ae", params={"limit": 1}, headers={"X-Forwarded-For": "203.0.113.77"}).status_code == 429


def test_query_budget_names_real_routes_and_can_be_switched_off(client, enforced_limits, monkeypatch):
    served = {(method, route.path) for method, route in _api_routes()}
    assert limits.QUERY_BUDGET_ROUTES <= served, sorted(limits.QUERY_BUDGET_ROUTES - served)
    monkeypatch.setattr(enforced_limits, "rate_limit_query_per_minute", 0)
    assert all(client.get("/api/validation").status_code == 200 for _ in range(5))


# --------------------------------------------------------------------------- SEC-07: Markdown table cells


def test_every_value_in_a_report_table_row_escapes_pipes(db, monkeypatch):
    import dataclasses

    from incretinos.engines.findings import get_finding
    from incretinos.reports import export

    finding = get_finding(db, result_id(db, "NCT04184622", "15 mg", estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=72))
    reading = export.interpret_finding(finding)
    monkeypatch.setattr(export, "interpret_finding", lambda _: reading)
    hostile = {f.name: f"{getattr(finding, f.name)} | PIPE-{f.name} |" for f in dataclasses.fields(finding) if isinstance(getattr(finding, f.name), str)}
    hostile.update(dose_amount=5.0, dispersion_value=0.4, formulation_key="INJ | PIPE-formulation |", dose_unit="mg | PIPE-unit |", dispersion_type="SD | PIPE-dispersion |")
    lines = export._finding_block("A", dataclasses.replace(finding, **hostile), 1)
    rows = [line for line in lines if line.startswith("| ") and not line.startswith("|---")]
    assert len(rows) >= 12
    for line in rows:
        cells = re.split(r"(?<!\\)\|", line.strip())[1:-1]
        assert len(cells) == 2, line
    by_name = {re.split(r"(?<!\\)\|", line)[1].strip(): line for line in rows}
    assert "0.4" in by_name["Uncertainty"] and "PIPE-dispersion" in by_name["Uncertainty"]
    assert "PIPE-unit" in by_name["Arm / dose"] and "PIPE-formulation" in by_name["Arm / dose"]
    for name in ("nct_id", "role", "multiplicity_controlled", "estimand_label", "estimand_basis", "verification_status"):
        if name in hostile:
            assert any(f"pipe-{name}" in line.lower().replace("\\_", "_") or f"pipe-{name}".replace("_", " ") in line.lower() for line in rows), name
    markdown_it = pytest.importorskip("markdown_it")
    tokens = markdown_it.MarkdownIt("commonmark").enable("table").parse("\n".join(lines))
    cells = [tokens[i + 1].content for i, t in enumerate(tokens) if t.type == "td_open"]
    uncertainty = cells[cells.index("Uncertainty") + 1]
    assert uncertainty.endswith("0.4") and "PIPE-dispersion" in uncertainty


# --------------------------------------------------------------------------- SEC-09 / SEC-11: images and pins


def _ignore_lines(path: Path) -> set[str]:
    return {line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")}


def test_image_build_contexts_exclude_secrets_and_local_build_output():
    for context in (BACKEND, BACKEND.parent / "frontend"):
        ignored = _ignore_lines(context / ".dockerignore")
        assert {".env*", "!.env.example", ".git"} <= ignored, context
    assert {".next", ".next-*", "*.tsbuildinfo", "var", "test-results", "playwright-report", "e2e", "AGENTS.md"} <= _ignore_lines(BACKEND.parent / "frontend" / ".dockerignore")
    assert {"var", ".venv", "tests", "**/__pycache__"} <= _ignore_lines(BACKEND / ".dockerignore")


def test_constraints_pin_final_releases_and_runtime_needs_no_test_only_stack():
    import tomllib

    pins = [line.strip() for line in (BACKEND / "constraints.txt").read_text().splitlines() if line.strip() and not line.startswith("#")]
    names = [pin.split("==")[0].lower().replace("_", "-") for pin in pins]
    assert all("==" in pin for pin in pins) and len(names) == len(set(names))
    prereleases = [pin for pin in pins if re.search(r"(a|b|rc|\.dev|\.post\d+\.dev)\d*$", pin.split("==")[1])]
    assert not prereleases, f"pre-release pins (pip chose them because a requirement names a pre-release): {prereleases}"
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text())["project"]
    runtime = {re.split(r"[<>=\[ ;]", dep)[0].lower() for dep in project["dependencies"]}
    assert not runtime & {"pandas", "statsmodels"}, "test-only reference libraries belong to the dev extra"
    assert any(dep.startswith("statsmodels") for dep in project["optional-dependencies"]["dev"])
    for path in (BACKEND / "incretinos").rglob("*.py"):
        assert not re.search(r"^\s*(import|from)\s+(pandas|statsmodels)\b", path.read_text(), re.M), path
