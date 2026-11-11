"""Container entry step: seed the database from offline fixtures if it is empty.

A database that is already seeded is not reloaded, but the review lifecycle is reconciled on
every start: after a schema upgrade the existing evidence rows have no evidence key yet, and
reconciling is what gives them one. It is idempotent and takes a few seconds.
"""

from __future__ import annotations

from sqlalchemy import func, select

from .config import get_settings
from .db import session_scope
from .models import Study


def main() -> None:
    settings = get_settings()
    with session_scope() as session:
        if session.scalar(select(func.count()).select_from(Study)):
            from .review.lifecycle import reconcile

            outcome = reconcile(session)
            print(f"Database already seeded; skipping the load. Review state reconciled: {outcome.rows} rows, {outcome.rows_updated} updated, {outcome.transitions_total} status change(s).")
            return
        from .ingestion.pipeline import Pipeline

        report = Pipeline(session, settings, offline=settings.offline).run()
        print(f"Seeded {len(report.studies)} studies ({report.mode}); failures: {len(report.failures)}")


if __name__ == "__main__":
    main()
