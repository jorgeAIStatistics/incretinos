"""Scheduled re-ingestion of tracked sources (live mode only).

Disabled by default. Set INCRETINOS_REFRESH_HOURS to a positive number and
INCRETINOS_OFFLINE=false to have the API process re-fetch watched registry records and
labels on that interval. Changed sources become ChangeEvents and review-queue items.

Registry rows are re-extracted on every run, so each step is followed in the same transaction by
the review lifecycle's reconcile pass (the hook lives in ``Pipeline._run``): a stored value that
differs from the one last seen becomes a HIGH-significance change event with the old and new value,
an approval of the old value turns into "recheck required", and rejected rows stay rejected.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from ..config import Settings
from ..db import session_scope

log = logging.getLogger("incretinos.scheduler")


def refresh_tracked_sources(settings: Settings) -> dict:
    from .pipeline import Pipeline

    with session_scope() as session:
        pipeline = Pipeline(session, settings, offline=False)
        pipeline._run("ctgov", lambda: pipeline.ingest_studies(demo_change=False))
        pipeline._run("openfda-label", pipeline.ingest_labels)
        pipeline._run("gap-engine", pipeline.refresh_gaps)
        report = {
            "changes": pipeline.report.changes,
            "failures": pipeline.report.failures,
            "evidence_changes": pipeline.report.evidence_changes,
            "review": pipeline.report.review,
        }
    log.info(
        "scheduled refresh finished: %s source change(s), %s changed evidence row(s), %s failure(s)",
        len(report["changes"]), len(report["evidence_changes"]), len(report["failures"]),
    )
    return report


def start_scheduler(settings: Settings) -> BackgroundScheduler | None:
    if settings.offline or settings.refresh_hours <= 0:
        return None
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(refresh_tracked_sources, "interval", hours=settings.refresh_hours, args=[settings], id="refresh-tracked-sources", max_instances=1, coalesce=True)
    scheduler.start()
    return scheduler
