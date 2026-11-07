"""Business profile and tenant root model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base


class Business(Base):
    __tablename__ = "businesses"
    __table_args__ = (
        Index("uq_businesses_single_demo", "is_demo", unique=True, postgresql_where=text("is_demo IS TRUE")),
        Index("uq_businesses_gstin_nonnull", "gstin", unique=True, postgresql_where=text("gstin IS NOT NULL")),
        CheckConstraint("gst_registration_status IN ('unregistered', 'pending', 'registered', 'not_applicable')", name="valid_gst_status"),
        CheckConstraint("storage_quota_bytes IS NULL OR storage_quota_bytes >= 0", name="nonnegative_storage_quota"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    business_name: Mapped[str] = mapped_column(String(255), nullable=False)
    proprietor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    gstin: Mapped[str | None] = mapped_column(String(15))
    business_type: Mapped[str] = mapped_column(String(64), nullable=False)
    business_email: Mapped[str | None] = mapped_column(String(320))
    business_phone: Mapped[str | None] = mapped_column(String(32))
    address_line_1: Mapped[str] = mapped_column(String(255), nullable=False)
    address_line_2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(128), nullable=False)
    pin_code: Mapped[str] = mapped_column(String(16), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False, server_default=text("'IN'"))
    gst_registration_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'unregistered'"))
    storage_quota_bytes: Mapped[int | None] = mapped_column(BigInteger)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    memberships: Mapped[list["BusinessMembership"]] = relationship(back_populates="business")
    invitations: Mapped[list["BusinessInvitation"]] = relationship(back_populates="business")
    monthly_report_approvals: Mapped[list["MonthlyReportApproval"]] = relationship(back_populates="business")
