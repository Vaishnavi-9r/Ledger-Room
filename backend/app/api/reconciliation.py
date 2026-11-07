"""Deterministic invoice-to-bank reconciliation endpoint."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..dependencies.business import BusinessContext, get_business_context, require_owner
from ..models import BankLine, Match, MatchEvent
from ..schemas.reconciliation import (
    ReconciliationHistoryEvent,
    ReconciliationHistoryMatch,
    ReconciliationHistoryResponse,
    ReconciliationRunResponse,
)
from ..services.reconciliation_service import run_reconciliation

router = APIRouter(prefix="/api/v1/reconciliation", tags=["Reconciliation"])
logger = logging.getLogger(__name__)


@router.post("/run", response_model=ReconciliationRunResponse)
def reconcile(
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> ReconciliationRunResponse:
    """Evaluate invoices and persist deterministic reconciliation decisions."""
    require_owner(context)
    try:
        return run_reconciliation(db, context.business_id)
    except SQLAlchemyError as exc:
        logger.error(
            "Reconciliation persistence failed exception_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reconciliation results could not be saved.",
        ) from exc


@router.get("/history", response_model=ReconciliationHistoryResponse)
def reconciliation_history(
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> ReconciliationHistoryResponse:
    """List current decisions and audit events for this business only."""
    matches = db.scalars(select(Match).where(
        Match.business_id == context.business_id,
    ).order_by(Match.updated_at.desc())).all()
    events = db.scalars(select(MatchEvent).where(
        MatchEvent.business_id == context.business_id,
    ).order_by(MatchEvent.changed_at.desc())).all()
    valid_bank_line_ids = set(db.scalars(select(BankLine.id).where(
        BankLine.business_id == context.business_id,
    )).all())
    return ReconciliationHistoryResponse(
        matches=[ReconciliationHistoryMatch(
            match_id=row.id, invoice_id=row.invoice_id, bank_line_id=row.bank_line_id,
            status=row.status, notes=row.notes,
            candidate_bank_line_ids=[
                value for value in (row.candidate_bank_line_ids or [])
                if _same_business_bank_line(value, valid_bank_line_ids)
            ],
            decision_source=row.decision_source, updated_at=row.updated_at,
        ) for row in matches],
        events=[ReconciliationHistoryEvent(
            event_id=row.id, match_id=row.match_id, invoice_id=row.invoice_id,
            previous_bank_line_id=row.previous_bank_line_id,
            new_bank_line_id=row.new_bank_line_id,
            previous_status=row.previous_status, new_status=row.new_status,
            previous_decision_source=row.previous_decision_source,
            new_decision_source=row.new_decision_source, changed_at=row.changed_at,
        ) for row in events],
    )


def _same_business_bank_line(value: str, allowed_ids: set) -> bool:
    try:
        from uuid import UUID
        return UUID(str(value)) in allowed_ids
    except (TypeError, ValueError, AttributeError):
        return False
