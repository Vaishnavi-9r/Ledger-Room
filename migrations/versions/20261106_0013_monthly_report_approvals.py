"""Add versioned monthly report review and immutable decision events.

Revision ID: 20261106_0013
Revises: 20261106_0012
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261106_0013"
down_revision: Union[str, None] = "20261106_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "monthly_report_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_year", sa.Integer(), nullable=False),
        sa.Column("report_month", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("submitted_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accountant_comments", sa.Text(), nullable=True),
        sa.Column("approval_decision", sa.String(length=24), nullable=True),
        sa.Column("report_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("report_year BETWEEN 1900 AND 9998", name="ck_report_approval_year"),
        sa.CheckConstraint("report_month BETWEEN 1 AND 12", name="ck_report_approval_month"),
        sa.CheckConstraint("version >= 1", name="ck_report_approval_version"),
        sa.CheckConstraint("status IN ('draft', 'pending_review', 'changes_requested', 'approved', 'stale')", name="ck_report_approval_status"),
        sa.CheckConstraint("approval_decision IS NULL OR approval_decision IN ('approved', 'changes_requested')", name="ck_report_approval_decision"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], name="fk_report_approval_business", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["submitted_by"], ["users.id"], name="fk_report_approval_submitter", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], name="fk_report_approval_reviewer", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "id", name="uq_report_approval_business_id"),
        sa.UniqueConstraint("business_id", "report_year", "report_month", "version", name="uq_report_approval_period_version"),
    )
    op.create_index(
        "uq_report_approval_pending_period", "monthly_report_approvals",
        ["business_id", "report_year", "report_month"], unique=True,
        postgresql_where=sa.text("status = 'pending_review'"),
    )
    op.create_index(
        "ix_report_approval_business_period", "monthly_report_approvals",
        ["business_id", "report_year", "report_month"], unique=False,
    )

    op.create_table(
        "monthly_report_approval_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("previous_status", sa.String(length=24), nullable=True),
        sa.Column("resulting_status", sa.String(length=24), nullable=False),
        sa.Column("comments", sa.Text(), nullable=True),
        sa.Column("report_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("event_type IN ('submitted', 'resubmitted', 'approved', 'changes_requested', 'stale')", name="ck_report_approval_event_type"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], name="fk_report_approval_event_tenant", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], name="fk_report_approval_event_actor", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["business_id", "approval_id"],
            ["monthly_report_approvals.business_id", "monthly_report_approvals.id"],
            name="fk_report_approval_event_business", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "id", name="uq_report_approval_event_business_id"),
    )
    op.create_index(
        "ix_report_approval_event_period", "monthly_report_approval_events",
        ["business_id", "created_at"], unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_events = bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM monthly_report_approval_events)"
    )).scalar()
    has_approvals = bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM monthly_report_approvals)"
    )).scalar()
    if has_events or has_approvals:
        raise RuntimeError(
            "Cannot downgrade 20261106_0013 while monthly report approvals or audit events exist; "
            "export and archive the approval history before an explicitly reviewed removal."
        )
    op.drop_index("ix_report_approval_event_period", table_name="monthly_report_approval_events")
    op.drop_table("monthly_report_approval_events")
    op.drop_index("ix_report_approval_business_period", table_name="monthly_report_approvals")
    op.drop_index("uq_report_approval_pending_period", table_name="monthly_report_approvals")
    op.drop_table("monthly_report_approvals")
