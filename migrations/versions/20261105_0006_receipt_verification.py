"""Persist deterministic receipt verification decisions.

Revision ID: 20261105_0006
Revises: 20261105_0005
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261105_0006"
down_revision: Union[str, None] = "20261105_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clarification_receipts",
        sa.Column("verification_status", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "clarification_receipts",
        sa.Column("verification_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "clarification_receipts",
        sa.Column(
            "verification_details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "clarification_receipts",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_clarification_receipts_valid_verification_status",
        "clarification_receipts",
        "verification_status IS NULL OR verification_status IN "
        "('verified', 'amount_mismatch', 'vendor_mismatch', 'date_mismatch', 'needs_review')",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM clarification_receipts
                    WHERE verification_status IS NOT NULL
                ) OR EXISTS (
                    SELECT 1 FROM chase_events
                    WHERE event_type = 'clarification_receipt_verified'
                ) THEN
                    RAISE EXCEPTION
                        'Cannot downgrade: receipt verification decisions or history exist';
                END IF;
            END
            $$;
            """
        )
    )
    op.drop_constraint(
        "ck_clarification_receipts_valid_verification_status",
        "clarification_receipts",
        type_="check",
    )
    op.drop_column("clarification_receipts", "verified_at")
    op.drop_column("clarification_receipts", "verification_details")
    op.drop_column("clarification_receipts", "verification_reason")
    op.drop_column("clarification_receipts", "verification_status")
