"""Database engine and session helpers (SQLite for offline/dev, PostgreSQL in Docker).

Two guarantees live here because every caller goes through this module:

* One schema path. ``upgrade_schema()`` runs the Alembic migrations; the CLI, the container and the test
  suite all create the schema that way, so a database can always be migrated forward.
* Read-only requests. The session handed to an HTTP request refuses to commit changes unless the request
  is a non-GET request carrying the configured reviewer token. With no token configured nothing an HTTP
  caller does can be persisted.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, ORMExecuteState, Session, sessionmaker
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


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def make_engine(url: str) -> Engine:
    connect_args: dict = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        if ":///" in url and ":memory:" not in url:
            Path(url.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args=connect_args, future=True)
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
    """The whole write boundary of the HTTP API: a non-GET request that carries the configured reviewer token."""
    return method.upper() not in SAFE_METHODS and get_settings().reviewer_token_matches(reviewer_token)


def get_db(request: Request = None) -> Iterator[Session]:
    """FastAPI dependency. The session cannot commit changes unless ``request_may_write`` holds for the request."""
    session = get_sessionmaker()()
    if request is not None and not request_may_write(request.method, request.headers.get(REVIEWER_TOKEN_HEADER)):
        session.info[_READ_ONLY] = True
    try:
        yield session
    finally:
        session.close()


def create_all() -> None:
    """Build tables straight from the models. For throw-away scratch databases only.

    Real databases (development, tests, containers) are created by ``upgrade_schema()`` so that they carry a
    migration version; a test fails if the two ever produce different schemas.
    """
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(get_engine())


# --------------------------------------------------------------------------- schema (Alembic)

VERSION_TABLE = "alembic_version"


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
            "that did not use migrations. Rebuild it with `incretinos seed --reset` (this deletes its contents, "
            "including recorded review decisions). To keep the contents instead, check that the schema matches the first migration, "
            "then run `alembic stamp 10d1699882ea && alembic upgrade head` followed by `alembic check`."
        )
    command.upgrade(alembic_config(), revision)


def drop_schema() -> None:
    """Drop every application table and the migration version table."""
    from . import models  # noqa: F401  (register mappers)

    engine = get_engine()
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {VERSION_TABLE}"))
