"""Versioned approval workflow for tenant-scoped monthly reports."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from ..models import (
    Business, MonthlyReportApproval, MonthlyReportApprovalEvent, User,
)
from ..schemas.monthly_report import MonthlyReportResponse
from ..schemas.monthly_report_approval import (
    MonthlyReportApprovalEventResponse,
    MonthlyReportApprovalResponse,
)
from .monthly_report_service import get_monthly_report


class ApprovalNotFoundError(Exception):
    pass


class ApprovalConflictError(Exception):
    pass


class ApprovalStaleError(Exception):
    pass


def report_fingerprint(report: MonthlyReportResponse) -> str:
    """Hash deterministic report evidence, excluding only generated_at."""
    payload: dict[str, Any] = report.model_dump(mode="json", exclude={"generated_at"})
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _with_details(statement: Any) -> Any:
    return statement.options(
        selectinload(MonthlyReportApproval.submitter),
        selectinload(MonthlyReportApproval.reviewer),
        selectinload(MonthlyReportApproval.events).selectinload(
            MonthlyReportApprovalEvent.actor
        ),
    )


def _to_response(
    approval: MonthlyReportApproval, *, effective_status: str | None = None,
) -> MonthlyReportApprovalResponse:
    events = sorted(
        list(approval.events or []),
        key=lambda event: (event.created_at, str(event.id)),
    )
    return MonthlyReportApprovalResponse(
        approval_id=approval.id,
        business_id=approval.business_id,
        report_year=approval.report_year,
        report_month=approval.report_month,
        version=approval.version,
        status=effective_status or approval.status,
        submitted_by=approval.submitted_by,
        submitted_by_name=approval.submitter.full_name if approval.submitter else "Unavailable",
        submitted_at=approval.submitted_at,
        reviewed_by=approval.reviewed_by,
        reviewed_by_name=approval.reviewer.full_name if approval.reviewer else None,
        reviewed_at=approval.reviewed_at,
        accountant_comments=approval.accountant_comments,
        approval_decision=approval.approval_decision,
        report_fingerprint=approval.report_fingerprint,
        events=[
            MonthlyReportApprovalEventResponse(
                event_id=event.id,
                actor_user_id=event.actor_user_id,
                actor_name=event.actor.full_name if event.actor else "Unavailable",
                event_type=event.event_type,
                previous_status=event.previous_status,
                resulting_status=event.resulting_status,
                comments=event.comments,
                report_fingerprint=event.report_fingerprint,
                created_at=event.created_at,
            )
            for event in events
        ],
    )


def _append_event(
    db: Session,
    approval: MonthlyReportApproval,
    actor: User,
    event_type: str,
    previous_status: str | None,
    resulting_status: str,
    fingerprint: str,
    comments: str | None = None,
) -> MonthlyReportApprovalEvent:
    event = MonthlyReportApprovalEvent(
        business_id=approval.business_id,
        approval_id=approval.id,
        actor_user_id=actor.id,
        event_type=event_type,
        previous_status=previous_status,
        resulting_status=resulting_status,
        comments=comments,
        report_fingerprint=fingerprint,
        created_at=datetime.now(timezone.utc),
        actor=actor,
    )
    db.add(event)
    approval.events.append(event)
    return event


def _latest_statement(business_id: UUID, year: int, month: int) -> Any:
    return (
        select(MonthlyReportApproval)
        .where(
            MonthlyReportApproval.business_id == business_id,
            MonthlyReportApproval.report_year == year,
            MonthlyReportApproval.report_month == month,
        )
        .order_by(MonthlyReportApproval.version.desc())
        .limit(1)
    )


def latest_approval(
    db: Session, business_id: UUID, year: int, month: int,
) -> MonthlyReportApproval | None:
    return db.scalar(_with_details(_latest_statement(business_id, year, month)))


def _current_fingerprint(
    db: Session, business: Business, year: int, month: int,
) -> str:
    report = get_monthly_report(db, year, month, business.id, business)
    return report_fingerprint(report)


def submit_monthly_report(
    db: Session, business: Business, owner: User, year: int, month: int,
) -> MonthlyReportApproval:
    """Submit a report version, serializing submissions for one business period."""
    now = datetime.now(timezone.utc)
    with db.begin():
        # Serializes the no-existing-version case as well as version increments.
        locked_business = db.scalar(
            select(Business.id).where(Business.id == business.id).with_for_update()
        )
        if locked_business is None:
            raise ApprovalNotFoundError
        fingerprint = _current_fingerprint(db, business, year, month)
        previous = db.scalar(
            _with_details(_latest_statement(business.id, year, month).with_for_update())
        )
        previous_status = previous.status if previous else None
        if previous and previous.status == "pending_review":
            if previous.report_fingerprint == fingerprint:
                raise ApprovalConflictError("A report version is already awaiting review.")
            old_status = previous.status
            previous.status = "stale"
            previous.updated_at = now
            _append_event(
                db, previous, owner, "stale", old_status, "stale",
                previous.report_fingerprint,
                "Report data changed after submission; a new version was submitted.",
            )
        elif previous and previous.status == "approved":
            if previous.report_fingerprint == fingerprint:
                raise ApprovalConflictError("This report version is already approved.")
            old_status = previous.status
            previous.status = "stale"
            previous.updated_at = now
            _append_event(
                db, previous, owner, "stale", old_status, "stale",
                previous.report_fingerprint,
                "Report data changed after approval; a new version was submitted.",
            )

        version = previous.version + 1 if previous else 1
        approval = MonthlyReportApproval(
            business_id=business.id,
            report_year=year,
            report_month=month,
            version=version,
            status="pending_review",
            submitted_by=owner.id,
            submitted_at=now,
            report_fingerprint=fingerprint,
            created_at=now,
            updated_at=now,
            submitter=owner,
            events=[],
        )
        db.add(approval)
        db.flush()
        event = _append_event(
            db, approval, owner,
            "resubmitted" if previous else "submitted",
            previous_status,
            "pending_review", fingerprint,
        )
    return approval


def list_pending_approvals(
    db: Session, business_id: UUID,
) -> list[MonthlyReportApprovalResponse]:
    statement = _with_details(
        select(MonthlyReportApproval)
        .where(
            MonthlyReportApproval.business_id == business_id,
            MonthlyReportApproval.status == "pending_review",
        )
        .order_by(
            MonthlyReportApproval.submitted_at,
            MonthlyReportApproval.report_year,
            MonthlyReportApproval.report_month,
        )
    )
    return [_to_response(row) for row in db.scalars(statement).all()]


def get_approval_response(
    db: Session, business: Business, approval_id: UUID,
    current_report: MonthlyReportResponse | None = None,
) -> MonthlyReportApprovalResponse:
    approval = db.scalar(_with_details(select(MonthlyReportApproval).where(
        MonthlyReportApproval.id == approval_id,
        MonthlyReportApproval.business_id == business.id,
    )))
    if approval is None:
        raise ApprovalNotFoundError
    latest = latest_approval(
        db, business.id, approval.report_year, approval.report_month,
    )
    effective_status = approval.status
    if latest is not None and latest.id == approval.id:
        current = report_fingerprint(current_report) if current_report is not None else _current_fingerprint(
            db, business, approval.report_year, approval.report_month
        )
        if current != approval.report_fingerprint:
            effective_status = "stale"
    return _to_response(approval, effective_status=effective_status)


def get_period_approval_response(
    db: Session, business: Business, year: int, month: int,
    current_report: MonthlyReportResponse | None = None,
) -> MonthlyReportApprovalResponse | None:
    approval = latest_approval(db, business.id, year, month)
    if approval is None:
        return None
    current = report_fingerprint(current_report) if current_report is not None else _current_fingerprint(
        db, business, year, month
    )
    status_value = "stale" if current != approval.report_fingerprint else approval.status
    return _to_response(approval, effective_status=status_value)


def _locked_approval(
    db: Session, business_id: UUID, approval_id: UUID,
) -> MonthlyReportApproval:
    approval = db.scalar(
        _with_details(select(MonthlyReportApproval).where(
            MonthlyReportApproval.id == approval_id,
            MonthlyReportApproval.business_id == business_id,
        ).with_for_update())
    )
    if approval is None:
        raise ApprovalNotFoundError
    return approval


def review_approval(
    db: Session,
    business: Business,
    approval_id: UUID,
    accountant: User,
    decision: str,
    comments: str | None = None,
) -> MonthlyReportApproval:
    """Approve or return one pending version, atomically with an audit event."""
    normalized_comments = comments.strip() if comments else None
    if decision == "changes_requested" and not normalized_comments:
        raise ValueError("A correction reason is required.")
    if decision not in {"approved", "changes_requested"}:
        raise ValueError("Unsupported report decision.")
    now = datetime.now(timezone.utc)
    stale = False
    with db.begin():
        approval = _locked_approval(db, business.id, approval_id)
        if approval.status != "pending_review":
            raise ApprovalConflictError("This report is no longer awaiting review.")
        if approval.submitted_by == accountant.id:
            raise ApprovalConflictError("The report submitter cannot review their own submission.")
        current = _current_fingerprint(db, business, approval.report_year, approval.report_month)
        if current != approval.report_fingerprint:
            old_status = approval.status
            approval.status = "stale"
            approval.updated_at = now
            _append_event(
                db, approval, accountant, "stale", old_status, "stale", current,
                "Report data changed after submission; a fresh version is required.",
            )
            stale = True
        else:
            old_status = approval.status
            approval.status = decision
            approval.approval_decision = decision
            approval.reviewed_by = accountant.id
            approval.reviewed_at = now
            approval.accountant_comments = normalized_comments
            approval.updated_at = now
            approval.reviewer = accountant
            event = _append_event(
                db, approval, accountant, decision, old_status, decision,
                current, normalized_comments,
            )
    if stale:
        raise ApprovalStaleError("Report data changed; submit a new version before review.")
    return approval
