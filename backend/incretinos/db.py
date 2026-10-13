"""Database engine and session helpers (SQLite for offline/dev, PostgreSQL in Docker).

Two rules about evidence are enforced here, at the session, so that no engine, report or
assistant module has to remember them:

* Rejected evidence is invisible. Every ORM SELECT (including relationship loads such as
  ``study.results``, counts and sub-queries) silently excludes rows whose verification status is
  REJECTED. Inspection code that must see them opts in for one statement with
  ``.execution_options(include_rejected=True)`` or for a block with ``rejected_visible(session)``.
* Rebuilding derived data never destroys human-authored state. ``reset_derived_data`` empties the
  evidence tables and leaves review decisions, evidence version history, the audit log, source
  snapshots and change events alone.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, inspect, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, ORMExecuteState, Session, sessionmaker, with_loader_criteria

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None

#: Execution option / ``session.info`` key that lets a statement or a session see rejected evidence.
INCLUDE_REJECTED = "include_rejected"

#: Tables ``reset_derived_data`` never empties: provenance history and everything a person wrote.
PRESERVED_ON_RESET: tuple[str, ...] = (
    "evidence_source",  # snapshots and change events hang off these rows; ids stay stable
    "source_snapshot",
    "change_event",
    "review_queue_item",
    "review_decision",
    "evidence_version",
    "audit_event",
    "ingestion_run",
    "evidence_gap",  # carries reviewer triage (status, note, importance); rule-derived fields are refreshed on load
)


class SchemaOutOfDate(RuntimeError):
    """The database was created by an older version of the models and has not been migrated."""


def _compact_json(value: Any) -> str:
    """JSON columns without padding or \\u escapes: the version history stores one document per evidence row."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def make_engine(url: str) -> Engine:
    connect_args: dict = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        if ":///" in url and ":memory:" not in url:
            Path(url.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args=connect_args, future=True, json_serializer=_compact_json)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = make_engine(get_settings().database_url)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine (tests switch databases)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def get_sessionmaker() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------- rejected evidence is invisible


@lru_cache(maxsize=1)
def _rejected_criteria() -> tuple[Any, ...]:
    from .models import REVIEWABLE_MODELS
    from .review.policy import EXCLUDED_STATUSES

    excluded = sorted(EXCLUDED_STATUSES)
    return tuple(
        with_loader_criteria(model, model.verification_status.not_in(excluded), include_aliases=True, propagate_to_loaders=False)
        for model in REVIEWABLE_MODELS.values()
    )


@event.listens_for(Session, "do_orm_execute")
def _hide_rejected_evidence(state: ORMExecuteState) -> None:
    """The single choke point that keeps rejected evidence out of every analytic surface.

    Applied to each SELECT separately (top-level queries and relationship loads alike), so a
    collection loaded lazily from an object obeys the same rule as a query. Column refreshes of an
    object already in the session are left alone: hiding the row there would look like a deletion.
    Statements that are not ORM SELECTs (bulk DELETE / UPDATE used by the loaders, raw SQL) are not
    filtered; nothing analytic uses them.
    """
    if not state.is_select or state.is_column_load:
        return
    if state.execution_options.get(INCLUDE_REJECTED) or state.session.info.get(INCLUDE_REJECTED):
        return
    state.statement = state.statement.options(*_rejected_criteria())


@contextmanager
def rejected_visible(session: Session) -> Iterator[Session]:
    """Let every query on ``session`` see rejected evidence inside the block (loaders, review, audit).

    Collections loaded inside the block stay on their objects afterwards, so analytic code must not
    reuse them: the review lifecycle expires the session when it finishes, and API handlers that opt
    in do so for the whole request.
    """
    previous = session.info.get(INCLUDE_REJECTED)
    session.info[INCLUDE_REJECTED] = True
    try:
        yield session
    finally:
        if previous is None:
            session.info.pop(INCLUDE_REJECTED, None)
        else:
            session.info[INCLUDE_REJECTED] = previous


# --------------------------------------------------------------------------- schema and reset


def create_all() -> None:
    from . import models  # noqa: F401  (register mappers)

    engine = get_engine()
    Base.metadata.create_all(engine)
    ensure_schema_current(engine)


def ensure_schema_current(engine: Engine | None = None) -> None:
    """Fail clearly when an existing database predates the current models.

    ``create_all`` creates missing tables but never alters existing ones, so a database made before the
    review lifecycle would fail later with an obscure "no such column" error.
    """
    engine = engine or get_engine()
    inspector = inspect(engine)
    missing: list[str] = []
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        present = {column["name"] for column in inspector.get_columns(table.name)}
        missing.extend(f"{table.name}.{column.name}" for column in table.columns if column.name not in present)
    if missing:
        shown = ", ".join(missing[:6]) + (" ..." if len(missing) > 6 else "")
        raise SchemaOutOfDate(
            f"The database schema is older than this version of IncretinOS (missing: {shown}). Upgrade it with "
            "`alembic upgrade head` (for a database that was created without Alembic, run "
            "`alembic stamp 10d1699882ea` first), or for a disposable SQLite file delete it and seed again."
        )


def reset_derived_data(session: Session) -> dict[str, int]:
    """Empty every table that is rebuilt from sources and curation; keep history and human-authored state.

    Rows are deleted, not tables dropped, so this behaves the same on SQLite and PostgreSQL and never
    triggers a cascade into a preserved table. Returns the number of rows removed per table.
    """
    from .models import EvidenceGap

    # Gaps point at drugs, which are rebuilt: detach them so the cascade from ``drug`` cannot delete a
    # gap that carries a reviewer's triage. The gap engine re-attaches the drug on the next load.
    session.execute(update(EvidenceGap).values(drug_id=None))
    removed: dict[str, int] = {}
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in PRESERVED_ON_RESET:
            continue
        removed[table.name] = session.execute(table.delete()).rowcount or 0
    session.flush()
    session.expire_all()
    return removed


def wipe_everything() -> None:
    """Drop every table, including review decisions, version history, the audit log and snapshots."""
    from . import models  # noqa: F401

    Base.metadata.drop_all(get_engine())
