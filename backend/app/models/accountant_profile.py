"""Self-maintained accountant professional profile."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base


class AccountantProfile(Base):
    __tablename__ = "accountant_profiles"
    __table_args__ = (CheckConstraint("verification_status IN ('unverified', 'pending', 'verified', 'rejected')", name="valid_verification_status"),)

    user_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT", name="fk_accountant_profiles_user"), primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    firm_name: Mapped[str | None] = mapped_column(String(255))
    office_address: Mapped[str | None] = mapped_column(String(1000))
    professional_qualification: Mapped[str] = mapped_column(String(255), nullable=False)
    professional_registration_number: Mapped[str | None] = mapped_column(String(128))
    icai_membership_number: Mapped[str | None] = mapped_column(String(64))
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'unverified'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="accountant_profile")
