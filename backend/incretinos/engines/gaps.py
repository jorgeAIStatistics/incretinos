"""Evidence Gap Engine (rules are added in a later module; this keeps the pipeline importable)."""

from __future__ import annotations

from sqlalchemy.orm import Session


def refresh_gaps(session: Session) -> int:
    return 0
