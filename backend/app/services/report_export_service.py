"""Presentation-only exporters for the shared monthly report response."""

from __future__ import annotations

import csv
import html
import io
import re
from decimal import Decimal
from typing import Any, Iterable

from ..schemas.monthly_report import MonthlyReportResponse
from ..schemas.monthly_report_approval import MonthlyReportApprovalResponse


CSV_FIELDS = (
    "section", "record_id", "invoice_id", "bank_line_id",
    "clarification_request_id", "receipt_id", "date", "vendor_or_description",
    "invoice_number", "status", "amount_paise", "amount_inr", "reference",
    "details", "business_id",
)
_NUMERIC_TEXT = re.compile(r"^-?[0-9][0-9,]*(?:\.[0-9]{1,2})?$")


def format_inr(amount_paise: int | None) -> str:
    """Format integer paise exactly, without binary floating point."""
    return f"{Decimal(amount_paise or 0) / Decimal(100):,.2f}"


def _safe_spreadsheet_text(value: object | None) -> str:
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")) and not _NUMERIC_TEXT.fullmatch(text.strip()):
        return "'" + text
    return text


def _csv_row(writer: csv.DictWriter, **values: object) -> None:
    safe = {
        key: (_safe_spreadsheet_text(value) if isinstance(value, str) else value)
        for key, value in values.items()
    }
    writer.writerow(safe)


def build_csv_report(
    report: MonthlyReportResponse,
    approval: MonthlyReportApprovalResponse | None = None,
) -> str:
    """Render labeled report sections from already calculated report data."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\r\n")
    writer.writeheader()
    period = report.reporting_period
    business_id = report.business.business_id if report.business else ""

    def section(title: str) -> None:
        _csv_row(writer, section=title)

    section("LEDGERROOM MONTHLY BOOKKEEPING REPORT")
    _csv_row(writer, section="Reporting period", date=f"{period.year}-{period.month:02d}")
    _csv_row(writer, section="Generated at", details=report.generated_at.isoformat())

    section("BUSINESS INFORMATION")
    if report.business:
        business = report.business
        address = ", ".join(filter(None, (
            business.address_line_1, business.address_line_2, business.city,
            business.state, business.pin_code, business.country,
        )))
        _csv_row(writer, section="Business", record_id=business.business_id,
                 vendor_or_description=business.business_name,
                 details=f"Proprietor: {business.proprietor_name}; GSTIN: {business.gstin or 'Not provided'}; Address: {address or 'Not provided'}",
                 business_id=business.business_id)
    else:
        _csv_row(writer, section="Business", details="Business profile unavailable")

    section("FINANCIAL SUMMARY")
    financial = report.financial_summary
    for label, amount in (
        ("Total invoice value", financial.total_invoice_value_paise),
        ("Total matched payment value", financial.total_matched_payment_value_paise),
        ("Total unmatched debit value", financial.total_unmatched_debit_value_paise),
        ("Total mismatch value", financial.total_amount_mismatch_value_paise),
    ):
        _csv_row(writer, section=label, amount_paise=amount, amount_inr=format_inr(amount))

    section("RECONCILIATION SUMMARY")
    summary = report.summary
    for label, count in (
        ("Total invoices received", summary.total_invoices_received),
        ("Total bank transactions", summary.total_bank_transactions),
        ("Matched payments", summary.matched_payments),
        ("Amount mismatches", summary.amount_mismatches),
        ("Missing payments", summary.missing_payments),
        ("Possible duplicates", summary.possible_duplicates),
        ("Transactions requiring review", summary.transactions_requiring_review),
        ("Bank transactions without invoices", summary.bank_transactions_without_invoices),
        ("Pending clarifications", summary.pending_clarifications),
        ("Answered clarifications", summary.answered_clarifications),
        ("Receipts uploaded", summary.receipts_uploaded),
    ):
        _csv_row(writer, section=label, details=count)
    for outcome, count in sorted(summary.receipt_verification_outcomes.items()):
        _csv_row(writer, section=f"Receipt verification: {outcome}", details=count)

    section("INVOICE DETAILS")
    for item in report.invoices:
        _csv_row(writer, section="Invoice", record_id=item.invoice_id,
                 invoice_id=item.invoice_id, date=item.invoice_date.isoformat() if item.invoice_date else "",
                 vendor_or_description=item.vendor or "", invoice_number=item.invoice_number or "",
                 status=f"extraction={item.extraction_status}; reconciliation={item.reconciliation_status or 'unreviewed'}",
                 amount_paise=item.total_amount_paise,
                 amount_inr=format_inr(item.total_amount_paise) if item.total_amount_paise is not None else "",
                 bank_line_id=";".join(item.associated_bank_line_ids), business_id=business_id)

    section("BANK TRANSACTIONS")
    for item in report.bank_transactions:
        _csv_row(writer, section="Bank transaction", record_id=item.bank_line_id,
                 bank_line_id=item.bank_line_id, date=item.transaction_date.isoformat(),
                 vendor_or_description=item.description,
                 status=";".join(item.reconciliation_statuses) or "unmatched",
                 amount_paise=item.amount_paise, amount_inr=format_inr(item.amount_paise),
                 reference=item.external_reference or "", invoice_id=";".join(item.invoice_ids),
                 clarification_request_id=item.clarification_request_id or "",
                 details=f"currency={item.currency or ''}; clarification={item.clarification_status or ''}",
                 business_id=business_id)

    section("CLARIFICATIONS")
    for item in report.bank_transactions:
        if item.clarification_request_id:
            _csv_row(writer, section="Clarification", record_id=item.clarification_request_id,
                     bank_line_id=item.bank_line_id,
                     clarification_request_id=item.clarification_request_id,
                     date=item.transaction_date.isoformat(), vendor_or_description=item.description,
                     status=item.clarification_status or "unknown", amount_paise=item.amount_paise,
                     amount_inr=format_inr(item.amount_paise), details=item.owner_response or "",
                     business_id=business_id)

    section("RECEIPT EVIDENCE")
    for item in report.receipt_evidence:
        _csv_row(writer, section="Receipt", record_id=item.receipt_id, receipt_id=item.receipt_id,
                 clarification_request_id=item.clarification_request_id,
                 vendor_or_description=item.vendor or "", status=item.verification_result or "not_verified",
                 amount_paise=item.amount_paise,
                 amount_inr=format_inr(item.amount_paise) if item.amount_paise is not None else "",
                 details=f"document_type={item.document_type or ''}; source={item.source_image_reference}",
                 business_id=business_id)

    section("RECONCILIATION AUDIT HISTORY")
    for item in report.audit_history:
        _csv_row(writer, section="Match event", record_id=item.match_event_id,
                 invoice_id=item.invoice_id,
                 bank_line_id=item.bank_line_id or item.previous_bank_line_id or "",
                 date=item.changed_at.isoformat(),
                 status=f"{item.previous_status} -> {item.resulting_status}",
                 details=f"event={item.match_event_id}; source={item.decision_source}; previous_source={item.previous_decision_source}; previous_bank_line_id={item.previous_bank_line_id or ''}",
                 business_id=business_id)

    section("ACCOUNTANT REVIEW")
    if approval is None:
        _csv_row(writer, section="Approval status", status="not_submitted")
    else:
        for label, value in (
            ("Approval status", approval.status),
            ("Approved report version", approval.version),
            ("Submitted at", approval.submitted_at.isoformat()),
            ("Submitted by", approval.submitted_by_name),
            ("Accountant", approval.reviewed_by_name or "Not reviewed"),
            ("Review date", approval.reviewed_at.isoformat() if approval.reviewed_at else "Not reviewed"),
            ("Approval decision", approval.approval_decision or "None"),
            ("Accountant comments", approval.accountant_comments or ""),
        ):
            _csv_row(writer, section=label, status=value if label == "Approval status" else "",
                     details=value if label != "Approval status" else "")
    return output.getvalue()


def _e(value: object | None) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _html_table(headers: list[str], rows: Iterable[Iterable[object | None]]) -> str:
    rows = [list(row) for row in rows]
    head = "".join(f"<th>{_e(header)}</th>" for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{_e(value)}</td>" for value in row) + "</tr>" for row in rows)
    if not body:
        body = f'<tr><td colspan="{len(headers)}" class="empty">No records for this period.</td></tr>'
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _svg_bar_chart(title: str, rows: list[tuple[str, int]], unit: str) -> str:
    """Create a self-contained SVG bar chart using integer scale arithmetic."""
    max_value = max((max(0, value) for _, value in rows), default=0)
    max_value = max_value or 1
    width, row_height, left, bar_max = 760, 34, 235, 380
    height = 56 + max(len(rows), 1) * row_height
    parts = [
        f'<figure class="chart"><figcaption>{_e(title)}</figcaption>',
        f'<svg role="img" aria-label="{_e(title)}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">',
        f'<rect width="{width}" height="{height}" fill="#fff"/>',
    ]
    for index, (label, raw_value) in enumerate(rows):
        value = max(0, raw_value)
        y = 36 + index * row_height
        bar_width = value * bar_max // max_value
        parts.extend((
            f'<text x="8" y="{y+16}" class="chart-label">{_e(label)}</text>',
            f'<rect x="{left}" y="{y}" width="{bar_max}" height="20" rx="4" fill="#edf2f7"/>',
            f'<rect x="{left}" y="{y}" width="{bar_width}" height="20" rx="4" fill="#168c8c"/>',
            f'<text x="{left+bar_max+10}" y="{y+16}" class="chart-value">{_e(format_inr(value) if unit == 'INR' else f'{value:,}')} {unit}</text>',
        ))
    parts.append('</svg></figure>')
    return "".join(parts)


def build_html_report(
    report: MonthlyReportResponse,
    approval: MonthlyReportApprovalResponse | None = None,
) -> str:
    summary, financial, period = report.summary, report.financial_summary, report.reporting_period
    business = report.business
    address = ""
    business_block = "<section class=\"business-identity muted\">Business profile unavailable.</section>"
    if business:
        address = ", ".join(filter(None, (
            business.address_line_1, business.address_line_2, business.city,
            business.state, business.pin_code, business.country,
        )))
        identity = [
            f"<strong>{_e(business.business_name)}</strong>",
            f"Proprietor: {_e(business.proprietor_name)}",
            f"Address: {_e(address or 'Not provided')}",
        ]
        if business.gstin:
            identity.append(f"GSTIN: {_e(business.gstin)}")
        business_block = '<section class="business-identity">' + "<br>".join(identity) + "</section>"

    finance_rows = [
        ("Total invoice value", financial.total_invoice_value_paise),
        ("Matched payment value", financial.total_matched_payment_value_paise),
        ("Unmatched debit value", financial.total_unmatched_debit_value_paise),
        ("Total mismatch value", financial.total_amount_mismatch_value_paise),
    ]
    count_rows = [
        ("Invoices received", summary.total_invoices_received),
        ("Bank transactions", summary.total_bank_transactions),
        ("Matched payments", summary.matched_payments),
        ("Amount mismatches", summary.amount_mismatches),
        ("Missing payments", summary.missing_payments),
        ("Possible duplicates", summary.possible_duplicates),
        ("Transactions requiring review", summary.transactions_requiring_review),
        ("Bank transactions without invoices", summary.bank_transactions_without_invoices),
        ("Pending clarifications", summary.pending_clarifications),
        ("Answered clarifications", summary.answered_clarifications),
        ("Receipts uploaded", summary.receipts_uploaded),
        *((f"Receipt verification: {name}", count) for name, count in sorted(summary.receipt_verification_outcomes.items())),
    ]
    invoice_rows = [[i.invoice_id, i.vendor, i.invoice_number, i.invoice_date,
                     format_inr(i.total_amount_paise) if i.total_amount_paise is not None else "",
                     i.extraction_status, i.reconciliation_status,
                     "; ".join(i.associated_bank_line_ids)] for i in report.invoices]
    bank_rows = [[i.bank_line_id, i.transaction_date, i.description,
                  format_inr(i.amount_paise), "; ".join(i.reconciliation_statuses) or "unmatched",
                  "; ".join(i.invoice_ids), i.external_reference,
                  i.clarification_request_id, i.clarification_status] for i in report.bank_transactions]
    clarification_rows = [[i.clarification_request_id, i.bank_line_id, i.transaction_date,
                           i.description, i.clarification_status, i.owner_response]
                          for i in report.bank_transactions if i.clarification_request_id]
    missing_rows = [[i.bank_line_id, i.transaction_date, i.description, format_inr(i.amount_paise),
                     i.clarification_request_id, i.clarification_status, i.owner_response]
                    for i in report.missing_invoice_transactions]
    receipt_rows = [[i.receipt_id, i.clarification_request_id, i.document_type, i.vendor,
                     format_inr(i.amount_paise) if i.amount_paise is not None else "",
                     i.verification_result, i.source_image_reference] for i in report.receipt_evidence]
    audit_rows = [[i.match_event_id, i.invoice_id, i.previous_bank_line_id, i.bank_line_id,
                   i.previous_status, i.resulting_status, i.previous_decision_source,
                   i.decision_source, i.previous_updated_at, i.changed_at]
                  for i in report.audit_history]
    charts = _svg_bar_chart("Financial overview", finance_rows[:3], "INR")
    charts += _svg_bar_chart("Reconciliation status distribution", [
        ("Matched payments", summary.matched_payments),
        ("Amount mismatches", summary.amount_mismatches),
        ("Missing payments", summary.missing_payments),
        ("Possible duplicates", summary.possible_duplicates),
        ("Requiring review", summary.transactions_requiring_review),
        ("Without invoices", summary.bank_transactions_without_invoices),
    ], "records")
    approval_rows = [["Approval status", approval.status if approval else "Not submitted"]]
    if approval:
        approval_rows.extend([
            ["Report version", approval.version],
            ["Submitted", approval.submitted_at.isoformat()],
            ["Submitted by", approval.submitted_by_name],
            ["Accountant", approval.reviewed_by_name or "Not reviewed"],
            ["Review date", approval.reviewed_at.isoformat() if approval.reviewed_at else "Not reviewed"],
            ["Decision", approval.approval_decision or "None"],
            ["Accountant comments", approval.accountant_comments or ""],
        ])
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LedgerRoom Monthly Bookkeeping Report - {period.year}-{period.month:02d}</title>
<style>
:root{{--navy:#17324d;--teal:#168c8c;--ink:#263747;--muted:#66788a;--line:#d9e2ea;--wash:#f3f7fa}}body{{font:14px/1.5 Aptos,Calibri,Arial,sans-serif;color:var(--ink);margin:0;background:var(--wash)}}main{{max-width:1240px;margin:28px auto;background:white;padding:38px 44px;box-shadow:0 8px 28px #17324d14}}h1{{font-size:27px;color:var(--navy);margin:0}}h2{{font-size:17px;color:var(--navy);margin:30px 0 12px;border-bottom:2px solid var(--line);padding-bottom:7px}}.topline{{height:5px;background:var(--teal);margin:-38px -44px 28px}}.business-identity{{margin-top:20px;padding:16px 18px;border-left:4px solid var(--teal);background:var(--wash);line-height:1.75}}.meta,.muted{{color:var(--muted)}}.charts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:18px}}.chart{{margin:0;padding:14px;border:1px solid var(--line);border-radius:8px;overflow:hidden}}.chart figcaption{{font-weight:700;color:var(--navy);margin-bottom:8px}}.chart svg{{display:block;width:100%;height:auto}}.chart-label,.chart-value{{font:12px Aptos,Calibri,Arial,sans-serif;fill:var(--ink)}}.chart-value{{font-weight:600}}.table-wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:6px}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{border-bottom:1px solid var(--line);padding:8px 9px;text-align:left;vertical-align:top;overflow-wrap:anywhere}}th{{background:#eaf1f6;color:var(--navy);font-weight:700}}tbody tr:nth-child(even){{background:#fbfcfd}}.empty{{text-align:center;color:var(--muted);padding:18px}}.note{{margin-top:28px;padding:12px;background:var(--wash);color:var(--muted)}}
@media print{{body{{background:#fff;font-size:9pt}}main{{box-shadow:none;margin:0;padding:0;max-width:none}}.topline{{margin:0 0 18px}}h2{{break-after:avoid}}table{{font-size:7.5pt}}tr{{break-inside:avoid}}.table-wrap{{overflow:visible}}.charts{{grid-template-columns:1fr 1fr}}.chart{{break-inside:avoid}}@page{{size:A4 landscape;margin:12mm}}}}
</style></head><body><main><div class="topline"></div>
<h1>LedgerRoom Monthly Bookkeeping Report</h1>{business_block}
<p class="meta">Reporting period: <strong>{period.year}-{period.month:02d}</strong> ({period.start_date} through {period.end_date})<br>Generated: {_e(report.generated_at.isoformat())}<br>Financial values are stored in integer paise and displayed in INR.</p>
<h2>Accountant review</h2>{_html_table(["Review item", "Recorded value"], approval_rows)}
<h2>Financial summary</h2>{_html_table(["Measure", "Amount (INR)"], [[label, f"₹ {format_inr(amount)}"] for label, amount in finance_rows])}
<h2>Reconciliation summary</h2>{_html_table(["Measure", "Count"], count_rows)}
<h2>Visual summary</h2><div class="charts">{charts}</div>
<h2>Invoice details</h2>{_html_table(["Invoice ID", "Vendor", "Invoice number", "Invoice date", "Total (INR)", "Extraction", "Reconciliation", "Bank line IDs"], invoice_rows)}
<h2>Bank transactions</h2>{_html_table(["Bank line ID", "Date", "Description", "Amount (INR)", "Reconciliation", "Invoice IDs", "Reference", "Clarification ID", "Clarification status"], bank_rows)}
<h2>Missing-invoice transactions</h2>{_html_table(["Bank line ID", "Date", "Description", "Amount (INR)", "Clarification ID", "Status", "Owner response"], missing_rows)}
<h2>Clarifications</h2>{_html_table(["Clarification ID", "Bank line ID", "Date", "Description", "Status", "Owner response"], clarification_rows)}
<h2>Receipt evidence</h2>{_html_table(["Receipt ID", "Clarification ID", "Document type", "Vendor", "Amount (INR)", "Verification", "Source image reference"], receipt_rows)}
<h2>Reconciliation audit history</h2>{_html_table(["Match event ID", "Invoice ID", "Previous bank line ID", "Resulting bank line ID", "Previous status", "Resulting status", "Previous source", "Decision source", "Previous timestamp", "Changed at"], audit_rows)}
<p class="note">This report presents recorded source evidence. An owner explanation is not proof of payment. Credits are shown as signed transactions and are not included as debit expenses.</p>
</main></body></html>'''


def _excel_safe(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_spreadsheet_text(value)
    return value


def _style_title(ws: Any, title: str, columns: int) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(1, columns))
    cell = ws.cell(1, 1, title)
    cell.font = Font(name="Aptos Display", size=18, bold=True, color="FFFFFF")
    cell.fill = PatternFill("solid", fgColor="17324D")
    cell.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 30


def _style_header(ws: Any, row: int, count: int) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill, Side, Border

    fill = PatternFill("solid", fgColor="DCE8F0")
    border = Border(bottom=Side(style="thin", color="9FB4C5"))
    for col in range(1, count + 1):
        cell = ws.cell(row, col)
        cell.font = Font(bold=True, color="17324D")
        cell.fill = fill
        cell.border = border
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 30


def _write_detail_sheet(ws: Any, title: str, headers: list[str], rows: list[list[Any]]) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    _style_title(ws, title, len(headers))
    ws.append([])
    ws.append(headers)
    _style_header(ws, 3, len(headers))
    for row in rows:
        row_no = ws.max_row + 1
        for col, value in enumerate(row, start=1):
            cell = ws.cell(row_no, col, _excel_safe(value))
            if isinstance(value, Decimal):
                cell.number_format = '"₹"#,##0.00;[Red]-"₹"#,##0.00'
            elif isinstance(value, int) and "paise" in headers[col - 1].lower():
                cell.number_format = "#,##0;[Red]-#,##0"
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    end_row = max(3, ws.max_row)
    ws.auto_filter.ref = f"A3:{get_column_letter(len(headers))}{end_row}"
    ws.freeze_panes = "A4"
    for col in range(1, len(headers) + 1):
        letter = get_column_letter(col)
        values = [len(str(ws.cell(row, col).value or "")) for row in range(3, min(end_row, 203) + 1)]
        ws.column_dimensions[letter].width = min(max(max(values + [12]) + 2, 14), 52)
    ws.sheet_view.showGridLines = False


def build_xlsx_report(
    report: MonthlyReportResponse,
    approval: MonthlyReportApprovalResponse | None = None,
) -> bytes:
    """Build an accountant-friendly workbook; all totals come from the report."""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import Alignment, Font, PatternFill, Side, Border
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    summary_ws = wb.active
    summary_ws.title = "Summary"
    _style_title(summary_ws, "LedgerRoom Monthly Bookkeeping Report", 8)
    summary_ws.sheet_view.showGridLines = False
    business = report.business
    period = report.reporting_period
    identity_rows = [
        ("Business", business.business_name if business else "Not available"),
        ("Proprietor", business.proprietor_name if business else "Not available"),
        ("GSTIN", business.gstin if business and business.gstin else "Not provided"),
        ("Address", ", ".join(filter(None, (
            business.address_line_1, business.address_line_2, business.city,
            business.state, business.pin_code, business.country,
        ))) if business else "Not provided"),
        ("Reporting period", f"{period.year}-{period.month:02d} ({period.start_date} to {period.end_date})"),
        ("Generated", report.generated_at.isoformat()),
    ]
    for row_no, (label, value) in enumerate(identity_rows, start=3):
        summary_ws.cell(row_no, 1, label).font = Font(bold=True, color="17324D")
        summary_ws.cell(row_no, 2, _excel_safe(value))
    # Keep the business value in columns A:C so it does not overlap the
    # accountant review block in columns D:H.
    summary_ws.merge_cells("B6:C6")

    summary_ws.merge_cells("D3:H3")
    summary_ws["D3"] = "Accountant review"
    summary_ws["D3"].font = Font(bold=True, color="FFFFFF")
    summary_ws["D3"].fill = PatternFill("solid", fgColor="17324D")
    review_values = [
        ("Approval status", approval.status if approval else "Not submitted"),
        ("Report version", approval.version if approval else "—"),
        ("Submitted", approval.submitted_at.isoformat() if approval else "—"),
        ("Accountant", approval.reviewed_by_name if approval and approval.reviewed_by_name else "Not reviewed"),
        ("Review date", approval.reviewed_at.isoformat() if approval and approval.reviewed_at else "Not reviewed"),
        ("Decision", approval.approval_decision if approval and approval.approval_decision else "None"),
        ("Comments", approval.accountant_comments if approval and approval.accountant_comments else ""),
    ]
    for row_no, (label, value) in enumerate(review_values, start=4):
        summary_ws.cell(row_no, 4, label).font = Font(bold=True, color="17324D")
        summary_ws.cell(row_no, 5, _excel_safe(value))
    summary_ws.merge_cells("E10:H10")
    summary_ws["E10"].alignment = Alignment(wrap_text=True, vertical="top")

    section_fill = PatternFill("solid", fgColor="17324D")
    for cell in (summary_ws.cell(10, 1, "Financial summary"),):
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = section_fill
    summary_ws.merge_cells("A10:C10")
    for col, value in enumerate(("Measure", "Amount (INR)", "Amount (paise)"), start=1):
        summary_ws.cell(11, col, value)
    _style_header(summary_ws, 11, 3)
    financial_rows = [
        ("Total invoice value", report.financial_summary.total_invoice_value_paise),
        ("Matched payment value", report.financial_summary.total_matched_payment_value_paise),
        ("Unmatched debit value", report.financial_summary.total_unmatched_debit_value_paise),
        ("Total mismatch value", report.financial_summary.total_amount_mismatch_value_paise),
    ]
    for row_no, (label, paise) in enumerate(financial_rows, start=12):
        summary_ws.cell(row_no, 1, label)
        inr_cell = summary_ws.cell(row_no, 2, Decimal(paise) / Decimal(100))
        inr_cell.number_format = '"₹"#,##0.00;[Red]-"₹"#,##0.00'
        summary_ws.cell(row_no, 3, paise).number_format = "#,##0"

    summary_ws.cell(18, 1, "Reconciliation summary").font = Font(bold=True, color="FFFFFF")
    summary_ws.cell(18, 1).fill = section_fill
    summary_ws.merge_cells("A18:C18")
    for col, value in enumerate(("Measure", "Count"), start=1):
        summary_ws.cell(19, col, value)
    _style_header(summary_ws, 19, 2)
    counts = [
        ("Total invoices received", report.summary.total_invoices_received),
        ("Total bank transactions", report.summary.total_bank_transactions),
        ("Matched payments", report.summary.matched_payments),
        ("Amount mismatches", report.summary.amount_mismatches),
        ("Missing payments", report.summary.missing_payments),
        ("Possible duplicates", report.summary.possible_duplicates),
        ("Transactions requiring review", report.summary.transactions_requiring_review),
        ("Bank transactions without invoices", report.summary.bank_transactions_without_invoices),
        ("Pending clarifications", report.summary.pending_clarifications),
        ("Answered clarifications", report.summary.answered_clarifications),
        ("Receipts uploaded", report.summary.receipts_uploaded),
    ] + [(f"Receipt verification: {name}", count)
         for name, count in sorted(report.summary.receipt_verification_outcomes.items())]
    for row_no, (label, count) in enumerate(counts, start=20):
        summary_ws.cell(row_no, 1, label)
        summary_ws.cell(row_no, 2, count)

    # Chart source values are the same report values displayed above.
    financial_chart = BarChart()
    financial_chart.type = "bar"
    financial_chart.style = 10
    financial_chart.title = "Monthly financial overview (INR)"
    financial_chart.y_axis.title = "Measure"
    financial_chart.x_axis.title = "INR"
    financial_chart.add_data(Reference(summary_ws, min_col=2, min_row=11, max_row=14), titles_from_data=True)
    financial_chart.set_categories(Reference(summary_ws, min_col=1, min_row=12, max_row=14))
    financial_chart.height, financial_chart.width = 7, 14
    summary_ws.add_chart(financial_chart, "E12")

    status_start = 20
    status_end = 19 + len(counts)
    status_chart = BarChart()
    status_chart.type = "col"
    status_chart.style = 11
    status_chart.title = "Reconciliation and workflow counts"
    status_chart.y_axis.title = "Records"
    status_chart.add_data(Reference(summary_ws, min_col=2, min_row=19, max_row=status_end), titles_from_data=True)
    status_chart.set_categories(Reference(summary_ws, min_col=1, min_row=status_start, max_row=status_end))
    status_chart.height, status_chart.width = 8, 18
    summary_ws.add_chart(status_chart, "A37")
    summary_ws.column_dimensions["A"].width = 39
    summary_ws.column_dimensions["B"].width = 30
    summary_ws.column_dimensions["C"].width = 20
    for col in "DEFGH":
        summary_ws.column_dimensions[col].width = 16
    summary_ws.freeze_panes = "A11"

    invoices = [[
        i.invoice_id, i.vendor or "", i.invoice_number or "", i.invoice_date,
        i.total_amount_paise, Decimal(i.total_amount_paise) / Decimal(100) if i.total_amount_paise is not None else None,
        i.extraction_status, i.reconciliation_status or "unreviewed", "; ".join(i.associated_bank_line_ids),
    ] for i in report.invoices]
    _write_detail_sheet(wb.create_sheet("Invoice Details"), "Invoice details", [
        "Invoice ID", "Vendor", "Invoice number", "Invoice date", "Amount (paise)",
        "Amount (INR)", "Extraction status", "Reconciliation status", "Bank line IDs",
    ], invoices)

    bank_rows = [[
        item.bank_line_id, item.transaction_date, item.description, item.amount_paise,
        Decimal(item.amount_paise) / Decimal(100), item.currency or "", item.external_reference or "",
        "; ".join(item.reconciliation_statuses) or "unmatched", "; ".join(item.invoice_ids),
        item.clarification_request_id or "", item.clarification_status or "",
    ] for item in report.bank_transactions]
    _write_detail_sheet(wb.create_sheet("Bank Transactions"), "Bank transactions", [
        "Bank line ID", "Transaction date", "Description", "Amount (paise)", "Amount (INR)",
        "Currency", "Reference", "Reconciliation status", "Invoice IDs",
        "Clarification ID", "Clarification status",
    ], bank_rows)

    reconciliation_rows = [[
        i.invoice_id, i.vendor or "", i.invoice_number or "", i.total_amount_paise,
        Decimal(i.total_amount_paise) / Decimal(100) if i.total_amount_paise is not None else None,
        i.reconciliation_status or "unreviewed", "; ".join(i.associated_bank_line_ids),
    ] for i in report.invoices]
    ws = wb.create_sheet("Reconciliation")
    _write_detail_sheet(ws, "Reconciliation by invoice", [
        "Invoice ID", "Vendor", "Invoice number", "Invoice amount (paise)",
        "Invoice amount (INR)", "Decision status", "Associated bank line IDs",
    ], reconciliation_rows)
    audit_title_row = max(5, ws.max_row + 3)
    ws.cell(audit_title_row, 1, "Reconciliation audit history")
    ws.cell(audit_title_row, 1).font = Font(bold=True, color="FFFFFF")
    ws.cell(audit_title_row, 1).fill = section_fill
    ws.merge_cells(start_row=audit_title_row, start_column=1, end_row=audit_title_row, end_column=10)
    audit_headers = ["Match event ID", "Invoice ID", "Previous bank line ID", "Resulting bank line ID",
                     "Previous status", "Resulting status", "Previous source", "Decision source",
                     "Previous timestamp", "Changed at"]
    for col, value in enumerate(audit_headers, start=1):
        ws.cell(audit_title_row + 1, col, value)
    _style_header(ws, audit_title_row + 1, len(audit_headers))
    for item in report.audit_history:
        values = [item.match_event_id, item.invoice_id, item.previous_bank_line_id or "",
                  item.bank_line_id or "", item.previous_status, item.resulting_status,
                  item.previous_decision_source, item.decision_source,
                  item.previous_updated_at.isoformat(), item.changed_at.isoformat()]
        ws.append([_excel_safe(value) for value in values])
    for col in range(1, len(audit_headers) + 1):
        letter = get_column_letter(col)
        ws.column_dimensions[letter].width = min(max(ws.column_dimensions[letter].width or 12, 18), 44)

    clarification_rows = [[
        item.clarification_request_id, item.bank_line_id, item.transaction_date,
        item.description, item.amount_paise, Decimal(item.amount_paise) / Decimal(100),
        item.clarification_status or "", item.owner_response or "",
    ] for item in report.bank_transactions if item.clarification_request_id]
    receipt_rows = [[
        item.receipt_id, item.clarification_request_id, item.document_type or "",
        item.vendor or "", item.amount_paise,
        Decimal(item.amount_paise) / Decimal(100) if item.amount_paise is not None else None,
        item.verification_result or "not_verified", item.source_image_reference,
    ] for item in report.receipt_evidence]
    ws = wb.create_sheet("Clarifications & Receipts")
    _write_detail_sheet(ws, "Clarifications and receipt evidence", [
        "Record type", "Clarification ID", "Bank line ID", "Receipt ID", "Date",
        "Vendor or description", "Amount (paise)", "Amount (INR)", "Status / verification",
        "Owner response", "Source image reference",
    ], [["Clarification", item[0], item[1], "", item[2], item[3], item[4], item[5], item[6], item[7], ""]
        for item in clarification_rows] + [["Receipt", item[1], "", item[0], "", item[3], item[4], item[5], item[6], "", item[7]]
        for item in receipt_rows])

    # Apply a consistent border/alignment to all populated cells and print setup.
    border = Border(bottom=Side(style="hair", color="D9E2EA"))
    for sheet in wb.worksheets:
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.page_setup.orientation = "landscape"
        for row in sheet.iter_rows():
            for cell in row:
                if cell.value is not None and cell.row != 1:
                    cell.border = border
                    if cell.data_type == "f":
                        raise ValueError("Report export must not contain formulas")

    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()
