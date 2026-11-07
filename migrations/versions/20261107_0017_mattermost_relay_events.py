"""Add Mattermost relay event idempotency ledger.

Revision ID: 20261107_0017
Revises: 20261107_0016
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261107_0017"
down_revision: Union[str, None] = "20261107_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mattermost_relay_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mattermost_server", sa.String(length=128), nullable=False),
        sa.Column("mattermost_post_id", sa.String(length=128), nullable=False),
        sa.Column("mattermost_channel_id", sa.String(length=128), nullable=False),
        sa.Column("mattermost_user_id", sa.String(length=128), nullable=False),
        sa.Column("business_room_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="processing"),
        sa.Column("result_code", sa.String(length=64), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["business_room_id"], ["business_rooms.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mattermost_server", "mattermost_post_id", name="uq_mattermost_relay_server_post"),
        sa.CheckConstraint("status IN ('processing', 'completed', 'rejected', 'ignored', 'failed')", name="ck_mattermost_relay_events_valid_status"),
    )
    op.create_index("ix_mattermost_relay_events_status", "mattermost_relay_events", ["status", "received_at"])


def downgrade() -> None:
    op.drop_index("ix_mattermost_relay_events_status", table_name="mattermost_relay_events")
    op.drop_table("mattermost_relay_events")
