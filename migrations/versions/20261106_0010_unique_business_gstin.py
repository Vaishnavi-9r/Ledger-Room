"""Prevent duplicate non-null GSTIN values across business profiles.

Revision ID: 20261106_0010
Revises: 20261106_0009
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261106_0010"
down_revision: Union[str, None] = "20261106_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(sa.text(
        "SELECT gstin FROM businesses WHERE gstin IS NOT NULL GROUP BY gstin HAVING count(*) > 1 LIMIT 1"
    )).first()
    if duplicates is not None:
        # Do not print existing GSTIN values into migration logs.
        raise RuntimeError("Cannot apply 20261106_0010: duplicate GSTIN values exist; resolve them before retrying.")
    op.create_index(
        "uq_businesses_gstin_nonnull", "businesses", ["gstin"], unique=True,
        postgresql_where=sa.text("gstin IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_businesses_gstin_nonnull", table_name="businesses")
