"""Command-line interface: database setup, ingestion, review ledger, validation, reports, server."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import typer

from .config import describe_settings_sources, get_settings
from .db import (
    UnversionedSchemaError,
    drop_schema,
    get_engine,
    reset_derived_data,
    session_scope,
    upgrade_schema,
)

app = typer.Typer(add_completion=False, help="IncretinOS — clinical evidence and trial intelligence (public data only).")
review_app = typer.Typer(add_completion=False, help="Evidence review: status, reconcile, and backup / restore of the decision ledger.")
app.add_typer(review_app, name="review")

_RESET_HELP = (
    "Empty the tables that are rebuilt from sources and curation (studies, results, analyses, adverse events, claims, drugs ...) first. "
    "Review decisions, evidence version history, the audit log, source snapshots and change events are kept."
)
_WIPE_HELP = (
    "With --reset: ALSO destroy review decisions, evidence version history, the audit log, source snapshots and change events by dropping "
    "every table. Irreversible; a ledger backup of the decisions is written first."
)


def _database_label() -> str:
    """The configured database without its password."""
    from sqlalchemy.engine import make_url

    return make_url(get_settings().database_url).render_as_string(hide_password=True)


def _schema_at_head() -> None:
    """Bring the schema to the migration head. The only way any command creates or changes tables."""
    try:
        upgrade_schema()
    except UnversionedSchemaError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _migrate(reset: bool = False, wipe_review_history: bool = False) -> None:
    """Shared by `init-db` and `seed`: one schema path, and a reset that keeps what people wrote.

    * no flag: run the migrations (creates the schema, or upgrades an older one in place);
    * ``--reset``: run the migrations, then empty the derived tables. Review decisions, evidence version
      history, the audit log, source snapshots and change events are kept;
    * ``--reset --wipe-review-history``: back up the decision ledger, drop every table, run the migrations.
    """
    typer.echo(describe_settings_sources(), err=True)
    if wipe_review_history and not reset:
        raise typer.BadParameter("--wipe-review-history only has an effect together with --reset.")
    if reset and wipe_review_history:
        backup = _backup_ledger_before_wipe()
        drop_schema()
        typer.echo("Dropped every table, including review decisions, version history, the audit log and source snapshots." + (f" Decision ledger backed up to {backup}." if backup else ""), err=True)
    _schema_at_head()
    if reset and not wipe_review_history:
        with session_scope() as session:
            removed = reset_derived_data(session)
        typer.echo(f"Emptied {sum(1 for n in removed.values() if n)} derived table(s) ({sum(removed.values())} rows). Review decisions, version history, audit log and snapshots were kept.", err=True)


def _backup_ledger_before_wipe() -> Path | None:
    """Write the decision ledger next to the database cache before a wipe. Returns the file, or None if there is nothing to save."""
    from sqlalchemy import func, inspect, select

    from .models import ReviewDecision
    from .review.ledger import write_ledger

    inspector = inspect(get_engine())
    if not inspector.has_table("review_decision") or "uid" not in {c["name"] for c in inspector.get_columns("review_decision")}:
        return None  # no evidence-level decisions can exist in this database
    with session_scope() as session:
        if not session.scalar(select(func.count()).select_from(ReviewDecision).where(ReviewDecision.evidence_type.is_not(None))):
            return None
        target = get_settings().cache_dir.parent / f"review-ledger-before-wipe-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        write_ledger(session, target)
    return target


@app.command("init-db")
def init_db(
    reset: bool = typer.Option(False, help=_RESET_HELP),
    wipe_review_history: bool = typer.Option(False, "--wipe-review-history", help=_WIPE_HELP),
) -> None:
    """Create or upgrade the database schema by running the migrations."""
    _migrate(reset, wipe_review_history)
    typer.echo(f"Database schema is at the migration head: {_database_label()}")


@app.command()
def seed(
    live: bool = typer.Option(False, help="Fetch from the public APIs instead of committed fixtures."),
    record: bool = typer.Option(False, help="With --live: write responses to the fixture directory."),
    reset: bool = typer.Option(False, help=_RESET_HELP),
    wipe_review_history: bool = typer.Option(False, "--wipe-review-history", help=_WIPE_HELP),
    demo_change: bool = typer.Option(True, help="Seed one labelled synthetic prior registry version to demonstrate change review."),
    only: list[str] = typer.Option(None, help="Restrict study ingestion to these NCT IDs."),
) -> None:
    """Ingest the watchlist (registry, PubMed, labels, FAERS), load curated evidence, re-apply review decisions, detect gaps."""
    from .ingestion.pipeline import Pipeline

    _migrate(reset, wipe_review_history)
    settings = get_settings()
    with session_scope() as session:
        report = Pipeline(session, settings, offline=not live, record=record).run(demo_change=demo_change, only=only or None)
    typer.echo(json.dumps(report.as_dict(), indent=1, default=str))
    if report.failures:
        raise typer.Exit(code=2)


@app.command("check-updates")
def check_updates(live: bool = typer.Option(False, help="Query the live registry (otherwise fixtures).")) -> None:
    """Re-fetch watched registry records and labels; report changed sources and changed stored evidence for review."""
    from .ingestion.pipeline import Pipeline

    settings = get_settings()
    with session_scope() as session:
        pipeline = Pipeline(session, settings, offline=not live)
        # Each step commits together with its reconcile pass (see Pipeline._run): re-extracted rows are re-keyed,
        # decisions re-applied and changed values reported before anything is visible to readers.
        pipeline._run("ctgov", lambda: pipeline.ingest_studies(demo_change=False))
        pipeline._run("openfda-label", pipeline.ingest_labels)
        typer.echo(json.dumps({
            "changes": pipeline.report.changes,
            "evidence_changes": pipeline.report.evidence_changes,
            "review": pipeline.report.review,
            "failures": pipeline.report.failures,
        }, indent=1, default=str))
    if pipeline.report.failures:
        raise typer.Exit(code=2)


@app.command()
def validate() -> None:
    """Run the scientific validation rules against the current database."""
    from .engines.validation import run_all

    with session_scope() as session:
        results = run_all(session)
    failed = [r for r in results if not r.passed]
    for result in results:
        typer.echo(f"[{'PASS' if result.passed else 'FAIL'}] {result.rule_id} — {result.title}")
        for violation in result.violations:
            typer.echo(f"        · {violation}")
    if failed:
        raise typer.Exit(code=1)


@app.command("anchor-web")
def anchor_web() -> None:
    """Fetch curated web sources and verify their locator quotes (writes web_anchors.json)."""
    from .curation.web_anchor import anchor_all

    summary = anchor_all(get_settings().curated_dir)
    typer.echo(json.dumps(summary, indent=1))


@app.command("report")
def report(
    a: int = typer.Argument(..., help="Result id (finding A)."),
    b: int = typer.Argument(..., help="Result id (finding B)."),
    out: Path = typer.Option(None, "--out", "-o", help="Write Markdown to this file."),
) -> None:
    """Export a cited comparison report for two findings."""
    from .reports.export import comparison_report_markdown

    with session_scope() as session:
        markdown = comparison_report_markdown(session, a, b)
    if out:
        out.write_text(markdown, encoding="utf-8")
        typer.echo(f"Wrote {out}")
    else:
        typer.echo(markdown)


@app.command("benchmark-claims")
def benchmark_claims(
    which: str = typer.Option("dev", "--set", help="Benchmark set to run: dev, heldout or all."),
    as_json: bool = typer.Option(False, "--json", help="Print the full report as JSON."),
) -> None:
    """Run the adversarial claim-verification benchmark against the current database."""
    from .assistant import benchmark

    if which not in (*benchmark.SETS, "all"):
        raise typer.BadParameter("--set must be dev, heldout or all")
    with session_scope() as session:
        reports = benchmark.run(session, which)
    if as_json:
        typer.echo(json.dumps(reports, indent=1, default=str))
    else:
        for report in reports:
            typer.echo(benchmark.format_report(report))
    if any(report.get("errors") for report in reports):
        raise typer.Exit(code=2)  # a case that could not be run is a defect in the set, not a detection


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Run the API server."""
    import uvicorn

    typer.echo(describe_settings_sources(), err=True)
    # proxy_headers=False: uvicorn would otherwise replace the client address with X-Forwarded-For for every
    # connection from 127.0.0.1, which is where the web application's proxy connects from, and the browser can
    # forge that header. Trusted proxies are declared to the application instead (RATE_LIMIT_TRUSTED_PROXIES).
    uvicorn.run("incretinos.api.main:app", host=host, port=port, reload=reload, proxy_headers=False)


# --------------------------------------------------------------------------- review


@review_app.command("status")
def review_status(as_json: bool = typer.Option(False, "--json", help="Print the full summary as JSON.")) -> None:
    """Show evidence by verification status, open requests, orphaned decisions and rows changed since review. Read-only."""
    from .review.queries import summarize

    with session_scope() as session:
        summary = summarize(session)
    if as_json:
        typer.echo(json.dumps(summary, indent=1, default=str))
        return
    typer.echo(f"Reviewable evidence rows: {summary['total']}   decisions recorded: {summary['decisions_recorded']}")
    for status, n in summary["by_status"].items():
        typer.echo(f"  {summary['status_labels'][status]:<42} {n:>7}")
    typer.echo("By type:")
    for evidence_type, counts in summary["by_type"].items():
        typer.echo(f"  {evidence_type:<14} " + ", ".join(f"{status} {n}" for status, n in sorted(counts.items())))
    typer.echo(f"Open requests: {summary['open_requests_total']}   changed since review: {summary['changed_since_review_total']}   "
               f"changed since last seen: {summary['changed_since_last_seen_total']}   orphaned decisions: {summary['orphaned_decisions_total']}")
    for orphan in summary["orphaned_decisions"][:20]:
        typer.echo(f"  orphaned: {orphan['decision']} by {orphan['reviewer']} on {orphan['evidence_type']} {orphan['evidence_key'][:12]}… ({orphan['label'] or 'evidence no longer stored'})")
    if summary["identity_collisions"]:
        typer.echo(f"Rows whose natural key needed a tie-breaker (data-quality finding): {summary['identity_collisions']}")
    if summary["unreconciled_rows"]:
        typer.echo(f"NOT RECONCILED (run `incretinos review reconcile`): {summary['unreconciled_rows']}")


@review_app.command("reconcile")
def review_reconcile(as_json: bool = typer.Option(False, "--json", help="Print the full report, including every changed row, as JSON.")) -> None:
    """Re-key evidence, re-validate anchoring, re-derive every status from the decision ledger and report what changed. Idempotent."""
    from .review.lifecycle import reconcile

    _schema_at_head()
    with session_scope() as session:
        outcome = reconcile(session)
    typer.echo(json.dumps(outcome.as_dict() if as_json else outcome.summary(), indent=1, default=str))


@review_app.command("export")
def review_export(file: Path = typer.Argument(..., help="Where to write the JSON ledger.")) -> None:
    """Write every evidence decision, with the evidence version it was made on, to a JSON ledger file."""
    from .review.ledger import write_ledger

    with session_scope() as session:
        n = write_ledger(session, file)
    typer.echo(f"Wrote {n} decision(s) to {file}")


@review_app.command("import")
def review_import(file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="A ledger written by `review export`.")) -> None:
    """Add the decisions of a ledger file to this database and re-derive every status. Validated; safe to repeat."""
    from .review.ledger import LedgerError, import_ledger, read_ledger_file

    _schema_at_head()
    try:
        data, sha256 = read_ledger_file(file)
        with session_scope() as session:
            outcome = import_ledger(session, data, file_sha256=sha256)
    except LedgerError as exc:
        typer.echo("Ledger rejected; nothing was imported:", err=True)
        for problem in exc.problems[:50]:
            typer.echo(f"  · {problem}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(outcome.as_dict(), indent=1, default=str))


if __name__ == "__main__":  # pragma: no cover
    app()
