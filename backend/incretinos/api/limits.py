"""Request budgets and request-body limits for the unauthenticated API.

Request budgets
---------------
The trial simulation, the assistant question and the heaviest reads do real work for anonymous callers, so
each client gets a budget per minute. The limiter is deliberately small: a token bucket per (budget, client)
held in process memory. There is no shared store, so with several worker processes each process keeps its own
budget, and a restart forgets them. That is enough to stop one caller from monopolising the service; it is not
a substitute for a rate limit at the reverse proxy of a public deployment.

Two ways to attach a budget:

* a dependency on the route (one line, nothing in its body), used for the simulation and the assistant::

      @app.post("/api/design/simulate", dependencies=[Depends(limit_simulation)])

* the ``QUERY_BUDGET_ROUTES`` table, enforced by ``RouteBudgetMiddleware`` before routing and before any work,
  used for the expensive reads (some of which live in a router of their own).

Settings (``INCRETINOS_`` prefix): ``RATE_LIMIT_ENABLED``, ``RATE_LIMIT_SIMULATION_PER_MINUTE``,
``RATE_LIMIT_ASSISTANT_PER_MINUTE``, ``RATE_LIMIT_QUERY_PER_MINUTE`` (0 disables one budget),
``RATE_LIMIT_TRUSTED_PROXIES`` and ``RATE_LIMIT_PROXY_HOPS``.

Who is "one client"? The socket peer, unless that peer is one of the deployment's declared trusted proxies.
``X-Forwarded-For`` is written by whoever sent the request, so it is read only from a trusted proxy's
connection. Requests that come through the bundled web application all arrive from the web server's address,
and its proxy neither adds nor replaces ``X-Forwarded-For`` (measured against Next.js 16.4: a header sent by the
browser is passed on unchanged). So behind the web application every user shares one budget, which makes the
defaults a cap on the whole deployment, and the web server must not be listed as a trusted proxy. List only a
reverse proxy that appends the address it saw.

The API server must not rewrite the peer address from the header either: uvicorn does that by default for
connections from 127.0.0.1, so every launcher in this repository starts it with proxy headers off
(``incretinos serve``, the API image, the browser-test server).

Request bodies
--------------
``RequestBodyLimitMiddleware`` refuses a body larger than its route can use with 413, before it is parsed. A
declared ``Content-Length`` is checked without reading anything; a streamed body is read up to the limit and
no further. The limits are in ``body_limit`` below. It also refuses a JSON body that holds a lone UTF-16
surrogate (``"\\ud800"``): such a string cannot be encoded as UTF-8, so it could be neither stored nor echoed
(I-JSON, RFC 7493, rules it out for the same reason).
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import threading
import time
from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import Any

from fastapi import HTTPException, Request
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import Settings, get_settings

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


# --------------------------------------------------------------------------- who is the client


@lru_cache(maxsize=16)
def trusted_networks(spec: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """The networks named by ``RATE_LIMIT_TRUSTED_PROXIES`` (validated when the settings load)."""
    return tuple(ipaddress.ip_network(part.strip(), strict=False) for part in spec.split(",") if part.strip())


def _is_trusted(address: str, networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    try:
        ip = ipaddress.ip_address(address.strip().strip("[]"))
    except ValueError:
        return False  # "testclient", a hostname, garbage: never a trusted proxy
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in network for network in networks)


def client_address(peer: str | None, forwarded_for: Iterable[str], cfg: Settings) -> str:
    """Who a request's budget belongs to.

    The socket peer, unless the peer is a declared trusted proxy. Only then is ``X-Forwarded-For`` read: with
    ``proxy_hops`` = n the client is the n-th address from the right (what the outermost of n appending proxies
    saw); with 0, the first address from the right that is not itself a trusted proxy. Anything further left was
    supplied by the caller and is ignored.
    """
    peer = peer or "unknown"
    networks = trusted_networks(cfg.rate_limit_trusted_proxies)
    if not networks or not _is_trusted(peer, networks):
        return peer[:64]
    forwarded = [part.strip() for header in forwarded_for for part in header.split(",") if part.strip()]
    if cfg.rate_limit_proxy_hops > 0:
        return forwarded[-cfg.rate_limit_proxy_hops][:64] if len(forwarded) >= cfg.rate_limit_proxy_hops else peer[:64]
    for address in reversed(forwarded):
        if not _is_trusted(address, networks):
            return address[:64]
    return peer[:64]


def client_key(request: Request, cfg: Settings) -> str:
    return client_address(request.client.host if request.client else None, request.headers.getlist("x-forwarded-for"), cfg)


def _too_many(what: str, per_minute: int, wait: float) -> tuple[str, dict[str, str]]:
    retry_after = max(1, math.ceil(wait))
    return (f"Too many {what} from this client (limit {per_minute} per minute). Try again in {retry_after} s.", {"Retry-After": str(retry_after)})


def rate_limit(scope: str, setting: str, what: str) -> Callable[[Request], None]:
    """A FastAPI dependency enforcing the budget named by ``setting`` (requests per minute per client)."""

    def dependency(request: Request) -> None:
        cfg = get_settings()
        per_minute = int(getattr(cfg, setting))
        if not cfg.rate_limit_enabled or per_minute <= 0:
            return
        wait = limiter.check(scope, client_key(request, cfg), per_minute)
        if wait > 0:
            detail, headers = _too_many(what, per_minute, wait)
            raise HTTPException(status_code=429, detail=detail, headers=headers)

    dependency.__name__ = f"limit_{scope}"
    return dependency


limit_simulation = rate_limit("simulation", "rate_limit_simulation_per_minute", "simulation requests")
limit_assistant = rate_limit("assistant", "rate_limit_assistant_per_minute", "assistant questions")


# --------------------------------------------------------------------------- budgets enforced before routing

#: Open reads that cost the single worker hundreds of milliseconds and megabytes each at their largest page
#: (measured on the seeded database: /api/validation about 0.7 s, /api/safety/trial-ae?limit=5000 0.4 s and 9 MB,
#: /api/findings?limit=2000 0.3 s and 7 MB, /api/review/summary 0.35 s, /api/review/evidence 0.26 s,
#: /api/analyses?limit=1500 0.1 s and 4 MB, /api/conflicts about 0.7 s, /api/quality about 2 s, /api/quality/records and
#: /api/quality/assessment about 1.5 s each, all three reading every stored evidence row twice,
#: /api/indirect/feasible?category=WEIGHT_RESPONDER 0.8 s for 3,777 pairs). They share one budget per client: RATE_LIMIT_QUERY_PER_MINUTE.
QUERY_BUDGET_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/validation"),
    ("GET", "/api/findings"),
    ("GET", "/api/analyses"),
    ("GET", "/api/safety/trial-ae"),
    ("GET", "/api/review/evidence"),
    ("GET", "/api/review/summary"),
    ("GET", "/api/conflicts"),
    ("GET", "/api/quality"),
    ("GET", "/api/quality/records"),
    ("GET", "/api/quality/assessment"),
    ("GET", "/api/indirect/feasible"),
})


def _normalised_path(path: str) -> str:
    return path.rstrip("/") or "/"


async def _send_json(send: Send, status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
    payload = json.dumps(body, ensure_ascii=True).encode("ascii")
    raw = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    raw += [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in (headers or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": payload})


class RouteBudgetMiddleware:
    """Spends the query budget for the routes in ``QUERY_BUDGET_ROUTES``; 429 with Retry-After when it is used up."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            method = "GET" if scope["method"] == "HEAD" else scope["method"]
            if (method, _normalised_path(scope["path"])) in QUERY_BUDGET_ROUTES:
                cfg = get_settings()
                per_minute = cfg.rate_limit_query_per_minute
                if cfg.rate_limit_enabled and per_minute > 0:
                    client = scope.get("client")
                    key = client_address(client[0] if client else None, Headers(scope=scope).getlist("x-forwarded-for"), cfg)
                    wait = limiter.check("query", key, per_minute)
                    if wait > 0:
                        detail, headers = _too_many("data queries", per_minute, wait)
                        await _send_json(send, 429, {"detail": detail}, headers)
                        return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- request bodies

KIB = 1024
#: Bodies sized by what each route's schema can hold, with room for pretty-printed JSON and UTF-8 text. A larger
#: body cannot be a valid request (short of padding), so it is refused before it is read.
#: (method, path pattern) -> bytes.
ROUTE_BODY_LIMITS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    # A few numbers and identifiers.
    ("POST", re.compile(r"/api/compare"), 4 * KIB),
    # Two identifiers, a level, a tolerance and a justification of at most 600 characters.
    ("POST", re.compile(r"/api/indirect/assess"), 4 * KIB),
    ("POST", re.compile(r"/api/stats/risk-contrast"), 4 * KIB),
    ("POST", re.compile(r"/api/safety/disproportionality"), 4 * KIB),
    ("POST", re.compile(r"/api/design/power"), 4 * KIB),
    ("POST", re.compile(r"/api/design/simulate"), 4 * KIB),
    # Two lists of at most 20 numbers each.
    ("POST", re.compile(r"/api/design/sensitivity"), 8 * KIB),
    # A question of at most 1,000 characters; at most 20 named criteria.
    ("POST", re.compile(r"/api/assistant/ask"), 16 * KIB),
    ("POST", re.compile(r"/api/benefit-risk/utility"), 16 * KIB),
    # Up to 50 arm labels as keys.
    ("POST", re.compile(r"/api/benefit-risk/dose-scenario"), 64 * KIB),
)
#: Routes that need the reviewer token. A body above the open ceiling is read only when the request carries a
#: token header; the token itself is checked by the route.
REVIEW_BODY_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"/api/review/evidence/[^/]+/[^/]+/decisions")),
    ("POST", re.compile(r"/api/review-queue/[^/]+/decision")),
    ("PATCH", re.compile(r"/api/gaps/[^/]+")),
)


def body_limit(method: str, path: str, *, has_reviewer_token: bool, cfg: Settings) -> int:
    """The largest body, in bytes, this request may carry."""
    path = _normalised_path(path)
    ceiling = cfg.max_request_body_bytes
    if has_reviewer_token and any(m == method and p.fullmatch(path) for m, p in REVIEW_BODY_ROUTES):
        return cfg.max_review_body_bytes
    for route_method, pattern, limit in ROUTE_BODY_LIMITS:
        if route_method == method and pattern.fullmatch(path):
            return min(limit, ceiling)
    return ceiling


_JSON_ESCAPE = re.compile(r"\\(?:u([0-9a-fA-F]{4})|.)", re.S)
_RAW_SURROGATE = re.compile("[\ud800-\udfff]")


def has_lone_surrogate(body: bytes) -> bool:
    """True when the JSON text ``body`` holds a UTF-16 surrogate that is not half of a pair.

    The text is decoded the way ``json.loads`` decodes bytes (detected encoding, surrogates passed through), then
    every escape is visited in order, so ``\\\\ud800`` (an escaped backslash followed by text) is not mistaken
    for an escape.
    """
    try:
        text = body.decode(json.detect_encoding(body), "surrogatepass")
    except (UnicodeDecodeError, LookupError):
        return False  # not text at all: the JSON parser reports it
    if _RAW_SURROGATE.search(text):
        return True
    if "\\u" not in text:
        return False
    open_high_at: int | None = None  # where a high surrogate escape ended, while it waits for its low half
    for match in _JSON_ESCAPE.finditer(text):
        code = int(match.group(1), 16) if match.group(1) else None
        if open_high_at is not None:
            if code is not None and 0xDC00 <= code <= 0xDFFF and match.start() == open_high_at:
                open_high_at = None
                continue
            return True
        if code is None:
            continue
        if 0xD800 <= code <= 0xDBFF:
            open_high_at = match.end()
        elif 0xDC00 <= code <= 0xDFFF:
            return True
    return open_high_at is not None


def _is_json(content_type: str | None) -> bool:
    if not content_type:
        return True  # FastAPI parses a body without a content type as JSON
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json" or (media.startswith("application/") and media.endswith("+json"))


class RequestBodyLimitMiddleware:
    """413 for a body larger than ``body_limit`` allows, decided before routing and before anything is parsed.

    A declared Content-Length over the limit is refused without reading the body. Otherwise the body is read
    here, at most limit + 1 bytes of it, and handed on to the application in one piece.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        limit = body_limit(scope["method"], scope["path"], has_reviewer_token=bool(headers.get("x-reviewer-token")), cfg=get_settings())
        declared = headers.get("content-length")
        if declared is not None:
            if not declared.strip().isdigit():
                await _send_json(send, 400, {"detail": "Invalid Content-Length header."}, {"Connection": "close"})
                return
            if int(declared) > limit:
                await self._too_large(send, limit)
                return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return  # the client went away before sending its body
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                await self._too_large(send, limit)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        if body and _is_json(headers.get("content-type")) and has_lone_surrogate(body):
            await _send_json(send, 422, {"detail": [{
                "type": "string_unicode", "loc": ["body"],
                "msg": "The request contains a lone UTF-16 surrogate (for example \\ud800 without its pair), which is not valid Unicode text.",
            }]})
            return
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()  # after the body: wait for the disconnect, as the server would

        await self.app(scope, replay, send)

    @staticmethod
    async def _too_large(send: Send, limit: int) -> None:
        await _send_json(send, 413, {"detail": f"Request body too large: this endpoint accepts at most {limit:,} bytes."}, {"Connection": "close"})
