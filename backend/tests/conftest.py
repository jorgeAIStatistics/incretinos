"""Shared fixtures: one offline-seeded database for the whole test session (no network)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="incretinos-test-"))
# INCRETINOS_TEST_DATABASE_URL runs the suite against another database (CI uses it for PostgreSQL).
# That database is dropped and rebuilt at the start of the session: never point it at data you want to keep.
os.environ["INCRETINOS_DATABASE_URL"] = os.environ.get("INCRETINOS_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'test.db'}"
os.environ["INCRETINOS_ENV_FILE"] = "none"  # tests never read a developer's .env
os.environ["INCRETINOS_OFFLINE"] = "true"
os.environ["INCRETINOS_REVIEWER_TOKEN"] = "test-reviewer-token"
os.environ["INCRETINOS_CACHE_DIR"] = str(_TMP / "cache")
os.environ["INCRETINOS_LLM_PROVIDER"] = "none"
# The request budgets are exercised by their own tests; the rest of the suite must not depend on timing.
os.environ["INCRETINOS_RATE_LIMIT_ENABLED"] = "false"

from incretinos.config import get_settings  # noqa: E402
from incretinos.db import drop_schema, get_sessionmaker, reset_engine, upgrade_schema  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(scope="session")
def seeded() -> dict:
    """Run the full offline pipeline once; returns the ingest report."""
    from incretinos.ingestion.pipeline import Pipeline

    get_settings.cache_clear()
    reset_engine()
    # The same schema path as the CLI and the container: the migrations, not create_all.
    drop_schema()
    upgrade_schema()
    session = get_sessionmaker()()
    try:
        report = Pipeline(session, get_settings(), offline=True).run()
        session.commit()
        return report.as_dict()
    finally:
        session.close()


@pytest.fixture()
def db(seeded):
    """A session whose changes are rolled back after each test."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(scope="session")
def client(seeded):
    from fastapi.testclient import TestClient

    from incretinos.api.main import app

    return TestClient(app)


def result_id(db, nct: str, arm_contains: str, *, category: str = "WEIGHT_PCT_CHANGE", estimand: str | None = None, tier: str | None = None, weeks: float | None = None, baseline_contains: str | None = None) -> int:
    """Locate a stored result by its scientific description (ids are not stable across seeds)."""
    from sqlalchemy import select

    from incretinos.models import Study

    study = db.scalar(select(Study).where(Study.nct_id == nct))
    assert study is not None, f"{nct} not seeded"
    for r in study.results:
        if r.endpoint.category != category or r.value is None or r.scope != "ARM" or " minus " in r.arm_label or " to " in r.arm_label.lower():
            continue
        if arm_contains.lower() not in r.arm_label.lower():
            continue
        if estimand and (r.estimand.label if r.estimand else "NOT_STATED") != estimand:
            continue
        if tier and r.tier != tier:
            continue
        if weeks and r.timepoint_weeks != weeks:
            continue
        if baseline_contains and baseline_contains.lower() not in (r.baseline_definition or "").lower():
            continue
        return r.id
    raise AssertionError(f"No result for {nct} / {arm_contains} / {estimand} / {tier}")


def analysis_id(db, nct: str, treatment_contains: str, *, comparator_contains: str = "placebo", category: str = "WEIGHT_PCT_CHANGE", weeks: float | None = 72,
                origin: str | None = "REGISTRY", estimand: str | None = None) -> int:
    """Locate a stored between-arm analysis by its scientific description (ids are not stable across seeds)."""
    from sqlalchemy import select

    from incretinos.models import Study

    study = db.scalar(select(Study).where(Study.nct_id == nct))
    assert study is not None, f"{nct} not seeded"
    for a in sorted(study.analyses, key=lambda x: x.id):
        if a.endpoint.category != category or (weeks is not None and a.timepoint_weeks != weeks) or (origin and a.origin != origin):
            continue
        if treatment_contains.lower() not in (a.treatment_label or "").lower() or comparator_contains.lower() not in (a.comparator_label or "").lower():
            continue
        if estimand and (a.estimand.label if a.estimand else "NOT_STATED") != estimand:
            continue
        return a.id
    raise AssertionError(f"No analysis for {nct} / {treatment_contains} vs {comparator_contains} / {category} / week {weeks}")
