"""Invoice persistence model."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base


class Invoice(Base):
    """Uploaded invoice metadata and normalized Gemini extraction fields."""

    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint("file_size >= 0", name="nonnegative_file_size"),
        CheckConstraint("storage_provider IN ('local', 's3')", name="valid_invoice_storage_provider"),
        UniqueConstraint("id", "business_id", name="uq_invoices_id_business"),
        UniqueConstraint("storage_provider", "storage_key", name="uq_invoice_storage_object"),
        ForeignKeyConstraint(["expected_vendor_id", "business_id"], ["expected_vendors.id", "expected_vendors.business_id"], name="fk_invoices_vendor_business"),
        CheckConstraint(
            "subtotal_paise IS NULL OR subtotal_paise >= 0",
            name="nonnegative_subtotal_paise",
        ),
        CheckConstraint(
            "tax_amount_paise IS NULL OR tax_amount_paise >= 0",
            name="nonnegative_tax_amount_paise",
        ),
        CheckConstraint(
            "total_amount_paise IS NULL OR total_amount_paise >= 0",
            name="nonnegative_total_amount_paise",
        ),
        Index("ix_invoices_invoice_date", "invoice_date"),
        Index("ix_invoices_vendor_name", "vendor_name"),
        Index("ix_invoices_expected_vendor_id", "expected_vendor_id"),
        Index("ix_invoices_upload_status", "upload_status"),
        Index("ix_invoice_evidence_business_provider", "business_id", "storage_provider"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    business_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="RESTRICT", name="fk_invoices_business"), nullable=False,
    )
    original_filename: Mapped[str | None] = mapped_column(String(512))
    stored_filename: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    storage_provider: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    upload_status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="uploaded",
        server_default=text("'uploaded'"),
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    expected_vendor_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("expected_vendors.id")
    )
    vendor_name: Mapped[str | None] = mapped_column(String(255))
    invoice_number: Mapped[str | None] = mapped_column(String(255))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    subtotal_paise: Mapped[int | None] = mapped_column(BigInteger)
    tax_amount_paise: Mapped[int | None] = mapped_column(BigInteger)
    total_amount_paise: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(3))
    confidence: Mapped[dict[str, float] | None] = mapped_column(JSONB)
    uncertain_fields: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    validation_warnings: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    extraction_status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    expected_vendor: Mapped["ExpectedVendor | None"] = relationship(
        back_populates="invoices", foreign_keys=[expected_vendor_id]
    )
    business: Mapped["Business"] = relationship(foreign_keys=[business_id])
    matches: Mapped[list["Match"]] = relationship(back_populates="invoice", foreign_keys="Match.invoice_id")
    chase_events: Mapped[list["ChaseEvent"]] = relationship(back_populates="invoice", foreign_keys="ChaseEvent.invoice_id")
