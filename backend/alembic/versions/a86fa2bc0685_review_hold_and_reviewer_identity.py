"""review hold, decision lineage and reviewer identity

Revision ID: a86fa2bc0685
Revises: 141983e167b9
Create Date: 2026-11-08 12:00:00.000000

What this revision does to an existing database

* adds review_hold (NOT NULL, default false) to the seven reviewable evidence tables. A held row is excluded
  from every analytic surface like a REJECTED one: its latest decisive decision is a rejection made on an earlier
  version, and nobody has decided on the current version yet;
* adds review_hold and inherited_decision_ids to evidence_version (the review index, and the decisions
  carried to a row from another evidence key after a relabel);
* adds identity_basis and ledger_match to review_decision. Decisions recorded before this revision were
  all made under the single shared reviewer token with a name typed by the client, so they are labelled
  SELF_DECLARED_SHARED_TOKEN; imported ones IMPORTED_UNRECORDED (the ledger did not say). Imported decisions are
  marked MATCHED_VERSION: the import of that time always attached them to a version record.

It marks nothing as human-verified or rejected and sets no hold. Holds, the lineage of relabelled rows and the
new fingerprints (which now include the estimand's definition text and intercurrent-event strategies and the
endpoint's description) are computed by incretinos review reconcile, which the container runs on every start.
After that first reconcile an approval made on a version whose estimand or endpoint text was not fingerprinted
asks for a recheck; a rejection stays excluded.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a86fa2bc0685"
down_revision: str | Sequence[str] | None = "141983e167b9"
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


def upgrade() -> None:
    for table in REVIEWABLE_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("review_hold", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("evidence_version") as batch:
        batch.add_column(sa.Column("review_hold", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("inherited_decision_ids", sa.JSON(), nullable=True))
    with op.batch_alter_table("review_decision") as batch:
        batch.add_column(sa.Column("identity_basis", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("ledger_match", sa.String(length=20), nullable=True))
    decision = sa.table("review_decision", sa.column("identity_basis", sa.String), sa.column("ledger_match", sa.String), sa.column("imported_from", sa.String))
    op.execute(decision.update().where(decision.c.imported_from.is_(None)).values(identity_basis="SELF_DECLARED_SHARED_TOKEN"))
    op.execute(decision.update().where(decision.c.imported_from.is_not(None)).values(identity_basis="IMPORTED_UNRECORDED", ledger_match="MATCHED_VERSION"))


def downgrade() -> None:
    with op.batch_alter_table("review_decision") as batch:
        batch.drop_column("ledger_match")
        batch.drop_column("identity_basis")
    with op.batch_alter_table("evidence_version") as batch:
        batch.drop_column("inherited_decision_ids")
        batch.drop_column("review_hold")
    for table in REVIEWABLE_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("review_hold")
