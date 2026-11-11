"""data integrity: analysis direction and class, estimand origin, participant flow

Revision ID: 65bce81e7f45
Revises: 10d1699882ea
Create Date: 2026-10-13 19:31:23.870733

Additive only:

* ``participant_flow`` — registry participant flow as posted (every milestone and withdrawal reason per period
  and group, with the number who started the period).
* ``statistical_analysis.group_labels / direction_basis / group_description / class_title`` — the compared groups
  in posted order, how treatment and comparator were told apart, the registry's groupDescription verbatim and the
  outcome class the analysis belongs to.
* ``estimand.origin`` — REGISTRY (rebuilt by the registry adapter) or CURATED (owned by the curated files).
  Existing rows are classified by their source: an estimand citing a non-registry source came from a curated file.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "65bce81e7f45"
down_revision: Union[str, Sequence[str], None] = "10d1699882ea"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "participant_flow",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("study_id", sa.Integer(), nullable=False),
        sa.Column("arm_id", sa.Integer(), nullable=True),
        sa.Column("origin", sa.String(length=10), nullable=False),
        sa.Column("arm_label", sa.Text(), nullable=False),
        sa.Column("period_index", sa.Integer(), nullable=False),
        sa.Column("period_title", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("n_participants", sa.Integer(), nullable=True),
        sa.Column("n_started", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("source_location", sa.Text(), nullable=True),
        sa.Column("tier", sa.String(length=40), nullable=False),
        sa.Column("verification_status", sa.String(length=30), nullable=False),
        sa.Column("missing_fields", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["arm_id"], ["study_arm.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_id"], ["evidence_source.id"]),
        sa.ForeignKeyConstraint(["study_id"], ["study.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_participant_flow_kind"), "participant_flow", ["kind"], unique=False)
    op.create_index(op.f("ix_participant_flow_source_id"), "participant_flow", ["source_id"], unique=False)
    op.create_index(op.f("ix_participant_flow_study_id"), "participant_flow", ["study_id"], unique=False)

    # Batch mode: SQLite cannot ALTER a table in place for every operation; other dialects run plain ALTERs.
    with op.batch_alter_table("estimand") as batch:
        batch.add_column(sa.Column("origin", sa.String(length=10), server_default="REGISTRY", nullable=False))
    with op.batch_alter_table("statistical_analysis") as batch:
        batch.add_column(sa.Column("group_labels", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("direction_basis", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("group_description", sa.Text(), nullable=True))
        batch.add_column(sa.Column("class_title", sa.Text(), nullable=True))

    # Estimands that cite a publication, label or web source were loaded from curated files.
    op.execute(
        "UPDATE estimand SET origin = 'CURATED' WHERE source_id IN "
        "(SELECT id FROM evidence_source WHERE source_type <> 'CTGOV')"
    )


def downgrade() -> None:
    with op.batch_alter_table("statistical_analysis") as batch:
        batch.drop_column("class_title")
        batch.drop_column("group_description")
        batch.drop_column("direction_basis")
        batch.drop_column("group_labels")
    with op.batch_alter_table("estimand") as batch:
        batch.drop_column("origin")
    op.drop_index(op.f("ix_participant_flow_study_id"), table_name="participant_flow")
    op.drop_index(op.f("ix_participant_flow_source_id"), table_name="participant_flow")
    op.drop_index(op.f("ix_participant_flow_kind"), table_name="participant_flow")
    op.drop_table("participant_flow")
