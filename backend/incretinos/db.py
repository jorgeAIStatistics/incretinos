"""Database engine and session helpers (SQLite for offline/dev, PostgreSQL in Docker).

Four guarantees live here, at the engine and the session, because every caller goes through this
module and no engine, report, route or assistant module should have to remember them:

* One schema path. ``upgrade_schema()`` runs the Alembic migrations; the CLI, the container and the test
  suite all create the schema that way, so a database can always be migrated forward.
* Read-only requests. The session handed to an HTTP request refuses to commit changes unless the request
  is a non-GET request carrying the configured reviewer token. With no token configured nothing an HTTP
  caller does can be persisted.
* Rejected evidence is invisible. Every ORM SELECT (including relationship loads such as
  ``study.results``, counts and sub-queries) silently excludes rows whose verification status is
  REJECTED, and rows under a review hold (rejected earlier, changed since, not yet decided again). Inspection code that must see them opts in for one statement with
  ``.execution_options(include_rejected=True)`` or for a block with ``rejected_visible(session)``.
* Rebuilding derived data never destroys human-authored state. ``reset_derived_data`` empties the
  evidence tables and leaves review decisions, evidence version history, the audit log, source
  snapshots and change events alone. Only ``drop_schema`` destroys them, and the CLI reaches it only
  through the explicit ``--reset --wipe-review-history``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import and_, create_engine, event, false, inspect, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, ORMExecuteState, Session, sessionmaker, with_loader_criteria
from starlette.requests import Request

from .config import get_settings

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
REVIEWER_TOKEN_HEADER = "x-reviewer-token"
_READ_ONLY = "incretinos_read_only"
_WRITE_ATTEMPTED = "incretinos_write_attempted"


class ReadOnlyRequestError(RuntimeError):
    """A request that is not allowed to change data tried to commit a change."""


class UnversionedSchemaError(RuntimeError):
    """The database holds application tables that were not created by the migrations."""


class SchemaOutOfDate(RuntimeError):
    """The database was created by an older version of the models and has not been migrated."""


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


def _compact_json(value: Any) -> str:
    """JSON columns without padding or \\u escapes: the version history stores one document per evidence row.

    NaN and Infinity are refused: they are not JSON, PostgreSQL rejects them and a ledger holding them cannot be
    restored. A caller that may receive them validates first (the review API does).
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


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


def _has_pending_changes(session: Session) -> bool:
    return bool(session.new or session.deleted or any(session.is_modified(obj) for obj in session.dirty))


def _install_read_only_guard(factory: sessionmaker[Session]) -> None:
    """Sessions flagged read-only may flush (the transaction is rolled back on close) but never commit a change."""

    @event.listens_for(factory, "before_flush")
    def _note_flush(session: Session, flush_context, instances) -> None:
        if session.info.get(_READ_ONLY) and _has_pending_changes(session):
            session.info[_WRITE_ATTEMPTED] = True

    @event.listens_for(factory, "do_orm_execute")
    def _note_dml(state: ORMExecuteState) -> None:
        if state.session.info.get(_READ_ONLY) and (state.is_insert or state.is_update or state.is_delete):
            state.session.info[_WRITE_ATTEMPTED] = True

    @event.listens_for(factory, "before_commit")
    def _refuse_commit(session: Session) -> None:
        if session.info.get(_READ_ONLY) and (session.info.get(_WRITE_ATTEMPTED) or _has_pending_changes(session)):
            raise ReadOnlyRequestError(
                "This request may not change data: only a non-GET request carrying the configured reviewer token can commit. "
                "Record usage in the application log instead of the database."
            )


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = make_engine(get_settings().database_url)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
        _install_read_only_guard(_SessionLocal)
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


def request_may_write(method: str, reviewer_token: str | None) -> bool:
    """The whole write boundary of the HTTP API: a non-GET request that carries a configured reviewer credential."""
    return method.upper() not in SAFE_METHODS and get_settings().authenticate_reviewer(reviewer_token) is not None


def get_db(request: Request = None) -> Iterator[Session]:
    """FastAPI dependency. The session cannot commit changes unless ``request_may_write`` holds for the request."""
    session = get_sessionmaker()()
    if request is not None and not request_may_write(request.method, request.headers.get(REVIEWER_TOKEN_HEADER)):
        session.info[_READ_ONLY] = True
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
    # A held row (rejected earlier; its content changed since and nobody has decided on it again) is excluded
    # exactly like a rejected one. The column is NOT NULL with a false default, so the comparison is total.
    return tuple(
        with_loader_criteria(
            model, and_(model.verification_status.not_in(excluded), model.review_hold.is_(false())), include_aliases=True, propagate_to_loaders=False
        )
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


# --------------------------------------------------------------------------- small reference tables read once per transaction

_MEMO = "incretinos_session_memo"


def session_memo(session: Session, name: str, build: Any) -> Any:
    """``build(session)``, computed at most once per transaction and per visibility of rejected rows.

    For small reference tables that an engine consults once per evidence row (for example the documented
    exposure equivalences behind arm identity): without it a list endpoint issues one query set per row.
    The value must hold plain data, not mapped objects. It is dropped as soon as the session writes
    anything (flush, bulk statement, raw SQL) and whenever a transaction ends, so it can never outlive the
    state it was read from; and it is kept separately for sessions that may see rejected evidence, so what
    an inspection block read is never handed to analytic code.
    """
    memo = session.info.setdefault(_MEMO, {})
    key = (name, bool(session.info.get(INCLUDE_REJECTED)))
    if key not in memo:
        memo[key] = build(session)
    return memo[key]


@event.listens_for(Session, "do_orm_execute")
def _forget_memo_on_statement(state: ORMExecuteState) -> None:
    if not state.is_select:
        state.session.info.pop(_MEMO, None)


@event.listens_for(Session, "after_flush")
def _forget_memo_on_flush(session: Session, flush_context: Any) -> None:
    session.info.pop(_MEMO, None)


@event.listens_for(Session, "after_transaction_end")
def _forget_memo_on_transaction_end(session: Session, transaction: Any) -> None:
    session.info.pop(_MEMO, None)


# --------------------------------------------------------------------------- schema (Alembic) and reset

VERSION_TABLE = "alembic_version"


def create_all(engine: Engine | None = None) -> None:
    """Build tables straight from the models. For throw-away scratch databases only.

    Real databases (development, tests, containers) are created by ``upgrade_schema()`` so that they carry a
    migration version; a test fails if the two ever produce different schemas. Refuses, without touching
    anything, a database whose existing tables are out of date: creating the missing tables beside them would
    leave a half-upgraded database that could not be migrated afterwards.
    """
    from . import models  # noqa: F401  (register mappers)

    engine = engine or get_engine()
    ensure_schema_current(engine)
    Base.metadata.create_all(engine)


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


def schema_state(engine: Engine | None = None) -> str:
    """``empty`` (no application tables), ``migrated`` (has a migration version) or ``unversioned``."""
    from . import models  # noqa: F401  (register mappers)

    names = set(inspect(engine or get_engine()).get_table_names())
    if VERSION_TABLE in names:
        return "migrated"
    return "unversioned" if names & set(Base.metadata.tables) else "empty"


def alembic_config():
    """Alembic configuration for the application's migration scripts and database (no .ini file needed)."""
    from alembic.config import Config

    location = get_settings().migrations_dir
    if not (location / "env.py").is_file() or not (location / "versions").is_dir():
        raise FileNotFoundError(f"No migration scripts at {location}. Set INCRETINOS_MIGRATIONS_DIR to the directory that holds env.py and versions/.")
    config = Config()
    config.set_main_option("script_location", str(location).replace("%", "%%"))
    return config


def upgrade_schema(revision: str = "head") -> None:
    """Create or upgrade the schema by running the migrations. This is the only way a real schema is built."""
    from alembic import command

    if schema_state() == "unversioned":
        raise UnversionedSchemaError(
            "This database has application tables but no migration version: it was created by an older build "
            "that did not use migrations. To keep its contents (sources, snapshots, audit log, review decisions), check "
            "that the schema matches the first migration, then run `alembic stamp 10d1699882ea && alembic upgrade head`, "
            "`alembic check` and `incretinos review reconcile`. To discard it, run `incretinos seed --reset --wipe-review-history` "
            "(this drops every table, including recorded review decisions and the audit log; a full backup of every table "
            "and a decision ledger are written first, and the wipe is recorded in the new database), or delete a disposable SQLite file and seed again."
        )
    command.upgrade(alembic_config(), revision)


def reset_derived_data(session: Session) -> dict[str, int]:
    """Empty every table that is rebuilt from sources and curation; keep history and human-authored state.

    Rows are deleted, not tables dropped, so this behaves the same on SQLite and PostgreSQL, never
    triggers a cascade into a preserved table, and leaves the migration version in place. Returns the
    number of rows removed per table.
    """
    from .models import EvidenceGap, EvidenceVersion

    # No evidence row exists after this, so no stored version is the current one. The history itself is untouched,
    # and the next reconcile compares every rebuilt row with the version it had when it was last seen.
    session.execute(update(EvidenceVersion).values(is_current=False))
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


def drop_schema() -> None:
    """Drop every application table and the migration version table.

    This destroys review decisions, evidence version history, the audit log, source snapshots and change
    events along with the derived data. The CLI calls it only for ``--reset --wipe-review-history`` (after
    backing up every table and the decision ledger); the test suite calls it to start from nothing.
    """
    from . import models  # noqa: F401  (register mappers)

    engine = get_engine()
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {VERSION_TABLE}"))
