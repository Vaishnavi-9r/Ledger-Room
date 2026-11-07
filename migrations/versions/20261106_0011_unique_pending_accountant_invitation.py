"""Prevent concurrent duplicate active accountant invitations.

Revision ID: 20261106_0011
Revises: 20261106_0010
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261106_0011"
down_revision: Union[str, None] = "20261106_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


INDEX_NAME = "uq_business_invitations_pending_business_email"


def upgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(sa.text(
        "SELECT 1 FROM business_invitations WHERE status = 'pending' "
        "GROUP BY business_id, email_normalized HAVING count(*) > 1 LIMIT 1"
    )).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot apply 20261106_0011: duplicate pending accountant invitations exist; "
            "resolve them before retrying. Email addresses are not logged."
        )
    op.create_index(
        INDEX_NAME,
        "business_invitations",
        ["business_id", "email_normalized"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    # Dropping this guard does not erase invitation history or records.
    op.drop_index(INDEX_NAME, table_name="business_invitations")
