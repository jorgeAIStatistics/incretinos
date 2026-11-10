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

import hashlib
import hmac
import ipaddress
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
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


class ReviewerCredentialsError(ValueError):
    """The per-reviewer credentials file is missing or malformed."""


@dataclass(frozen=True)
class ReviewerIdentity:
    """Who is making a request that passed the reviewer check, and how that was established.

    ``name`` is set when the token is a personal reviewer token (the name comes from the server's credentials
    file and cannot be chosen by the client). It is None under the shared development token, where the client
    states a name itself and every decision is labelled as self-declared.
    """

    name: str | None
    basis: str  # policy.IDENTITY_AUTHENTICATED | policy.IDENTITY_SELF_DECLARED


_SHA256_HEX = frozenset("0123456789abcdef")


@lru_cache(maxsize=4)
def _read_reviewers_file(path: str, mtime_ns: int, size: int) -> tuple[tuple[str, bytes], ...]:
    """{reviewer name: sha256 hex of that reviewer's token} -> ((name, digest bytes), ...). Cached per file version."""
    from .review.policy import normalise_reviewer, reviewer_problem

    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewerCredentialsError(f"INCRETINOS_REVIEWERS_FILE {path} is not a readable JSON document: {exc}") from exc
    if not isinstance(document, dict) or not document:
        raise ReviewerCredentialsError(f"INCRETINOS_REVIEWERS_FILE {path} must be a non-empty JSON object {{reviewer name: sha256 hex of the token}}")
    out: list[tuple[str, bytes]] = []
    seen_names: set[str] = set()
    seen_digests: set[str] = set()
    for raw_name, digest in document.items():
        name = normalise_reviewer(raw_name)
        problem = reviewer_problem(name)
        if problem:
            raise ReviewerCredentialsError(f"INCRETINOS_REVIEWERS_FILE {path}: reviewer {raw_name!r}: {problem}")
        if not isinstance(digest, str) or len(digest) != 64 or not set(digest.lower()) <= _SHA256_HEX:
            raise ReviewerCredentialsError(f"INCRETINOS_REVIEWERS_FILE {path}: the value for {name!r} must be the 64-character sha256 hex digest of the token")
        if name.casefold() in seen_names or digest.lower() in seen_digests:
            raise ReviewerCredentialsError(f"INCRETINOS_REVIEWERS_FILE {path}: duplicate reviewer name or token digest ({name!r})")
        seen_names.add(name.casefold())
        seen_digests.add(digest.lower())
        out.append((name, bytes.fromhex(digest.lower())))
    return tuple(out)


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
    # A single shared token is a DEVELOPMENT mode: whoever holds it types a reviewer name, and every decision is
    # labelled "self-declared". For real review, set reviewers_file instead.
    reviewer_token: str | None = None
    # Per-reviewer credentials: a JSON file {"Reviewer Name": "<sha256 hex of that reviewer's token>"}. When set,
    # only these personal tokens can write, the reviewer name is taken from the matching entry (a name sent by the
    # client must equal it), and the shared reviewer_token is not accepted.
    reviewers_file: Path | None = None

    # Optional LLM assistant. "none" keeps the deterministic assistant only.
    llm_provider: str = "none"
    llm_model: str = "claude-opus-5-5"
    anthropic_api_key: str | None = None

    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    # Scheduled live re-ingestion interval in hours (0 disables; ignored in offline mode).
    refresh_hours: float = 0.0

    # Interactive API documentation (/docs, /redoc, /openapi.json). Off unless asked for.
    docs_enabled: bool = False

    # In-process request budgets for the expensive unauthenticated endpoints, per client and per process (they
    # multiply by the number of worker processes). 0 disables a budget. "query" covers the heaviest open reads
    # (validation report, findings, analyses, trial adverse events, the evidence review list and summary).
    rate_limit_enabled: bool = True
    rate_limit_simulation_per_minute: int = Field(30, ge=0, le=100_000)
    rate_limit_assistant_per_minute: int = Field(60, ge=0, le=100_000)
    rate_limit_query_per_minute: int = Field(120, ge=0, le=100_000)
    # Reverse proxies whose X-Forwarded-For is believed: comma-separated IP addresses or CIDR networks.
    # Empty (the default) = clients are identified by the socket peer address and the header is never read,
    # whoever sends it. The bundled Next.js proxy forwards whatever the browser sent, so it must NOT be listed.
    rate_limit_trusted_proxies: str = ""
    # Used only for a request whose socket peer is one of the trusted proxies above: the number of proxies that
    # APPEND the client address, so the client is the n-th address from the right. 0 = walk the header from the
    # right and take the first address that is not itself a trusted proxy.
    rate_limit_proxy_hops: int = Field(0, ge=0, le=10)

    # Largest request body accepted, in bytes, checked before the body is parsed (413 otherwise). The first is
    # the ceiling for every route a caller can reach without the reviewer token (calculation routes have tighter
    # limits of their own, set by what their schemas can hold: api/limits.py); the second applies to the reviewer
    # write routes, and only when a reviewer token header is present.
    max_request_body_bytes: int = Field(64 * 1024, ge=1024, le=16 * 1024 * 1024)
    max_review_body_bytes: int = Field(256 * 1024, ge=1024, le=16 * 1024 * 1024)

    @field_validator("rate_limit_trusted_proxies")
    @classmethod
    def _proxies_are_addresses(cls, value: str) -> str:
        """A misspelt proxy address must fail at start-up, not silently trust nobody (or everybody)."""
        for part in (p.strip() for p in value.split(",")):
            if part:
                try:
                    ipaddress.ip_network(part, strict=False)
                except ValueError as exc:
                    raise ValueError(f"INCRETINOS_RATE_LIMIT_TRUSTED_PROXIES: {part!r} is not an IP address or network") from exc
        return value

    http_timeout_s: float = 30.0
    http_max_retries: int = 5

    @property
    def review_enabled(self) -> bool:
        """True when some credential can write: personal reviewer tokens, or the shared development token."""
        return bool(self.reviewers_file or self.reviewer_token)

    @property
    def reviewer_identity_mode(self) -> str:
        """How a decision's reviewer name is established: PERSONAL_TOKENS (the server's, from the token),
        SHARED_TOKEN_SELF_DECLARED (typed by the client) or READ_ONLY (no credential configured)."""
        if self.reviewers_file:
            return "PERSONAL_TOKENS"
        return "SHARED_TOKEN_SELF_DECLARED" if self.reviewer_token else "READ_ONLY"

    def reviewer_credentials(self) -> tuple[tuple[str, bytes], ...]:
        """The configured personal tokens as (name, sha256 digest). Raises ReviewerCredentialsError when unusable."""
        if not self.reviewers_file:
            return ()
        path = Path(self.reviewers_file).expanduser()
        try:
            stat = path.stat()
        except OSError as exc:
            raise ReviewerCredentialsError(f"INCRETINOS_REVIEWERS_FILE points to {path}, which cannot be read: {exc}") from exc
        return _read_reviewers_file(str(path.resolve()), stat.st_mtime_ns, stat.st_size)

    def authenticate_reviewer(self, candidate: str | None) -> ReviewerIdentity | None:
        """The identity a reviewer token establishes, or None. Never raises on odd header bytes.

        With a credentials file, every configured digest is compared (constant time, no short-circuit) and the
        shared token is ignored. Without one, the shared token gives a self-declared identity.
        """
        from .review.policy import IDENTITY_AUTHENTICATED, IDENTITY_SELF_DECLARED

        if not candidate:
            return None
        if self.reviewers_file:
            try:
                credentials = self.reviewer_credentials()
            except ReviewerCredentialsError:
                return None  # a broken credentials file never falls back to the shared token
            digests = {hashlib.sha256(_header_bytes(candidate)).digest(), hashlib.sha256(candidate.encode("utf-8", "surrogatepass")).digest()}
            found: str | None = None
            for name, expected in credentials:
                hit = False
                for digest in digests:
                    hit |= hmac.compare_digest(digest, expected)
                if hit and found is None:
                    found = name
            return ReviewerIdentity(found, IDENTITY_AUTHENTICATED) if found is not None else None
        if self.reviewer_token_matches(candidate):
            return ReviewerIdentity(None, IDENTITY_SELF_DECLARED)
        return None

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
