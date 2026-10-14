"""Runtime configuration.

Everything has a safe default so the product runs fully offline with no keys:
SQLite database, committed fixtures, no LLM provider, no reviewer token (read-only API).

Where settings come from, in order of precedence:

1. Process environment variables (``INCRETINOS_*``).
2. An env file, never looked up in the current working directory:
   ``INCRETINOS_ENV_FILE`` if set (an explicit path, where a missing file is an error; or ``none`` to read
   no file at all), otherwise ``<repository>/.env`` (next to ``.env.example``) and ``<repository>/backend/.env``,
   the second overriding the first.
3. The defaults below.

``describe_settings_sources()`` says which file, if any, was read; the CLI and the API print it at start.
"""

from __future__ import annotations

import hmac
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_ROOT.parent

ENV_FILE_VARIABLE = "INCRETINOS_ENV_FILE"
#: Looked up in this order when INCRETINOS_ENV_FILE is not set. Later files override earlier ones.
DEFAULT_ENV_FILES: tuple[Path, ...] = (REPO_ROOT / ".env", BACKEND_ROOT / ".env")


def _header_bytes(value: str) -> bytes:
    """Bytes of a value as it travelled in an HTTP header (ASGI servers decode header values as latin-1)."""
    try:
        return value.encode("latin-1")
    except UnicodeEncodeError:
        return value.encode("utf-8", "surrogatepass")


class Settings(BaseSettings):
    # env_file is supplied by get_settings(); nothing is read from the working directory.
    # An empty variable means "not set" (Docker Compose passes VAR= for anything left unset), so it never
    # replaces a default with an empty string or fails to parse as a number.
    model_config = SettingsConfigDict(env_prefix="INCRETINOS_", env_file=None, extra="ignore", env_ignore_empty=True)

    database_url: str = f"sqlite:///{BACKEND_ROOT / 'var' / 'incretinos.db'}"
    # Offline mode serves every external request from committed fixtures.
    offline: bool = True
    fixture_dir: Path = BACKEND_ROOT / "fixtures"
    curated_dir: Path = PACKAGE_ROOT / "curation" / "data"
    # Local cache for source text that may not be redistributed (e.g. full abstracts). Git-ignored.
    cache_dir: Path = BACKEND_ROOT / "var" / "cache"
    # Alembic script directory. The schema is only ever created by these migrations (CLI, tests, container).
    migrations_dir: Path = BACKEND_ROOT / "alembic"

    # Optional keys — only raise rate limits; never required.
    ncbi_api_key: str | None = None
    ncbi_tool: str = "incretinos"
    ncbi_email: str | None = None
    openfda_api_key: str | None = None

    # Mutating endpoints (review decisions, gap triage) need this token. Unset => the API cannot write at all.
    reviewer_token: str | None = None

    # Optional LLM assistant. "none" keeps the deterministic assistant only.
    llm_provider: str = "none"
    llm_model: str = "claude-opus-5-5"
    anthropic_api_key: str | None = None

    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    # Scheduled live re-ingestion interval in hours (0 disables; ignored in offline mode).
    refresh_hours: float = 0.0

    # Interactive API documentation (/docs, /redoc, /openapi.json). Off unless asked for.
    docs_enabled: bool = False

    # In-process request budgets for the two expensive unauthenticated endpoints, per client and per
    # process (they multiply by the number of worker processes). 0 disables a budget.
    rate_limit_enabled: bool = True
    rate_limit_simulation_per_minute: int = Field(30, ge=0, le=100_000)
    rate_limit_assistant_per_minute: int = Field(60, ge=0, le=100_000)
    # Number of trusted reverse proxies in front of the API that APPEND the client address to X-Forwarded-For.
    # 0 = identify clients by the socket peer address and ignore the header; n = use the n-th address from the
    # right. The bundled Next.js proxy does not append anything (it forwards whatever the browser sent), so
    # behind it this must stay 0, and all of its users then share one budget.
    rate_limit_proxy_hops: int = Field(0, ge=0, le=10)

    http_timeout_s: float = 30.0
    http_max_retries: int = 5

    def reviewer_token_matches(self, candidate: str | None) -> bool:
        """Constant-time comparison that never raises, whatever bytes the header carried."""
        if not self.reviewer_token or not candidate:
            return False
        expected = self.reviewer_token.encode("utf-8")
        # Compare bytes, never str: hmac.compare_digest raises TypeError on non-ASCII str. Both readings of the
        # candidate are checked (raw header bytes, and the text itself) without short-circuiting.
        as_sent = hmac.compare_digest(_header_bytes(candidate), expected)
        as_text = hmac.compare_digest(candidate.encode("utf-8", "surrogatepass"), expected)
        return as_sent | as_text


def env_files() -> tuple[Path, ...]:
    """Env files that exist and are read, in the order they are applied (later overrides earlier)."""
    explicit = os.environ.get(ENV_FILE_VARIABLE, "").strip()
    if explicit.lower() == "none":
        return ()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"{ENV_FILE_VARIABLE} points to {path}, which is not a file")
        return (path.resolve(),)
    return tuple(p for p in DEFAULT_ENV_FILES if p.is_file())


def describe_settings_sources() -> str:
    """One line for the start-up log: where configuration was read from."""
    files = env_files()
    if files:
        return "configuration: environment variables, then env file(s) " + ", ".join(str(p) for p in files)
    if os.environ.get(ENV_FILE_VARIABLE, "").strip().lower() == "none":
        return f"configuration: environment variables only ({ENV_FILE_VARIABLE}=none)"
    return "configuration: environment variables only (no env file found; the working directory is never searched)"


@lru_cache
def get_settings() -> Settings:
    files = env_files()
    return Settings(_env_file=files or None)
