"""Expected vendor configuration model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base


class ExpectedVendor(Base):
    """Known vendor names and aliases used for future invoice review."""

    __tablename__ = "expected_vendors"
    __table_args__ = (
        UniqueConstraint("id", "business_id", name="uq_expected_vendors_id_business"),
        UniqueConstraint("business_id", "normalized_name", name="uq_expected_vendors_business_name"),
        Index("ix_expected_vendors_active", "active"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    business_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="RESTRICT", name="fk_expected_vendors_business"), nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    aliases: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    currency: Mapped[str | None] = mapped_column(String(3))
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    business: Mapped["Business"] = relationship(foreign_keys=[business_id])
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="expected_vendor", foreign_keys="Invoice.expected_vendor_id")
