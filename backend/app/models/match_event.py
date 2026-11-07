"""Append-only audit snapshots for changes to reconciliation decisions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Index, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base


class MatchEvent(Base):
    """A full before-and-after snapshot for a changed match decision."""

    __tablename__ = "match_events"
    __table_args__ = (
        UniqueConstraint("id", "business_id", name="uq_match_events_id_business"),
        ForeignKeyConstraint(["match_id", "business_id"], ["matches.id", "matches.business_id"], name="fk_match_events_match_business"),
        ForeignKeyConstraint(["invoice_id", "business_id"], ["invoices.id", "invoices.business_id"], name="fk_match_events_invoice_business"),
        ForeignKeyConstraint(["previous_bank_line_id", "business_id"], ["bank_lines.id", "bank_lines.business_id"], name="fk_match_events_previous_bank_business"),
        ForeignKeyConstraint(["new_bank_line_id", "business_id"], ["bank_lines.id", "bank_lines.business_id"], name="fk_match_events_new_bank_business"),
        Index("ix_match_events_match_id", "match_id"),
        Index("ix_match_events_invoice_id", "invoice_id"),
        Index("ix_match_events_changed_at", "changed_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    business_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="RESTRICT", name="fk_match_events_business"), nullable=False,
    )
    match_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("matches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    invoice_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    previous_bank_line_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("bank_lines.id", ondelete="RESTRICT"),
    )
    previous_status: Mapped[str] = mapped_column(String(24), nullable=False)
    previous_notes: Mapped[str | None] = mapped_column(Text)
    previous_candidate_bank_line_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    previous_decision_source: Mapped[str] = mapped_column(String(16), nullable=False)
    previous_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    previous_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    new_bank_line_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("bank_lines.id", ondelete="RESTRICT"),
    )
    new_status: Mapped[str] = mapped_column(String(24), nullable=False)
    new_notes: Mapped[str | None] = mapped_column(Text)
    new_candidate_bank_line_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    new_decision_source: Mapped[str] = mapped_column(String(16), nullable=False)
    new_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    business: Mapped["Business"] = relationship(foreign_keys=[business_id])
    match: Mapped["Match"] = relationship(back_populates="events", foreign_keys=[match_id])
