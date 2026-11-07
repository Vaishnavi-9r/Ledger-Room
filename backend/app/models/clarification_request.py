"""Persistent owner clarification requests for unmatched bank transactions."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base


class ClarificationRequest(Base):
    """One durable clarification lifecycle per bank transaction."""

    __tablename__ = "clarification_requests"
    __table_args__ = (
        UniqueConstraint("bank_line_id", name="uq_clarification_requests_bank_line_id"),
        UniqueConstraint("id", "business_id", name="uq_clarification_requests_id_business"),
        ForeignKeyConstraint(["bank_line_id", "business_id"], ["bank_lines.id", "bank_lines.business_id"], name="fk_clarification_bank_business"),
        CheckConstraint(
            "status IN ('pending', 'answered', 'receipt_received', 'resolved', 'dismissed')",
            name="valid_status",
        ),
        Index("ix_clarification_requests_status", "status"),
        Index("ix_clarification_requests_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    business_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="RESTRICT", name="fk_clarification_requests_business"), nullable=False,
    )
    bank_line_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("bank_lines.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default=text("'pending'")
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    owner_response: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    business: Mapped["Business"] = relationship(foreign_keys=[business_id])
    bank_line: Mapped["BankLine"] = relationship(
        back_populates="clarification_request", foreign_keys=[bank_line_id]
    )
    chase_events: Mapped[list["ChaseEvent"]] = relationship(
        back_populates="clarification_request", foreign_keys="ChaseEvent.clarification_request_id"
    )
    receipts: Mapped[list["ClarificationReceipt"]] = relationship(
        back_populates="clarification_request", foreign_keys="ClarificationReceipt.clarification_request_id",
        order_by="ClarificationReceipt.uploaded_at"
    )
