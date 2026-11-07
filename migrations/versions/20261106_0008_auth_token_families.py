"""Add family identifiers for refresh-token rotation and replay revocation.

Revision ID: 20261106_0008
Revises: 20261106_0007
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261106_0008"
down_revision: Union[str, None] = "20261106_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM auth_tokens WHERE purpose = 'refresh')")).scalar_one():
        raise RuntimeError(
            "Cannot apply 20261106_0008: refresh tokens already exist without family IDs; "
            "revoke/expire them before retrying. No token rows were modified."
        )
    op.add_column("auth_tokens", sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_check_constraint(
        "ck_auth_tokens_refresh_requires_family",
        "auth_tokens",
        "purpose <> 'refresh' OR family_id IS NOT NULL",
    )
    op.create_index("ix_auth_tokens_family_id", "auth_tokens", ["family_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM auth_tokens WHERE purpose = 'refresh')")).scalar_one():
        raise RuntimeError("Cannot downgrade: refresh-token family/revocation history exists.")
    op.drop_index("ix_auth_tokens_family_id", table_name="auth_tokens")
    op.drop_constraint("ck_auth_tokens_refresh_requires_family", "auth_tokens", type_="check")
    op.drop_column("auth_tokens", "family_id")
