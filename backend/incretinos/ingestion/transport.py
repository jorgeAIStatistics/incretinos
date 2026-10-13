"""Transports for external sources.

``LiveTransport`` talks to the public APIs with per-service rate limiting and
retry-with-exponential-backoff. ``FixtureTransport`` serves the same resources from
committed files so tests and the offline demo never touch the network. Adapters
address resources by a stable ``fixture_key`` so both transports are interchangeable.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

SERVICE_BASE_URLS = {
    "ctgov": "https://clinicaltrials.gov/api/v2",
    "eutils": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
    "openfda": "https://api.fda.gov",
}

# Conservative request budgets (requests per second) for keyless access.
DEFAULT_RATE_LIMITS = {"ctgov": 3.0, "eutils": 2.5, "openfda": 3.0}

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class TransportError(RuntimeError):
    """An external request failed after all retries."""

    def __init__(self, message: str, *, service: str, status: int | None = None):
        super().__init__(message)
        self.service = service
        self.status = status


class ResourceNotFound(TransportError):
    """The source answered definitively that the resource does not exist (HTTP 404)."""


class FixtureMissing(TransportError):
    """Offline mode has no recorded copy of this resource."""


class Transport(Protocol):
    mode: str

    def get(self, service: str, path: str, params: dict | None = None, *, fixture_key: str | None = None) -> bytes: ...

    def record(self, fixture_key: str, content: bytes) -> None: ...


class RateLimiter:
    """Minimum-interval limiter: at most ``rate`` requests per second per service."""

    def __init__(self, rate_per_s: float, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.min_interval = 1.0 / rate_per_s if rate_per_s > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> float:
        """Block until a request may be sent; returns the time waited."""
        with self._lock:
            now = self._clock()
            wait = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self.min_interval
        if wait > 0:
            self._sleep(wait)
        return wait


def backoff_delay(attempt: int, *, base: float = 0.5, cap: float = 30.0, jitter: Callable[[], float] = random.random) -> float:
    """Exponential backoff with full jitter (attempt is 0-based)."""
    return min(cap, base * (2**attempt)) * (0.5 + 0.5 * jitter())


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


@dataclass
class LiveTransport:
    """HTTP transport with rate limiting, retries and optional fixture recording."""

    client: httpx.Client | None = None
    max_retries: int = 5
    timeout_s: float = 30.0
    rate_limits: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_RATE_LIMITS))
    record_dir: Path | None = None
    default_params: dict[str, dict[str, str]] = field(default_factory=dict)
    sleep: Callable[[float], None] = time.sleep
    mode: str = "LIVE"

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = httpx.Client(
                timeout=self.timeout_s,
                headers={"User-Agent": "IncretinOS/0.1 (research evidence platform; public-data only)"},
                follow_redirects=True,
            )
        self._limiters = {svc: RateLimiter(rate, sleep=self.sleep) for svc, rate in self.rate_limits.items()}
        self.request_log: list[dict] = []

    def get(self, service: str, path: str, params: dict | None = None, *, fixture_key: str | None = None) -> bytes:
        if service not in SERVICE_BASE_URLS:
            raise TransportError(f"Unknown service '{service}'", service=service)
        url = SERVICE_BASE_URLS[service] + path
        merged = {**self.default_params.get(service, {}), **(params or {})}
        last_error: str = "no attempt made"
        for attempt in range(self.max_retries + 1):
            if service in self._limiters:
                self._limiters[service].acquire()
            try:
                assert self.client is not None
                response = self.client.get(url, params=merged)
            except httpx.HTTPError as exc:  # network failure / timeout
                last_error = f"{type(exc).__name__}: {exc}"
                self.request_log.append({"service": service, "path": path, "attempt": attempt, "error": last_error})
            else:
                self.request_log.append({"service": service, "path": path, "attempt": attempt, "status": response.status_code})
                if response.status_code == 200:
                    if self.record_dir is not None and fixture_key:
                        self.record(fixture_key, response.content)
                    return response.content
                if response.status_code == 404:
                    if self.record_dir is not None and fixture_key:
                        self.record(fixture_key + ".404", b"recorded: source answered 404 (no such resource / no matching records)\n")
                    raise ResourceNotFound(f"{service} {path} returned 404", service=service, status=404)
                if response.status_code not in RETRYABLE_STATUS:
                    raise TransportError(
                        f"{service} {path} returned HTTP {response.status_code}", service=service, status=response.status_code
                    )
                last_error = f"HTTP {response.status_code}"
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if attempt < self.max_retries:
                    self.sleep(retry_after if retry_after is not None else backoff_delay(attempt))
                    continue
            if attempt < self.max_retries:
                self.sleep(backoff_delay(attempt))
        raise TransportError(f"{service} {path} failed after {self.max_retries + 1} attempts ({last_error})", service=service)

    def record(self, fixture_key: str, content: bytes) -> None:
        if self.record_dir is None:
            return
        target = self.record_dir / fixture_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


@dataclass
class FixtureTransport:
    """Serves recorded responses; never opens a socket."""

    fixture_dir: Path
    mode: str = "OFFLINE"

    def get(self, service: str, path: str, params: dict | None = None, *, fixture_key: str | None = None) -> bytes:
        if not fixture_key:
            raise FixtureMissing(f"No fixture key supplied for {service} {path}", service=service)
        target = self.fixture_dir / fixture_key
        if not target.is_file():
            if target.with_name(target.name + ".404").is_file():
                raise ResourceNotFound(f"{service} {path} returned 404 when the fixture was recorded", service=service, status=404)
            raise FixtureMissing(f"No offline fixture for {service} {path} ({fixture_key})", service=service)
        return target.read_bytes()

    def record(self, fixture_key: str, content: bytes) -> None:  # pragma: no cover - no-op offline
        return None

    def has(self, fixture_key: str) -> bool:
        return (self.fixture_dir / fixture_key).is_file()


def build_transport(*, offline: bool, fixture_dir: Path, record: bool = False, settings=None) -> Transport:
    if offline:
        return FixtureTransport(fixture_dir)
    default_params: dict[str, dict[str, str]] = {}
    rate_limits = dict(DEFAULT_RATE_LIMITS)
    if settings is not None:
        eutils: dict[str, str] = {"tool": settings.ncbi_tool}
        if settings.ncbi_email:
            eutils["email"] = settings.ncbi_email
        if settings.ncbi_api_key:
            eutils["api_key"] = settings.ncbi_api_key
            rate_limits["eutils"] = 8.0
        default_params["eutils"] = eutils
        if settings.openfda_api_key:
            default_params["openfda"] = {"api_key": settings.openfda_api_key}
    return LiveTransport(
        record_dir=fixture_dir if record else None,
        default_params=default_params,
        rate_limits=rate_limits,
        max_retries=settings.http_max_retries if settings else 5,
        timeout_s=settings.http_timeout_s if settings else 30.0,
    )
