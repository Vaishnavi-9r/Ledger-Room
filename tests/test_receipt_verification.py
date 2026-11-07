from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import date
from uuid import uuid4

from backend.app.models import BankLine, ChaseEvent, ClarificationReceipt, ClarificationRequest
from backend.app.services.receipt_verification_service import (
    evaluate_receipt_against_bank_line,
    get_receipt_verification as _get_receipt_verification,
    verify_clarification_receipt as _verify_clarification_receipt,
)

TEST_BUSINESS_ID = uuid4()


def verify_clarification_receipt(db, clarification_id, receipt_id, **kwargs):
    return _verify_clarification_receipt(db, TEST_BUSINESS_ID, clarification_id, receipt_id, **kwargs)


def get_receipt_verification(db, clarification_id, receipt_id):
    return _get_receipt_verification(db, TEST_BUSINESS_ID, clarification_id, receipt_id)


def _fixtures():
    bank_line = BankLine(
        id=uuid4(),
        transaction_date=date(2026, 11, 3),
        description="ABC STATIONERY",
        amount_paise=250000,
        currency="INR",
    )
    clarification = ClarificationRequest(
        id=uuid4(),
        bank_line_id=bank_line.id,
        bank_line=bank_line,
        status="receipt_received",
        question="Please provide evidence.",
    )
    receipt = ClarificationReceipt(
        id=uuid4(),
        clarification_request_id=clarification.id,
        clarification_request=clarification,
        stored_filename="receipt.jpg",
        content_type="image/jpeg",
        file_size=100,
        extraction_status="completed",
        merchant_name="ABC STATIONERY",
        document_type="payment_receipt",
        document_number="R-1",
        document_date=date(2026, 11, 3),
        total_amount_paise=250000,
        payment_method="UPI",
        payment_reference="UPI-1",
        confidence={
            "merchant_name": 0.98,
            "document_type": 0.97,
            "document_number": 0.95,
            "document_date": 0.99,
            "total_amount_paise": 0.99,
            "payment_method": 0.91,
            "payment_reference": 0.92,
        },
        uncertain_fields=[],
    )
    return bank_line, clarification, receipt


class _FakeSession:
    def __init__(self, bank_line, clarification, receipt):
        self.bank_line = bank_line
        self.clarification = clarification
        self.receipt = receipt
        self.events = []

    def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is ClarificationReceipt:
            return self.receipt
        if entity is ClarificationRequest:
            return self.clarification
        if entity is BankLine:
            return self.bank_line
        return None

    def get(self, model, identity):
        if model is ClarificationRequest and identity == self.clarification.id:
            return self.clarification
        if model is BankLine and identity == self.bank_line.id:
            return self.bank_line
        return None

    def add(self, instance):
        if isinstance(instance, ChaseEvent):
            self.events.append(instance)

    def flush(self):
        return None

    def rollback(self):
        return None

    @contextmanager
    def begin(self):
        values = (
            self.receipt.verification_status,
            self.receipt.verification_reason,
            self.receipt.verification_details,
            self.receipt.verified_at,
            len(self.events),
        )
        try:
            yield self
        except Exception:
            (
                self.receipt.verification_status,
                self.receipt.verification_reason,
                self.receipt.verification_details,
                self.receipt.verified_at,
                event_count,
            ) = values
            del self.events[event_count:]
            raise


class ReceiptVerificationTests(unittest.TestCase):
    def test_exact_payment_receipt_match_is_verified(self):
        bank_line, _, receipt = _fixtures()
        decision = evaluate_receipt_against_bank_line(receipt, bank_line, 7)
        self.assertEqual(decision.status, "verified")
        self.assertEqual(receipt.total_amount_paise, 250000)
        self.assertEqual(bank_line.amount_paise, 250000)

    def test_amount_mismatch_uses_integer_paise(self):
        bank_line, _, receipt = _fixtures()
        receipt.total_amount_paise = 250001
        self.assertEqual(
            evaluate_receipt_against_bank_line(receipt, bank_line, 7).status,
            "amount_mismatch",
        )

    def test_vendor_mismatch(self):
        bank_line, _, receipt = _fixtures()
        receipt.merchant_name = "Different Merchant"
        self.assertEqual(
            evaluate_receipt_against_bank_line(receipt, bank_line, 7).status,
            "vendor_mismatch",
        )

    def test_date_mismatch(self):
        bank_line, _, receipt = _fixtures()
        receipt.document_date = date(2026, 11, 20)
        self.assertEqual(
            evaluate_receipt_against_bank_line(receipt, bank_line, 7).status,
            "date_mismatch",
        )

    def test_missing_or_uncertain_core_fields_need_review(self):
        bank_line, _, receipt = _fixtures()
        receipt.total_amount_paise = None
        receipt.uncertain_fields = ["total_amount_paise"]
        decision = evaluate_receipt_against_bank_line(receipt, bank_line, 7)
        self.assertEqual(decision.status, "needs_review")
        self.assertIn("total_amount_paise", decision.details["missing_or_uncertain_core_fields"])

    def test_invoice_is_not_treated_as_proof_of_payment(self):
        bank_line, _, receipt = _fixtures()
        receipt.document_type = "invoice"
        self.assertEqual(
            evaluate_receipt_against_bank_line(receipt, bank_line, 7).status,
            "needs_review",
        )

    def test_repeated_verification_is_idempotent_and_retrievable(self):
        bank_line, clarification, receipt = _fixtures()
        session = _FakeSession(bank_line, clarification, receipt)
        first = verify_clarification_receipt(
            session, clarification.id, receipt.id, date_window_days=7
        )
        first_timestamp = first.verified_at
        second = verify_clarification_receipt(
            session, clarification.id, receipt.id, date_window_days=7
        )
        self.assertEqual(first.status, "verified")
        self.assertEqual(second.status, "verified")
        self.assertEqual(first_timestamp, second.verified_at)
        self.assertEqual(len(session.events), 1)
        self.assertEqual(session.events[0].event_type, "clarification_receipt_verified")

        loaded = get_receipt_verification(session, clarification.id, receipt.id)
        self.assertEqual(loaded.receipt_id, str(receipt.id))
        self.assertEqual(loaded.bank_line_id, str(bank_line.id))
        self.assertEqual(loaded.status, "verified")


if __name__ == "__main__":
    unittest.main()
