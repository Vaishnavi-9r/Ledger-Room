"""Original receipt evidence and extracted fields for a clarification."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base


class ClarificationReceipt(Base):
    """One immutable upload attempt and its optional Gemini extraction."""

    __tablename__ = "clarification_receipts"
    __table_args__ = (
        CheckConstraint("file_size >= 0", name="nonnegative_file_size"),
        CheckConstraint("storage_provider IN ('local', 's3')", name="valid_receipt_storage_provider"),
        CheckConstraint(
            "total_amount_paise IS NULL OR total_amount_paise >= 0",
            name="nonnegative_total_amount_paise",
        ),
        CheckConstraint(
            "document_type IS NULL OR document_type IN "
            "('invoice', 'payment_receipt', 'cash_receipt', 'unknown')",
            name="valid_document_type",
        ),
        CheckConstraint(
            "extraction_status IN ('completed', 'failed', 'not_configured')",
            name="valid_extraction_status",
        ),
        CheckConstraint(
            "verification_status IS NULL OR verification_status IN "
            "('verified', 'amount_mismatch', 'vendor_mismatch', 'date_mismatch', 'needs_review')",
            name="valid_verification_status",
        ),
        UniqueConstraint("stored_filename", name="uq_clarification_receipts_stored_filename"),
        UniqueConstraint("storage_provider", "storage_key", name="uq_receipt_storage_object"),
        UniqueConstraint("id", "business_id", name="uq_clarification_receipts_id_business"),
        ForeignKeyConstraint(["clarification_request_id", "business_id"], ["clarification_requests.id", "clarification_requests.business_id"], name="fk_receipts_clarification_business"),
        Index("ix_clarification_receipts_request_id", "clarification_request_id"),
        Index("ix_clarification_receipts_uploaded_at", "uploaded_at"),
        Index("ix_receipt_evidence_business_provider", "business_id", "storage_provider"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    business_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="RESTRICT", name="fk_clarification_receipts_business"), nullable=False,
    )
    clarification_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "clarification_requests.id",
            ondelete="RESTRICT",
            name="fk_clarification_receipt_request",
        ),
        nullable=False,
    )
    original_filename: Mapped[str | None] = mapped_column(String(512))
    stored_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_provider: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    extraction_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="completed", server_default=text("'completed'")
    )
    merchant_name: Mapped[str | None] = mapped_column(String(255))
    document_type: Mapped[str | None] = mapped_column(String(32))
    document_number: Mapped[str | None] = mapped_column(String(255))
    document_date: Mapped[date | None] = mapped_column(Date)
    total_amount_paise: Mapped[int | None] = mapped_column(BigInteger)
    payment_method: Mapped[str | None] = mapped_column(String(128))
    payment_reference: Mapped[str | None] = mapped_column(String(255))
    confidence: Mapped[dict[str, float | None]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    uncertain_fields: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    extraction_message: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str | None] = mapped_column(String(24))
    verification_reason: Mapped[str | None] = mapped_column(Text)
    verification_details: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    business: Mapped["Business"] = relationship(foreign_keys=[business_id])
    clarification_request: Mapped["ClarificationRequest"] = relationship(
        back_populates="receipts", foreign_keys=[clarification_request_id]
    )
