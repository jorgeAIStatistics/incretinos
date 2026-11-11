"""The one reviewer check of the HTTP API.

It lives in its own module so that ``api.main`` and every router depend on the same function object:
a route is recognised as reviewer-only by that identity (tests/test_hardening.py walks the route
table), and a fix to the check (for example the byte-wise token comparison) protects every router.

Two credential modes (see ``config.Settings``):

* personal reviewer tokens (``INCRETINOS_REVIEWERS_FILE``): the token identifies the reviewer; the name recorded
  with a decision is the server's, never the client's;
* one shared token (``INCRETINOS_REVIEWER_TOKEN``, development): the client states a name, and every decision is
  labelled as self-declared.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Header, HTTPException

from ..config import ReviewerCredentialsError, ReviewerIdentity, get_settings
from ..review.policy import IDENTITY_AUTHENTICATED, normalise_reviewer, reviewer_problem


def require_reviewer(x_reviewer_token: Annotated[str | None, Header()] = None) -> ReviewerIdentity:
    """Least privilege: mutations need a configured credential; without one the API is read-only.

    Never raises anything but 403 or 401 (or 503 for an unusable credentials file): the comparison is on bytes,
    so a header with non-ASCII or otherwise malformed content is simply a wrong token. Routes that write must
    depend on this function; the database session of a request that would not pass it cannot commit (see
    ``db.get_db``).
    """
    cfg = get_settings()
    if not cfg.review_enabled:
        raise HTTPException(status_code=403, detail="This deployment is read-only: no reviewer token is configured.")
    if cfg.reviewers_file:
        try:
            cfg.reviewer_credentials()
        except ReviewerCredentialsError as exc:
            raise HTTPException(status_code=503, detail="Reviewer credentials are misconfigured on the server; no decision can be recorded.") from exc
    identity = cfg.authenticate_reviewer(x_reviewer_token)
    if identity is None:
        raise HTTPException(status_code=401, detail="A valid reviewer token is required for this action.")
    return identity


def reviewer_name(identity: ReviewerIdentity, claimed: str | None) -> str:
    """The reviewer name to record for a request that passed ``require_reviewer``. Raises 422 when unacceptable.

    With a personal token the name is the server's; a name sent by the client must be the same person (after
    normalisation) or the request is refused. Under the shared token the client's name is used, after the
    reserved-name and plausibility checks.
    """
    if identity.basis == IDENTITY_AUTHENTICATED and identity.name:
        if claimed is not None and normalise_reviewer(claimed).casefold() != identity.name.casefold():
            raise HTTPException(status_code=422, detail="The reviewer name is taken from your reviewer token; omit 'reviewer' or send your own name.")
        return identity.name
    if claimed is None:
        raise HTTPException(status_code=422, detail="'reviewer' is required: with the shared development token the reviewer states a name.")
    problem = reviewer_problem(claimed)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    return normalise_reviewer(claimed)
