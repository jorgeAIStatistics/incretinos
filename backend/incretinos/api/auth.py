"""The one reviewer check of the HTTP API.

It lives in its own module so that ``api.main`` and every router depend on the same function object:
a route is recognised as reviewer-only by that identity (tests/test_hardening.py walks the route
table), and a fix to the check (for example the byte-wise token comparison) protects every router.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Header, HTTPException

from ..config import get_settings


def require_reviewer(x_reviewer_token: Annotated[str | None, Header()] = None) -> str:
    """Least privilege: mutations need a configured token; without one the API is read-only.

    Never raises anything but 403 or 401: the comparison is on bytes, so a header with non-ASCII or otherwise
    malformed content is simply a wrong token. Routes that write must depend on this function; the database
    session of a request that would not pass it cannot commit (see ``db.get_db``).
    """
    cfg = get_settings()
    if not cfg.reviewer_token:
        raise HTTPException(status_code=403, detail="This deployment is read-only: no reviewer token is configured.")
    if not cfg.reviewer_token_matches(x_reviewer_token):
        raise HTTPException(status_code=401, detail="A valid reviewer token is required for this action.")
    return "reviewer"
