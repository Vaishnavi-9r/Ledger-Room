"""Bank statement line persistence model."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base


class BankLine(Base):
    """A bank transaction line, with a signed integer amount in paise."""

    __tablename__ = "bank_lines"
    __table_args__ = (
        UniqueConstraint("id", "business_id", name="uq_bank_lines_id_business"),
        Index("ix_bank_lines_transaction_date", "transaction_date"),
        Index("ix_bank_lines_amount_paise", "amount_paise"),
        Index("ix_bank_lines_external_reference", "external_reference"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    business_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="RESTRICT", name="fk_bank_lines_business"), nullable=False,
    )
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3))
    external_reference: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    business: Mapped["Business"] = relationship(foreign_keys=[business_id])
    matches: Mapped[list["Match"]] = relationship(back_populates="bank_line", foreign_keys="Match.bank_line_id")
    chase_events: Mapped[list["ChaseEvent"]] = relationship(back_populates="bank_line", foreign_keys="ChaseEvent.bank_line_id")
    clarification_request: Mapped["ClarificationRequest | None"] = relationship(
        back_populates="bank_line", foreign_keys="ClarificationRequest.bank_line_id"
    )
