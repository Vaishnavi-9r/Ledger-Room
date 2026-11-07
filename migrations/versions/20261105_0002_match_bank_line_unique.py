"""Allow invoice-only decisions and prevent bank line reuse across matches.

Revision ID: 20261105_0002
Revises: 20261104_0001
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261105_0002"
down_revision: Union[str, None] = "20261104_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Fail before changing the schema if existing records already assign one
    # bank transaction to more than one match. No existing rows are rewritten.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM matches
                    WHERE bank_line_id IS NOT NULL
                    GROUP BY bank_line_id
                    HAVING COUNT(*) > 1
                ) THEN
                    RAISE EXCEPTION
                        'Cannot add uq_matches_bank_line_id: duplicate bank_line_id values exist in matches';
                END IF;
            END
            $$;
            """
        )
    )
    op.create_unique_constraint(
        "uq_matches_bank_line_id", "matches", ["bank_line_id"]
    )
    op.alter_column(
        "matches",
        "bank_line_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    # Refuse to restore NOT NULL if any invoice-only decisions have been saved.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM matches WHERE bank_line_id IS NULL) THEN
                    RAISE EXCEPTION
                        'Cannot downgrade: matches contain invoice-only rows with NULL bank_line_id';
                END IF;
            END
            $$;
            """
        )
    )
    op.drop_constraint("uq_matches_bank_line_id", "matches", type_="unique")
    op.alter_column(
        "matches",
        "bank_line_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
