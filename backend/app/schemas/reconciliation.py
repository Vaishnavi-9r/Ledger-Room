"""API response schemas for deterministic reconciliation."""

from typing import Literal
from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ReconciliationStatus = Literal[
    "matched",
    "amount_mismatch",
    "missing_payment",
    "possible_duplicate",
    "needs_review",
]


class MissingInvoiceTransactionResponse(BaseModel):
    """A positive bank debit with no invoice link or reconciliation candidate."""

    status: Literal["missing_invoice"] = "missing_invoice"
    bank_line_id: str
    clarification_request_id: str | None = None
    description: str
    transaction_date: date
    amount_paise: int = Field(gt=0)
    explanation: str


class ReconciliationDecisionResponse(BaseModel):
    """An explainable reconciliation decision for one invoice."""

    invoice_id: str
    match_ids: list[str] = Field(default_factory=list)
    status: ReconciliationStatus
    bank_line_ids: list[str] = Field(default_factory=list)
    candidate_bank_line_ids: list[str] = Field(default_factory=list)
    vendor_similarity: float | None = Field(default=None, ge=0, le=1)
    explanation: str


class ReconciliationRunResponse(BaseModel):
    """Counts and per-invoice explanations from one reconciliation run."""

    model_config = ConfigDict(extra="forbid")

    total_invoices: int = Field(ge=0)
    matched: int = Field(ge=0)
    amount_mismatch: int = Field(ge=0)
    missing_payment: int = Field(ge=0)
    possible_duplicate: int = Field(ge=0)
    needs_review: int = Field(ge=0)
    decisions: list[ReconciliationDecisionResponse] = Field(default_factory=list)
    missing_invoice: int = Field(default=0, ge=0)
    missing_invoice_transactions: list[MissingInvoiceTransactionResponse] = Field(
        default_factory=list
    )


class ReconciliationHistoryMatch(BaseModel):
    match_id: UUID
    invoice_id: UUID
    bank_line_id: UUID | None
    status: str
    notes: str | None
    candidate_bank_line_ids: list[str]
    decision_source: str
    updated_at: datetime


class ReconciliationHistoryEvent(BaseModel):
    event_id: UUID
    match_id: UUID
    invoice_id: UUID
    previous_bank_line_id: UUID | None
    new_bank_line_id: UUID | None
    previous_status: str
    new_status: str
    previous_decision_source: str
    new_decision_source: str
    changed_at: datetime


class ReconciliationHistoryResponse(BaseModel):
    matches: list[ReconciliationHistoryMatch]
    events: list[ReconciliationHistoryEvent]
