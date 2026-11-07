"""Add persistent business-scoped shared room messages.

Revision ID: 20261107_0015
Revises: 20261106_0014
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261107_0015"
down_revision: Union[str, None] = "20261106_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversation_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sender_role", sa.String(length=24), nullable=False),
        sa.Column("sender_name", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("attachments", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("sender_role IN ('owner', 'accountant', 'agent')", name="ck_conversation_messages_valid_sender_role"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], name="fk_conversation_messages_business_id_businesses", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_user_id"], ["users.id"], name="fk_conversation_messages_sender_user_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_conversation_messages"),
    )
    op.create_index("ix_conversation_messages_business_created", "conversation_messages", ["business_id", "created_at", "id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_conversation_messages_business_created", table_name="conversation_messages")
    op.drop_table("conversation_messages")
