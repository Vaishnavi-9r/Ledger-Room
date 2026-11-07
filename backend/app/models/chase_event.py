"""Follow-up and audit event model."""

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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base


class ChaseEvent(Base):
    """A recorded invoice, bank-line, or match follow-up event."""

    __tablename__ = "chase_events"
    __table_args__ = (
        CheckConstraint(
            "invoice_id IS NOT NULL OR bank_line_id IS NOT NULL OR match_id IS NOT NULL",
            name="requires_related_record",
        ),
        ForeignKeyConstraint(["invoice_id", "business_id"], ["invoices.id", "invoices.business_id"], name="fk_chase_invoice_business"),
        ForeignKeyConstraint(["bank_line_id", "business_id"], ["bank_lines.id", "bank_lines.business_id"], name="fk_chase_bank_business"),
        ForeignKeyConstraint(["clarification_request_id", "business_id"], ["clarification_requests.id", "clarification_requests.business_id"], name="fk_chase_clarification_business"),
        ForeignKeyConstraint(["match_id", "business_id"], ["matches.id", "matches.business_id"], name="fk_chase_match_business"),
        Index("ix_chase_events_created_at", "created_at"),
        Index("ix_chase_events_event_type", "event_type"),
        Index("ix_chase_events_invoice_id", "invoice_id"),
        Index("ix_chase_events_bank_line_id", "bank_line_id"),
        Index("ix_chase_events_match_id", "match_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    business_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="RESTRICT", name="fk_chase_events_business"), nullable=False,
    )
    invoice_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("invoices.id", ondelete="SET NULL")
    )
    bank_line_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("bank_lines.id", ondelete="SET NULL")
    )
    clarification_request_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("clarification_requests.id", ondelete="RESTRICT"),
    )
    match_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("matches.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    business: Mapped["Business"] = relationship(foreign_keys=[business_id])
    invoice: Mapped["Invoice | None"] = relationship(back_populates="chase_events", foreign_keys=[invoice_id])
    bank_line: Mapped["BankLine | None"] = relationship(back_populates="chase_events", foreign_keys=[bank_line_id])
    clarification_request: Mapped["ClarificationRequest | None"] = relationship(
        back_populates="chase_events", foreign_keys=[clarification_request_id]
    )
    match: Mapped["Match | None"] = relationship(back_populates="chase_events", foreign_keys=[match_id])
