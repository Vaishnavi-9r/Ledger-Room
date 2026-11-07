"""Add reconciliation audit history and candidate evidence fields.

Revision ID: 20261105_0003
Revises: 20261105_0002
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261105_0003"
down_revision: Union[str, None] = "20261105_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "matches",
        sa.Column(
            "candidate_bank_line_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    # Legacy rows have no provenance marker. Treat them as human-owned so an
    # automated rerun cannot silently replace a pre-existing decision.
    op.add_column(
        "matches",
        sa.Column(
            "decision_source",
            sa.String(length=16),
            server_default=sa.text("'human'"),
            nullable=False,
        ),
    )
    op.add_column(
        "matches",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text("UPDATE matches SET updated_at = created_at WHERE updated_at IS NULL")
    )
    op.alter_column(
        "matches",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )

    op.create_table(
        "match_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("match_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("previous_bank_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("previous_status", sa.String(length=24), nullable=False),
        sa.Column("previous_notes", sa.Text(), nullable=True),
        sa.Column(
            "previous_candidate_bank_line_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("previous_decision_source", sa.String(length=16), nullable=False),
        sa.Column("previous_confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("previous_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("new_bank_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("new_status", sa.String(length=24), nullable=False),
        sa.Column("new_notes", sa.Text(), nullable=True),
        sa.Column(
            "new_candidate_bank_line_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("new_decision_source", sa.String(length=16), nullable=False),
        sa.Column("new_confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["match_id"], ["matches.id"], ondelete="RESTRICT", name="fk_match_events_match_id_matches"
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"], ondelete="RESTRICT", name="fk_match_events_invoice_id_invoices"
        ),
        sa.ForeignKeyConstraint(
            ["previous_bank_line_id"],
            ["bank_lines.id"],
            ondelete="RESTRICT",
            name="fk_match_events_previous_bank_line_id_bank_lines",
        ),
        sa.ForeignKeyConstraint(
            ["new_bank_line_id"],
            ["bank_lines.id"],
            ondelete="RESTRICT",
            name="fk_match_events_new_bank_line_id_bank_lines",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_match_events"),
    )
    op.create_index("ix_match_events_match_id", "match_events", ["match_id"])
    op.create_index("ix_match_events_invoice_id", "match_events", ["invoice_id"])
    op.create_index("ix_match_events_changed_at", "match_events", ["changed_at"])


def downgrade() -> None:
    # Refuse to discard audit history or structured provenance during downgrade.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM match_events)
                   OR EXISTS (
                       SELECT 1
                       FROM matches
                       WHERE decision_source <> 'human'
                          OR candidate_bank_line_ids <> '[]'::jsonb
                          OR updated_at <> created_at
                   ) THEN
                    RAISE EXCEPTION
                        'Cannot downgrade: reconciliation audit or provenance data exists';
                END IF;
            END
            $$;
            """
        )
    )
    op.drop_table("match_events")
    op.drop_column("matches", "updated_at")
    op.drop_column("matches", "decision_source")
    op.drop_column("matches", "candidate_bank_line_ids")
