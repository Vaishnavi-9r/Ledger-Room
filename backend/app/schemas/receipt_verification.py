"""API result for deterministic receipt-to-bank verification."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

ReceiptVerificationStatus = Literal[
    "verified",
    "amount_mismatch",
    "vendor_mismatch",
    "date_mismatch",
    "needs_review",
]


class ReceiptVerificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    clarification_id: str
    bank_line_id: str
    status: ReceiptVerificationStatus
    reason: str
    receipt_amount_paise: int | None
    bank_amount_paise: int
    merchant_name: str | None
    bank_description: str
    normalized_merchant: str | None
    normalized_description: str
    vendor_similarity: float | None
    document_type: str | None
    document_date: date | None
    transaction_date: date
    date_difference_days: int | None
    allowed_date_window_days: int
    verified_at: datetime


class ReceiptVerificationNotFoundResponse(BaseModel):
    detail: str
