"""Response models for the monthly bookkeeping report."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class ReportingPeriod(BaseModel):
    year: int
    month: int
    start_date: date
    end_date: date


class MonthlyBusinessIdentity(BaseModel):
    business_id: str
    business_name: str
    proprietor_name: str
    gstin: str | None
    address_line_1: str
    address_line_2: str | None
    city: str
    state: str
    pin_code: str
    country: str


class MonthlyReportSummary(BaseModel):
    total_invoices_received: int
    total_bank_transactions: int
    matched_payments: int
    amount_mismatches: int
    missing_payments: int
    possible_duplicates: int
    transactions_requiring_review: int
    bank_transactions_without_invoices: int
    pending_clarifications: int
    answered_clarifications: int
    receipts_uploaded: int
    receipt_verification_outcomes: dict[str, int]


class MonthlyFinancialSummary(BaseModel):
    total_invoice_value_paise: int
    total_matched_payment_value_paise: int
    total_unmatched_debit_value_paise: int
    total_amount_mismatch_value_paise: int


class MonthlyInvoiceDetail(BaseModel):
    invoice_id: str
    vendor: str | None
    invoice_number: str | None
    invoice_date: date | None
    total_amount_paise: int | None
    extraction_status: str
    reconciliation_status: str | None
    associated_bank_line_id: str | None
    associated_bank_line_ids: list[str]


class MissingInvoiceDetail(BaseModel):
    bank_line_id: str
    transaction_date: date
    description: str
    amount_paise: int
    clarification_request_id: str | None
    clarification_status: str | None
    owner_response: str | None


class MonthlyBankTransactionDetail(BaseModel):
    bank_line_id: str
    transaction_date: date
    description: str
    amount_paise: int
    currency: str | None
    external_reference: str | None
    reconciliation_statuses: list[str]
    invoice_ids: list[str]
    clarification_request_id: str | None
    clarification_status: str | None
    owner_response: str | None


class ReceiptEvidenceDetail(BaseModel):
    receipt_id: str
    clarification_request_id: str
    document_type: str | None
    vendor: str | None
    amount_paise: int | None
    verification_result: str | None
    source_image_reference: str


class MatchAuditDetail(BaseModel):
    match_event_id: str
    invoice_id: str
    previous_bank_line_id: str | None
    bank_line_id: str | None
    previous_status: str
    resulting_status: str
    previous_decision_source: str
    decision_source: str
    previous_updated_at: datetime
    changed_at: datetime


class MonthlyReportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    reporting_period: ReportingPeriod
    business: MonthlyBusinessIdentity | None = None
    summary: MonthlyReportSummary
    financial_summary: MonthlyFinancialSummary
    invoices: list[MonthlyInvoiceDetail]
    bank_transactions: list[MonthlyBankTransactionDetail] = Field(default_factory=list)
    missing_invoice_transactions: list[MissingInvoiceDetail]
    receipt_evidence: list[ReceiptEvidenceDetail]
    audit_history: list[MatchAuditDetail]
