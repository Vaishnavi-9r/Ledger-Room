"""Monthly bookkeeping report aggregation and API validation tests."""

from __future__ import annotations

import unittest
import csv
import io
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import Text, select
from sqlalchemy.dialects.postgresql import ARRAY, dialect
from sqlalchemy.exc import SQLAlchemyError

from backend.app.db.session import get_db
from backend.app.dependencies.business import get_business_context
from backend.app.main import app
from backend.app.schemas.monthly_report_approval import MonthlyReportApprovalResponse
from backend.app.models import (
    BankLine,
    Business,
    ClarificationReceipt,
    Invoice,
    Match,
    MatchEvent,
)
from backend.app.services.monthly_report_service import (
    _candidate_bank_line_condition,
    build_monthly_report,
)
UTC = timezone.utc
NOV_START = datetime(2026, 11, 1, tzinfo=UTC)
DEC_START = datetime(2026, 12, 1, tzinfo=UTC)


def _invoice(*, uploaded_at=NOV_START, total=1000, matches=(), vendor="Acme"):
    return SimpleNamespace(
        id=uuid4(), uploaded_at=uploaded_at, vendor_name=vendor,
        invoice_number="INV-1", invoice_date=date(2026, 10, 29),
        total_amount_paise=total, extraction_status="completed", matches=list(matches),
    )


def _bank(*, day=3, amount=1000, description="Acme", clarification=None):
    return SimpleNamespace(
        id=uuid4(), transaction_date=date(2026, 11, day), amount_paise=amount,
        description=description, clarification_request=clarification, matches=[],
    )


def _match(invoice, bank=None, *, status="matched", candidates=(), updated=NOV_START):
    return SimpleNamespace(
        id=uuid4(), invoice_id=invoice.id, bank_line_id=bank.id if bank else None,
        bank_line=bank, status=status, candidate_bank_line_ids=list(candidates),
        created_at=updated, updated_at=updated,
    )


class _Scalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return list(self.rows)


class _ReadOnlySession:
    def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is Business:
            return SimpleNamespace(
                id=uuid4(), is_demo=True, business_name="LedgerRoom Demo Business",
                proprietor_name="Unassigned demo data", gstin=None,
                address_line_1="Legacy records", address_line_2=None,
                city="Not provided", state="Not provided", pin_code="000000", country="IN",
            )
        return None

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        rows = {
            Invoice: [],
            BankLine: [],
            Match: [],
            ClarificationReceipt: [],
            MatchEvent: [],
        }
        return _Scalars(rows[entity])


class MonthlyReportTests(unittest.TestCase):
    def build(self, invoices=(), bank_lines=(), matches=(), receipts=(), events=()):
        return build_monthly_report(
            2026, 11, invoices, bank_lines, matches, receipts, events,
            generated_at=NOV_START,
        )

    def request_with_read_only_session(self, method, url, **kwargs):
        session = _ReadOnlySession()
        business = SimpleNamespace(
            id=uuid4(), business_name="Test shop", proprietor_name="Asha Rao",
            gstin=None, address_line_1="1 Main", address_line_2=None,
            city="Pune", state="Maharashtra", pin_code="411001", country="IN",
        )
        context = SimpleNamespace(
            business=business, business_id=business.id, role="owner",
            membership=SimpleNamespace(role="owner"),
            principal=SimpleNamespace(user=SimpleNamespace(id=uuid4())),
        )
        def override_get_db():
            yield session
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_business_context] = lambda: context
        try:
            return getattr(TestClient(app), method)(url, **kwargs)
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_business_context, None)

    def test_month_filters_invoice_upload_and_bank_transaction_dates(self):
        # 18:31 UTC on Oct 31 is 00:01 on Nov 1 in the LedgerRoom reporting zone.
        in_month = _invoice(uploaded_at=datetime(2026, 10, 31, 18, 31, tzinfo=UTC))
        outside_invoice = _invoice(uploaded_at=DEC_START)
        in_bank = _bank()
        outside_bank = SimpleNamespace(
            id=uuid4(), transaction_date=date(2026, 12, 1), amount_paise=900,
            description="Later", clarification_request=None, matches=[],
        )

        report = self.build([in_month, outside_invoice], [in_bank, outside_bank])

        self.assertEqual(report.summary.total_invoices_received, 1)
        self.assertEqual(report.summary.total_bank_transactions, 1)
        self.assertEqual(report.invoices[0].invoice_date, date(2026, 10, 29))

    def test_empty_reporting_period_has_zero_totals_and_empty_details(self):
        report = self.build()

        self.assertEqual(report.summary.total_invoices_received, 0)
        self.assertEqual(report.summary.total_bank_transactions, 0)
        self.assertEqual(report.financial_summary.total_invoice_value_paise, 0)
        self.assertEqual(report.invoices, [])
        self.assertEqual(report.missing_invoice_transactions, [])

    def test_report_contains_selected_business_identity(self):
        business = SimpleNamespace(
            id=uuid4(), business_name="Asha's Shop", proprietor_name="Asha Rao",
            gstin="27AAACR5055K1Z7", address_line_1="1 Main", address_line_2=None,
            city="Pune", state="Maharashtra", pin_code="411001", country="IN",
        )
        report = build_monthly_report(2026, 11, [], [], [], [], [], business=business)
        self.assertEqual(report.business.business_id, str(business.id))
        self.assertEqual(report.business.gstin, business.gstin)

    def test_financial_totals_use_integer_paise_and_exclude_credits(self):
        invoice = _invoice(total=1000)
        matched = _bank(amount=1000)
        mismatch = _bank(amount=900, description="Acme debit")
        credit = _bank(amount=-8000, description="Customer refund")
        match_rows = [
            _match(invoice, matched),
            _match(invoice, mismatch, status="amount_mismatch"),
        ]
        # The current per-invoice reconciliation status is the most recently updated row.
        mismatch_match = match_rows[1]
        mismatch_match.updated_at = DEC_START
        report = self.build([invoice], [matched, mismatch, credit], match_rows)

        self.assertEqual(report.financial_summary.total_invoice_value_paise, 1000)
        self.assertEqual(report.financial_summary.total_matched_payment_value_paise, 1000)
        self.assertEqual(report.financial_summary.total_unmatched_debit_value_paise, 900)
        self.assertEqual(report.financial_summary.total_amount_mismatch_value_paise, 100)
        self.assertEqual(report.summary.total_bank_transactions, 3)

    def test_matched_and_missing_payment_decisions_are_distinct(self):
        matched_invoice = _invoice(total=1000)
        missing_invoice = _invoice(total=500)
        matched_bank = _bank(amount=1000)
        matched = _match(matched_invoice, matched_bank)
        missing = _match(missing_invoice, status="missing_payment")

        report = self.build(
            [matched_invoice, missing_invoice], [matched_bank], [matched, missing]
        )

        statuses = {row.invoice_id: row.reconciliation_status for row in report.invoices}
        self.assertEqual(statuses[str(matched_invoice.id)], "matched")
        self.assertEqual(statuses[str(missing_invoice.id)], "missing_payment")
        self.assertEqual(report.summary.missing_payments, 1)

    def test_duplicate_candidate_bank_lines_are_not_reported_as_missing_invoice(self):
        invoice = _invoice()
        first = _bank(amount=1000)
        second = _bank(amount=1000, day=4)
        match = _match(invoice, first, status="possible_duplicate", candidates=[str(second.id)])

        report = self.build([invoice], [first, second], [match])

        self.assertEqual(report.summary.possible_duplicates, 1)
        self.assertEqual(report.summary.bank_transactions_without_invoices, 0)
        self.assertEqual(report.summary.transactions_requiring_review, 2)

    def test_pending_and_answered_clarifications_are_counted_separately(self):
        pending = SimpleNamespace(id=uuid4(), status="pending", owner_response=None)
        answered = SimpleNamespace(id=uuid4(), status="answered", owner_response="Office supplies")
        first = _bank(amount=100, clarification=pending)
        second = _bank(amount=200, day=4, clarification=answered)

        report = self.build(bank_lines=[first, second])

        self.assertEqual(report.summary.pending_clarifications, 1)
        self.assertEqual(report.summary.answered_clarifications, 1)
        details = {item.bank_line_id: item for item in report.missing_invoice_transactions}
        self.assertEqual(details[str(second.id)].owner_response, "Office supplies")

    def test_receipt_verification_counts_and_source_reference_are_preserved(self):
        request_id = uuid4()
        receipt = SimpleNamespace(
            id=uuid4(), clarification_request_id=request_id, uploaded_at=NOV_START,
            document_type="payment_receipt", merchant_name="Acme", total_amount_paise=1000,
            verification_status="verified", stored_filename="evidence-uuid.png",
        )

        report = self.build(receipts=[receipt])

        self.assertEqual(report.summary.receipts_uploaded, 1)
        self.assertEqual(report.summary.receipt_verification_outcomes["verified"], 1)
        self.assertEqual(report.receipt_evidence[0].source_image_reference, "evidence-uuid.png")
        self.assertEqual(report.receipt_evidence[0].clarification_request_id, str(request_id))

    def test_audit_event_retains_status_sources_timestamps_and_evidence_ids(self):
        invoice_id, old_bank_id, new_bank_id = uuid4(), uuid4(), uuid4()
        event = SimpleNamespace(
            id=uuid4(), invoice_id=invoice_id, previous_bank_line_id=old_bank_id,
            new_bank_line_id=new_bank_id, previous_status="needs_review", new_status="matched",
            previous_decision_source="automated", new_decision_source="human",
            previous_updated_at=NOV_START, changed_at=NOV_START,
        )

        report = self.build(events=[event])

        audit = report.audit_history[0]
        self.assertEqual(audit.invoice_id, str(invoice_id))
        self.assertEqual(audit.previous_bank_line_id, str(old_bank_id))
        self.assertEqual(audit.bank_line_id, str(new_bank_id))
        self.assertEqual(audit.previous_status, "needs_review")
        self.assertEqual(audit.resulting_status, "matched")

    def test_invalid_month_and_year_are_rejected_by_api(self):
        invalid_month = self.request_with_read_only_session("get", "/api/v1/reports/monthly?year=2026&month=13")
        invalid_year = self.request_with_read_only_session("get", "/api/v1/reports/monthly?year=1899&month=11")
        self.assertEqual(invalid_month.status_code, 422)
        self.assertEqual(invalid_year.status_code, 422)

    def test_candidate_bank_line_filter_binds_postgresql_text_array(self):
        statement = select(Match.id).where(
            _candidate_bank_line_condition([uuid4(), uuid4()])
        )
        compiled = statement.compile(dialect=dialect())
        bind = compiled.binds["reporting_candidate_bank_line_ids"]

        self.assertIn("?| %(reporting_candidate_bank_line_ids)s::TEXT[]", str(compiled))
        self.assertIsInstance(bind.type, ARRAY)
        self.assertIsInstance(bind.type.item_type, Text)

    def test_monthly_endpoint_returns_empty_report_without_database_writes(self):
        response = self.request_with_read_only_session(
            "get", f"/api/v1/reports/monthly?year=2026&month=11&business_id={uuid4()}"
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["reporting_period"]["month"], 11)
        self.assertEqual(body["summary"]["total_invoices_received"], 0)
        self.assertEqual(body["financial_summary"]["total_invoice_value_paise"], 0)
        self.assertEqual(body["business"]["business_name"], "Test shop")

    def test_business_report_selection_requires_membership(self):
        no_auth = TestClient(app).get(
            f"/api/v1/reports/monthly?year=2026&month=11&business_id={uuid4()}"
        )
        self.assertEqual(no_auth.status_code, 401)

    def test_authenticated_report_requires_explicit_business_selection(self):
        response = TestClient(app).get("/api/v1/reports/monthly?year=2026&month=11")
        self.assertEqual(response.status_code, 401)

    def test_csv_export_has_summary_inr_totals_details_and_trace_ids(self):
        invoice = _invoice(total=123456, vendor='Acme "Office", Inc.')
        bank = _bank(amount=123456, description='Shop, "Central"', clarification=SimpleNamespace(
            id=uuid4(), status="pending", owner_response=None,
        ))
        bank.clarification_request.receipts = []
        business = SimpleNamespace(
            id=uuid4(), business_name="Ledger Shop", proprietor_name="Asha Rao", gstin=None,
            address_line_1="1 Main Road", address_line_2="Unit 2", city="Pune",
            state="Maharashtra", pin_code="411001", country="IN",
        )
        report = build_monthly_report(2026, 11, [invoice], [bank], [], [], [], generated_at=NOV_START, business=business)

        with patch("backend.app.api.reports.get_monthly_report", return_value=report):
            response = self.request_with_read_only_session("get", "/api/v1/reports/monthly/export/csv?year=2026&month=11")

        self.assertEqual(response.status_code, 200)
        self.assertIn('filename="ledgerroom_report_2026_11.csv"', response.headers["content-disposition"])
        rows = list(csv.DictReader(io.StringIO(response.text)))
        invoice_row = next(row for row in rows if row["section"] == "Invoice")
        missing_row = next(row for row in rows if row["section"] == "Bank transaction")
        self.assertEqual(invoice_row["amount_inr"], "1,234.56")
        self.assertEqual(invoice_row["invoice_id"], str(invoice.id))
        self.assertEqual(invoice_row["vendor_or_description"], 'Acme "Office", Inc.')
        self.assertEqual(missing_row["bank_line_id"], str(bank.id))
        self.assertEqual(missing_row["clarification_request_id"], str(bank.clarification_request.id))
        profile_row = next(row for row in rows if row["section"] == "Business")
        self.assertEqual(profile_row["business_id"], str(business.id))
        self.assertEqual(profile_row["vendor_or_description"], "Ledger Shop")
        self.assertTrue(any(row["section"] == "FINANCIAL SUMMARY" for row in rows))
        sections = {row["section"] for row in rows}
        self.assertTrue({"BUSINESS INFORMATION", "Reporting period", "INVOICE DETAILS",
                         "BANK TRANSACTIONS", "CLARIFICATIONS", "RECEIPT EVIDENCE"}.issubset(sections))
        financial_row = next(row for row in rows if row["section"] == "Total invoice value")
        self.assertEqual(financial_row["amount_inr"], "1,234.56")

    def test_html_export_escapes_user_values_and_displays_inr(self):
        invoice = _invoice(total=250000, vendor='<script>alert("x")</script>')
        business = SimpleNamespace(
            id=uuid4(), business_name="<b>Bookshop</b>", proprietor_name="Asha Rao",
            gstin="27AAACR5055K1Z7", address_line_1="<img src=x>", address_line_2=None,
            city="Pune", state="Maharashtra", pin_code="411001", country="IN",
        )
        report = build_monthly_report(2026, 11, [invoice], [], [], [], [], generated_at=NOV_START, business=business)

        with patch("backend.app.api.reports.get_monthly_report", return_value=report):
            response = self.request_with_read_only_session("get", "/api/v1/reports/monthly/export/html?year=2026&month=11")

        self.assertEqual(response.status_code, 200)
        self.assertIn('filename="ledgerroom_report_2026_11.html"', response.headers["content-disposition"])
        self.assertIn("₹ 2,500.00", response.text)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", response.text)
        self.assertNotIn("<script>alert", response.text)
        self.assertIn("@media print", response.text)
        self.assertIn("Reconciliation audit history", response.text)
        self.assertIn("&lt;b&gt;Bookshop&lt;/b&gt;", response.text)
        self.assertIn("GSTIN: 27AAACR5055K1Z7", response.text)
        self.assertIn("&lt;img src=x&gt;", response.text)
        self.assertIn("<svg role=\"img\"", response.text)
        self.assertIn("Financial overview", response.text)
        self.assertIn("Reconciliation status distribution", response.text)
        self.assertNotIn("https://", response.text)

    def test_exports_include_versioned_accountant_review_state(self):
        business = SimpleNamespace(
            id=uuid4(), business_name="Ledger Shop", proprietor_name="Asha Rao",
            gstin=None, address_line_1="1 Main Road", address_line_2=None,
            city="Pune", state="Maharashtra", pin_code="411001", country="IN",
        )
        report = build_monthly_report(
            2026, 11, [], [], [], [], [], generated_at=NOV_START, business=business,
        )
        approval = MonthlyReportApprovalResponse(
            approval_id=uuid4(), business_id=business.id, report_year=2026,
            report_month=11, version=3, status="changes_requested", submitted_by=uuid4(),
            submitted_by_name="Owner", submitted_at=NOV_START, reviewed_by=uuid4(),
            reviewed_by_name="Accountant", reviewed_at=NOV_START,
            accountant_comments='<script>alert("fix");</script>',
            approval_decision="changes_requested", report_fingerprint="b" * 64,
            events=[],
        )
        with patch("backend.app.api.reports.get_monthly_report", return_value=report), patch(
            "backend.app.services.monthly_report_approval_service.get_period_approval_response",
            return_value=approval,
        ):
            csv_response = self.request_with_read_only_session(
                "get", "/api/v1/reports/monthly/export/csv?year=2026&month=11",
            )
            html_response = self.request_with_read_only_session(
                "get", "/api/v1/reports/monthly/export/html?year=2026&month=11",
            )
            xlsx_response = self.request_with_read_only_session(
                "get", "/api/v1/reports/monthly/export/xlsx?year=2026&month=11",
            )

        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("changes_requested", csv_response.text)
        self.assertIn("3", csv_response.text)
        csv_rows = list(csv.DictReader(io.StringIO(csv_response.text)))
        comment_row = next(row for row in csv_rows if row["section"] == "Accountant comments")
        self.assertEqual(comment_row["details"], '<script>alert("fix");</script>')
        self.assertEqual(html_response.status_code, 200)
        self.assertIn("changes_requested", html_response.text)
        self.assertIn("&lt;script&gt;alert(&quot;fix&quot;);&lt;/script&gt;", html_response.text)
        self.assertNotIn('<script>alert("fix")', html_response.text)
        self.assertEqual(xlsx_response.status_code, 200)
        workbook = load_workbook(io.BytesIO(xlsx_response.content), data_only=True)
        summary = workbook["Summary"]
        self.assertEqual(summary["E4"].value, "changes_requested")
        self.assertEqual(summary["E5"].value, 3)
        self.assertEqual(summary["E10"].value, '<script>alert("fix");</script>')

    def test_export_handles_empty_month_and_invalid_period(self):
        empty = self.build()
        with patch("backend.app.api.reports.get_monthly_report", return_value=empty):
            csv_response = self.request_with_read_only_session("get", "/api/v1/reports/monthly/export/csv?year=2026&month=11")
            html_response = self.request_with_read_only_session("get", "/api/v1/reports/monthly/export/html?year=2026&month=11")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("No records for this period.", html_response.text)
        self.assertEqual(self.request_with_read_only_session("get", "/api/v1/reports/monthly/export/csv?year=2026&month=13").status_code, 422)
        self.assertEqual(self.request_with_read_only_session("get", "/api/v1/reports/monthly/export/html?year=1899&month=11").status_code, 422)

    def test_export_database_error_returns_safe_503(self):
        with patch("backend.app.api.reports.get_monthly_report", side_effect=SQLAlchemyError("db internal detail")):
            response = self.request_with_read_only_session("get", "/api/v1/reports/monthly/export/csv?year=2026&month=11")
            xlsx_response = self.request_with_read_only_session("get", "/api/v1/reports/monthly/export/xlsx?year=2026&month=11")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Monthly report could not be loaded."})
        self.assertNotIn("db internal detail", response.text)
        self.assertEqual(xlsx_response.status_code, 503)
        self.assertEqual(xlsx_response.json(), {"detail": "Monthly report could not be loaded."})

    def test_xlsx_export_has_sheets_formats_filters_and_charts(self):
        invoice = _invoice(total=123456, vendor="Ledger Supply")
        clarification = SimpleNamespace(
            id=uuid4(), status="answered", owner_response="Bought printer paper",
        )
        debit = _bank(amount=123456, description="Ledger Supply", clarification=clarification)
        credit = _bank(amount=-8000, description="Customer credit")
        receipt = SimpleNamespace(
            id=uuid4(), clarification_request_id=clarification.id, uploaded_at=NOV_START,
            document_type="payment_receipt", merchant_name="Ledger Supply",
            total_amount_paise=123456, verification_status="verified",
            stored_filename="receipt-evidence.png",
        )
        business = SimpleNamespace(
            id=uuid4(), business_name="Ledger Shop", proprietor_name="Asha Rao",
            gstin=None, address_line_1="1 Main Road", address_line_2=None,
            city="Pune", state="Maharashtra", pin_code="411001", country="IN",
        )
        report = build_monthly_report(
            2026, 11, [invoice], [debit, credit], [_match(invoice, debit)], [receipt], [],
            generated_at=NOV_START, business=business,
        )
        with patch("backend.app.api.reports.get_monthly_report", return_value=report):
            response = self.request_with_read_only_session(
                "get", "/api/v1/reports/monthly/export/xlsx?year=2026&month=11"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("ledgerroom_report_2026_11.xlsx", response.headers["content-disposition"])
        workbook = load_workbook(io.BytesIO(response.content), data_only=False)
        self.assertEqual(workbook.sheetnames, [
            "Summary", "Invoice Details", "Bank Transactions", "Reconciliation",
            "Clarifications & Receipts",
        ])
        summary = workbook["Summary"]
        self.assertEqual(len(summary._charts), 2)
        self.assertEqual(summary["B12"].value, 1234.56)
        self.assertIn("₹", summary["B12"].number_format)
        bank_sheet = workbook["Bank Transactions"]
        self.assertEqual(bank_sheet.freeze_panes, "A4")
        self.assertIsNotNone(bank_sheet.auto_filter.ref)
        self.assertEqual({bank_sheet["D4"].value, bank_sheet["D5"].value}, {123456, -8000})
        evidence_sheet = workbook["Clarifications & Receipts"]
        self.assertEqual(evidence_sheet["A4"].value, "Clarification")
        self.assertEqual(evidence_sheet["A5"].value, "Receipt")
        self.assertEqual(evidence_sheet["D5"].value, str(receipt.id))
        self.assertEqual(evidence_sheet["K5"].value, "receipt-evidence.png")
        self.assertEqual(bank_sheet["E4"].number_format, '"₹"#,##0.00;[Red]-"₹"#,##0.00')

    def test_xlsx_empty_month_and_optional_business_fields(self):
        business = SimpleNamespace(
            id=uuid4(), business_name="Simple Shop", proprietor_name="Owner",
            gstin=None, address_line_1="", address_line_2=None, city="", state="",
            pin_code="", country="IN",
        )
        report = build_monthly_report(
            2026, 11, [], [], [], [], [], generated_at=NOV_START, business=business,
        )
        with patch("backend.app.api.reports.get_monthly_report", return_value=report):
            response = self.request_with_read_only_session(
                "get", "/api/v1/reports/monthly/export/xlsx?year=2026&month=11"
            )
        workbook = load_workbook(io.BytesIO(response.content))
        self.assertEqual(workbook["Invoice Details"].max_row, 3)
        self.assertEqual(workbook["Bank Transactions"].max_row, 3)
        self.assertEqual(workbook["Summary"]["B5"].value, "Not provided")
        self.assertEqual(len(workbook["Summary"]._charts), 2)

    def test_spreadsheet_exports_neutralize_formula_like_user_text(self):
        invoice = _invoice(vendor='=HYPERLINK("https://bad.example","click")')
        report = build_monthly_report(2026, 11, [invoice], [], [], [], [], generated_at=NOV_START)
        with patch("backend.app.api.reports.get_monthly_report", return_value=report):
            csv_response = self.request_with_read_only_session(
                "get", "/api/v1/reports/monthly/export/csv?year=2026&month=11"
            )
            xlsx_response = self.request_with_read_only_session(
                "get", "/api/v1/reports/monthly/export/xlsx?year=2026&month=11"
            )
        invoice_row = next(row for row in csv.DictReader(io.StringIO(csv_response.text)) if row["section"] == "Invoice")
        self.assertTrue(invoice_row["vendor_or_description"].startswith("'=HYPERLINK"))
        workbook = load_workbook(io.BytesIO(xlsx_response.content), data_only=False)
        vendor_cell = workbook["Invoice Details"]["B4"]
        self.assertEqual(vendor_cell.data_type, "s")
        self.assertTrue(vendor_cell.value.startswith("'=HYPERLINK"))

    def test_all_export_formats_require_authenticated_business_membership(self):
        client = TestClient(app)
        for path in (
            "/api/v1/reports/monthly/export/csv?year=2026&month=11",
            "/api/v1/reports/monthly/export/html?year=2026&month=11",
            "/api/v1/reports/monthly/export/xlsx?year=2026&month=11",
        ):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 401)


if __name__ == "__main__":
    unittest.main()
