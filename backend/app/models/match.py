"""Invoice-to-bank-line match persistence model."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base


class Match(Base):
    """A candidate or confirmed association between an invoice and bank line."""

    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("invoice_id", "bank_line_id", name="uq_matches_invoice_bank_line"),
        UniqueConstraint("bank_line_id", name="uq_matches_bank_line_id"),
        UniqueConstraint("id", "business_id", name="uq_matches_id_business"),
        ForeignKeyConstraint(["invoice_id", "business_id"], ["invoices.id", "invoices.business_id"], name="fk_matches_invoice_business"),
        ForeignKeyConstraint(["bank_line_id", "business_id"], ["bank_lines.id", "bank_lines.business_id"], name="fk_matches_bank_business"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_between_zero_and_one",
        ),
        Index("ix_matches_status", "status"),
        Index("ix_matches_invoice_id", "invoice_id"),
        Index("ix_matches_bank_line_id", "bank_line_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    business_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="RESTRICT", name="fk_matches_business"), nullable=False,
    )
    invoice_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False
    )
    bank_line_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("bank_lines.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="suggested",
        server_default=text("'suggested'"),
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    notes: Mapped[str | None] = mapped_column(String(1000))
    candidate_bank_line_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    decision_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="human", server_default=text("'human'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    business: Mapped["Business"] = relationship(foreign_keys=[business_id])
    invoice: Mapped["Invoice"] = relationship(back_populates="matches", foreign_keys=[invoice_id])
    bank_line: Mapped["BankLine | None"] = relationship(back_populates="matches", foreign_keys=[bank_line_id])
    chase_events: Mapped[list["ChaseEvent"]] = relationship(back_populates="match", foreign_keys="ChaseEvent.match_id")
    events: Mapped[list["MatchEvent"]] = relationship(back_populates="match", foreign_keys="MatchEvent.match_id")
