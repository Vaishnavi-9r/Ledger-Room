"""Read-only monthly bookkeeping report queries and deterministic aggregation."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Iterable
from uuid import UUID
from zoneinfo import ZoneInfo
from uuid import UUID

from sqlalchemy import Text, bindparam, false, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session, selectinload

from ..models import (
    BankLine,
    ClarificationRequest,
    ClarificationReceipt,
    Invoice,
    Match,
    MatchEvent,
    Business,
)
from ..schemas.monthly_report import (
    MatchAuditDetail,
    MonthlyBusinessIdentity,
    MonthlyBankTransactionDetail,
    MissingInvoiceDetail,
    MonthlyFinancialSummary,
    MonthlyInvoiceDetail,
    MonthlyReportResponse,
    MonthlyReportSummary,
    ReceiptEvidenceDetail,
    ReportingPeriod,
)

RECEIPT_VERIFICATION_OUTCOMES = (
    "verified",
    "amount_mismatch",
    "vendor_mismatch",
    "date_mismatch",
    "needs_review",
    "not_verified",
)
REVIEW_STATUSES = {"amount_mismatch", "possible_duplicate", "needs_review"}
REPORT_TIME_ZONE = ZoneInfo("Asia/Kolkata")


def _candidate_bank_line_condition(bank_line_ids: list[UUID]):
    """Match JSONB string-array candidates against a PostgreSQL text array."""
    return Match.candidate_bank_line_ids.op("?|")(
        bindparam(
            "reporting_candidate_bank_line_ids",
            [str(bank_line_id) for bank_line_id in bank_line_ids],
            type_=ARRAY(Text),
        )
    )


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime, date, date]:
    if not 1 <= month <= 12:
        raise ValueError("month must be between 1 and 12.")
    start_date = date(year, month, 1)
    end_day = monthrange(year, month)[1]
    end_date = date(year, month, end_day)
    start_at = datetime(year, month, 1, tzinfo=REPORT_TIME_ZONE)
    if month == 12:
        end_at = datetime(year + 1, 1, 1, tzinfo=REPORT_TIME_ZONE)
    else:
        end_at = datetime(year, month + 1, 1, tzinfo=REPORT_TIME_ZONE)
    return start_at, end_at, start_date, end_date


def _inside(value: date | datetime | None, start: date | datetime, end: date | datetime) -> bool:
    return value is not None and start <= value < end


def _match_status(matches: Iterable[Match]) -> str | None:
    rows = list(matches)
    if not rows:
        return None
    latest = max(
        rows,
        key=lambda row: (
            row.updated_at or row.created_at,
            str(row.id),
        ),
    )
    return latest.status


def build_monthly_report(
    year: int,
    month: int,
    invoices: Iterable[Invoice],
    bank_lines: Iterable[BankLine],
    matches: Iterable[Match],
    receipts: Iterable[ClarificationReceipt],
    match_events: Iterable[MatchEvent],
    *,
    generated_at: datetime | None = None,
    business: Business | None = None,
) -> MonthlyReportResponse:
    """Build the response using integer paise and source-record references only."""
    start_at, end_at, start_date, end_date = _month_bounds(year, month)
    invoice_rows = [row for row in invoices if _inside(row.uploaded_at, start_at, end_at)]
    bank_rows = [row for row in bank_lines if _inside(row.transaction_date, start_date, date.fromordinal(end_date.toordinal() + 1))]
    match_rows = list(matches)
    receipt_rows = [row for row in receipts if _inside(row.uploaded_at, start_at, end_at)]
    event_rows = [row for row in match_events if _inside(row.changed_at, start_at, end_at)]

    matches_by_invoice: dict[UUID, list[Match]] = {}
    for match in match_rows:
        matches_by_invoice.setdefault(match.invoice_id, []).append(match)

    invoice_details: list[MonthlyInvoiceDetail] = []
    invoice_statuses: dict[UUID, str | None] = {}
    for invoice in sorted(invoice_rows, key=lambda row: (row.uploaded_at, str(row.id))):
        associated = sorted(
            {str(row.bank_line_id) for row in matches_by_invoice.get(invoice.id, []) if row.bank_line_id is not None}
        )
        status_value = _match_status(matches_by_invoice.get(invoice.id, []))
        invoice_statuses[invoice.id] = status_value
        invoice_details.append(
            MonthlyInvoiceDetail(
                invoice_id=str(invoice.id),
                vendor=invoice.vendor_name,
                invoice_number=invoice.invoice_number,
                invoice_date=invoice.invoice_date,
                total_amount_paise=invoice.total_amount_paise,
                extraction_status=invoice.extraction_status,
                reconciliation_status=status_value,
                associated_bank_line_id=associated[0] if len(associated) == 1 else None,
                associated_bank_line_ids=associated,
            )
        )

    bank_by_id = {row.id: row for row in bank_rows}
    direct_matches_by_bank: dict[UUID, list[Match]] = {}
    candidate_matches_by_bank: dict[UUID, list[Match]] = {}
    for match in match_rows:
        if match.bank_line_id is not None:
            direct_matches_by_bank.setdefault(match.bank_line_id, []).append(match)
        for candidate_id in match.candidate_bank_line_ids or []:
            try:
                candidate_uuid = UUID(str(candidate_id))
            except (ValueError, TypeError, AttributeError):
                continue
            if candidate_uuid in bank_by_id:
                candidate_matches_by_bank.setdefault(candidate_uuid, []).append(match)

    bank_transaction_details: list[MonthlyBankTransactionDetail] = []
    for bank_line in sorted(bank_rows, key=lambda row: (row.transaction_date, str(row.id))):
        clarification = bank_line.clarification_request
        direct = direct_matches_by_bank.get(bank_line.id, [])
        candidate = candidate_matches_by_bank.get(bank_line.id, [])
        statuses = sorted({row.status for row in direct})
        statuses.extend(
            f"candidate:{value}"
            for value in sorted({row.status for row in candidate})
            if value not in statuses
        )
        bank_transaction_details.append(
            MonthlyBankTransactionDetail(
                bank_line_id=str(bank_line.id),
                transaction_date=bank_line.transaction_date,
                description=bank_line.description,
                amount_paise=bank_line.amount_paise,
                currency=getattr(bank_line, "currency", None),
                external_reference=getattr(bank_line, "external_reference", None),
                reconciliation_statuses=statuses,
                invoice_ids=sorted({str(row.invoice_id) for row in direct}),
                clarification_request_id=(str(clarification.id) if clarification else None),
                clarification_status=(clarification.status if clarification else None),
                owner_response=(clarification.owner_response if clarification else None),
            )
        )

    represented_bank_ids: set[UUID] = set()
    matched_bank_ids: set[UUID] = set()
    review_bank_ids: set[UUID] = set()
    mismatched_value = 0
    invoice_by_id = {row.id: row for row in invoice_rows}
    for match in match_rows:
        if match.bank_line_id is not None:
            represented_bank_ids.add(match.bank_line_id)
            bank_line = bank_by_id.get(match.bank_line_id) or match.bank_line
            if match.status == "matched" and bank_line is not None and bank_line.amount_paise > 0 and match.bank_line_id in bank_by_id:
                matched_bank_ids.add(match.bank_line_id)
            if (
                match.status in REVIEW_STATUSES
                and match.bank_line_id in bank_by_id
                and bank_by_id[match.bank_line_id].amount_paise > 0
            ):
                review_bank_ids.add(match.bank_line_id)
            if (
                match.status == "amount_mismatch"
                and match.invoice_id in invoice_by_id
                and match.bank_line_id in bank_by_id
                and bank_line is not None
            ):
                invoice_total = invoice_by_id[match.invoice_id].total_amount_paise
                if invoice_total is not None and bank_line.amount_paise > 0:
                    mismatched_value += abs(invoice_total - bank_line.amount_paise)
        for candidate_id in match.candidate_bank_line_ids or []:
            try:
                candidate_uuid = UUID(str(candidate_id))
            except (ValueError, TypeError, AttributeError):
                continue
            represented_bank_ids.add(candidate_uuid)
            if (
                match.status in REVIEW_STATUSES
                and candidate_uuid in bank_by_id
                and bank_by_id[candidate_uuid].amount_paise > 0
            ):
                review_bank_ids.add(candidate_uuid)

    positive_bank_lines = [row for row in bank_rows if row.amount_paise > 0]
    missing_bank_lines = [row for row in positive_bank_lines if row.id not in represented_bank_ids]
    unmatched_debits = [row for row in positive_bank_lines if row.id not in matched_bank_ids]

    missing_invoice_details: list[MissingInvoiceDetail] = []
    for bank_line in sorted(missing_bank_lines, key=lambda row: (row.transaction_date, str(row.id))):
        clarification = bank_line.clarification_request
        missing_invoice_details.append(
            MissingInvoiceDetail(
                bank_line_id=str(bank_line.id),
                transaction_date=bank_line.transaction_date,
                description=bank_line.description,
                amount_paise=bank_line.amount_paise,
                clarification_request_id=str(clarification.id) if clarification else None,
                clarification_status=clarification.status if clarification else None,
                owner_response=clarification.owner_response if clarification else None,
            )
        )

    statuses = list(invoice_statuses.values())
    receipt_outcomes = {name: 0 for name in RECEIPT_VERIFICATION_OUTCOMES}
    receipt_details: list[ReceiptEvidenceDetail] = []
    for receipt in sorted(receipt_rows, key=lambda row: (row.uploaded_at, str(row.id))):
        outcome = receipt.verification_status or "not_verified"
        if outcome in receipt_outcomes:
            receipt_outcomes[outcome] += 1
        receipt_details.append(
            ReceiptEvidenceDetail(
                receipt_id=str(receipt.id),
                clarification_request_id=str(receipt.clarification_request_id),
                document_type=receipt.document_type,
                vendor=receipt.merchant_name,
                amount_paise=receipt.total_amount_paise,
                verification_result=receipt.verification_status,
                source_image_reference=receipt.stored_filename,
            )
        )

    clarifications = [row.clarification_request for row in bank_rows if row.amount_paise > 0 and row.clarification_request]
    unique_clarifications = {row.id: row for row in clarifications}
    audit = [
        MatchAuditDetail(
            match_event_id=str(event.id),
            invoice_id=str(event.invoice_id),
            previous_bank_line_id=str(event.previous_bank_line_id) if event.previous_bank_line_id else None,
            bank_line_id=str(event.new_bank_line_id) if event.new_bank_line_id else None,
            previous_status=event.previous_status,
            resulting_status=event.new_status,
            previous_decision_source=event.previous_decision_source,
            decision_source=event.new_decision_source,
            previous_updated_at=event.previous_updated_at,
            changed_at=event.changed_at,
        )
        for event in sorted(event_rows, key=lambda row: (row.changed_at, str(row.id)))
    ]

    return MonthlyReportResponse(
        generated_at=generated_at or datetime.now(timezone.utc),
        reporting_period=ReportingPeriod(year=year, month=month, start_date=start_date, end_date=end_date),
        business=(MonthlyBusinessIdentity(
            business_id=str(business.id),
            business_name=business.business_name,
            proprietor_name=business.proprietor_name,
            gstin=business.gstin,
            address_line_1=business.address_line_1,
            address_line_2=business.address_line_2,
            city=business.city,
            state=business.state,
            pin_code=business.pin_code,
            country=business.country,
        ) if business is not None else None),
        summary=MonthlyReportSummary(
            total_invoices_received=len(invoice_rows),
            total_bank_transactions=len(bank_rows),
            matched_payments=len(matched_bank_ids),
            amount_mismatches=sum(value == "amount_mismatch" for value in statuses),
            missing_payments=sum(value == "missing_payment" for value in statuses),
            possible_duplicates=sum(value == "possible_duplicate" for value in statuses),
            transactions_requiring_review=len(review_bank_ids),
            bank_transactions_without_invoices=len(missing_bank_lines),
            pending_clarifications=sum(row.status == "pending" for row in unique_clarifications.values()),
            answered_clarifications=sum(row.status == "answered" for row in unique_clarifications.values()),
            receipts_uploaded=len(receipt_rows),
            receipt_verification_outcomes=receipt_outcomes,
        ),
        financial_summary=MonthlyFinancialSummary(
            total_invoice_value_paise=sum(row.total_amount_paise or 0 for row in invoice_rows),
            total_matched_payment_value_paise=sum(bank_by_id[bank_id].amount_paise for bank_id in matched_bank_ids),
            total_unmatched_debit_value_paise=sum(row.amount_paise for row in unmatched_debits),
            total_amount_mismatch_value_paise=mismatched_value,
        ),
        invoices=invoice_details,
        bank_transactions=bank_transaction_details,
        missing_invoice_transactions=missing_invoice_details,
        receipt_evidence=receipt_details,
        audit_history=audit,
    )


def get_monthly_report(
    db: Session, year: int, month: int, business_id: UUID,
    business: Business | None = None,
) -> MonthlyReportResponse:
    """Load month-scoped records in batches and build a read-only report."""
    start_at, end_at, start_date, end_date = _month_bounds(year, month)
    end_date_exclusive = end_date + timedelta(days=1)

    invoice_statement = select(Invoice).where(
        Invoice.business_id == business_id,
        Invoice.uploaded_at >= start_at, Invoice.uploaded_at < end_at,
    )
    invoices = db.scalars(
        invoice_statement
        .options(selectinload(Invoice.matches).selectinload(Match.bank_line))
    ).all()
    bank_statement = select(BankLine).where(
        BankLine.business_id == business_id,
        BankLine.transaction_date >= start_date, BankLine.transaction_date < end_date_exclusive,
    )
    bank_lines = db.scalars(
        bank_statement
        .options(
            selectinload(BankLine.matches),
            selectinload(BankLine.clarification_request).selectinload(
                ClarificationRequest.receipts
            ),
        )
    ).all()
    match_conditions = []
    invoice_ids = [row.id for row in invoices]
    bank_line_ids = [row.id for row in bank_lines]
    if invoice_ids:
        match_conditions.append(Match.invoice_id.in_(invoice_ids))
    if bank_line_ids:
        match_conditions.extend(
            (
                Match.bank_line_id.in_(bank_line_ids),
                _candidate_bank_line_condition(bank_line_ids),
            )
        )
    match_statement = select(Match).where(Match.business_id == business_id).options(selectinload(Match.bank_line))
    if match_conditions:
        match_statement = match_statement.where(or_(*match_conditions))
    else:
        match_statement = match_statement.where(false())
    matches = db.scalars(match_statement).all()
    receipt_statement = select(ClarificationReceipt).where(
        ClarificationReceipt.business_id == business_id,
        ClarificationReceipt.uploaded_at >= start_at, ClarificationReceipt.uploaded_at < end_at,
    )
    receipts = db.scalars(
        receipt_statement
        .options(selectinload(ClarificationReceipt.clarification_request))
    ).all()
    event_statement = select(MatchEvent).where(
        MatchEvent.business_id == business_id,
        MatchEvent.changed_at >= start_at, MatchEvent.changed_at < end_at,
    )
    events = db.scalars(
        event_statement
    ).all()
    return build_monthly_report(year, month, invoices, bank_lines, matches, receipts, events, business=business)
