"""Canonical LedgerRoom finance room and its Mattermost channel mapping."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base


class BusinessRoom(Base):
    __tablename__ = "business_rooms"
    __table_args__ = (
        UniqueConstraint("business_id", name="uq_business_rooms_business_id"),
        UniqueConstraint("mattermost_server", "mattermost_channel_id", name="uq_business_rooms_mattermost_channel"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    business_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    mattermost_server: Mapped[str | None] = mapped_column(String(512))
    mattermost_team_id: Mapped[str | None] = mapped_column(String(128))
    mattermost_channel_id: Mapped[str | None] = mapped_column(String(128))
    mattermost_channel_name: Mapped[str | None] = mapped_column(String(64))
    mattermost_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    mattermost_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MattermostOAuthState(Base):
    __tablename__ = "mattermost_oauth_states"
    __table_args__ = (Index("ix_mattermost_oauth_states_user_id", "user_id"),)

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    state_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    user_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MattermostRelayEvent(Base):
    """At-most-once receipt ledger for Mattermost WebSocket post events."""
    __tablename__ = "mattermost_relay_events"
    __table_args__ = (
        UniqueConstraint("mattermost_server", "mattermost_post_id", name="uq_mattermost_relay_server_post"),
        CheckConstraint("status IN ('processing', 'completed', 'rejected', 'ignored', 'failed')", name="valid_status"),
        Index("ix_mattermost_relay_events_status", "status", "received_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    mattermost_server: Mapped[str] = mapped_column(String(128), nullable=False)
    mattermost_post_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mattermost_channel_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mattermost_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    business_room_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True), ForeignKey("business_rooms.id", ondelete="SET NULL"))
    user_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="processing")
    result_code: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
