"""Expiring invitation record for a business membership."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base


class BusinessInvitation(Base):
    __tablename__ = "business_invitations"
    __table_args__ = (
        Index("ix_business_invitations_business_email", "business_id", "email_normalized"),
        Index(
            "uq_business_invitations_pending_business_email",
            "business_id", "email_normalized", unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        CheckConstraint("role = 'accountant'", name="accountant_only_role"),
        CheckConstraint("status IN ('pending', 'accepted', 'revoked', 'expired')", name="valid_status"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    business_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="RESTRICT", name="fk_business_invitations_business"), nullable=False)
    invited_by_membership_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), ForeignKey("business_memberships.id", ondelete="RESTRICT", name="fk_business_invitations_inviter"), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'accountant'"))
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'pending'"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    business: Mapped["Business"] = relationship(back_populates="invitations")
