"""Container entry step: seed the database from offline fixtures if it is empty."""

from __future__ import annotations

from sqlalchemy import func, select

from .config import get_settings
from .db import session_scope
from .models import Study


def main() -> None:
    settings = get_settings()
    with session_scope() as session:
        if session.scalar(select(func.count()).select_from(Study)):
            print("Database already seeded; skipping.")
            return
        from .ingestion.pipeline import Pipeline

        report = Pipeline(session, settings, offline=settings.offline).run()
        print(f"Seeded {len(report.studies)} studies ({report.mode}); failures: {len(report.failures)}")


if __name__ == "__main__":
    main()
