"""Deterministic receipt evidence comparison against its linked bank line."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..models import BankLine, ChaseEvent, ClarificationReceipt, ClarificationRequest
from ..schemas.receipt_verification import (
    ReceiptVerificationResponse,
    ReceiptVerificationStatus,
)
from .reconciliation_service import normalize_vendor_name, vendor_similarity

logger = logging.getLogger(__name__)
VENDOR_SIMILARITY_THRESHOLD = 0.90
CORE_FIELDS = (
    "merchant_name",
    "document_type",
    "document_date",
    "total_amount_paise",
)


class ReceiptVerificationNotFoundError(Exception):
    """Raised when the requested receipt or clarification link is absent."""


class ReceiptVerificationUnavailableError(Exception):
    """Raised when no stored verification result exists yet."""


class ReceiptVerificationPersistenceError(Exception):
    """Raised when verification could not be stored atomically."""


@dataclass(frozen=True)
class VerificationDecision:
    status: ReceiptVerificationStatus
    reason: str
    details: dict[str, Any]


def evaluate_receipt_against_bank_line(
    receipt: ClarificationReceipt,
    bank_line: BankLine,
    date_window_days: int,
) -> VerificationDecision:
    """Compare extraction values with integer amounts and deterministic name/date rules."""
    merchant = normalize_vendor_name(receipt.merchant_name)
    description = normalize_vendor_name(bank_line.description)
    similarity = vendor_similarity(receipt.merchant_name, bank_line.description)
    date_difference = (
        (receipt.document_date - bank_line.transaction_date).days
        if receipt.document_date is not None
        else None
    )
    uncertain = set(receipt.uncertain_fields or [])
    confidence = receipt.confidence or {}
    missing_or_uncertain = [
        field
        for field in CORE_FIELDS
        if getattr(receipt, field) is None
        or field in uncertain
        or confidence.get(field) is None
    ]

    details: dict[str, Any] = {
        "receipt_amount_paise": receipt.total_amount_paise,
        "bank_amount_paise": bank_line.amount_paise,
        "merchant_name": receipt.merchant_name,
        "bank_description": bank_line.description,
        "normalized_merchant": merchant or None,
        "normalized_description": description,
        "vendor_similarity": round(similarity, 6) if merchant and description else None,
        "document_type": receipt.document_type,
        "document_date": (
            receipt.document_date.isoformat() if receipt.document_date else None
        ),
        "transaction_date": bank_line.transaction_date.isoformat(),
        "date_difference_days": date_difference,
        "allowed_date_window_days": date_window_days,
        "uncertain_fields": sorted(uncertain),
        "missing_or_uncertain_core_fields": missing_or_uncertain,
    }

    if missing_or_uncertain:
        reason = (
            "Review required because core receipt fields are missing or uncertain: "
            + ", ".join(missing_or_uncertain)
            + "."
        )
        return VerificationDecision("needs_review", reason, details)
    if bank_line.amount_paise <= 0:
        return VerificationDecision(
            "needs_review",
            "The linked bank transaction is not a positive debit, so receipt verification cannot confirm a vendor payment.",
            details,
        )
    if receipt.total_amount_paise != bank_line.amount_paise:
        return VerificationDecision(
            "amount_mismatch",
            f"Receipt amount {receipt.total_amount_paise} paise does not equal bank debit {bank_line.amount_paise} paise.",
            details,
        )
    if similarity < VENDOR_SIMILARITY_THRESHOLD:
        return VerificationDecision(
            "vendor_mismatch",
            f"Normalized merchant and bank description similarity is {similarity:.1%}, below the {VENDOR_SIMILARITY_THRESHOLD:.0%} threshold.",
            details,
        )
    if abs(date_difference) > date_window_days:
        return VerificationDecision(
            "date_mismatch",
            f"Receipt date differs from the bank transaction date by {abs(date_difference)} days, outside the allowed {date_window_days}-day window.",
            details,
        )

    if receipt.document_type == "payment_receipt":
        return VerificationDecision(
            "verified",
            "Payment receipt has an exact integer-paise amount match, a matching merchant, and a date within the configured window.",
            details,
        )
    if receipt.document_type == "invoice":
        return VerificationDecision(
            "needs_review",
            "The invoice amount, merchant, and date align with the bank transaction, but an invoice alone is not proof that payment was made.",
            details,
        )
    return VerificationDecision(
        "needs_review",
        "The document type does not establish that this receipt supports a bank payment.",
        details,
    )


def verify_clarification_receipt(
    db: Session,
    business_id: UUID,
    clarification_id: UUID,
    receipt_id: UUID,
    date_window_days: int | None = None,
) -> ReceiptVerificationResponse:
    """Run and persist deterministic verification, appending only changed decisions."""
    if date_window_days is None:
        date_window_days = get_settings().RECEIPT_VERIFICATION_DATE_WINDOW_DAYS
    if date_window_days < 0:
        raise ValueError("date_window_days must be non-negative.")

    try:
        with db.begin():
            receipt = db.scalar(
                select(ClarificationReceipt)
                .where(
                    ClarificationReceipt.id == receipt_id,
                    ClarificationReceipt.clarification_request_id == clarification_id,
                    ClarificationReceipt.business_id == business_id,
                )
                .with_for_update()
            )
            if receipt is None:
                raise ReceiptVerificationNotFoundError(
                    "Receipt not found for this clarification."
                )
            clarification = db.scalar(select(ClarificationRequest).where(
                ClarificationRequest.id == clarification_id,
                ClarificationRequest.business_id == business_id,
            ))
            if clarification is None:
                raise ReceiptVerificationNotFoundError(
                    "Clarification request not found."
                )
            bank_line = db.scalar(select(BankLine).where(
                BankLine.id == clarification.bank_line_id,
                BankLine.business_id == business_id,
            ))
            if bank_line is None:
                raise ReceiptVerificationNotFoundError(
                    "Linked bank transaction not found."
                )

            decision = evaluate_receipt_against_bank_line(
                receipt, bank_line, date_window_days
            )
            current_snapshot = (
                receipt.verification_status,
                receipt.verification_reason,
                receipt.verification_details,
            )
            new_snapshot = (
                decision.status,
                decision.reason,
                decision.details,
            )
            if current_snapshot != new_snapshot:
                previous_status = receipt.verification_status
                receipt.verification_status = decision.status
                receipt.verification_reason = decision.reason
                receipt.verification_details = decision.details
                receipt.verified_at = datetime.now(timezone.utc)
                db.add(
                    ChaseEvent(
                        business_id=business_id,
                        bank_line_id=bank_line.id,
                        clarification_request_id=clarification.id,
                        event_type="clarification_receipt_verified",
                        details={
                            "receipt_id": str(receipt.id),
                            "previous_status": previous_status,
                            "verification_status": decision.status,
                            "reason": decision.reason,
                            **decision.details,
                        },
                    )
                )
                db.flush()
    except ReceiptVerificationNotFoundError:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error(
            "Receipt verification persistence failed clarification_id=%s receipt_id=%s exception_type=%s",
            clarification_id,
            receipt_id,
            type(exc).__name__,
        )
        raise ReceiptVerificationPersistenceError(
            "Receipt verification could not be saved."
        ) from exc

    return _response(receipt, clarification, bank_line)


def get_receipt_verification(
    db: Session, business_id: UUID, clarification_id: UUID, receipt_id: UUID
) -> ReceiptVerificationResponse:
    """Load the latest persisted verification without changing audit history."""
    receipt = db.scalar(
        select(ClarificationReceipt).where(
            ClarificationReceipt.id == receipt_id,
            ClarificationReceipt.clarification_request_id == clarification_id,
            ClarificationReceipt.business_id == business_id,
        )
    )
    if receipt is None:
        raise ReceiptVerificationNotFoundError("Receipt not found for this clarification.")
    if receipt.verification_status is None or receipt.verification_details is None:
        raise ReceiptVerificationUnavailableError(
            "This receipt has not been verified yet."
        )
    clarification = receipt.clarification_request
    return _response(receipt, clarification, clarification.bank_line)


def _response(
    receipt: ClarificationReceipt,
    clarification: ClarificationRequest,
    bank_line: BankLine,
) -> ReceiptVerificationResponse:
    return ReceiptVerificationResponse(
        receipt_id=str(receipt.id),
        clarification_id=str(clarification.id),
        bank_line_id=str(bank_line.id),
        status=receipt.verification_status,  # type: ignore[arg-type]
        reason=receipt.verification_reason or "",
        receipt_amount_paise=receipt.total_amount_paise,
        bank_amount_paise=bank_line.amount_paise,
        merchant_name=receipt.merchant_name,
        bank_description=bank_line.description,
        normalized_merchant=(receipt.verification_details or {}).get("normalized_merchant"),
        normalized_description=(receipt.verification_details or {}).get(
            "normalized_description", ""
        ),
        vendor_similarity=(receipt.verification_details or {}).get("vendor_similarity"),
        document_type=receipt.document_type,
        document_date=receipt.document_date,
        transaction_date=bank_line.transaction_date,
        date_difference_days=(receipt.verification_details or {}).get(
            "date_difference_days"
        ),
        allowed_date_window_days=(receipt.verification_details or {}).get(
            "allowed_date_window_days", 0
        ),
        verified_at=receipt.verified_at,
    )
