"""Add canonical finance rooms and one-time Mattermost OAuth link state.

Revision ID: 20261107_0016
Revises: 20261107_0015
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261107_0016"
down_revision: Union[str, None] = "20261107_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "business_rooms",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mattermost_server", sa.String(length=512), nullable=True),
        sa.Column("mattermost_team_id", sa.String(length=128), nullable=True),
        sa.Column("mattermost_channel_id", sa.String(length=128), nullable=True),
        sa.Column("mattermost_channel_name", sa.String(length=64), nullable=True),
        sa.Column("mattermost_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("mattermost_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", name="uq_business_rooms_business_id"),
        sa.UniqueConstraint("mattermost_server", "mattermost_channel_id", name="uq_business_rooms_mattermost_channel"),
    )
    op.create_table(
        "mattermost_oauth_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state_hash", sa.String(length=128), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash", name="uq_mattermost_oauth_states_state_hash"),
    )
    op.create_index("ix_mattermost_oauth_states_user_id", "mattermost_oauth_states", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_mattermost_oauth_states_user_id", table_name="mattermost_oauth_states")
    op.drop_table("mattermost_oauth_states")
    op.drop_table("business_rooms")
