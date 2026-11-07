"""Create initial LedgerRoom persistence schema.

Revision ID: 20261104_0001
Revises:
Create Date: 2026-11-04
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261104_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "expected_vendors",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column(
            "aliases",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_expected_vendors"),
        sa.UniqueConstraint(
            "normalized_name", name="uq_expected_vendors_normalized_name"
        ),
    )
    op.create_index(
        "ix_expected_vendors_active", "expected_vendors", ["active"], unique=False
    )

    op.create_table(
        "bank_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("external_reference", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_bank_lines"),
    )
    op.create_index(
        "ix_bank_lines_amount_paise", "bank_lines", ["amount_paise"], unique=False
    )
    op.create_index(
        "ix_bank_lines_external_reference",
        "bank_lines",
        ["external_reference"],
        unique=False,
    )
    op.create_index(
        "ix_bank_lines_transaction_date",
        "bank_lines",
        ["transaction_date"],
        unique=False,
    )

    op.create_table(
        "invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=True),
        sa.Column("stored_filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=127), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "upload_status",
            sa.String(length=24),
            server_default=sa.text("'uploaded'"),
            nullable=False,
        ),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expected_vendor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("vendor_name", sa.String(length=255), nullable=True),
        sa.Column("invoice_number", sa.String(length=255), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("subtotal_paise", sa.BigInteger(), nullable=True),
        sa.Column("tax_amount_paise", sa.BigInteger(), nullable=True),
        sa.Column("total_amount_paise", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("confidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "uncertain_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "validation_warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "extraction_status",
            sa.String(length=24),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("file_size >= 0", name="ck_invoices_nonnegative_file_size"),
        sa.CheckConstraint(
            "subtotal_paise IS NULL OR subtotal_paise >= 0",
            name="ck_invoices_nonnegative_subtotal_paise",
        ),
        sa.CheckConstraint(
            "tax_amount_paise IS NULL OR tax_amount_paise >= 0",
            name="ck_invoices_nonnegative_tax_amount_paise",
        ),
        sa.CheckConstraint(
            "total_amount_paise IS NULL OR total_amount_paise >= 0",
            name="ck_invoices_nonnegative_total_amount_paise",
        ),
        sa.ForeignKeyConstraint(
            ["expected_vendor_id"],
            ["expected_vendors.id"],
            name="fk_invoices_expected_vendor_id_expected_vendors",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invoices"),
        sa.UniqueConstraint("stored_filename", name="uq_invoices_stored_filename"),
    )
    op.create_index(
        "ix_invoices_expected_vendor_id", "invoices", ["expected_vendor_id"], unique=False
    )
    op.create_index(
        "ix_invoices_invoice_date", "invoices", ["invoice_date"], unique=False
    )
    op.create_index(
        "ix_invoices_upload_status", "invoices", ["upload_status"], unique=False
    )
    op.create_index(
        "ix_invoices_vendor_name", "invoices", ["vendor_name"], unique=False
    )

    op.create_table(
        "matches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bank_line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default=sa.text("'suggested'"), nullable=False
        ),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("notes", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_matches_confidence_between_zero_and_one",
        ),
        sa.ForeignKeyConstraint(
            ["bank_line_id"], ["bank_lines.id"], name="fk_matches_bank_line_id_bank_lines"
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"], name="fk_matches_invoice_id_invoices"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_matches"),
        sa.UniqueConstraint(
            "invoice_id", "bank_line_id", name="uq_matches_invoice_bank_line"
        ),
    )
    op.create_index("ix_matches_bank_line_id", "matches", ["bank_line_id"], unique=False)
    op.create_index("ix_matches_invoice_id", "matches", ["invoice_id"], unique=False)
    op.create_index("ix_matches_status", "matches", ["status"], unique=False)

    op.create_table(
        "chase_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("bank_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("match_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "invoice_id IS NOT NULL OR bank_line_id IS NOT NULL OR match_id IS NOT NULL",
            name="ck_chase_events_requires_related_record",
        ),
        sa.ForeignKeyConstraint(
            ["bank_line_id"],
            ["bank_lines.id"],
            name="fk_chase_events_bank_line_id_bank_lines",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            name="fk_chase_events_invoice_id_invoices",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["match_id"],
            ["matches.id"],
            name="fk_chase_events_match_id_matches",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chase_events"),
    )
    op.create_index(
        "ix_chase_events_bank_line_id", "chase_events", ["bank_line_id"], unique=False
    )
    op.create_index(
        "ix_chase_events_created_at", "chase_events", ["created_at"], unique=False
    )
    op.create_index(
        "ix_chase_events_event_type", "chase_events", ["event_type"], unique=False
    )
    op.create_index(
        "ix_chase_events_invoice_id", "chase_events", ["invoice_id"], unique=False
    )
    op.create_index(
        "ix_chase_events_match_id", "chase_events", ["match_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_chase_events_match_id", table_name="chase_events")
    op.drop_index("ix_chase_events_invoice_id", table_name="chase_events")
    op.drop_index("ix_chase_events_event_type", table_name="chase_events")
    op.drop_index("ix_chase_events_created_at", table_name="chase_events")
    op.drop_index("ix_chase_events_bank_line_id", table_name="chase_events")
    op.drop_table("chase_events")
    op.drop_index("ix_matches_status", table_name="matches")
    op.drop_index("ix_matches_invoice_id", table_name="matches")
    op.drop_index("ix_matches_bank_line_id", table_name="matches")
    op.drop_table("matches")
    op.drop_index("ix_invoices_vendor_name", table_name="invoices")
    op.drop_index("ix_invoices_upload_status", table_name="invoices")
    op.drop_index("ix_invoices_invoice_date", table_name="invoices")
    op.drop_index("ix_invoices_expected_vendor_id", table_name="invoices")
    op.drop_table("invoices")
    op.drop_index("ix_bank_lines_transaction_date", table_name="bank_lines")
    op.drop_index("ix_bank_lines_external_reference", table_name="bank_lines")
    op.drop_index("ix_bank_lines_amount_paise", table_name="bank_lines")
    op.drop_table("bank_lines")
    op.drop_index("ix_expected_vendors_active", table_name="expected_vendors")
    op.drop_table("expected_vendors")
