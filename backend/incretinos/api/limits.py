"""Request budgets for the expensive unauthenticated endpoints.

The trial simulation and the assistant question do real work for anonymous callers, so each client gets a
budget per minute. The limiter is deliberately small: a token bucket per (endpoint, client) held in process
memory. There is no shared store, so with several worker processes each process keeps its own budget, and a
restart forgets them. That is enough to stop one caller from monopolising the service; it is not a
substitute for a rate limit at the reverse proxy of a public deployment.

Usage (one line on the route, nothing in its body)::

    @app.post("/api/design/simulate", dependencies=[Depends(limit_simulation)])

Settings (``INCRETINOS_`` prefix): ``RATE_LIMIT_ENABLED``, ``RATE_LIMIT_SIMULATION_PER_MINUTE``,
``RATE_LIMIT_ASSISTANT_PER_MINUTE`` (0 disables one budget) and ``RATE_LIMIT_PROXY_HOPS``.

Who is "one client"? The socket peer, unless the deployment declares trusted proxies. Requests that come
through the bundled web application all arrive from the web server's address, and its proxy neither adds
nor replaces ``X-Forwarded-For`` (measured against Next.js 16.4: a header sent by the browser is passed on
unchanged). So behind the web application every user shares one budget, which makes the defaults a cap on
the whole deployment; ``RATE_LIMIT_PROXY_HOPS`` must stay 0 there, because the header is the caller's to
forge. Set it only for a reverse proxy that appends the address it saw.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from fastapi import HTTPException, Request

from ..config import get_settings

MAX_TRACKED_CLIENTS = 10_000
_OVERFLOW_CLIENT = "(other clients)"


class TokenBucketLimiter:
    """``per_minute`` requests per key, refilled continuously; a full bucket allows a burst of ``per_minute``."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_keys: int = MAX_TRACKED_CLIENTS) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}  # key -> (tokens, time of last update)

    def check(self, scope: str, client: str, per_minute: int) -> float:
        """Spend one request. Returns 0.0 when allowed, otherwise the seconds until the next one is available."""
        capacity = float(per_minute)
        rate = capacity / 60.0
        now = self._clock()
        with self._lock:
            key = (scope, client)
            if key not in self._buckets and len(self._buckets) >= self._max_keys:
                self._evict_idle(now)
                if len(self._buckets) >= self._max_keys:
                    # Memory stays bounded whatever the callers do: clients beyond the cap share one bucket.
                    key = (scope, _OVERFLOW_CLIENT)
            tokens, updated = self._buckets.get(key, (capacity, now))
            tokens = min(capacity, tokens + (now - updated) * rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return 0.0
            self._buckets[key] = (tokens, now)
            return (1.0 - tokens) / rate

    def _evict_idle(self, now: float) -> None:
        """Forget clients that have been quiet for a minute: a bucket that old is full again whatever its budget."""
        for key in [k for k, (_, updated) in self._buckets.items() if now - updated >= 60.0]:
            del self._buckets[key]

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def __len__(self) -> int:
        return len(self._buckets)


limiter = TokenBucketLimiter()


def client_key(request: Request, proxy_hops: int) -> str:
    """Who the budget belongs to.

    By default the socket peer. X-Forwarded-For is client-controlled, so it is consulted only when the
    deployment declares how many reverse proxies it trusts; the address those proxies appended is then the
    ``proxy_hops``-th from the right, and anything further left was supplied by the caller and is ignored.
    """
    peer = request.client.host if request.client else "unknown"
    if proxy_hops > 0:
        forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
        if len(forwarded) >= proxy_hops:
            return forwarded[-proxy_hops][:64]
    return peer[:64]


def rate_limit(scope: str, setting: str, what: str) -> Callable[[Request], None]:
    """A FastAPI dependency enforcing the budget named by ``setting`` (requests per minute per client)."""

    def dependency(request: Request) -> None:
        cfg = get_settings()
        per_minute = int(getattr(cfg, setting))
        if not cfg.rate_limit_enabled or per_minute <= 0:
            return
        wait = limiter.check(scope, client_key(request, cfg.rate_limit_proxy_hops), per_minute)
        if wait > 0:
            retry_after = max(1, math.ceil(wait))
            raise HTTPException(
                status_code=429,
                detail=f"Too many {what} from this client (limit {per_minute} per minute). Try again in {retry_after} s.",
                headers={"Retry-After": str(retry_after)},
            )

    dependency.__name__ = f"limit_{scope}"
    return dependency


limit_simulation = rate_limit("simulation", "rate_limit_simulation_per_minute", "simulation requests")
limit_assistant = rate_limit("assistant", "rate_limit_assistant_per_minute", "assistant questions")
