"""Versioned accountant approval state and append-only decision history."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index,
    Integer, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base


APPROVAL_STATUSES = ("draft", "pending_review", "changes_requested", "approved", "stale")


class MonthlyReportApproval(Base):
    __tablename__ = "monthly_report_approvals"
    __table_args__ = (
        UniqueConstraint("business_id", "id", name="uq_report_approval_business_id"),
        UniqueConstraint(
            "business_id", "report_year", "report_month", "version",
            name="uq_report_approval_period_version",
        ),
        CheckConstraint("report_year BETWEEN 1900 AND 9998", name="ck_report_approval_year"),
        CheckConstraint("report_month BETWEEN 1 AND 12", name="ck_report_approval_month"),
        CheckConstraint("version >= 1", name="ck_report_approval_version"),
        CheckConstraint(
            "status IN ('draft', 'pending_review', 'changes_requested', 'approved', 'stale')",
            name="ck_report_approval_status",
        ),
        CheckConstraint(
            "approval_decision IS NULL OR approval_decision IN ('approved', 'changes_requested')",
            name="ck_report_approval_decision",
        ),
        Index(
            "uq_report_approval_pending_period",
            "business_id", "report_year", "report_month", unique=True,
            postgresql_where=text("status = 'pending_review'"),
        ),
        Index("ix_report_approval_business_period", "business_id", "report_year", "report_month"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    business_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="RESTRICT", name="fk_report_approval_business"),
        nullable=False,
    )
    report_year: Mapped[int] = mapped_column(Integer, nullable=False)
    report_month: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    submitted_by: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_report_approval_submitter"),
        nullable=False,
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    reviewed_by: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_report_approval_reviewer"),
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accountant_comments: Mapped[str | None] = mapped_column(Text)
    approval_decision: Mapped[str | None] = mapped_column(String(24))
    report_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    business: Mapped["Business"] = relationship(back_populates="monthly_report_approvals")
    submitter: Mapped["User"] = relationship(foreign_keys=[submitted_by], back_populates="submitted_report_approvals")
    reviewer: Mapped["User | None"] = relationship(foreign_keys=[reviewed_by], back_populates="reviewed_report_approvals")
    events: Mapped[list["MonthlyReportApprovalEvent"]] = relationship(
        back_populates="approval", foreign_keys="MonthlyReportApprovalEvent.approval_id",
        order_by="(MonthlyReportApprovalEvent.created_at, MonthlyReportApprovalEvent.id)",
    )


class MonthlyReportApprovalEvent(Base):
    __tablename__ = "monthly_report_approval_events"
    __table_args__ = (
        UniqueConstraint("business_id", "id", name="uq_report_approval_event_business_id"),
        ForeignKeyConstraint(
            ["business_id", "approval_id"],
            ["monthly_report_approvals.business_id", "monthly_report_approvals.id"],
            name="fk_report_approval_event_business",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_type IN ('submitted', 'resubmitted', 'approved', 'changes_requested', 'stale')",
            name="ck_report_approval_event_type",
        ),
        Index("ix_report_approval_event_period", "business_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    business_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="RESTRICT", name="fk_report_approval_event_tenant"),
        nullable=False,
    )
    approval_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_report_approval_event_actor"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(24))
    resulting_status: Mapped[str] = mapped_column(String(24), nullable=False)
    comments: Mapped[str | None] = mapped_column(Text)
    report_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    approval: Mapped[MonthlyReportApproval] = relationship(
        back_populates="events", foreign_keys=[approval_id],
    )
    actor: Mapped["User"] = relationship(back_populates="report_approval_events")
