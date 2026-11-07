"""LedgerRoom user identity model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_email_normalized", "email_normalized", unique=True),)

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    mobile_number: Mapped[str] = mapped_column(String(32), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    memberships: Mapped[list["BusinessMembership"]] = relationship(back_populates="user")
    accountant_profile: Mapped["AccountantProfile | None"] = relationship(back_populates="user", uselist=False)
    auth_tokens: Mapped[list["AuthToken"]] = relationship(back_populates="user")
    external_identities: Mapped[list["ExternalIdentity"]] = relationship(back_populates="user")
    submitted_report_approvals: Mapped[list["MonthlyReportApproval"]] = relationship(
        foreign_keys="MonthlyReportApproval.submitted_by", back_populates="submitter",
    )
    reviewed_report_approvals: Mapped[list["MonthlyReportApproval"]] = relationship(
        foreign_keys="MonthlyReportApproval.reviewed_by", back_populates="reviewer",
    )
    report_approval_events: Mapped[list["MonthlyReportApprovalEvent"]] = relationship(back_populates="actor")
