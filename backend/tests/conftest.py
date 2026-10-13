"""Shared fixtures: one offline-seeded database for the whole test session (no network)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="incretinos-test-"))
os.environ["INCRETINOS_DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["INCRETINOS_OFFLINE"] = "true"
os.environ["INCRETINOS_REVIEWER_TOKEN"] = "test-reviewer-token"
os.environ["INCRETINOS_CACHE_DIR"] = str(_TMP / "cache")
os.environ["INCRETINOS_LLM_PROVIDER"] = "none"

from incretinos.config import get_settings  # noqa: E402
from incretinos.db import create_all, get_sessionmaker, reset_engine  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(scope="session")
def seeded() -> dict:
    """Run the full offline pipeline once; returns the ingest report."""
    from incretinos.ingestion.pipeline import Pipeline

    get_settings.cache_clear()
    reset_engine()
    create_all()
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
