"""Add persistent clarification requests for unmatched bank debits.

Revision ID: 20261105_0004
Revises: 20261105_0003
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261105_0004"
down_revision: Union[str, None] = "20261105_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "clarification_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bank_line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("owner_response", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'answered', 'receipt_received', 'resolved', 'dismissed')",
            name="ck_clarification_requests_valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["bank_line_id"],
            ["bank_lines.id"],
            ondelete="RESTRICT",
            name="fk_clarification_requests_bank_line_id_bank_lines",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_clarification_requests"),
        sa.UniqueConstraint(
            "bank_line_id", name="uq_clarification_requests_bank_line_id"
        ),
    )
    op.create_index(
        "ix_clarification_requests_status", "clarification_requests", ["status"]
    )
    op.create_index(
        "ix_clarification_requests_created_at",
        "clarification_requests",
        ["created_at"],
    )

    op.add_column(
        "chase_events",
        sa.Column("clarification_request_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_chase_events_clarification_request_id_clarification_requests",
        "chase_events",
        "clarification_requests",
        ["clarification_request_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_chase_events_clarification_request_id",
        "chase_events",
        ["clarification_request_id"],
    )


def downgrade() -> None:
    # Retain clarification and response history rather than dropping populated data.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM clarification_requests)
                   OR EXISTS (
                       SELECT 1 FROM chase_events
                       WHERE clarification_request_id IS NOT NULL
                   ) THEN
                    RAISE EXCEPTION
                        'Cannot downgrade: clarification requests or their event history exist';
                END IF;
            END
            $$;
            """
        )
    )
    op.drop_index(
        "ix_chase_events_clarification_request_id", table_name="chase_events"
    )
    op.drop_constraint(
        "fk_chase_events_clarification_request_id_clarification_requests",
        "chase_events",
        type_="foreignkey",
    )
    op.drop_column("chase_events", "clarification_request_id")
    op.drop_index(
        "ix_clarification_requests_created_at",
        table_name="clarification_requests",
    )
    op.drop_index(
        "ix_clarification_requests_status", table_name="clarification_requests"
    )
    op.drop_table("clarification_requests")
