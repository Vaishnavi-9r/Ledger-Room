"""Monthly bookkeeping report endpoint."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..dependencies.business import BusinessContext, get_business_context
from ..schemas.monthly_report import MonthlyReportResponse
from ..schemas.monthly_report_approval import (
    ApprovalChangesRequest,
    ApprovalReviewRequest,
    MonthlyReportApprovalListResponse,
    MonthlyReportApprovalResponse,
    MonthlyReportApprovalStatusResponse,
)
from ..services.monthly_report_service import get_monthly_report
from ..services.monthly_report_approval_service import (
    ApprovalConflictError,
    ApprovalNotFoundError,
    ApprovalStaleError,
    get_approval_response,
    get_period_approval_response,
    list_pending_approvals,
    review_approval,
    submit_monthly_report,
)
from ..services.report_export_service import (
    build_csv_report,
    build_html_report,
    build_xlsx_report,
)

router = APIRouter(prefix="/api/v1/reports", tags=["Reports"])
logger = logging.getLogger(__name__)


def _require_accountant(context: BusinessContext) -> None:
    if context.role != "accountant":
        raise HTTPException(status_code=403, detail="This operation requires an active business accountant.")


def _approval_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ApprovalNotFoundError):
        return HTTPException(status_code=404, detail="Monthly report approval not found.")
    if isinstance(exc, ApprovalStaleError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ApprovalConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, SQLAlchemyError):
        logger.exception("Monthly report approval operation failed")
        return HTTPException(status_code=503, detail="Monthly report approval is unavailable.")
    raise exc


def _load_report(
    db: Session, year: int, month: int, context: BusinessContext,
) -> MonthlyReportResponse:
    try:
        return get_monthly_report(db, year, month, context.business_id, context.business)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        logger.exception("Monthly report query failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Monthly report could not be loaded.",
        ) from exc


def _load_approval_for_report(
    db: Session, context: BusinessContext, report: MonthlyReportResponse,
):
    from ..services.monthly_report_approval_service import get_period_approval_response

    try:
        period = report.reporting_period
        return get_period_approval_response(
            db, context.business, period.year, period.month, report,
        )
    except SQLAlchemyError as exc:
        logger.exception("Monthly report approval lookup failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Monthly report approval could not be loaded.",
        ) from exc


@router.get("/monthly", response_model=MonthlyReportResponse)
def monthly_report(
    year: int = Query(ge=1900, le=9998),
    month: int = Query(ge=1, le=12),
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> MonthlyReportResponse:
    """Return a read-only, calendar-month report using source record dates."""
    return _load_report(db, year, month, context)


@router.get("/monthly/export/csv", response_class=Response)
def monthly_report_csv(
    year: int = Query(ge=1900, le=9998),
    month: int = Query(ge=1, le=12),
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> Response:
    report = _load_report(db, year, month, context)
    approval = _load_approval_for_report(db, context, report)
    filename = f"ledgerroom_report_{year}_{month:02d}.csv"
    return Response(
        content=build_csv_report(report, approval), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/monthly/export/html", response_class=Response)
def monthly_report_html(
    year: int = Query(ge=1900, le=9998),
    month: int = Query(ge=1, le=12),
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> Response:
    report = _load_report(db, year, month, context)
    approval = _load_approval_for_report(db, context, report)
    filename = f"ledgerroom_report_{year}_{month:02d}.html"
    return Response(
        content=build_html_report(report, approval), media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/monthly/export/xlsx", response_class=Response)
def monthly_report_xlsx(
    year: int = Query(ge=1900, le=9998),
    month: int = Query(ge=1, le=12),
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> Response:
    report = _load_report(db, year, month, context)
    approval = _load_approval_for_report(db, context, report)
    filename = f"ledgerroom_report_{year}_{month:02d}.xlsx"
    return Response(
        content=build_xlsx_report(report, approval),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/monthly/submit", response_model=MonthlyReportApprovalResponse, status_code=201)
def submit_monthly_report_for_review(
    year: int = Query(ge=1900, le=9998),
    month: int = Query(ge=1, le=12),
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> MonthlyReportApprovalResponse:
    """Owner submits the current report snapshot for accountant review."""
    if context.role != "owner":
        raise HTTPException(status_code=403, detail="Only a business owner can submit reports for review.")
    try:
        approval = submit_monthly_report(
            db, context.business, context.principal.user, year, month,
        )
        return get_approval_response(db, context.business, approval.id)
    except (ApprovalNotFoundError, ApprovalConflictError, ApprovalStaleError, ValueError, SQLAlchemyError) as exc:
        raise _approval_http_error(exc) from exc


@router.get("/monthly/approvals", response_model=MonthlyReportApprovalListResponse)
def pending_monthly_report_approvals(
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> MonthlyReportApprovalListResponse:
    """List reports awaiting review in the selected accountant business."""
    _require_accountant(context)
    try:
        return MonthlyReportApprovalListResponse(
            approvals=list_pending_approvals(db, context.business_id)
        )
    except SQLAlchemyError as exc:
        raise _approval_http_error(exc) from exc


@router.get("/monthly/approvals/{approval_id}", response_model=MonthlyReportApprovalResponse)
def monthly_report_approval_detail(
    approval_id: UUID,
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> MonthlyReportApprovalResponse:
    """View one version and its immutable event history for an active member."""
    try:
        return get_approval_response(db, context.business, approval_id)
    except (ApprovalNotFoundError, SQLAlchemyError) as exc:
        raise _approval_http_error(exc) from exc


@router.post("/monthly/approvals/{approval_id}/approve", response_model=MonthlyReportApprovalResponse)
def approve_monthly_report(
    approval_id: UUID,
    payload: ApprovalReviewRequest | None = None,
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> MonthlyReportApprovalResponse:
    _require_accountant(context)
    try:
        review_approval(
            db, context.business, approval_id, context.principal.user,
            "approved", payload.comments if payload else None,
        )
        return get_approval_response(db, context.business, approval_id)
    except (ApprovalNotFoundError, ApprovalConflictError, ApprovalStaleError, ValueError, SQLAlchemyError) as exc:
        raise _approval_http_error(exc) from exc


@router.post("/monthly/approvals/{approval_id}/request-changes", response_model=MonthlyReportApprovalResponse)
def request_monthly_report_changes(
    approval_id: UUID,
    payload: ApprovalChangesRequest,
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> MonthlyReportApprovalResponse:
    _require_accountant(context)
    try:
        review_approval(
            db, context.business, approval_id, context.principal.user,
            "changes_requested", payload.comments,
        )
        return get_approval_response(db, context.business, approval_id)
    except (ApprovalNotFoundError, ApprovalConflictError, ApprovalStaleError, ValueError, SQLAlchemyError) as exc:
        raise _approval_http_error(exc) from exc


@router.get("/monthly/approval-status", response_model=MonthlyReportApprovalStatusResponse)
def monthly_report_approval_status(
    year: int = Query(ge=1900, le=9998),
    month: int = Query(ge=1, le=12),
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> MonthlyReportApprovalStatusResponse:
    """Return the latest version and derive stale status from the current fingerprint."""
    try:
        return MonthlyReportApprovalStatusResponse(
            approval=get_period_approval_response(db, context.business, year, month)
        )
    except SQLAlchemyError as exc:
        raise _approval_http_error(exc) from exc
