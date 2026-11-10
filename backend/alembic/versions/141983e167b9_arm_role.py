"""arm role: stored role of each arm, and the arm type as registered

Revision ID: 141983e167b9
Revises: 17814f6d32bf
Create Date: 2026-11-08 22:40:00.000000

Additive only, on ``study_arm``:

* ``arm_role`` / ``arm_role_basis`` — what the arm is in its trial (PLACEBO, ACTIVE_COMPARATOR, STUDY_DRUG,
  SEQUENCE, UNCLASSIFIED) and why, derived from stored arm data and the trial's drug roles
  (``vocab.classify_arm_role``), so every page reads one value instead of re-deriving it.
* ``registered_arm_type`` — the arm type exactly as the registry posts it. ``arm_type`` may differ for a
  placebo/active switch arm, which is stored as ``SEQUENCE`` so that nothing treats it as a placebo arm.

Existing rows keep NULL until the next ingest; the registry adapter and the curated loader fill the columns.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "141983e167b9"
down_revision: Union[str, Sequence[str], None] = "17814f6d32bf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Batch mode: SQLite cannot ALTER a table in place for every operation; other dialects run plain ALTERs.
    with op.batch_alter_table("study_arm") as batch:
        batch.add_column(sa.Column("registered_arm_type", sa.Text(), nullable=True))
        batch.add_column(sa.Column("arm_role", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("arm_role_basis", sa.Text(), nullable=True))
    # The registered type of existing registry arms is the type they carry today.
    op.execute("UPDATE study_arm SET registered_arm_type = arm_type WHERE origin = 'REGISTRY' AND arm_type NOT IN ('RESULTS_GROUP', 'CURATED')")


def downgrade() -> None:
    with op.batch_alter_table("study_arm") as batch:
        batch.drop_column("arm_role_basis")
        batch.drop_column("arm_role")
        batch.drop_column("registered_arm_type")
