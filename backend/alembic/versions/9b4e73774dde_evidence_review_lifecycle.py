"""evidence review lifecycle

Revision ID: 9b4e73774dde
Revises: 10d1699882ea
Create Date: 2026-10-13 19:51:24.035891

What this revision does to an existing database

* widens every ``verification_status`` column from 30 to 40 characters
  (SOURCE_CHANGED_RECHECK_REQUIRED has 31 and would not fit on PostgreSQL);
* renames the stored status CURATED_PENDING_REVIEW to PENDING_HUMAN_REVIEW in every table that has
  the column and in the "verification" entries inside ``population.summary``;
* adds the review-lifecycle columns to the seven reviewable evidence tables (stable evidence key,
  content fingerprint, mechanical status, source snapshot, automated check flags, risk level);
* extends ``review_decision`` so a decision names the exact evidence version it was made on, and
  creates ``evidence_version`` (the history of every evidence version ever seen);
* retires open review-queue items of the CURATED_CLAIM type: they pointed at a source rather than
  at the evidence under review. They are kept, with any decision recorded on them, as history.

It marks nothing as human-verified or rejected. The new columns are filled by
``incretinos review reconcile`` (the container entry step runs it on every start).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "9b4e73774dde"
down_revision: str | Sequence[str] | None = "10d1699882ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVIEWABLE_TABLES = (
    "outcome_result",
    "statistical_analysis",
    "adverse_event",
    "evidence_claim",
    "drug_mechanism",
    "indication",
    "exposure_equivalence",
)
LEGACY_PENDING = "CURATED_PENDING_REVIEW"
PENDING = "PENDING_HUMAN_REVIEW"


def _rename_population_summary_status(old: str, new: str) -> None:
    """Rewrite the "verification" entries of the population.summary JSON documents (needs a live connection)."""
    if context.is_offline_mode():
        op.execute(f"-- population.summary JSON: rename verification '{old}' to '{new}' with `alembic upgrade` online")
        return
    bind = op.get_bind()
    population = sa.table("population", sa.column("id", sa.Integer), sa.column("summary", sa.JSON))
    for row_id, summary in bind.execute(sa.select(population.c.id, population.c.summary)).all():
        if not isinstance(summary, dict):
            continue
        changed = False
        for name, entry in summary.items():
            if isinstance(entry, dict) and entry.get("verification") == old:
                summary[name] = {**entry, "verification": new}
                changed = True
        if changed:
            bind.execute(population.update().where(population.c.id == row_id).values(summary=summary))


def upgrade() -> None:
    op.create_table(
        "evidence_version",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("evidence_type", sa.String(length=20), nullable=False),
        sa.Column("evidence_key", sa.String(length=64), nullable=False),
        sa.Column("natural_key", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=True),
        sa.Column("origin", sa.String(length=10), nullable=True),
        sa.Column("study_nct", sa.String(length=11), nullable=True),
        sa.Column("study_name", sa.Text(), nullable=True),
        sa.Column("drug_slugs", sa.Text(), nullable=True),
        sa.Column("source_key", sa.String(length=300), nullable=True),
        sa.Column("source_type", sa.String(length=40), nullable=True),
        sa.Column("tier", sa.String(length=40), nullable=True),
        sa.Column("endpoint_category", sa.String(length=30), nullable=True),
        sa.Column("missing_fields", sa.Text(), nullable=True),
        sa.Column("collision_level", sa.Integer(), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=True),
        sa.Column("source_snapshot_version", sa.Integer(), nullable=True),
        sa.Column("last_source_content_hash", sa.String(length=64), nullable=True),
        sa.Column("last_source_snapshot_version", sa.Integer(), nullable=True),
        sa.Column("mechanical_status", sa.String(length=40), nullable=True),
        sa.Column("effective_status", sa.String(length=40), nullable=True),
        sa.Column("risk_level", sa.String(length=10), nullable=True),
        sa.Column("check_flags", sa.JSON(), nullable=True),
        sa.Column("supersedes_id", sa.Integer(), nullable=True),
        sa.Column("changed_fields", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_since", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["supersedes_id"], ["evidence_version.id"], name="fk_evidence_version_supersedes"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evidence_type", "evidence_key", "fingerprint", name="uq_evidence_version_identity"),
    )
    op.create_index("ix_evidence_version_current", "evidence_version", ["is_current", "evidence_type"], unique=False)
    op.create_index(op.f("ix_evidence_version_evidence_key"), "evidence_version", ["evidence_key"], unique=False)
    op.create_index(op.f("ix_evidence_version_natural_key"), "evidence_version", ["natural_key"], unique=False)
    op.create_index(op.f("ix_evidence_version_study_nct"), "evidence_version", ["study_nct"], unique=False)

    # Batch mode: SQLite cannot ALTER a column type or add a constraint in place, so each table is rebuilt there.
    # None of these tables is referenced by another table, so rebuilding them cannot cascade.
    for table in REVIEWABLE_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column("verification_status", existing_type=sa.String(length=30), type_=sa.String(length=40), existing_nullable=False)
            batch.add_column(sa.Column("evidence_key", sa.String(length=64), nullable=True))
            batch.add_column(sa.Column("fingerprint", sa.String(length=64), nullable=True))
            batch.add_column(sa.Column("base_verification_status", sa.String(length=40), nullable=True))
            batch.add_column(sa.Column("check_flags", sa.JSON(), nullable=True))
            batch.add_column(sa.Column("risk_level", sa.String(length=10), nullable=True))
            batch.add_column(sa.Column("source_snapshot_id", sa.Integer(), nullable=True))
            batch.create_index(op.f(f"ix_{table}_evidence_key"), ["evidence_key"], unique=True)
            batch.create_index(op.f(f"ix_{table}_source_snapshot_id"), ["source_snapshot_id"], unique=False)
            batch.create_foreign_key(f"fk_{table}_source_snapshot", "source_snapshot", ["source_snapshot_id"], ["id"], ondelete="SET NULL")
        op.execute(sa.text(f"UPDATE {table} SET verification_status = :new WHERE verification_status = :old").bindparams(new=PENDING, old=LEGACY_PENDING))
    _rename_population_summary_status(LEGACY_PENDING, PENDING)

    with op.batch_alter_table("review_decision") as batch:
        batch.alter_column("decision", existing_type=sa.String(length=20), type_=sa.String(length=30), existing_nullable=False)
        batch.add_column(sa.Column("uid", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("evidence_type", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("evidence_key", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("natural_key", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("fingerprint", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("evidence_version_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("source_key", sa.String(length=300), nullable=True))
        batch.add_column(sa.Column("source_content_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("source_snapshot_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("proposed_correction", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("actor_kind", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("attested", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("status_before", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("status_after", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("imported_from", sa.String(length=80), nullable=True))
        batch.create_index("ix_review_decision_evidence", ["evidence_type", "evidence_key"], unique=False)
        batch.create_index(op.f("ix_review_decision_natural_key"), ["natural_key"], unique=False)
        batch.create_index(op.f("ix_review_decision_uid"), ["uid"], unique=True)
        batch.create_foreign_key("fk_review_decision_evidence_version", "evidence_version", ["evidence_version_id"], ["id"])

    op.execute(
        sa.text("UPDATE review_queue_item SET status = 'RETIRED' WHERE item_type = 'CURATED_CLAIM' AND status IN ('OPEN', 'IN_REVIEW')")
    )


def downgrade() -> None:
    """Back to the schema without evidence-level review.

    Evidence decisions lose the columns that tie them to an evidence version, the version history is
    dropped, and rows that were human-verified, rejected or awaiting a recheck go back to their
    mechanical status, because the earlier schema cannot express anything else. Export the ledger
    first (`incretinos review export FILE`) if the decisions matter.
    """
    op.execute(sa.text("UPDATE review_queue_item SET status = 'OPEN' WHERE item_type = 'CURATED_CLAIM' AND status = 'RETIRED'"))

    with op.batch_alter_table("review_decision") as batch:
        batch.drop_constraint("fk_review_decision_evidence_version", type_="foreignkey")
        batch.drop_index(op.f("ix_review_decision_uid"))
        batch.drop_index(op.f("ix_review_decision_natural_key"))
        batch.drop_index("ix_review_decision_evidence")
        for column in ("imported_from", "status_after", "status_before", "attested", "actor_kind", "proposed_correction", "source_snapshot_version",
                       "source_content_hash", "source_key", "evidence_version_id", "fingerprint", "natural_key", "evidence_key", "evidence_type", "uid"):
            batch.drop_column(column)
        batch.alter_column("decision", existing_type=sa.String(length=30), type_=sa.String(length=20), existing_nullable=False)

    _rename_population_summary_status(PENDING, LEGACY_PENDING)
    for table in REVIEWABLE_TABLES:
        op.execute(sa.text(
            f"UPDATE {table} SET verification_status = COALESCE(base_verification_status, :pending) "
            "WHERE verification_status IN ('HUMAN_VERIFIED', 'REJECTED', 'SOURCE_CHANGED_RECHECK_REQUIRED')"
        ).bindparams(pending=PENDING))
        op.execute(sa.text(f"UPDATE {table} SET verification_status = :old WHERE verification_status = :new").bindparams(new=PENDING, old=LEGACY_PENDING))
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"fk_{table}_source_snapshot", type_="foreignkey")
            batch.drop_index(op.f(f"ix_{table}_source_snapshot_id"))
            batch.drop_index(op.f(f"ix_{table}_evidence_key"))
            for column in ("source_snapshot_id", "risk_level", "check_flags", "base_verification_status", "fingerprint", "evidence_key"):
                batch.drop_column(column)
            batch.alter_column("verification_status", existing_type=sa.String(length=40), type_=sa.String(length=30), existing_nullable=False)

    op.drop_index(op.f("ix_evidence_version_study_nct"), table_name="evidence_version")
    op.drop_index(op.f("ix_evidence_version_natural_key"), table_name="evidence_version")
    op.drop_index(op.f("ix_evidence_version_evidence_key"), table_name="evidence_version")
    op.drop_index("ix_evidence_version_current", table_name="evidence_version")
    op.drop_table("evidence_version")
