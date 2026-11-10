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
    "every table. A full backup of every table and a decision ledger are written first, and the wipe is recorded in the new database."
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
    * ``--reset --wipe-review-history``: back up every table and the decision ledger, drop every table, run the
      migrations, and record the wipe (with the backup's location and checksums) in the new audit log.
    """
    typer.echo(describe_settings_sources(), err=True)
    if wipe_review_history and not reset:
        raise typer.BadParameter("--wipe-review-history only has an effect together with --reset.")
    wiped: dict | None = None
    if reset and wipe_review_history:
        wiped = _backup_before_wipe()
        drop_schema()
        typer.echo("Dropped every table, including review decisions, version history, the audit log and source snapshots." +
                   (f" Everything was backed up first to {wiped['directory']}." if wiped else ""), err=True)
    _schema_at_head()
    if wiped:
        _record_wipe(wiped)
    if reset and not wipe_review_history:
        with session_scope() as session:
            removed = reset_derived_data(session)
        typer.echo(f"Emptied {sum(1 for n in removed.values() if n)} derived table(s) ({sum(removed.values())} rows). Review decisions, version history, audit log and snapshots were kept.", err=True)


def _backup_before_wipe() -> dict | None:
    """Back up everything ``drop_schema`` is about to destroy. Returns what was written, or None for an empty database.

    Written to ``<var>/wipe-backup-<UTC timestamp>/``:

    * ``tables/<table>.jsonl``: every row of every application table (review decisions of all kinds, the audit log,
      evidence versions, change events, queue items, gap triage, source snapshots, and the derived tables too);
    * ``review-ledger.json``: the evidence decisions as an importable ledger (``incretinos review import``);
    * ``manifest.json``: row counts and the sha256 of each file.

    Nothing is dropped unless the backup was written completely (an exception here aborts the command).
    """
    import hashlib

    from sqlalchemy import func, inspect, select

    from .db import Base
    from .models import ReviewDecision
    from .review.ledger import write_ledger

    engine = get_engine()
    present = set(inspect(engine).get_table_names())
    tables = [t for t in Base.metadata.sorted_tables if t.name in present]
    if not tables:
        return None
    directory = get_settings().cache_dir.parent / f"wipe-backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
    (directory / "tables").mkdir(parents=True, exist_ok=False)
    files: dict[str, str] = {}
    counts: dict[str, int] = {}

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    with engine.connect() as connection:
        for table in tables:
            existing = {c["name"] for c in inspect(engine).get_columns(table.name)}
            columns = [c for c in table.columns if c.name in existing]  # an older schema may lack newer columns
            path = directory / "tables" / f"{table.name}.jsonl"
            n = 0
            with path.open("w", encoding="utf-8") as out:
                for row in connection.execute(select(*columns)).mappings():
                    out.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")
                    n += 1
            counts[table.name] = n
            files[f"tables/{table.name}.jsonl"] = digest(path)
    decisions: dict[str, int] = {}
    ledger_decisions = 0
    if "review_decision" in present:
        existing = {c["name"] for c in inspect(engine).get_columns("review_decision")}
        if "uid" in existing:
            with session_scope() as session:
                for kind, evidence_type, n in session.execute(
                    select(ReviewDecision.decision, ReviewDecision.evidence_type, func.count()).group_by(ReviewDecision.decision, ReviewDecision.evidence_type)
                ):
                    label = f"{'evidence' if evidence_type else 'queue_or_gap'}:{kind}"
                    decisions[label] = decisions.get(label, 0) + n
                ledger_decisions = write_ledger(session, directory / "review-ledger.json")
            files["review-ledger.json"] = digest(directory / "review-ledger.json")
    manifest = {"created_at": datetime.now(UTC).isoformat(), "database": _database_label(), "row_counts": counts, "decisions": decisions,
                "ledger_decisions": ledger_decisions, "files": files}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return {**manifest, "directory": str(directory), "manifest_sha256": digest(directory / "manifest.json")}


def _record_wipe(wiped: dict) -> None:
    """Leave a trace of the wipe in the fresh database and say how to restore the decisions."""
    from .ingestion.snapshots import audit

    rejections = sum(n for label, n in wiped["decisions"].items() if label in ("evidence:REJECT", "evidence:FLAG_WRONG_ATTRIBUTION"))
    with session_scope() as session:
        audit(session, "review.history_wiped", actor="operator", target_type="database", target_id=wiped["database"],
              backup_directory=wiped["directory"], manifest_sha256=wiped["manifest_sha256"], files=wiped["files"], row_counts=wiped["row_counts"],
              decisions=wiped["decisions"], ledger_decisions=wiped["ledger_decisions"],
              note="Review history was dropped with `--reset --wipe-review-history`. Decisions are not re-applied automatically.")
    typer.echo(f"The wipe is recorded in the new audit log (review.history_wiped). Backup: {wiped['directory']}", err=True)
    if wiped["ledger_decisions"]:
        typer.echo(
            f"NOTE: {wiped['ledger_decisions']} evidence decision(s), {rejections} of them rejection(s), are NOT applied to the new database. "
            f"Rejected values are back in every analytic surface until you run: incretinos review import {wiped['directory']}/review-ledger.json",
            err=True,
        )


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
    from .review.lifecycle import aggregate_summaries, refresh_dependent_gaps

    settings = get_settings()
    with session_scope() as session:
        pipeline = Pipeline(session, settings, offline=not live)
        # Each step commits together with its reconcile pass (see Pipeline._run): re-extracted rows are re-keyed,
        # decisions re-applied and changed values reported before anything is visible to readers. Every step's
        # reconcile summary is kept: the registry step is where re-keying and orphaned decisions show up.
        steps: list[tuple[str, dict]] = []
        for name, step in (("ctgov", lambda: pipeline.ingest_studies(demo_change=False)), ("openfda-label", pipeline.ingest_labels)):
            before = pipeline.report.review
            pipeline._run(name, step)
            if pipeline.report.review is not before and pipeline.report.review:
                steps.append((name, pipeline.report.review))
        review = aggregate_summaries(steps)
        if review.get("exclusions_changed"):
            review["gaps_refreshed"] = refresh_dependent_gaps(session)
            session.commit()
        typer.echo(json.dumps({
            "changes": pipeline.report.changes,
            "evidence_changes": pipeline.report.evidence_changes,
            "review": review,
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
    typer.echo(f"Held out of use after a rejection (awaiting a new decision): {summary['held_total']}   "
               f"decisions carried to a relabelled row: {summary['decisions_carried_by_anchor_total']}")
    if summary["orphaned_rejections_total"]:
        typer.echo(f"WARNING: {summary['orphaned_rejections_total']} orphaned rejection(s): the excluded value may be in use again under another identity "
                   "(see the ORPHANED_DECISION items in the review queue).")
    for orphan in summary["orphaned_decisions"][:20]:
        typer.echo(f"  orphaned: {orphan['decision']} by {orphan['reviewer']} on {orphan['evidence_type']} {orphan['evidence_key'][:12]}… ({orphan['label'] or 'evidence no longer stored'})")
    if summary["identity_collisions"]:
        typer.echo(f"Rows whose natural key needed a tie-breaker (data-quality finding): {summary['identity_collisions']}")
    if summary["unreconciled_rows"]:
        typer.echo(f"NOT RECONCILED (run `incretinos review reconcile`): {summary['unreconciled_rows']}")


@review_app.command("reconcile")
def review_reconcile(
    as_json: bool = typer.Option(False, "--json", help="Print the full report, including every changed row, as JSON."),
    check: bool = typer.Option(False, "--check", help="Dry run: report what reconcile would change, roll everything back, and exit 1 when a stored "
                               "human status or hold is not supported by the decision ledger."),
) -> None:
    """Re-key evidence, re-validate anchoring, re-derive every status from the decision ledger and report what changed. Idempotent.

    Every stored status it has to correct is audited (review.status_repaired) with the stored and the derived value.
    """
    from .db import get_sessionmaker
    from .review.lifecycle import reconcile, refresh_dependent_gaps

    _schema_at_head()
    if check:
        session = get_sessionmaker()()
        try:
            outcome = reconcile(session)
        finally:
            session.rollback()
            session.close()
        typer.echo(json.dumps({**outcome.summary(), "repairs": outcome.repairs, "dry_run": True}, indent=1, default=str))
        if outcome.repairs_total:
            raise typer.Exit(code=1)
        return
    with session_scope() as session:
        outcome = reconcile(session)
        summary = outcome.summary()
        if outcome.exclusions_changed:
            summary["gaps_refreshed"] = refresh_dependent_gaps(session)
    typer.echo(json.dumps({**outcome.as_dict(), **summary} if as_json else {**summary, "repairs": outcome.repairs}, indent=1, default=str))


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
