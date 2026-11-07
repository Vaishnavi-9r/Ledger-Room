"""Persist clarification receipt evidence and extraction results.

Revision ID: 20261105_0005
Revises: 20261105_0004
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261105_0005"
down_revision: Union[str, None] = "20261105_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "clarification_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clarification_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=True),
        sa.Column("stored_filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=127), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "extraction_status",
            sa.String(length=24),
            server_default=sa.text("'completed'"),
            nullable=False,
        ),
        sa.Column("merchant_name", sa.String(length=255), nullable=True),
        sa.Column("document_type", sa.String(length=32), nullable=True),
        sa.Column("document_number", sa.String(length=255), nullable=True),
        sa.Column("document_date", sa.Date(), nullable=True),
        sa.Column("total_amount_paise", sa.BigInteger(), nullable=True),
        sa.Column("payment_method", sa.String(length=128), nullable=True),
        sa.Column("payment_reference", sa.String(length=255), nullable=True),
        sa.Column(
            "confidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "uncertain_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("extraction_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "file_size >= 0",
            name="ck_clarification_receipts_nonnegative_file_size",
        ),
        sa.CheckConstraint(
            "total_amount_paise IS NULL OR total_amount_paise >= 0",
            name="ck_clarification_receipts_nonnegative_total_amount_paise",
        ),
        sa.CheckConstraint(
            "document_type IS NULL OR document_type IN "
            "('invoice', 'payment_receipt', 'cash_receipt', 'unknown')",
            name="ck_clarification_receipts_valid_document_type",
        ),
        sa.CheckConstraint(
            "extraction_status IN ('completed', 'failed', 'not_configured')",
            name="ck_clarification_receipts_valid_extraction_status",
        ),
        sa.ForeignKeyConstraint(
            ["clarification_request_id"],
            ["clarification_requests.id"],
            ondelete="RESTRICT",
            name="fk_clarification_receipt_request",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_clarification_receipts"),
        sa.UniqueConstraint(
            "stored_filename",
            name="uq_clarification_receipts_stored_filename",
        ),
    )
    op.create_index(
        "ix_clarification_receipts_request_id",
        "clarification_receipts",
        ["clarification_request_id"],
    )
    op.create_index(
        "ix_clarification_receipts_uploaded_at",
        "clarification_receipts",
        ["uploaded_at"],
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM clarification_receipts)
                   OR EXISTS (
                       SELECT 1 FROM chase_events
                       WHERE event_type = 'clarification_receipt_uploaded'
                   ) THEN
                    RAISE EXCEPTION
                        'Cannot downgrade: clarification receipt evidence or history exists';
                END IF;
            END
            $$;
            """
        )
    )
    op.drop_index(
        "ix_clarification_receipts_uploaded_at",
        table_name="clarification_receipts",
    )
    op.drop_index(
        "ix_clarification_receipts_request_id",
        table_name="clarification_receipts",
    )
    op.drop_table("clarification_receipts")
