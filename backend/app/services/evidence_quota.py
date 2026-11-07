"""Aggregate per-business evidence quota checks."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..models import Business, ClarificationReceipt, Invoice


class EvidenceQuotaExceeded(Exception):
    """The upload would exceed the business evidence storage limit."""


class EvidenceBusinessUnavailable(Exception):
    """The business could not be locked for a quota-safe upload."""


def enforce_business_quota(
    db: Session, business_id: UUID, incoming_bytes: int, *, lock: bool = True,
) -> tuple[int, int]:
    """Check total invoice plus receipt bytes; caller owns the transaction.

    Locking the business row serializes concurrent uploads for one tenant while
    leaving other businesses independent. The file is written before the caller
    commits; callers must delete that new object if this transaction fails.
    """
    statement = select(Business).where(Business.id == business_id)
    if lock:
        statement = statement.with_for_update()
    business = db.scalar(statement)
    if business is None:
        raise EvidenceBusinessUnavailable

    invoice_bytes = db.scalar(select(func.coalesce(func.sum(Invoice.file_size), 0)).where(
        Invoice.business_id == business_id,
    ))
    receipt_bytes = db.scalar(select(func.coalesce(func.sum(ClarificationReceipt.file_size), 0)).where(
        ClarificationReceipt.business_id == business_id,
    ))
    used = int(invoice_bytes or 0) + int(receipt_bytes or 0)
    limit = business.storage_quota_bytes
    if limit is None:
        limit = get_settings().EVIDENCE_DEFAULT_BUSINESS_QUOTA_BYTES
    if incoming_bytes < 0 or used + incoming_bytes > limit:
        raise EvidenceQuotaExceeded
    return used, int(limit)
