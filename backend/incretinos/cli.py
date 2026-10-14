"""Command-line interface: database setup, ingestion, validation, reports, server."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .config import describe_settings_sources, get_settings
from .db import UnversionedSchemaError, drop_schema, session_scope, upgrade_schema

app = typer.Typer(add_completion=False, help="IncretinOS — clinical evidence and trial intelligence (public data only).")


def _database_label() -> str:
    """The configured database without its password."""
    from sqlalchemy.engine import make_url

    return make_url(get_settings().database_url).render_as_string(hide_password=True)


def _migrate(reset: bool) -> None:
    """Bring the schema to the migration head. Every command that needs tables goes through here."""
    typer.echo(describe_settings_sources(), err=True)
    if reset:
        drop_schema()
    try:
        upgrade_schema()
    except UnversionedSchemaError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("init-db")
def init_db(reset: bool = typer.Option(False, help="Drop all tables first.")) -> None:
    """Create or upgrade the database schema by running the migrations."""
    _migrate(reset)
    typer.echo(f"Database schema is at the migration head: {_database_label()}")


@app.command()
def seed(
    live: bool = typer.Option(False, help="Fetch from the public APIs instead of committed fixtures."),
    record: bool = typer.Option(False, help="With --live: write responses to the fixture directory."),
    reset: bool = typer.Option(False, help="Drop and recreate all tables first."),
    demo_change: bool = typer.Option(True, help="Seed one labelled synthetic prior registry version to demonstrate change review."),
    only: list[str] = typer.Option(None, help="Restrict study ingestion to these NCT IDs."),
) -> None:
    """Ingest the watchlist (registry, PubMed, labels, FAERS), load curated evidence, detect gaps."""
    from .ingestion.pipeline import Pipeline

    _migrate(reset)
    settings = get_settings()
    with session_scope() as session:
        report = Pipeline(session, settings, offline=not live, record=record).run(demo_change=demo_change, only=only or None)
    typer.echo(json.dumps(report.as_dict(), indent=1, default=str))
    if report.failures:
        raise typer.Exit(code=2)


@app.command("check-updates")
def check_updates(live: bool = typer.Option(False, help="Query the live registry (otherwise fixtures).")) -> None:
    """Re-fetch watched registry records and labels; report changed sources for review."""
    from .ingestion.pipeline import Pipeline

    settings = get_settings()
    with session_scope() as session:
        pipeline = Pipeline(session, settings, offline=not live)
        pipeline.ingest_studies(demo_change=False)
        pipeline.ingest_labels()
        session.commit()
        typer.echo(json.dumps({"changes": pipeline.report.changes, "failures": pipeline.report.failures}, indent=1))


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


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Run the API server."""
    import uvicorn

    typer.echo(describe_settings_sources(), err=True)
    uvicorn.run("incretinos.api.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":  # pragma: no cover
    app()
