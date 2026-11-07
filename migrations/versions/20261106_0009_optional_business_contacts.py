"""Allow business email and phone to be omitted from business profiles.

Revision ID: 20261106_0009
Revises: 20261106_0008
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261106_0009"
down_revision: Union[str, None] = "20261106_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.alter_column("businesses", "business_email", existing_type=sa.String(length=320), nullable=True)
    op.alter_column("businesses", "business_phone", existing_type=sa.String(length=32), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM businesses WHERE business_email IS NULL OR business_phone IS NULL)")).scalar_one():
        raise RuntimeError("Cannot downgrade: business profiles omit contact details.")
    op.alter_column("businesses", "business_phone", existing_type=sa.String(length=32), nullable=False)
    op.alter_column("businesses", "business_email", existing_type=sa.String(length=320), nullable=False)
