"""full-text documents, extracted tables and the cell-level link of FULLTEXT evidence rows

Revision ID: cb85e55cbfb4
Revises: a86fa2bc0685
Create Date: 2026-11-09 12:00:00.000000

What this revision does to an existing database

* creates fulltext_document (one row per full-text document: identifiers, the licence exactly as the source's API
  returned it and how it was established, whether the full text may be and is stored, hash, size, page count,
  retrieval time, fetch / parse status and the recorded limitations);
* creates extracted_table (one row per parsed table: label, caption, page or element locator, header rows with
  spans, body rows with stub labels, footnotes, abbreviations, units, parse method and version, structural notes);
* creates fulltext_extraction (the link from one FULLTEXT evidence row to the exact cells or sentence it was read
  from, with the parsing method and the quality assessment);
* adds a nullable fulltext_extraction_id to outcome_result, statistical_analysis, adverse_event and evidence_claim.

A downgrade deletes the FULLTEXT rows (and their endpoints and estimands) before it drops the link: they are derived
data the older code cannot represent. Their evidence version history is kept.

Existing rows are untouched: the new column is NULL on every registry and curated row, and no fingerprint of an
existing row changes (the extraction is part of the fingerprint of FULLTEXT rows only). The tables are filled by
the full-text step of the next seed.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cb85e55cbfb4"
down_revision: str | Sequence[str] | None = "a86fa2bc0685"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LINKED_TABLES = ("outcome_result", "statistical_analysis", "adverse_event", "evidence_claim")


def upgrade() -> None:
    op.create_table(
        "fulltext_document",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("pmcid", sa.String(length=20), nullable=True),
        sa.Column("pmid", sa.String(length=12), nullable=True),
        sa.Column("doi", sa.String(length=200), nullable=True),
        sa.Column("application_number", sa.String(length=40), nullable=True),
        sa.Column("licence", sa.Text(), nullable=True),
        sa.Column("licence_url", sa.Text(), nullable=True),
        sa.Column("licence_basis", sa.Text(), nullable=True),
        sa.Column("open_access_flag", sa.String(length=10), nullable=True),
        sa.Column("full_text_storable", sa.Boolean(), nullable=False),
        sa.Column("full_text_stored", sa.Boolean(), nullable=False),
        sa.Column("storage_basis", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("version_label", sa.Text(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetch_status", sa.String(length=20), nullable=False),
        sa.Column("parse_status", sa.String(length=20), nullable=False),
        sa.Column("extraction_scope", sa.Text(), nullable=True),
        sa.Column("limitations", sa.JSON(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["evidence_source.id"], name="fk_fulltext_document_source", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_fulltext_document_key"), "fulltext_document", ["key"], unique=True)
    op.create_index(op.f("ix_fulltext_document_source_id"), "fulltext_document", ["source_id"], unique=False)

    op.create_table(
        "extracted_table",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("element_id", sa.String(length=80), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("printed_page", sa.String(length=20), nullable=True),
        sa.Column("authorship", sa.String(length=30), nullable=False),
        sa.Column("source_line", sa.Text(), nullable=True),
        sa.Column("header_rows", sa.JSON(), nullable=False),
        sa.Column("body_rows", sa.JSON(), nullable=False),
        sa.Column("footnotes", sa.JSON(), nullable=False),
        sa.Column("abbreviations", sa.JSON(), nullable=False),
        sa.Column("units", sa.JSON(), nullable=False),
        sa.Column("n_columns", sa.Integer(), nullable=False),
        sa.Column("parse_method", sa.String(length=60), nullable=False),
        sa.Column("parse_version", sa.String(length=20), nullable=False),
        sa.Column("structural_notes", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["fulltext_document.id"], name="fk_extracted_table_document", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index(op.f("ix_extracted_table_document_id"), "extracted_table", ["document_id"], unique=False)

    op.create_table(
        "fulltext_extraction",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("table_id", sa.Integer(), nullable=True),
        sa.Column("evidence_type", sa.String(length=20), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("row_label", sa.Text(), nullable=True),
        sa.Column("column_header", sa.Text(), nullable=True),
        sa.Column("comparator_header", sa.Text(), nullable=True),
        sa.Column("cells", sa.JSON(), nullable=False),
        sa.Column("passage", sa.JSON(), nullable=True),
        sa.Column("parse_method", sa.String(length=60), nullable=False),
        sa.Column("parse_version", sa.String(length=20), nullable=False),
        sa.Column("quality", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.String(length=10), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["fulltext_document.id"], name="fk_fulltext_extraction_document", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["extracted_table.id"], name="fk_fulltext_extraction_table", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_fulltext_extraction_document_id"), "fulltext_extraction", ["document_id"], unique=False)
    op.create_index(op.f("ix_fulltext_extraction_table_id"), "fulltext_extraction", ["table_id"], unique=False)

    for table in LINKED_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("fulltext_extraction_id", sa.Integer(), nullable=True))
            batch.create_index(batch.f(f"ix_{table}_fulltext_extraction_id"), ["fulltext_extraction_id"], unique=False)
            batch.create_foreign_key(f"fk_{table}_fulltext_extraction", "fulltext_extraction", ["fulltext_extraction_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    # FULLTEXT rows are derived (the full-text step rebuilds them) and the older code cannot show where they came from;
    # it would also anchor their assembled quotes as if they were curated. They are removed; their version history stays.
    for table in (*LINKED_TABLES, "endpoint"):
        op.execute(sa.text(f"DELETE FROM {table} WHERE origin = 'FULLTEXT'"))
    op.execute(sa.text("DELETE FROM intercurrent_event_strategy WHERE estimand_id IN (SELECT id FROM estimand WHERE origin = 'FULLTEXT')"))
    op.execute(sa.text("DELETE FROM estimand WHERE origin = 'FULLTEXT'"))
    for table in LINKED_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"fk_{table}_fulltext_extraction", type_="foreignkey")
            batch.drop_index(batch.f(f"ix_{table}_fulltext_extraction_id"))
            batch.drop_column("fulltext_extraction_id")
    op.drop_index(op.f("ix_fulltext_extraction_table_id"), table_name="fulltext_extraction")
    op.drop_index(op.f("ix_fulltext_extraction_document_id"), table_name="fulltext_extraction")
    op.drop_table("fulltext_extraction")
    op.drop_index(op.f("ix_extracted_table_document_id"), table_name="extracted_table")
    op.drop_table("extracted_table")
    op.drop_index(op.f("ix_fulltext_document_source_id"), table_name="fulltext_document")
    op.drop_index(op.f("ix_fulltext_document_key"), table_name="fulltext_document")
    op.drop_table("fulltext_document")
