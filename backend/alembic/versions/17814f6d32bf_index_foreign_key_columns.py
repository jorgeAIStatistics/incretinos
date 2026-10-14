"""index foreign key columns

Every foreign-key column that was not already the leading column of an index gets one. Without it the
database scans the child table whenever a parent row is deleted or updated (ON DELETE SET NULL / CASCADE
on arms, estimands, snapshots and doses) and whenever rows are looked up by their parent.

Index-only: no table is rebuilt and no data changes, so the revision is independent of any revision that
adds tables or columns. It can be re-parented freely when revisions from several branches are linearized.

It was written against 10d1699882ea and placed last at integration (after 65bce81e7f45 and 9b4e73774dde), so
it also covers the three foreign-key columns those two revisions added without an index:
``participant_flow.arm_id``, ``evidence_version.supersedes_id`` and ``review_decision.evidence_version_id``.

Revision ID: 17814f6d32bf
Revises: 9b4e73774dde
Create Date: 2026-10-13 19:40:15.621086

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '17814f6d32bf'
down_revision: Union[str, Sequence[str], None] = '9b4e73774dde'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column); the index name is ix_<table>_<column>, the name SQLAlchemy gives to index=True.
FOREIGN_KEY_INDEXES: tuple[tuple[str, str], ...] = (
    ('adverse_event', 'arm_id'),
    ('change_event', 'from_snapshot_id'),
    ('change_event', 'to_snapshot_id'),
    ('discovered_trial', 'source_id'),
    ('drug', 'regulatory_source_id'),
    ('drug_mechanism', 'source_id'),
    ('endpoint', 'source_id'),
    ('estimand', 'source_id'),
    ('evidence_claim', 'source_id'),
    ('evidence_version', 'supersedes_id'),
    ('exposure_equivalence', 'dose_a_id'),
    ('exposure_equivalence', 'dose_b_id'),
    ('exposure_equivalence', 'source_id'),
    ('formulation', 'source_id'),
    ('indication', 'source_id'),
    ('label_record', 'source_id'),
    ('outcome_result', 'arm_id'),
    ('outcome_result', 'estimand_id'),
    ('participant_flow', 'arm_id'),
    ('population', 'source_id'),
    ('publication', 'source_id'),
    ('review_decision', 'evidence_version_id'),
    ('review_decision', 'queue_item_id'),
    ('spontaneous_report_series', 'source_id'),
    ('statistical_analysis', 'comparator_arm_id'),
    ('statistical_analysis', 'estimand_id'),
    ('statistical_analysis', 'treatment_arm_id'),
    ('study', 'source_id'),
    ('study_version', 'snapshot_id'),
)


def upgrade() -> None:
    """Upgrade schema."""
    for table, column in FOREIGN_KEY_INDEXES:
        op.create_index(op.f(f'ix_{table}_{column}'), table, [column], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    for table, column in reversed(FOREIGN_KEY_INDEXES):
        op.drop_index(op.f(f'ix_{table}_{column}'), table_name=table)
