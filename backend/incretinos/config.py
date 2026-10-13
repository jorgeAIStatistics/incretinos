"""Runtime configuration.

Everything has a safe default so the product runs fully offline with no keys:
SQLite database, committed fixtures, no LLM provider.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INCRETINOS_", env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{BACKEND_ROOT / 'var' / 'incretinos.db'}"
    # Offline mode serves every external request from committed fixtures.
    offline: bool = True
    fixture_dir: Path = BACKEND_ROOT / "fixtures"
    curated_dir: Path = PACKAGE_ROOT / "curation" / "data"
    # Local cache for source text that may not be redistributed (e.g. full abstracts). Git-ignored.
    cache_dir: Path = BACKEND_ROOT / "var" / "cache"

    # Optional keys — only raise rate limits; never required.
    ncbi_api_key: str | None = None
    ncbi_tool: str = "incretinos"
    ncbi_email: str | None = None
    openfda_api_key: str | None = None

    # Mutating endpoints (review decisions, ingestion triggers) need this token. Unset => read-only API.
    reviewer_token: str | None = None

    # Optional LLM assistant. "none" keeps the deterministic assistant only.
    llm_provider: str = "none"
    llm_model: str = "claude-opus-5-5"
    anthropic_api_key: str | None = None

    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    http_timeout_s: float = 30.0
    http_max_retries: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()
