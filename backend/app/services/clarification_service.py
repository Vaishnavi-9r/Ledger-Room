"""Creation and owner-response operations for clarification requests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models import ChaseEvent, ClarificationRequest
from ..schemas.clarification import ClarificationRequestResponse
from ..schemas.reconciliation import MissingInvoiceTransactionResponse


class ClarificationNotFoundError(Exception):
    """Raised when a clarification request ID does not exist."""


class ClarificationStateConflictError(Exception):
    """Raised when a request is no longer open for an owner response."""


def _question_for(transaction: MissingInvoiceTransactionResponse) -> str:
    whole_rupees, paise = divmod(transaction.amount_paise, 100)
    amount_display = f"₹{whole_rupees:,}.{paise:02d}"
    return (
        f"We found a bank debit of {amount_display} to {transaction.description} "
        f"on {transaction.transaction_date.isoformat()} but could not find an invoice. "
        "Please reply with what this transaction was for or provide its receipt."
    )


def create_clarifications_for_missing_transactions(
    db: Session,
    transactions: list[MissingInvoiceTransactionResponse],
    business_id: UUID,
) -> None:
    """Create one lifetime request per unmatched positive bank line.

    The bank_line_id uniqueness constraint intentionally retains one request
    row for the lifetime of a transaction. Terminal requests are reused rather
    than recreated; ChaseEvent provides append-only history for that lifecycle.
    """
    positive_transactions = [row for row in transactions if row.amount_paise > 0]
    if not positive_transactions:
        return

    bank_line_ids = [UUID(row.bank_line_id) for row in positive_transactions]
    existing_requests = db.scalars(
        select(ClarificationRequest).where(
            ClarificationRequest.business_id == business_id,
            ClarificationRequest.bank_line_id.in_(bank_line_ids)
        )
    ).all()
    requests_by_bank_line = {
        request.bank_line_id: request for request in existing_requests
    }

    for transaction in positive_transactions:
        bank_line_id = UUID(transaction.bank_line_id)
        if bank_line_id in requests_by_bank_line:
            existing = requests_by_bank_line[bank_line_id]
            transaction.clarification_request_id = str(existing.id)
            continue

        now = datetime.now(timezone.utc)
        request = ClarificationRequest(
            id=uuid4(),
            business_id=business_id,
            bank_line_id=bank_line_id,
            status="pending",
            question=_question_for(transaction),
            updated_at=now,
        )
        db.add(request)
        db.flush()
        db.add(
            ChaseEvent(
                id=uuid4(),
                business_id=business_id,
                bank_line_id=bank_line_id,
                clarification_request_id=request.id,
                event_type="clarification_created",
                details={
                    "clarification_request_id": str(request.id),
                    "status": "pending",
                    "question": request.question,
                },
            )
        )
        requests_by_bank_line[bank_line_id] = request
        transaction.clarification_request_id = str(request.id)


def respond_to_clarification(
    db: Session, business_id: UUID, request_id: UUID, owner_response: str,
    authorization_check: Callable[[Session], None] | None = None,
) -> ClarificationRequest:
    """Record one owner response, atomically advancing pending to answered."""
    answer = owner_response.strip()
    if not answer:
        raise ValueError("Owner response must not be blank.")

    try:
        with db.begin():
            if authorization_check is not None:
                authorization_check(db)
            request = db.scalar(
                select(ClarificationRequest).where(
                    ClarificationRequest.id == request_id,
                    ClarificationRequest.business_id == business_id,
                ).with_for_update()
            )
            if request is None:
                raise ClarificationNotFoundError("Clarification request not found.")
            if request.status != "pending":
                raise ClarificationStateConflictError(
                    "This clarification request is no longer pending."
                )

            now = datetime.now(timezone.utc)
            request.owner_response = answer
            request.status = "answered"
            request.updated_at = now
            db.add(
                ChaseEvent(
                    id=uuid4(),
                    business_id=business_id,
                    bank_line_id=request.bank_line_id,
                    clarification_request_id=request.id,
                    event_type="clarification_owner_responded",
                    details={
                        "clarification_request_id": str(request.id),
                        "previous_status": "pending",
                        "status": "answered",
                        "owner_response": answer,
                    },
                )
            )
            db.flush()
    except SQLAlchemyError:
        db.rollback()
        raise
    return request


def clarification_response(
    request: ClarificationRequest,
) -> ClarificationRequestResponse:
    """Build the API representation including the referenced bank transaction."""
    bank_line = request.bank_line
    return ClarificationRequestResponse(
        id=str(request.id),
        bank_line_id=str(request.bank_line_id),
        status=request.status,  # type: ignore[arg-type]
        question=request.question,
        owner_response=request.owner_response,
        created_at=request.created_at,
        updated_at=request.updated_at,
        resolved_at=request.resolved_at,
        bank_description=bank_line.description,
        transaction_date=bank_line.transaction_date,
        amount_paise=bank_line.amount_paise,
    )
